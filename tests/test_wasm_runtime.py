"""HESTIA_RUNTIME=wasm end to end: deploy, the tenant's door, the routed invoke, signing,
owner code rights — against a scripted runner (always) and the real one (when installed).

The scripted runner is the real hestia.wasm_runner HTTP layer over a Unix socket with a
fake sandbox behind it, so the hearth <-> runner contract is exercised without wasmtime.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from fastapi.testclient import TestClient

from hestia.app import build_app
from hestia.config import Settings
from hestia.owners import sign_owner_request
from hestia.runtime.wasm import LANE_BUSY, OpenLane
from hestia.sandbox import Outcome
from hestia.scan import HandlerDenied, lint_wasm_handler
from hestia.signing import _canonical_receipt
from hestia.wasm_runner import _UnixHTTPServer, make_handler
from tests.conftest import auth, capability, deploy_payload
from tests.test_payments import bound_settings, deploy_priced, pay

MONOREPO = Path(__file__).resolve().parent.parent.parent
WASM_ROOT = Path(os.environ.get("HESTIA_WASM_ROOT", "/opt/python-wasi"))


class ScriptedSandbox:
    """Answers by the payload's `mode`, the way the real sandbox would."""

    memory_mb = 64
    timeout_s = 5.0

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        # mode "hold": the call stays in the sandbox until the test releases it, the way a
        # handler spinning to its deadline would.
        self.held = threading.Event()
        self.release = threading.Event()

    def run(self, source, payload, *, timeout_s=None, fetched=None, language="python", seed=""):
        self.calls.append((source, payload))
        self.language = language
        self.seed = seed
        self.fetched = dict(fetched or {})
        mode = payload.get("mode", "ok") if isinstance(payload, dict) else "ok"
        if mode == "fetch":
            # What the real guest does for `fetch(url)`: answer from `fetched`, or ask for it.
            from hestia.egress import request_key
            wanted = {"method": "GET", "url": payload["url"], "headers": {}, "body": None}
            got = self.fetched.get(request_key(wanted))
            if got is None:
                return Outcome(kind="fetch", fetch=wanted)
            if got.get("refused"):
                return Outcome(kind="error", error="egress refused: " + got["refused"])
            return Outcome(kind="ok", result={"status": got["status"], "text": got["text"]})
        if mode == "hold":
            self.held.set()
            self.release.wait(10)
            mode = "ok"
        if mode == "ok":
            return Outcome(kind="ok", result={"echo": payload, "source_len": len(source)})
        return Outcome(kind=mode, error=f"scripted {mode}")


def short_socket_dir() -> str:
    # macOS caps a Unix socket path at 104 bytes; pytest's tmp_path is longer than that.
    return tempfile.mkdtemp(prefix="hr", dir="/tmp")


@pytest.fixture
def scripted_runner():
    path = os.path.join(short_socket_dir(), "r.sock")
    sandbox = ScriptedSandbox()
    server = _UnixHTTPServer(path, make_handler(sandbox, threading.BoundedSemaphore(2), 0.5))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield path, sandbox
    server.shutdown()
    server.server_close()


def wasm_client(tmp_path: Path, socket_path: str, **overrides) -> TestClient:
    settings = Settings.for_test(tmp_path)
    settings = Settings(**{**settings.__dict__, "runtime": "wasm", "runner_socket": socket_path,
                           **overrides})
    return TestClient(build_app(settings))


def verify_signature(answer: dict, request: dict, cap: dict) -> None:
    canonical = _canonical_receipt(answer["result"], cap["capability_id"], cap["product_id"],
                                   request)
    key = Ed25519PublicKey.from_public_bytes(base64.b64decode(answer["provider_pubkey"]))
    key.verify(base64.b64decode(answer["signature"]), canonical.encode())


HANDLER = "import os\n\ndef handle(payload):\n    return {'n': len(payload)}\n"


# ------------------------------------------------------------------ the lint


def test_lint_admits_what_the_sandbox_contains() -> None:
    lint_wasm_handler("import os, socket, subprocess\nfrom typing import Any\n"
                      "def handle(p: 'Any'):\n    return '{0}'.format(p).__class__")
    lint_wasm_handler("handle = lambda p: {}")
    with pytest.raises(HandlerDenied, match="must define handle"):
        lint_wasm_handler("def other(p):\n    return {}")
    with pytest.raises(HandlerDenied, match="plain function"):
        lint_wasm_handler("async def handle(p):\n    return {}")
    with pytest.raises(HandlerDenied, match="not valid Python"):
        lint_wasm_handler("def handle(p) return")


# ------------------------------------------------------------------ scripted runner


