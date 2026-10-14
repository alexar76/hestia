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

    def run(self, source, payload, *, timeout_s=None):
        self.calls.append((source, payload))
        mode = payload.get("mode", "ok") if isinstance(payload, dict) else "ok"
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
    owners = {o["label"]: o["admitted_by"] for o in
              c.get("/v1/owners", headers=auth(c)).json()["owners"]}
    assert owners == {"open signup": "open"}
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