def test_deploy_health_and_signed_invoke(tmp_path, scripted_runner) -> None:
    socket_path, sandbox = scripted_runner
    c = wasm_client(tmp_path, socket_path)
    out = c.post("/v1/tenants", json=deploy_payload(handler=HANDLER), headers=auth(c))
    assert out.status_code == 200, out.text
    assert c.app.state.store.get("demo-echo").listen_url == "wasm://demo-echo"
    assert c.get("/t/demo-echo/health").json() == {
        "ok": True, "slug": "demo-echo", "status": "running", "runtime": "wasm"}
    request = {"a": 1, "word": "é"}
    answer = c.post("/t/demo-echo/invoke", json=request)
    assert answer.status_code == 200
    body = answer.json()
    assert body["result"] == {"echo": request, "source_len": len(HANDLER)}
    verify_signature(body, request, capability())
    assert sandbox.calls[-1] == (HANDLER, request)
    # The tenant keeps one key: the next answer is signed by the same one.
    again = c.post("/t/demo-echo/invoke", json=request).json()
    assert again["provider_pubkey"] == body["provider_pubkey"]
    assert c.get("/t/demo-echo/anything").status_code == 404


def test_routed_invoke_reaches_the_sandbox(tmp_path, scripted_runner) -> None:
    socket_path, _ = scripted_runner
    c = wasm_client(tmp_path, socket_path)
    c.post("/v1/tenants", json=deploy_payload(handler=HANDLER), headers=auth(c))
    out = c.post("/ai-market/v2/invoke", json={"capability_id": "demo.echo@v1",
                                               "input": {"x": 2}})
    assert out.status_code == 200, out.text
    # The tenant's own signed envelope comes back untouched, as from a stub tenant.
    assert out.json()["result"] == {"echo": {"x": 2}, "source_len": len(HANDLER)}


@pytest.mark.parametrize(("mode", "status"), [("error", 400), ("memory", 400), ("timeout", 504),
                                              ("output", 502), ("crash", 502)])
def test_outcomes_keep_the_stubs_status_codes(tmp_path, scripted_runner, mode, status) -> None:
    socket_path, _ = scripted_runner
    c = wasm_client(tmp_path, socket_path)
    c.post("/v1/tenants", json=deploy_payload(handler=HANDLER), headers=auth(c))
    out = c.post("/t/demo-echo/invoke", json={"mode": mode})
    assert out.status_code == status and out.json() == {"ok": False, "error": f"scripted {mode}"}


def test_a_dead_runner_is_a_platform_failure(tmp_path) -> None:
    c = wasm_client(tmp_path, os.path.join(short_socket_dir(), "absent.sock"))
    c.post("/v1/tenants", json=deploy_payload(handler=HANDLER), headers=auth(c))
    out = c.post("/t/demo-echo/invoke", json={})
    assert out.status_code == 502 and "unreachable" in out.json()["error"]


def test_paid_wasm_invocation_keeps_the_checked_handler(tmp_path, monkeypatch, scripted_runner) -> None:
    socket_path, sandbox = scripted_runner
    c = wasm_client(tmp_path, socket_path, **{
        **bound_settings(tmp_path), "runtime": "wasm", "runner_socket": socket_path,
    })
    deploy_priced(c)
    headers = pay(c, monkeypatch)
    runtime = c.app.state.stub
    original = runtime.stored_handler("paid")
    invoke = runtime.invoke

    async def send(*args, **kwargs):
        monkeypatch.setattr(runtime, "stored_handler", lambda _slug: "replacement handler")
        return await invoke(*args, **kwargs)

    monkeypatch.setattr(runtime, "invoke", send)
    response = c.post("/t/paid/invoke", json={}, headers=headers)
    assert response.status_code == 200, response.text
    assert sandbox.calls == [(original, {})]


@pytest.mark.parametrize("outcome", ["busy", "unreachable"])
def test_paid_wasm_continuation_after_egress_is_ambiguous(tmp_path, monkeypatch, outcome) -> None:
    socket_path = os.path.join(short_socket_dir(), "absent.sock")
    c = wasm_client(tmp_path, socket_path, **{
        **bound_settings(tmp_path), "runtime": "wasm", "runner_socket": socket_path,
    })
    deploy_priced(c)
    headers = pay(c, monkeypatch)
    runtime = c.app.state.stub
    sent = []
    runs = []

    async def run(_source, _body, fetched, _language="python", _seed=""):
        runs.append(fetched)
        if not fetched:
            return 200, {"kind": "fetch", "fetch": {
                "method": "POST", "url": "https://example.invalid/order", "body": "fixture",
            }}
        if outcome == "busy":
            return 503, {"error": "runner busy"}
        raise httpx.ConnectError("runner unavailable in subsequent round")

    async def fetch(_slug, request, **_kw):
        sent.append(request)
        return {"text": "fixture response"}

    monkeypatch.setattr(runtime, "_run", run)
    monkeypatch.setattr(runtime, "egress_hosts", lambda _slug: ["example.invalid"])
    monkeypatch.setattr(runtime, "egress", SimpleNamespace(fetch=fetch))
    for _ in range(2):
        response = c.post("/t/paid/invoke", json={}, headers=headers)
        assert response.status_code == 409, response.text
        assert response.json()["error"] == "payment_reconciliation_required"
    assert len(runs) == 2
    assert len(sent) == 1


def test_bad_bodies_are_refused_at_the_door(tmp_path, scripted_runner) -> None:
    socket_path, sandbox = scripted_runner
    c = wasm_client(tmp_path, socket_path)
    c.post("/v1/tenants", json=deploy_payload(handler=HANDLER), headers=auth(c))
    assert c.post("/t/demo-echo/invoke", content=b"{nope").status_code == 400
    assert c.post("/t/demo-echo/invoke", json=[1, 2]).json()["error"] == "body must be an object"
    assert sandbox.calls == []


def test_a_sealed_stub_answers_without_the_runner(tmp_path) -> None:
    c = wasm_client(tmp_path, os.path.join(short_socket_dir(), "absent.sock"))
    c.post("/v1/tenants", json=deploy_payload(), headers=auth(c))
    body = c.post("/t/demo-echo/invoke", json={"q": 1}).json()
    assert body["result"]["note"] == "sealed stub — no custom handler"
    verify_signature(body, {"q": 1}, capability())


def test_restart_restores_wasm_tenants_with_their_keys(tmp_path, scripted_runner) -> None:
    socket_path, _ = scripted_runner
    first = wasm_client(tmp_path, socket_path)
    first.post("/v1/tenants", json=deploy_payload(handler=HANDLER), headers=auth(first))
    key = first.post("/t/demo-echo/invoke", json={}).json()["provider_pubkey"]
    second = wasm_client(tmp_path, socket_path)
    assert second.app.state.store.get("demo-echo").status == "running"
    assert second.post("/t/demo-echo/invoke", json={}).json()["provider_pubkey"] == key


def test_a_stub_tenant_moves_to_wasm_with_its_key(tmp_path, scripted_runner) -> None:
    socket_path, _ = scripted_runner
    stub = TestClient(build_app(Settings.for_test(tmp_path)))
    stub.post("/v1/tenants", json=deploy_payload(handler="def handle(p):\n    return {}\n"),
              headers=auth(stub))
    key = stub.post("/t/demo-echo/invoke", json={}).json()["provider_pubkey"]
    stub.app.state.stub._stop_all()
    moved = wasm_client(tmp_path, socket_path)
    assert moved.app.state.store.get("demo-echo").listen_url == "wasm://demo-echo"
    assert moved.post("/t/demo-echo/invoke", json={}).json()["provider_pubkey"] == key


def test_wasm_handlers_skip_the_stub_allow_list(tmp_path, scripted_runner) -> None:
    socket_path, _ = scripted_runner
    c = wasm_client(tmp_path, socket_path)
    out = c.post("/v1/tenants", json=deploy_payload(
        handler="import subprocess\ndef handle(p):\n    return {}\n"), headers=auth(c))
    assert out.status_code == 200, out.text
    refused = c.post("/v1/tenants", json=deploy_payload(slug="nohandle", handler="x = 1\n"),
                     headers=auth(c))
    assert refused.status_code == 400 and "handle" in refused.text


def test_prod_profile_accepts_the_wasm_sandbox(tmp_path, scripted_runner) -> None:
    socket_path, _ = scripted_runner
    c = wasm_client(tmp_path, socket_path, require_sandbox=True)
    assert c.post("/v1/tenants", json=deploy_payload(handler=HANDLER),
                  headers=auth(c)).status_code == 200


# ------------------------------------------------------------------ the real runner


@pytest.fixture(scope="module")
def real_runner():
    pytest.importorskip("wasmtime")
    if not (WASM_ROOT / "python.wasm").is_file():
        pytest.skip("CPython for WASI is not installed (HESTIA_WASM_ROOT)")
    path = os.path.join(short_socket_dir(), "r.sock")
    env = {**os.environ, "HESTIA_RUNNER_SOCKET": path, "HESTIA_WASM_ROOT": str(WASM_ROOT),
           "HESTIA_RUNNER_WORKERS": "2"}
    proc = subprocess.Popen([sys.executable, "-m", "hestia.wasm_runner"], env=env,  # noqa: S603
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    deadline = time.time() + 30
    while time.time() < deadline and not os.path.exists(path):
        time.sleep(0.1)
    if not os.path.exists(path):
        proc.kill()
        pytest.fail("runner did not start: " + proc.stderr.read().decode()[-500:])
    yield path
    proc.terminate()
    proc.wait(timeout=10)


def _agents():
    sys.path.insert(0, str(MONOREPO / "hestia-agents"))
    try:
        from hestia_agents.cli import PROBES
        from hestia_agents.manifests import AGENTS, handler_source
    except ImportError:
        pytest.skip("hestia-agents is not beside this package")
    return AGENTS, PROBES, handler_source


def test_every_reference_agent_answers_the_same_in_the_sandbox(tmp_path, real_runner) -> None:
    agents, probes, source_of = _agents()
    c = wasm_client(tmp_path, real_runner, max_tenants=32)
    for slug, meta in agents.items():
        cap = capability(product_id="hestia-agents", capability_id=meta["capability_id"])
        deploy = {**deploy_payload(slug=slug, handler=source_of(slug)), "capability": cap}
        assert c.post("/v1/tenants", json=deploy, headers=auth(c)).status_code == 200, slug
        namespace: dict = {}
        exec(compile(source_of(slug), slug, "exec"), namespace)  # noqa: S102 — our own agents
        native = namespace["handle"](probes[slug])
        answer = c.post(f"/t/{slug}/invoke", json=probes[slug])
        assert answer.status_code == 200, (slug, answer.text)
        body = answer.json()
        assert json.dumps(body["result"], sort_keys=True) == json.dumps(native, sort_keys=True), slug
        verify_signature(body, probes[slug], cap)


def test_a_hostile_handler_is_contained_end_to_end(tmp_path, real_runner) -> None:
    c = wasm_client(tmp_path, real_runner)
    hostile = ("import os\ndef handle(p):\n"
               "    return {'key': open(os.environ.get('K', '/data/provider.key')).read()}\n")
    c.post("/v1/tenants", json=deploy_payload(handler=hostile), headers=auth(c))
    out = c.post("/t/demo-echo/invoke", json={})
    assert out.status_code == 400 and "No such file" in out.json()["error"]
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.connect(real_runner)
    probe.close()


# ------------------------------------------------------------------ the open lane


class Stranger:
    """A key that admits itself on its first deploy (HESTIA_OPEN_OWNERS) and brings code."""

    def __init__(self) -> None:
        self.key = Ed25519PrivateKey.generate()
        self.pub = base64.b64encode(self.key.public_key().public_bytes_raw()).decode()

    def deploy(self, c: TestClient, slug: str, cap_id: str):
        payload = deploy_payload(slug=slug, handler=HANDLER)
        payload["owner_pubkey"] = self.pub
        mark = hashlib.sha256(self.pub.encode()).hexdigest()[:10]
        payload["capability"].update(capability_id=cap_id, name="Agent " + cap_id,
                                     product_id=f"product-{mark}",
                                     publisher_id=f"publisher-{mark}")
        raw = json.dumps(payload).encode()
        headers = sign_owner_request(self.key, hearth="http://127.0.0.1:9480", method="POST",
                                     path="/v1/tenants", body=raw)
        out = c.post("/v1/tenants", content=raw,
                     headers={**headers, "content-type": "application/json"})
        assert out.status_code == 200, out.text
        return out


def open_client(tmp_path: Path, socket_path: str, **overrides) -> TestClient:
    return wasm_client(tmp_path, socket_path, open_owners=True, open_owner_code=True,
                       open_lane_wait_s=0.2, **overrides)


def hold(c: TestClient, sandbox: ScriptedSandbox, slug: str) -> tuple[threading.Thread, dict]:
    """Start a call to `slug` that stays in the sandbox until sandbox.release is set."""
    done: dict = {}
    thread = threading.Thread(
        target=lambda: done.update(answer=c.post(f"/t/{slug}/invoke", json={"mode": "hold"})))
    thread.start()
    assert sandbox.held.wait(5), "the held call never reached the sandbox"
    return thread, done


def test_the_open_lane_bounds_all_strangers_together_and_one_call_per_owner() -> None:
    async def scenario() -> list[bool]:
        lane = OpenLane(slots=2, wait_s=0.0)
        steps = [await lane.enter("a"), await lane.enter("a"), await lane.enter("b"),
                 await lane.enter("c")]
        lane.leave("a")
        steps.append(await lane.enter("c"))
        return steps

    # a enters; a again is refused (one call per owner); b fills the lane; c finds it full;
    # once a leaves, c gets in.
    assert asyncio.run(scenario()) == [True, False, True, False, True]


def test_a_full_open_lane_waits_then_gives_up() -> None:
    async def scenario() -> tuple[bool, float]:
        lane = OpenLane(slots=1, wait_s=0.3)
        await lane.enter("a")
        started = time.perf_counter()
        return await lane.enter("b"), time.perf_counter() - started

    entered, waited = asyncio.run(scenario())
    assert entered is False and 0.25 <= waited < 2.0


def test_strangers_share_one_lane_and_the_operator_keeps_a_slot(tmp_path, scripted_runner):
    socket_path, sandbox = scripted_runner
    c = open_client(tmp_path, socket_path)
    assert c.post("/v1/tenants", json=deploy_payload(handler=HANDLER),
                  headers=auth(c)).status_code == 200
    Stranger().deploy(c, "alpha-lane", "alpha.lane@v1")
    Stranger().deploy(c, "omega-check", "omega.check@v1")
    listed = c.get("/v1/owners", headers=auth(c)).json()["owners"]
    owners = {o["label"]: o["admitted_by"] for o in listed}
    assert owners == {"open signup": "open"}
    # The strangers ran code through the live HESTIA_OPEN_OWNER_CODE lane; none of them holds
    # a stored right that would outlive it (GCVE-110-PYPI-2026-020712).
    assert {o["may_run_code"] for o in listed} == {0}
    thread, done = hold(c, sandbox, "alpha-lane")
    try:
        # Another stranger, with another key, finds the one open slot taken...
        busy = c.post("/t/omega-check/invoke", json={})
        assert busy.status_code == 503 and busy.json() == {"ok": False, "error": LANE_BUSY}
        # ...while the operator's agent still runs: the lane never takes the last slot.
        assert c.post("/t/demo-echo/invoke", json={}).status_code == 200
    finally:
        sandbox.release.set()
        thread.join(10)
    assert done["answer"].status_code == 200
    assert c.post("/t/omega-check/invoke", json={}).status_code == 200


def test_one_stranger_cannot_fill_a_wider_lane_alone(tmp_path, scripted_runner):
    socket_path, sandbox = scripted_runner
    c = open_client(tmp_path, socket_path, runner_workers=3, open_lane_slots=2)
    Stranger().deploy(c, "alpha-lane", "alpha.lane@v1")
    Stranger().deploy(c, "omega-check", "omega.check@v1")
    thread, _ = hold(c, sandbox, "alpha-lane")
    try:
        assert c.post("/t/alpha-lane/invoke", json={}).status_code == 503
        assert c.post("/t/omega-check/invoke", json={}).status_code == 200
    finally:
        sandbox.release.set()
        thread.join(10)


def test_owners_the_operator_admitted_run_outside_the_open_lane(tmp_path, scripted_runner):
    socket_path, sandbox = scripted_runner
    c = open_client(tmp_path, socket_path)
    vetted, stranger = Stranger(), Stranger()
    assert c.post("/v1/owners", json={"pubkey": vetted.pub, "label": "partner"},
                  headers=auth(c)).json()["owner"]["admitted_by"] == "operator"
    vetted.deploy(c, "partner-agent", "partner.agent@v1")
    stranger.deploy(c, "alpha-lane", "alpha.lane@v1")
    Stranger().deploy(c, "omega-check", "omega.check@v1")
    thread, _ = hold(c, sandbox, "alpha-lane")
    try:
        assert c.post("/t/partner-agent/invoke", json={}).status_code == 200
        assert c.post("/t/omega-check/invoke", json={}).status_code == 503
    finally:
        sandbox.release.set()
        thread.join(10)
    # Raising a stranger's quota does not vouch for it; saying so does.
    raised = c.post("/v1/owners", json={"pubkey": stranger.pub, "max_tenants": 2},
                    headers=auth(c)).json()["owner"]
    assert raised["admitted_by"] == "open"
    vouched = c.post("/v1/owners", json={"pubkey": stranger.pub, "admitted_by": "operator"},
                     headers=auth(c)).json()["owner"]
    assert vouched["admitted_by"] == "operator"
    assert c.post("/v1/owners", json={"pubkey": stranger.pub, "admitted_by": "root"},
                  headers=auth(c)).status_code == 400


# ------------------------------------------------------------------ JavaScript agents (2026-11-20)

JS_HANDLER = "function handle(payload) { return { n: Object.keys(payload).length }; }\n"


def js_payload(slug: str = "demo-echo", handler: str = JS_HANDLER) -> dict:
    body = deploy_payload(slug=slug, handler=handler)
    body["source"]["language"] = "javascript"
    return body


def test_a_javascript_agent_is_stored_as_handler_js_and_runs_as_javascript(tmp_path, scripted_runner) -> None:
    socket_path, sandbox = scripted_runner
    c = wasm_client(tmp_path, socket_path)
    out = c.post("/v1/tenants", json=js_payload(), headers=auth(c))
    assert out.status_code == 200, out.text
    tenant_dir = tmp_path / "tenants" / "demo-echo"
    assert (tenant_dir / "handler.js").read_text() == JS_HANDLER and not (tenant_dir / "handler.py").exists()
    answer = c.post("/t/demo-echo/invoke", json={"a": 1})
    assert answer.status_code == 200, answer.text
    assert sandbox.calls[-1] == (JS_HANDLER, {"a": 1}) and sandbox.language == "javascript"
    verify_signature(answer.json(), {"a": 1}, capability())
    roster = {t["slug"]: t for t in c.get("/v1/hearth").json()["tenants"]}
    assert roster["demo-echo"]["language"] == "javascript"
    # Python agents keep their old shape: no language field, the runner is not told one.
    py = deploy_payload(slug="py-agent", handler=HANDLER)
    py["capability"]["capability_id"] = "demo.python@v1"
    assert c.post("/v1/tenants", json=py, headers=auth(c)).status_code == 200
    assert c.post("/t/py-agent/invoke", json={}).status_code == 200
    assert sandbox.language == "python"
    assert "language" not in {t["slug"]: t for t in c.get("/v1/hearth").json()["tenants"]}["py-agent"]


def test_redeploying_in_the_other_language_replaces_the_handler(tmp_path, scripted_runner) -> None:
    socket_path, sandbox = scripted_runner
    c = wasm_client(tmp_path, socket_path)
    tenant_dir = tmp_path / "tenants" / "demo-echo"
    assert c.post("/v1/tenants", json=js_payload(), headers=auth(c)).status_code == 200
    assert c.post("/v1/tenants", json=deploy_payload(handler=HANDLER), headers=auth(c)).status_code == 200
    assert (tenant_dir / "handler.py").exists() and not (tenant_dir / "handler.js").exists()
    c.post("/t/demo-echo/invoke", json={})
    assert sandbox.calls[-1][0] == HANDLER and sandbox.language == "python"
    assert c.post("/v1/tenants", json=js_payload(), headers=auth(c)).status_code == 200
    assert (tenant_dir / "handler.js").exists() and not (tenant_dir / "handler.py").exists()


def test_a_restart_brings_a_javascript_agent_back_as_javascript(tmp_path, scripted_runner) -> None:
    socket_path, sandbox = scripted_runner
    c = wasm_client(tmp_path, socket_path)
    assert c.post("/v1/tenants", json=js_payload(), headers=auth(c)).status_code == 200
    again = wasm_client(tmp_path, socket_path)       # a new process on the same data volume
    assert (tmp_path / "tenants" / "demo-echo" / "handler.js").read_text() == JS_HANDLER
    assert again.post("/t/demo-echo/invoke", json={}).status_code == 200
    assert sandbox.calls[-1][0] == JS_HANDLER and sandbox.language == "javascript"


def test_javascript_is_refused_outside_the_sandbox_and_when_malformed(tmp_path, scripted_runner) -> None:
    from tests.conftest import client
    stub = client(tmp_path / "stub")
    out = stub.post("/v1/tenants", json=js_payload(), headers=auth(stub))
    assert out.status_code == 400 and "HESTIA_RUNTIME=wasm" in out.json()["detail"]
    socket_path, _ = scripted_runner
    c = wasm_client(tmp_path / "wasm", socket_path)
    for source, why in (("export function handle(p) { return {}; }", "classic script"),
                        ("import x from 'y'; function handle(){}", "classic script"),
                        ("function other() {}", "must define handle")):
        out = c.post("/v1/tenants", json=js_payload(handler=source), headers=auth(c))
        assert out.status_code == 400 and why in out.json()["detail"], (source, out.text)
    big = "function handle(p) { return {}; }\n// " + "x" * 200_000
    out = c.post("/v1/tenants", json=js_payload(handler=big), headers=auth(c))
    assert out.status_code in (400, 422) and "200000" in out.text, out.text
    # Python in the sandbox may be as large as JavaScript; outside it the AST admission keeps 32 000.
    py_big = deploy_payload(slug="py-big", handler="def handle(p):\n    return {}\n#" + "x" * 40_000)
    py_big["capability"]["capability_id"] = "demo.big@v1"
    assert c.post("/v1/tenants", json=py_big, headers=auth(c)).status_code == 200
    out = stub.post("/v1/tenants", json=py_big, headers=auth(stub))
    assert out.status_code == 400 and "32000" in out.text, out.text
    ok_size = "function handle(p) { return {}; }\n// " + "x" * 150_000
    assert c.post("/v1/tenants", json=js_payload(handler=ok_size), headers=auth(c)).status_code == 200


def test_a_javascript_agent_left_behind_by_a_switch_to_the_stub_is_stopped_not_echoed(tmp_path, scripted_runner) -> None:
    from tests.conftest import client
    socket_path, _ = scripted_runner
    c = wasm_client(tmp_path, socket_path)
    assert c.post("/v1/tenants", json=js_payload(), headers=auth(c)).status_code == 200
    stub = client(tmp_path)                           # the same data volume, HESTIA_RUNTIME=stub
    row = stub.app.state.store.get("demo-echo")
    assert row.status == "stopped" and "JavaScript" in row.last_error


def test_the_owner_tool_fills_a_javascript_handler_from_handler_js(tmp_path) -> None:
    from hestia.owner_cli import _with_handler
    (tmp_path / "handler.js").write_text(JS_HANDLER, encoding="utf-8")
    body = {"source": {"kind": "template", "handler": ""}}
    assert _with_handler(body, tmp_path / "deploy.json")["source"] == {
        "kind": "template", "handler": JS_HANDLER, "language": "javascript"}
    (tmp_path / "handler.py").write_text(HANDLER, encoding="utf-8")
    assert _with_handler({"source": {"kind": "template", "handler": ""}},
                         tmp_path / "deploy.json")["source"]["handler"] == HANDLER
    both = {"source": {"kind": "template", "handler": "", "language": "javascript"}}
    assert _with_handler(both, tmp_path / "deploy.json")["source"]["handler"] == JS_HANDLER


def test_the_runner_probe_reads_the_whole_answer(scripted_runner) -> None:
    from hestia.wasm_runner import probe
    socket_path, _ = scripted_runner
    assert probe(socket_path) is True
    assert probe(os.path.join(short_socket_dir(), "absent.sock")) is False


# ------------------------------------------------------------------ caller-named hosts (2026-11-20)

def egress_payload(slug: str = "demo-echo", fields=("domain",), hosts=()) -> dict:
    body = deploy_payload(slug=slug, handler=HANDLER)
    body["egress_input_fields"] = list(fields)
    if hosts:
        body["egress_hosts"] = list(hosts)
    return body


def test_a_call_may_reach_the_host_its_input_names_and_nothing_else(tmp_path, monkeypatch, scripted_runner) -> None:
    socket_path, sandbox = scripted_runner
    c = wasm_client(tmp_path, socket_path, tenant_egress=True)
    assert c.post("/v1/tenants", json=egress_payload(hosts=("dns.google",)), headers=auth(c)).status_code == 200
    runtime = c.app.state.stub
    sent = []

    async def fetch(slug, request, *, caller_named=False):
        sent.append((request.host, caller_named))
        return {"status": 200, "headers": {}, "text": "fixture"}

    monkeypatch.setattr(runtime, "egress", SimpleNamespace(fetch=fetch))
    ok = c.post("/t/demo-echo/invoke", json={"mode": "fetch", "domain": "Example.ORG.",
                                             "url": "https://example.org/.well-known/x"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["result"] == {"status": 200, "text": "fixture"}
    assert sent == [("example.org", True)]
    # The declared host is reachable too, and is not counted as caller-named.
    c.post("/t/demo-echo/invoke", json={"mode": "fetch", "domain": "example.org", "url": "https://dns.google/q"})
    assert sent[-1] == ("dns.google", False)
    # A host the input does not name is refused; so is a domain field that is not a public host.
    for body in ({"mode": "fetch", "domain": "example.org", "url": "https://other.example.net/"},
                 {"mode": "fetch", "domain": "10.0.0.1", "url": "https://10.0.0.1/"},
                 {"mode": "fetch", "domain": "localhost", "url": "https://localhost/"},
                 {"mode": "fetch", "domain": "https://example.org:8443/", "url": "https://example.org:8443/"},
                 {"mode": "fetch", "url": "https://example.org/"}):
        out = c.post("/t/demo-echo/invoke", json=body)
        assert out.status_code == 400 and "egress refused" in out.json()["error"], (body, out.text)
    assert len(sent) == 2
    roster = {t["slug"]: t for t in c.get("/v1/hearth").json()["tenants"]}["demo-echo"]
    assert roster["egress_input_fields"] == ["domain"] and roster["egress_hosts"] == ["dns.google"]


def test_every_round_of_one_call_gets_the_same_nonce(tmp_path, monkeypatch, scripted_runner) -> None:
    socket_path, sandbox = scripted_runner
    c = wasm_client(tmp_path, socket_path, tenant_egress=True)
    c.post("/v1/tenants", json=egress_payload(), headers=auth(c))
    seeds = []
    original = sandbox.run

    def run(source, payload, **kw):
        seeds.append(kw.get("seed"))
        return original(source, payload, **kw)

    monkeypatch.setattr(sandbox, "run", run)

    async def fetch(slug, request, *, caller_named=False):
        return {"status": 200, "headers": {}, "text": "x"}

    monkeypatch.setattr(c.app.state.stub, "egress", SimpleNamespace(fetch=fetch))
    c.post("/t/demo-echo/invoke", json={"mode": "fetch", "domain": "example.org", "url": "https://example.org/"})
    c.post("/t/demo-echo/invoke", json={"mode": "fetch", "domain": "example.org", "url": "https://example.org/"})
    assert len(seeds) == 4 and seeds[0] == seeds[1] and seeds[2] == seeds[3] and seeds[0] != seeds[2]
    assert all(len(s) == 64 and int(s, 16) >= 0 for s in seeds)


def test_caller_named_hosts_need_egress_and_an_owner_the_operator_admitted(tmp_path, scripted_runner) -> None:
    socket_path, _ = scripted_runner
    off = wasm_client(tmp_path / "off", socket_path)
    out = off.post("/v1/tenants", json=egress_payload(), headers=auth(off))
    assert out.status_code == 400 and "HESTIA_TENANT_EGRESS" in out.json()["detail"]
    c = open_client(tmp_path / "open", socket_path, tenant_egress=True)
    stranger = Stranger()
    payload = egress_payload()
    payload["owner_pubkey"] = stranger.pub
    mark = hashlib.sha256(stranger.pub.encode()).hexdigest()[:10]
    payload["capability"].update(capability_id="stranger.fetch@v1", product_id=f"product-{mark}",
                                 publisher_id=f"publisher-{mark}")
    raw = json.dumps(payload).encode()
    headers = sign_owner_request(stranger.key, hearth="http://127.0.0.1:9480", method="POST",
                                 path="/v1/tenants", body=raw)
    out = c.post("/v1/tenants", content=raw, headers={**headers, "content-type": "application/json"})
    assert out.status_code == 403 and "operator admitted" in out.json()["detail"]
    for bad in (["a-b"], ["1x"], ["a", "b", "c", "d"]):
        out = c.post("/v1/tenants", json=egress_payload(fields=bad), headers=auth(c))
        assert out.status_code in (400, 422) and "egress_input_fields" in out.text, (bad, out.text)


def test_a_restart_keeps_the_input_fields(tmp_path, scripted_runner) -> None:
    socket_path, _ = scripted_runner
    c = wasm_client(tmp_path, socket_path, tenant_egress=True)
    c.post("/v1/tenants", json=egress_payload(fields=("domain", "endpoint")), headers=auth(c))
    again = wasm_client(tmp_path, socket_path, tenant_egress=True)
    assert again.app.state.stub.egress_input_fields("demo-echo") == ["domain", "endpoint"]
    # A redeploy without them removes them.
    again.post("/v1/tenants", json=deploy_payload(handler=HANDLER), headers=auth(again))
    assert again.app.state.stub.egress_input_fields("demo-echo") == []


# ------------------------------------------------------------------ unpaid priced agents (2026-11-20)

def priced(slug="demo-echo", price=0.002, payout=""):
    body = deploy_payload(slug=slug, handler=HANDLER)
    body["capability"]["price_per_call_usd"] = price
    if payout:
        body["payout_address"] = payout
    return body


def test_a_priced_agent_without_a_payout_is_told_it_serves_free(tmp_path, scripted_runner) -> None:
    socket_path, _ = scripted_runner
    c = wasm_client(tmp_path, socket_path, payments_enabled=True)
    out = c.post("/v1/tenants", json=priced(), headers=auth(c)).json()
    assert len(out["warnings"]) == 1 and "every call is served free" in out["warnings"][0]
    assert "$0.002" in out["warnings"][0]
    roster = {t["slug"]: t for t in c.get("/v1/hearth").json()["tenants"]}
    assert "served free" in roster["demo-echo"]["unpaid_reason"]
    listed = {t["slug"]: t for t in c.get("/v1/tenants", headers=auth(c)).json()["tenants"]}
    assert "served free" in listed["demo-echo"]["unpaid_reason"]
    # The call is indeed served without payment: what the warning says is what happens.
    assert c.post("/t/demo-echo/invoke", json={}).status_code == 200


@pytest.mark.parametrize(("price", "payout", "payments"), [
    (0.002, "0x" + "ab" * 20, True),     # paid: an address
    (0, "", True),                        # free on purpose
    (0.002, "", False),                   # the operator turned payments off for every agent
])
def test_no_warning_when_nothing_is_lost(tmp_path, scripted_runner, price, payout, payments) -> None:
    socket_path, _ = scripted_runner
    c = wasm_client(tmp_path, socket_path, payments_enabled=payments)
    out = c.post("/v1/tenants", json=priced(price=price, payout=payout), headers=auth(c)).json()
    assert out["warnings"] == []
    assert "unpaid_reason" not in c.get("/v1/hearth").json()["tenants"][0]


def test_a_hub_that_bills_for_the_agent_narrows_the_warning(tmp_path, scripted_runner) -> None:
    socket_path, _ = scripted_runner
    c = wasm_client(tmp_path, socket_path, payments_enabled=True,
                    tenant_hub_keys=(("https://hub.example.org", "k" * 40),))
    body = priced()
    body["owner_pubkey"] = "A" * 43 + "="          # the operator's own agent (hestia/names.py)
    out = c.post("/v1/tenants", json=body, headers=auth(c)).json()
    assert "calls through https://hub.example.org are billed there" in out["warnings"][0]
    assert "straight to this hearth are served free" in out["warnings"][0]


def test_the_owner_sees_its_unpaid_agents(tmp_path, scripted_runner) -> None:
    socket_path, _ = scripted_runner
    c = open_client(tmp_path, socket_path, payments_enabled=True)
    stranger = Stranger()
    stranger.deploy(c, "s-agent", "stranger.agent@v1")
    row = c.app.state.store.get("s-agent")
    c.app.state.store.upsert(row.__class__(**{**row.__dict__, "capability": {**row.capability, "price_per_call_usd": 0.001}}))
    headers = sign_owner_request(stranger.key, hearth="http://127.0.0.1:9480", method="GET",
                                 path="/v1/owners/me", body=b"")
    me = c.get("/v1/owners/me", headers=headers).json()
    assert "served free" in me["unpaid"]["s-agent"] and "hestia-owner billing" in me["unpaid"]["s-agent"]


def test_the_owner_tool_repeats_the_warning(capsys, monkeypatch) -> None:
    import httpx

    from hestia import owner_cli
    answer = {"ok": True, "warnings": ["priced at $0.001 per call with no payout_address: x"],
              "unpaid": {"b": "priced at $0.001 per call with no payout_address: y"}}
    monkeypatch.setattr(httpx, "request", lambda *a, **k: httpx.Response(200, json=answer))
    assert owner_cli._send("http://hearth.test", Ed25519PrivateKey.generate(), "GET", "/v1/owners/me") == 0
    err = capsys.readouterr().err
    assert "WARNING: priced at $0.001" in err and "WARNING: b: priced at" in err
