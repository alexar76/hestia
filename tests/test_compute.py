"""Verified compute: buyer data through an operator-published function.

What these pin down is the claim a compute receipt makes and the money around it:
two replicas of a pure function agree and the answer is signed; a function whose
output depends on hash order disagrees under different seeds and NOTHING is
signed; a spin is killed at its limit and the kernel's numbers say so; a replica
cannot read the hearth's key or a tenant's; and compute is never free when on.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import sys

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi.testclient import TestClient

from hestia import compute, compute_worker
from hestia.app import build_app
from hestia.compute import COMPUTE_RUN, COMPUTE_VERIFY, Limits, Replica, judge
from hestia.config import Settings
from hestia.payments import nonce_for_secret
from hestia.scan import HandlerDenied, admit_handler
from hestia.signing import object_canonical
from tests.conftest import auth, deploy_payload
from tests.test_payments import (
    ATTACKER,
    BUYER,
    auth_used,
    fake_rpc,
    fake_rpc_bound,
    fake_rpc_logs,
    transfer,
)

HUB_KEY = "hub-key-" + "k" * 32
COMPUTE_WALLET = "0x00000000000000000000000000000000c0ffee01"

PURE = (
    "import hashlib, json\n"
    "def handle(payload):\n"
    "    doc = json.dumps(payload, sort_keys=True)\n"
    "    return {'sha256': hashlib.sha256(doc.encode()).hexdigest(), 'length': len(doc)}\n"
)
# The order a set of str iterates in is a function of PYTHONHASHSEED: exactly the
# nondeterminism the AST cannot see and replication exists to catch.
SET_ORDER = (
    "def handle(payload):\n"
    "    return {'order': list(set(payload['words']))}\n"
)
SPIN = (
    "def handle(payload):\n"
    "    n = 0\n"
    "    while True:\n"
    "        n += 1\n"
)
CLOCK = (
    "import datetime\n"
    "def handle(payload):\n"
    "    return {'at': datetime.datetime.now().isoformat()}\n"
)
BOOM = "def handle(payload):\n    raise ValueError('bad input: ' + str(payload.get('x')))\n"
BIG = "def handle(payload):\n    return {'x': 'a' * 20000}\n"
HOG = "def handle(payload):\n    blob = 'a' * (220 * 1024 * 1024)\n    return {'n': len(blob)}\n"
# print is an admitted builtin; its output must never reach the envelope.
CHATTY = (
    "def handle(payload):\n"
    "    for i in range(5000):\n"
    "        print('noise', i)\n"
    "    return {'n': payload['n']}\n"
)

FUNCTIONS = {
    "chatty": CHATTY,
    "pure": PURE,
    "set-order": SET_ORDER,
    "spin": SPIN,
    "clock": CLOCK,
    "boom": BOOM,
    "big": BIG,
    "hog": HOG,
}
WORDS = [f"w{i}-{chr(97 + i) * 3}" for i in range(26)]


@pytest.mark.parametrize("expression", [
    "repr(object())", "str(object())", "ascii(object())", "format(object())",
    "f'{object()!r}'", "repr(handle)",
    # format without a literal spec falls back to str(x); an alias escapes the call check
    "format(handle)", "format(handle, '')", "format(1, p.get('spec', 'x'))", "[format][0](handle)",
])
def test_process_address_sources_are_refused(expression):
    source = f"def handle(p):\n    return {{'address': {expression}}}\n"
    with pytest.raises(HandlerDenied):
        admit_handler(source, deterministic=True)


def test_dunder_definitions_are_refused():
    with pytest.raises(HandlerDenied, match="__str__"):
        admit_handler("class Sneaky:\n    def __str__(self):\n        return 'hidden'\n")


def compute_settings(tmp_path, **overrides) -> Settings:
    base = Settings.for_test(tmp_path)
    fields = {
        **base.__dict__,
        "compute_enabled": True,
        "compute_hub_keys": (HUB_KEY,),
        "compute_functions": tuple(FUNCTIONS),
        **overrides,
    }
    return Settings(**fields)


def compute_client(tmp_path, *slugs: str, **overrides) -> TestClient:
    api = TestClient(build_app(compute_settings(tmp_path, **overrides)))
    for slug in slugs:
        deploy(api, slug)
    return api


def deploy(api: TestClient, slug: str, source: str | None = None) -> None:
    body = deploy_payload(slug, FUNCTIONS[slug] if source is None else source)
    body["capability"]["capability_id"] = f"{slug}.fn@v1"
    res = api.post("/v1/tenants", json=body, headers=auth(api))
    assert res.status_code == 200, res.text


def call(api, cap, slug, payload, *, headers=None, pin=None):
    body: dict = {"slug": slug, "input": payload}
    if pin is not None:
        body["function_sha256"] = pin
    return api.post(
        "/ai-market/v2/invoke",
        json={"capability_id": cap, "input": body},
        headers={"X-API-Key": HUB_KEY} if headers is None else headers,
    )


def signature_holds(document: dict, public_key_b64: str) -> bool:
    signature = document["signature"]
    key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
    try:
        key.verify(base64.b64decode(signature["value"]), object_canonical(document).encode())
    except InvalidSignature:
        return False
    return True


def canonical_sha(value) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


# ------------------------------------------------------------ the claim itself


def test_a_pure_function_agrees_and_the_receipt_is_signed(tmp_path) -> None:
    api = compute_client(tmp_path, "pure")
    payload = {"b": [1, 2, 3], "a": "x"}
    res = call(api, COMPUTE_VERIFY, "pure", payload)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    output = body["result"]["output"]
    assert output["sha256"] == hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()
    ).hexdigest()

    receipt = body["result"]["compute_receipt"]
    assert receipt["method"] == "hestia.replicate@v1"
    assert receipt["replicas"] == 2
    assert receipt["same_operator"] is True
    assert receipt["runtime"] == "subprocess"
    assert receipt["function_sha256"] == hashlib.sha256(PURE.encode()).hexdigest()
    assert receipt["input_sha256"] == canonical_sha(payload)
    assert receipt["output_sha256"] == canonical_sha(output)
    assert len(set(receipt["hash_seeds"])) == 2, "the replicas must be perturbed"
    for series in ("cpu_ms", "max_rss_kb", "wall_ms"):
        assert len(receipt[series]) == 2
        assert all(isinstance(v, int) and v > 0 for v in receipt[series])
    assert receipt["limits"]["cpu_ms"] == 2000
    assert isinstance(receipt["limits"]["memory_capped_by_kernel"], bool)
    assert receipt["python_version"] and receipt["platform"]

    # Signed by the hearth, under the key a buyer can fetch from .well-known.
    well_known_key = api.get("/.well-known/ai-market.json").json()["signer_public_key"]
    assert receipt["signature"]["public_key"] == well_known_key
    assert signature_holds(receipt, well_known_key)
    # The hub-interop receipt around it now reports the real latency.
    assert body["receipt"]["latency_ms"] >= max(receipt["wall_ms"])
    assert body["receipt"]["price_usd"] == 0.0025


def test_a_tampered_receipt_does_not_verify(tmp_path) -> None:
    api = compute_client(tmp_path, "pure")
    receipt = call(api, COMPUTE_VERIFY, "pure", {"n": 1}).json()["result"]["compute_receipt"]
    key = receipt["signature"]["public_key"]
    assert signature_holds(receipt, key)
    forged = {**receipt, "output_sha256": "0" * 64}
    assert not signature_holds(forged, key)


def test_hash_order_dependence_disagrees_and_nothing_is_signed(tmp_path) -> None:
    api = compute_client(tmp_path, "set-order")
    res = call(api, COMPUTE_VERIFY, "set-order", {"words": WORDS})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is False
    assert body["refuse_reason"] == "replicas_disagree"
    assert body["signed"] is False
    for signed_part in ("result", "receipt", "signature", "compute_receipt"):
        assert signed_part not in body
    digests = {r["output_sha256"] for r in body["replicas"]}
    assert len(digests) == 2
    assert len({r["hash_seed"] for r in body["replicas"]}) == 2


def test_one_replica_is_a_metered_run_not_a_verification(tmp_path) -> None:
    api = compute_client(tmp_path, "set-order")
    body = call(api, COMPUTE_RUN, "set-order", {"words": WORDS}).json()
    # A single replica cannot disagree with itself: run mode says so by method.
    assert body["ok"] is True
    receipt = body["result"]["compute_receipt"]
    assert receipt["method"] == "hestia.run@v1"
    assert receipt["replicas"] == 1
    assert body["receipt"]["price_usd"] == 0.001


def test_a_cpu_spin_is_killed_at_its_limit_and_metered(tmp_path) -> None:
    api = compute_client(tmp_path, "spin", compute_cpu_ms=300)
    body = call(api, COMPUTE_VERIFY, "spin", {}).json()
    assert body["ok"] is False
    assert body["refuse_reason"] == "cpu_limit_exceeded"
    for replica in body["replicas"]:
        assert replica["outcome"] == "cpu_limit_exceeded"
        # Measured by the kernel, not reported by the function: it ran to the limit.
        assert replica["cpu_ms"] >= 300


def test_a_wall_clock_overrun_is_killed_by_the_parent(tmp_path) -> None:
    api = compute_client(tmp_path, "spin", compute_cpu_ms=10_000, compute_wall_ms=600)
    body = call(api, COMPUTE_RUN, "spin", {}).json()
    assert body["ok"] is False
    assert body["refuse_reason"] == "wall_time_exceeded"
    assert body["replicas"][0]["wall_ms"] >= 600


def test_memory_over_the_limit_is_refused(tmp_path) -> None:
    # Linux caps it with RLIMIT_AS (MemoryError inside the replica); macOS refuses
    # RLIMIT_AS, so there the kernel's peak-RSS measurement decides. Same answer.
    api = compute_client(tmp_path, "hog", compute_memory_mb=128)
    body = call(api, COMPUTE_RUN, "hog", {}).json()
    assert body["ok"] is False
    assert body["refuse_reason"] == "memory_limit_exceeded"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="only Linux enforces RLIMIT_AS")
def test_on_linux_the_kernel_itself_holds_the_memory_limit(tmp_path) -> None:
    """macOS will not set RLIMIT_AS, so a run there proves only the peak-RSS fallback. On
    Linux the ceiling must be in place and be what stopped the function: a MemoryError
    inside the replica while its resident set is still under the limit — not the parent
    reading an overrun after the fact — and a signed receipt that says the kernel held it."""
    api = compute_client(tmp_path, "hog", "pure", compute_memory_mb=128)
    body = call(api, COMPUTE_RUN, "hog", {}).json()
    assert body["refuse_reason"] == "memory_limit_exceeded"
    (replica,) = body["replicas"]
    assert replica["error"] == "the function ran out of memory", replica
    assert replica["max_rss_kb"] <= 128 * 1024
    receipt = call(api, COMPUTE_VERIFY, "pure", {"a": 1}).json()["result"]["compute_receipt"]
    assert receipt["limits"]["memory_capped_by_kernel"] is True


def test_output_over_the_ceiling_is_refused(tmp_path) -> None:
    api = compute_client(tmp_path, "big", compute_max_output_bytes=4096)
    body = call(api, COMPUTE_RUN, "big", {}).json()
    assert body["refuse_reason"] == "output_too_large"


def test_a_function_that_prints_still_answers(tmp_path) -> None:
    """A print used to land in front of the envelope: the parent read garbage,
    booked the function's chatter as the hearth's failure, and gave the payment
    back — so the same transaction could buy the same work again."""
    api = compute_client(tmp_path, "chatty")
    body = call(api, COMPUTE_VERIFY, "chatty", {"n": 4}).json()
    assert body["ok"] is True, body
    assert body["result"]["output"] == {"n": 4}


def test_the_functions_own_error_comes_back_unsigned(tmp_path) -> None:
    api = compute_client(tmp_path, "boom")
    body = call(api, COMPUTE_VERIFY, "boom", {"x": 7}).json()
    assert body["ok"] is False
    assert body["refuse_reason"] == "function_error"
    assert "bad input: 7" in body["detail"]


# ----------------------------------------------------------- what may run


def test_a_clock_reading_function_is_not_admissible(tmp_path) -> None:
    api = compute_client(tmp_path, "clock")
    res = call(api, COMPUTE_VERIFY, "clock", {})
    assert res.status_code == 422
    assert "clock" in res.json()["detail"]
    listing = {f["slug"]: f for f in api.get("/v1/compute").json()["functions"]}
    assert listing["clock"]["admissible"] is False


def test_a_pinned_digest_that_does_not_match_is_refused(tmp_path) -> None:
    api = compute_client(tmp_path, "pure")
    res = call(api, COMPUTE_VERIFY, "pure", {}, pin="a" * 64)
    assert res.status_code == 409
    right = hashlib.sha256(PURE.encode()).hexdigest()
    assert right in res.json()["detail"]
    assert call(api, COMPUTE_VERIFY, "pure", {}, pin=right).json()["ok"] is True


def test_only_published_running_template_tenants_are_functions(tmp_path) -> None:
    api = compute_client(tmp_path, "pure", compute_functions=("pure",))
    deploy(api, "boom")  # running, but not published for compute
    assert call(api, COMPUTE_RUN, "boom", {}).status_code == 404
    assert call(api, COMPUTE_RUN, "nobody-here", {}).status_code == 404
    api.post("/v1/tenants/pure/stop", headers=auth(api))
    # Stopping is how an operator withdraws it; the file on disk is not enough.
    assert call(api, COMPUTE_RUN, "pure", {}).status_code == 404


@pytest.mark.parametrize(
    "body, fragment",
    [
        ({"slug": "pure"}, "input must be a JSON object"),
        ({"slug": "pure", "input": [1]}, "input must be a JSON object"),
        ({"slug": "pure", "input": {}, "code": "x"}, "unknown compute field"),
        ({"slug": "Bad Slug", "input": {}}, "slug"),
        ({"slug": "pure", "input": {}, "function_sha256": "xyz"}, "64 lowercase hex"),
    ],
)
def test_malformed_requests_are_refused_before_anything_runs(tmp_path, body, fragment) -> None:
    api = compute_client(tmp_path, "pure")
    res = api.post(
        "/ai-market/v2/invoke",
        json={"capability_id": COMPUTE_RUN, "input": body},
        headers={"X-API-Key": HUB_KEY},
    )
    assert res.status_code == 400
    assert fragment in res.json()["detail"]


def test_non_json_input_is_refused() -> None:
    with pytest.raises(compute.ComputeRefused, match="canonical JSON"):
        compute.parse_request({"slug": "pure", "input": {"x": float("nan")}})
    with pytest.raises(compute.ComputeRefused, match="must be an object"):
        compute.parse_request(["not", "an", "object"])


# ------------------------------------------------------------ containment


def _bypass_admission(monkeypatch) -> None:
    """Start replicas whose worker skips admission — as if a gadget had beaten
    the scanner — so what is asserted is the process the parent built."""
    monkeypatch.setattr(
        compute,
        "_BOOTSTRAP",
        f"import sys; sys.path.append({compute.PACKAGE_ROOT!r}); "
        "import hestia.compute_worker as w; w.admit_handler = lambda *a, **k: None; "
        "raise SystemExit(w.main())",
    )


LIMITS = Limits(cpu_ms=2000, memory_mb=256, wall_ms=5000, max_output_bytes=1 << 20)


def test_a_replica_cannot_read_the_hearth_key_or_a_tenant_key(tmp_path, monkeypatch) -> None:
    api = compute_client(tmp_path, "pure")  # a hearth key and a running tenant's key
    provider_key = tmp_path / "provider.key"
    tenant_key = tmp_path / "tenants" / "pure" / "tenant.key"
    assert provider_key.is_file() and tenant_key.is_file()
    _bypass_admission(monkeypatch)
    for target in (provider_key, tenant_key):
        source = f"def handle(p):\n    return {{'k': open({str(target)!r}, 'rb').read().hex()}}\n"
        job = compute_worker.canonical_bytes({"source": source, "input": {}})
        replica = compute.run_replica(job, LIMITS, index=0, hash_seed=7)
        assert replica.outcome == "containment_violation", replica
        assert str(target) in replica.error
    assert api.get("/health").json()["tenants"] == 1


def test_a_replica_inherits_no_secret_and_sees_an_empty_directory(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HESTIA_DEPLOY_TOKEN", "operator-secret")
    monkeypatch.setenv("AIMARKET_PEER_API_KEYS", "https://x=aimk_secret")
    _bypass_admission(monkeypatch)
    source = (
        "import os\n"
        "def handle(p):\n"
        "    return {'env': dict(os.environ), 'cwd': os.getcwd(), 'files': os.listdir('.')}\n"
    )
    job = compute_worker.canonical_bytes({"source": source, "input": {}})
    replica = compute.run_replica(job, LIMITS, index=0, hash_seed=12345)
    assert replica.outcome == "ok", replica
    env = replica.output["env"]
    assert not any(k.startswith(("HESTIA_", "AIMARKET_")) for k in env)
    assert "operator-secret" not in json.dumps(env)
    assert env["PYTHONHASHSEED"] == "12345"
    assert env["TZ"] == "UTC"
    assert replica.output["files"] == []
    assert "hestia-compute-" in replica.output["cwd"]
    # ...and the scratch directory is gone afterwards.
    assert not os.path.exists(replica.output["cwd"])


def test_a_replica_that_writes_no_envelope_is_the_platforms_failure(monkeypatch) -> None:
    monkeypatch.setattr(compute, "_BOOTSTRAP", "import sys; sys.stdout.write('garbage')")
    job = compute_worker.canonical_bytes({"source": PURE, "input": {}})
    replica = compute.run_replica(job, LIMITS, index=0, hash_seed=3)
    assert replica.outcome == compute.PLATFORM_FAILURE
    assert "no envelope" in replica.error


def test_a_replica_that_refuses_its_job_is_the_platforms_failure(monkeypatch) -> None:
    monkeypatch.setattr(compute, "_BOOTSTRAP", "raise SystemExit(70)")
    replica = compute.run_replica(b"{}", LIMITS, index=0, hash_seed=3)
    assert replica.outcome == compute.PLATFORM_FAILURE
    assert "refused its job" in replica.error


def test_a_replica_that_cannot_start_is_the_platforms_failure(monkeypatch) -> None:
    monkeypatch.setattr(compute, "worker_argv", lambda: ["/nonexistent/python"])
    replica = compute.run_replica(b"{}", LIMITS, index=0, hash_seed=3)
    assert replica.outcome == compute.PLATFORM_FAILURE
    assert "did not start" in replica.error


def test_the_worker_rlimits_are_the_published_ones(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(compute.resource, "setrlimit", lambda res, lim: calls.append((res, lim)))
    compute.rlimit_preexec(Limits(cpu_ms=1500, memory_mb=128, wall_ms=1000, max_output_bytes=1))()
    r = compute.resource
    assert (r.RLIMIT_CORE, (0, 0)) in calls
    assert (r.RLIMIT_CPU, (2, 3)) in calls  # whole seconds, rounded up; hard +1
    assert (r.RLIMIT_NOFILE, (32, 32)) in calls
    assert (r.RLIMIT_FSIZE, (0, 0)) in calls
    assert (r.RLIMIT_AS, (128 * 1024 * 1024,) * 2) in calls


# --------------------------------------------------------------- the gate


def test_an_unpaid_direct_call_is_refused_and_nothing_runs(tmp_path, monkeypatch) -> None:
    api = compute_client(tmp_path, "pure")
    ran = []
    monkeypatch.setattr("hestia.app.run_replicas", lambda *a, **k: ran.append(a) or [])
    res = call(api, COMPUTE_VERIFY, "pure", {}, headers={})
    # A hub-key-only hearth has no direct door at all.
    assert res.status_code == 401
    assert res.json()["error"] == "hub_key_required"
    wrong = call(api, COMPUTE_VERIFY, "pure", {}, headers={"X-API-Key": "x" * 40})
    assert wrong.status_code == 401
    assert ran == []


def paid_client(tmp_path, *slugs, **overrides) -> TestClient:
    return compute_client(
        tmp_path,
        *slugs,
        **{
            "compute_hub_keys": (),
            "compute_payout_address": COMPUTE_WALLET,
            "payment_rpc_url": "http://rpc.invalid",
            **overrides,
        },
    )


SECRET = "0x" + hashlib.sha256(b"a direct buyer's own secret").hexdigest()


def paid_directly(monkeypatch, *, to: str = COMPUTE_WALLET, units: int = 2500,
                  secret: str = SECRET) -> None:
    """A chain on which the buyer paid with transferWithAuthorization, nonce = sha256(secret)."""
    fake_rpc_bound(monkeypatch, to=to, units=units, nonce=nonce_for_secret(secret))


def direct(tx: str, secret: str = SECRET) -> dict:
    return {"X-Payment": tx, "X-Payment-Secret": secret}


def test_an_unpaid_call_is_quoted_without_a_nonce(tmp_path) -> None:
    api = paid_client(tmp_path, "pure")
    res = call(api, COMPUTE_VERIFY, "pure", {}, headers={})
    assert res.status_code == 402
    body = res.json()
    assert body["pay_to"] == COMPUTE_WALLET
    assert body["amount_units"] == "2500"
    assert (body["binding"], body["nonce_rule"]) == ("secret", "sha256(secret)")
    assert "X-Payment-Secret" in body["how"]
    # The hub is the till on the production rail. A nonce minted here would be a
    # second one, which one EIP-3009 authorization cannot satisfy: a direct buyer
    # picks the nonce, as the hash of a secret only they hold.
    assert "nonce" not in body
    assert "X-Payment-Nonce" not in res.headers
    assert body["accepts"][0]["payTo"] == COMPUTE_WALLET


def test_a_verified_payment_buys_exactly_one_call(tmp_path, monkeypatch) -> None:
    api = paid_client(tmp_path, "pure")
    paid_directly(monkeypatch)
    tx = direct("0x" + "c" * 64)
    first = call(api, COMPUTE_VERIFY, "pure", {"n": 1}, headers=tx)
    assert first.status_code == 200 and first.json()["ok"] is True
    again = call(api, COMPUTE_VERIFY, "pure", {"n": 1}, headers=tx)
    assert again.status_code == 402
    assert "already been spent" in again.json()["detail"]
    assert api.app.state.store.payment_count(COMPUTE_VERIFY) == 1


def test_someone_watching_the_chain_cannot_redeem_a_direct_payment(tmp_path, monkeypatch) -> None:
    """The race this door had. The buyer's transferWithAuthorization is mined: its hash and
    its nonce are now public. A watcher who presents them first used to get the call the
    buyer paid for, and the buyer was refused as "already spent". The nonce is sha256 of a
    secret only the buyer holds, so the watcher has nothing that opens it."""
    api = paid_client(tmp_path, "pure")
    paid_directly(monkeypatch)
    tx = "0x" + "9" * 64
    public_nonce = nonce_for_secret(SECRET)
    for attempt in ({"X-Payment": tx}, {"X-Payment": tx, "X-Payment-Nonce": public_nonce},
                    direct(tx, secret=public_nonce), direct(tx, secret="0x" + hashlib.sha256(b"a guess").hexdigest())):
        watcher = call(api, COMPUTE_VERIFY, "pure", {"n": 1}, headers=attempt)
        assert watcher.status_code == 402, attempt
    assert api.app.state.store.payment_count(COMPUTE_VERIFY) == 0
    buyer = call(api, COMPUTE_VERIFY, "pure", {"n": 1}, headers=direct(tx))
    assert buyer.status_code == 200 and buyer.json()["ok"] is True
    assert api.app.state.store.payment_count(COMPUTE_VERIFY) == 1


def test_a_plain_transfer_is_refused_at_the_keyless_door(tmp_path, monkeypatch) -> None:
    """A plain ERC-20 transfer carries no nonce at all, so nothing ties it to whoever
    presents it. Only the hub, with its key, may forward one."""
    api = paid_client(tmp_path, "pure")
    fake_rpc(monkeypatch, to=COMPUTE_WALLET, units=2500)
    res = call(api, COMPUTE_VERIFY, "pure", {}, headers=direct("0x" + "8" * 64))
    assert res.status_code == 402 and "not bound" in res.json()["detail"]
    assert api.app.state.store.payment_count(COMPUTE_VERIFY) == 0


def test_a_direct_payment_bundled_by_someone_else_pays_only_its_payer(tmp_path, monkeypatch) -> None:
    api = paid_client(tmp_path, "pure")
    theirs = "0x" + hashlib.sha256(b"attacker").hexdigest()
    fake_rpc_logs(monkeypatch, [
        auth_used(BUYER, nonce_for_secret(SECRET)), transfer(BUYER, COMPUTE_WALLET, 2500),
        auth_used(ATTACKER, nonce_for_secret(theirs)), transfer(ATTACKER, COMPUTE_WALLET, 1),
    ])
    tx = "0x" + "b" * 64
    stolen = call(api, COMPUTE_VERIFY, "pure", {}, headers=direct(tx, secret=theirs))
    assert stolen.status_code == 402 and "paid 1 base units" in stolen.json()["detail"]
    assert call(api, COMPUTE_VERIFY, "pure", {}, headers=direct(tx)).json()["ok"] is True


def test_a_same_nonce_authorization_placed_first_cannot_deny_a_direct_buyer(tmp_path, monkeypatch) -> None:
    api = paid_client(tmp_path, "pure")
    n = nonce_for_secret(SECRET)
    fake_rpc_logs(monkeypatch, [
        auth_used(ATTACKER, n), transfer(ATTACKER, COMPUTE_WALLET, 1),
        auth_used(BUYER, n), transfer(BUYER, COMPUTE_WALLET, 2500),
    ])
    assert call(api, COMPUTE_VERIFY, "pure", {}, headers=direct("0x" + "a" * 64)).json()["ok"] is True


def test_a_malformed_secret_on_the_hub_key_door_is_a_refusal_not_a_crash(tmp_path, monkeypatch) -> None:
    api = paid_client(tmp_path, "pure", compute_hub_keys=(HUB_KEY,))
    paid_directly(monkeypatch)
    res = call(api, COMPUTE_VERIFY, "pure", {}, headers={
        "X-API-Key": HUB_KEY, "X-Payment": "0x" + "b" * 64, "X-Payment-Secret": "0xzz"})
    assert res.status_code == 402 and "not a payment secret" in res.json()["detail"]


def test_two_direct_payments_in_one_transaction_buy_two_calls(tmp_path, monkeypatch) -> None:
    api = paid_client(tmp_path, "pure")
    second = "0x" + hashlib.sha256(b"a second payment").hexdigest()
    fake_rpc_logs(monkeypatch, [
        auth_used(BUYER, nonce_for_secret(SECRET)), transfer(BUYER, COMPUTE_WALLET, 2500),
        auth_used(BUYER, nonce_for_secret(second)), transfer(BUYER, COMPUTE_WALLET, 2500),
    ])
    tx = "0x" + "f" * 64
    for secret in (SECRET, second):
        assert call(api, COMPUTE_VERIFY, "pure", {}, headers=direct(tx, secret=secret)).json()["ok"] is True
    assert call(api, COMPUTE_VERIFY, "pure", {}, headers=direct(tx)).status_code == 402
    assert api.app.state.store.payment_count(COMPUTE_VERIFY) == 2


def test_the_hub_names_the_authorization_it_forwards_and_it_pays_once(tmp_path, monkeypatch) -> None:
    """The hub settled the buyer's payment against its own invoice, whose nonce the buyer
    signed. Forwarded with the key, that authorization is the payment the hearth claims —
    so the same one cannot be redeemed again at the direct door by the secret behind it."""
    api = paid_client(tmp_path, "pure", compute_hub_keys=(HUB_KEY,))
    hub_invoice = nonce_for_secret(SECRET)
    fake_rpc_logs(monkeypatch, [auth_used(BUYER, hub_invoice), transfer(BUYER, COMPUTE_WALLET, 2500)])
    tx = "0x" + "c" * 64
    unnamed = call(api, COMPUTE_VERIFY, "pure", {}, headers={"X-API-Key": HUB_KEY, "X-Payment": tx})
    assert unnamed.status_code == 402 and "X-Payment-Nonce" in unnamed.json()["detail"]
    named = {"X-API-Key": HUB_KEY, "X-Payment": tx, "X-Payment-Nonce": hub_invoice}
    assert call(api, COMPUTE_VERIFY, "pure", {}, headers=named).json()["ok"] is True
    twice = call(api, COMPUTE_VERIFY, "pure", {}, headers=direct(tx))
    assert twice.status_code == 402 and "already been spent" in twice.json()["detail"]
    assert api.app.state.store.payment_count(COMPUTE_VERIFY) == 1


@pytest.mark.parametrize("secret, why", [
    ("0x" + "00" * 32, "guessable"),
    ("0x" + "0123456789abcdef" * 4, "guessable"),
    ("0x" + "ab" * 31, "X-Payment-Secret"),
    ("not-hex", "X-Payment-Secret"),
], ids=["zeros", "eight-byte-pattern", "short", "not-hex"])
def test_a_guessable_or_malformed_secret_is_refused_before_the_chain(tmp_path, monkeypatch, secret, why) -> None:
    api = paid_client(tmp_path, "pure")
    looked: list = []
    monkeypatch.setattr("hestia.payments.httpx.post", lambda *a, **k: looked.append(a))
    res = call(api, COMPUTE_VERIFY, "pure", {}, headers=direct("0x" + "d" * 64, secret=secret))
    assert res.status_code == 402 and why in res.json()["detail"]
    assert looked == []


def test_a_secret_that_does_not_open_the_named_nonce_is_refused(tmp_path, monkeypatch) -> None:
    api = paid_client(tmp_path, "pure", compute_hub_keys=(HUB_KEY,))
    paid_directly(monkeypatch)
    other = "0x" + hashlib.sha256(b"other").hexdigest()
    for headers in (
        {**direct("0x" + "e" * 64), "X-Payment-Nonce": nonce_for_secret(other)},
        {"X-API-Key": HUB_KEY, "X-Payment": "0x" + "e" * 64, "X-Payment-Secret": other,
         "X-Payment-Nonce": nonce_for_secret(SECRET)},
    ):
        res = call(api, COMPUTE_VERIFY, "pure", {}, headers=headers)
        assert res.status_code == 402 and "does not open" in res.json()["detail"], headers
    assert api.app.state.store.payment_count(COMPUTE_VERIFY) == 0


def test_a_transfer_forwarded_with_the_hub_key_is_claimed(tmp_path, monkeypatch) -> None:
    """The hub sends its key along with a buyer's transfer. Accepted on the key alone, the
    transfer stayed unclaimed on chain and bought a second call for whoever replayed it."""
    api = paid_client(tmp_path, "pure", compute_hub_keys=(HUB_KEY,))
    hub_invoice = nonce_for_secret(SECRET)
    fake_rpc_logs(monkeypatch, [auth_used(BUYER, hub_invoice), transfer(BUYER, COMPUTE_WALLET, 2500)])
    both = {"X-API-Key": HUB_KEY, "X-Payment": "0x" + "a" * 64, "X-Payment-Nonce": hub_invoice}
    assert call(api, COMPUTE_VERIFY, "pure", {"n": 1}, headers=both).json()["ok"] is True
    assert api.app.state.store.payment_count(COMPUTE_VERIFY) == 1
    # Without the key, the same hash is refused whatever secret comes with it.
    for replayed in ({"X-Payment": both["X-Payment"]}, direct(both["X-Payment"])):
        assert call(api, COMPUTE_VERIFY, "pure", {"n": 1}, headers=replayed).status_code == 402
    again = call(api, COMPUTE_VERIFY, "pure", {"n": 1}, headers=both)
    assert again.status_code == 402
    # The key on its own is still the hub's credits door.
    assert call(api, COMPUTE_VERIFY, "pure", {"n": 1}).json()["ok"] is True
    assert api.app.state.store.payment_count(COMPUTE_VERIFY) == 1


def test_a_plain_transfer_buys_nothing_even_with_the_hub_key(tmp_path, monkeypatch) -> None:
    """A plain transfer names no authorization, so nothing in it says who paid or for what:
    whoever presented it first would take the call. The hub never settles one, and the
    hearth takes none from it either."""
    api = paid_client(tmp_path, "pure", compute_hub_keys=(HUB_KEY,))
    looked: list = []
    monkeypatch.setattr("hestia.payments.httpx.post", lambda *a, **k: looked.append(a))
    plain = {"X-API-Key": HUB_KEY, "X-Payment": "0x" + "a" * 64}
    res = call(api, COMPUTE_VERIFY, "pure", {"n": 1}, headers=plain)
    assert res.status_code == 402 and "X-Payment-Nonce" in res.json()["detail"]
    assert looked == [], "refused before the chain is asked"
    assert api.app.state.store.payment_count(COMPUTE_VERIFY) == 0


def test_an_underpayment_or_a_payment_elsewhere_buys_nothing(tmp_path, monkeypatch) -> None:
    api = paid_client(tmp_path, "pure")
    paid_directly(monkeypatch, units=2499)
    short = call(api, COMPUTE_VERIFY, "pure", {}, headers=direct("0x" + "d" * 64))
    assert short.status_code == 402
    # Paying the tenant owner (or anyone else) is not paying for compute.
    paid_directly(monkeypatch, to=BUYER, units=10_000)
    wrong = call(api, COMPUTE_VERIFY, "pure", {}, headers=direct("0x" + "e" * 64))
    assert wrong.status_code == 402
    assert api.app.state.store.payment_count(COMPUTE_VERIFY) == 0


def test_a_refused_request_never_spends_the_payment(tmp_path, monkeypatch) -> None:
    api = paid_client(tmp_path, "pure")
    paid_directly(monkeypatch)
    tx = direct("0x" + "1" * 64)
    assert call(api, COMPUTE_VERIFY, "pure", {}, headers=tx, pin="b" * 64).status_code == 409
    assert api.app.state.store.payment_count(COMPUTE_VERIFY) == 0
    assert call(api, COMPUTE_VERIFY, "pure", {}, headers=tx).json()["ok"] is True


def test_a_platform_failure_gives_the_payment_back(tmp_path, monkeypatch) -> None:
    api = paid_client(tmp_path, "pure")
    paid_directly(monkeypatch)
    monkeypatch.setattr(
        "hestia.app.run_replicas",
        lambda *a, **k: [Replica(index=0, hash_seed=1, error="spawn failed")],
    )
    res = call(api, COMPUTE_VERIFY, "pure", {}, headers=direct("0x" + "2" * 64))
    assert res.status_code == 502
    assert api.app.state.store.payment_count(COMPUTE_VERIFY) == 0


def test_an_exception_while_running_gives_the_payment_back(tmp_path, monkeypatch) -> None:
    api = paid_client(tmp_path, "pure")
    paid_directly(monkeypatch)

    def _explode(*_a, **_k):
        raise RuntimeError("fork failed")

    monkeypatch.setattr("hestia.app.run_replicas", _explode)
    with pytest.raises(RuntimeError):
        call(api, COMPUTE_VERIFY, "pure", {}, headers=direct("0x" + "5" * 64))
    assert api.app.state.store.payment_count(COMPUTE_VERIFY) == 0


def test_a_busy_hearth_gives_the_payment_back(tmp_path, monkeypatch) -> None:
    class Full:
        def __init__(self, _capacity):
            pass

        def acquire(self, _count, timeout):
            return False

        def release(self, _count):  # pragma: no cover - never acquired
            raise AssertionError

    monkeypatch.setattr("hestia.app.ReplicaSlots", Full)
    api = paid_client(tmp_path, "pure")
    paid_directly(monkeypatch)
    res = call(api, COMPUTE_VERIFY, "pure", {}, headers=direct("0x" + "3" * 64))
    assert res.status_code == 503
    assert api.app.state.store.payment_count(COMPUTE_VERIFY) == 0


def test_a_disagreement_is_delivered_work_and_keeps_the_payment(tmp_path, monkeypatch) -> None:
    api = paid_client(tmp_path, "set-order")
    paid_directly(monkeypatch)
    res = call(
        api, COMPUTE_VERIFY, "set-order", {"words": WORDS}, headers=direct("0x" + "4" * 64)
    )
    assert res.json()["refuse_reason"] == "replicas_disagree"
    # Released, the same transaction would buy the same refused work forever.
    assert api.app.state.store.payment_count(COMPUTE_VERIFY) == 1


def test_an_unreachable_chain_refuses_rather_than_computing_free(tmp_path, monkeypatch) -> None:
    import httpx

    api = paid_client(tmp_path, "pure")

    def _down(*_a, **_k):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr("hestia.payments.httpx.post", _down)
    res = call(api, COMPUTE_VERIFY, "pure", {}, headers=direct("0x" + "6" * 64))
    assert res.status_code == 503


def test_hub_key_comparison_checks_every_key() -> None:
    keys = ("a" * 30, "b" * 30)
    assert compute.hub_key_matches("b" * 30, keys)
    assert not compute.hub_key_matches("c" * 30, keys)
    assert not compute.hub_key_matches("b" * 30, ())


# ----------------------------------------------------------- publication


def test_compute_is_absent_until_it_is_enabled(tmp_path) -> None:
    api = TestClient(build_app(Settings.for_test(tmp_path)))
    tools = [t["capability_id"] for t in api.get("/ai-market/v2/manifest").json()["tools"]]
    assert COMPUTE_RUN not in tools and COMPUTE_VERIFY not in tools
    assert api.get("/v1/compute").json() == {"ok": True, "enabled": False}
    res = api.post(
        "/ai-market/v2/invoke", json={"capability_id": COMPUTE_RUN, "input": {"slug": "x"}}
    )
    assert res.status_code == 404


def test_the_manifest_names_the_compute_wallet_so_the_hub_can_sell_it(tmp_path) -> None:
    api = paid_client(tmp_path)
    manifest = api.get("/ai-market/v2/manifest").json()
    caps = {t["capability_id"]: t for t in manifest["tools"]}
    for cap_id, price in ((COMPUTE_RUN, 0.001), (COMPUTE_VERIFY, 0.0025)):
        cap = caps[cap_id]
        assert cap["payout_address"] == COMPUTE_WALLET
        assert cap["price_per_call_usd"] == price
        assert cap["limits"]["cpu_ms"] == 2000
        assert cap["input_schema"]["required"] == ["slug", "input"]
    assert manifest["capabilities_count"] == len(manifest["tools"])
    well_known = api.get("/.well-known/ai-market.json").json()
    assert COMPUTE_VERIFY in well_known["capabilities"]
    assert well_known["capabilities_count"] == len(manifest["tools"])


def test_the_compute_catalogue_is_signed_and_names_each_digest(tmp_path) -> None:
    api = compute_client(tmp_path, "pure", "clock")
    catalogue = api.get("/v1/compute").json()
    assert signature_holds(catalogue, catalogue["provider_pubkey"])
    functions = {f["slug"]: f for f in catalogue["functions"]}
    assert functions["pure"]["function_sha256"] == hashlib.sha256(PURE.encode()).hexdigest()
    assert functions["pure"]["admissible"] is True
    assert catalogue["same_operator"] is True
    assert {c["capability_id"] for c in catalogue["capabilities"]} == {COMPUTE_RUN, COMPUTE_VERIFY}


# ----------------------------------------------------------------- config


@pytest.mark.parametrize(
    "overrides, fragment",
    [
        ({"compute_hub_keys": ()}, "needs a way to be paid"),
        ({"compute_payout_address": "0xnope"}, "0x EVM address"),
        ({"compute_payout_address": COMPUTE_WALLET}, "HESTIA_PAYMENT_RPC_URL"),
        ({"compute_hub_keys": ("short",)}, "at least 24"),
        ({"compute_functions": ("Not A Slug",)}, "HESTIA_COMPUTE_FUNCTIONS"),
        ({"require_sandbox": True}, "HESTIA_COMPUTE_ALLOW_UNSANDBOXED"),
        ({"compute_max_concurrent": 1}, "at least 2"),
        ({"compute_cpu_ms": 10}, "HESTIA_COMPUTE_CPU_MS"),
        ({"compute_memory_mb": 8}, "HESTIA_COMPUTE_MEMORY_MB"),
        ({"compute_wall_ms": 60_000}, "HESTIA_COMPUTE_WALL_S"),
        ({"compute_max_output_bytes": 10}, "HESTIA_COMPUTE_MAX_OUTPUT_BYTES"),
        ({"compute_verify_price_usd": 0.0}, "prices"),
    ],
)
def test_a_compute_config_that_cannot_work_refuses_to_start(tmp_path, overrides, fragment) -> None:
    with pytest.raises(RuntimeError, match=fragment):
        build_app(compute_settings(tmp_path, **overrides))


def test_a_sandboxed_hearth_can_accept_unsandboxed_compute_explicitly(tmp_path) -> None:
    settings = compute_settings(tmp_path, require_sandbox=True, compute_allow_unsandboxed=True)
    assert compute.settings_problem(settings) == ""


def test_compute_env_is_parsed(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HESTIA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HESTIA_COMPUTE_ENABLED", "1")
    monkeypatch.setenv("HESTIA_COMPUTE_HUB_KEYS", f"{HUB_KEY}, {'z' * 30}")
    monkeypatch.setenv("HESTIA_COMPUTE_FUNCTIONS", "Pure, json-canonical")
    monkeypatch.setenv("HESTIA_COMPUTE_WALL_S", "2.5")
    monkeypatch.setenv("HESTIA_COMPUTE_PAYOUT_ADDRESS", COMPUTE_WALLET)
    settings = Settings.from_env()
    assert settings.compute_enabled is True
    assert settings.compute_hub_keys == (HUB_KEY, "z" * 30)
    assert settings.compute_functions == ("pure", "json-canonical")
    assert settings.compute_wall_ms == 2500
    assert settings.compute_payout_address == COMPUTE_WALLET
    assert settings.compute_verify_price_usd == 0.0025


# ------------------------------------------------------ admission + worker


@pytest.mark.parametrize(
    "src, fragment",
    [
        ("import datetime\nx = datetime.datetime.now()\n", "clock"),
        ("from datetime import date\nx = date.today()\n", "clock"),
        ("from datetime import datetime\nx = datetime.fromtimestamp(0)\n", "clock"),
        ("from datetime import datetime\nx = datetime.utcnow\n", "clock"),
        ("x = id(object())\n", "id"),
        ("x = hash('a')\n", "hash"),
        ("import random\n", "random"),
        ("import time\n", "clock"),
        ("from datetime import now\n", "clock"),
        ("import os\n", "host"),
    ],
)
def test_deterministic_admission_refuses_what_differs_per_process(src, fragment) -> None:
    with pytest.raises(HandlerDenied, match=fragment):
        admit_handler(src, deterministic=True)


def test_deterministic_admission_keeps_date_arithmetic_and_narrows_only() -> None:
    src = (
        "import datetime\n"
        "def handle(p):\n"
        "    d = datetime.date(2026, 1, 1) + datetime.timedelta(days=p['n'])\n"
        "    return {'d': d.isoformat()}\n"
    )
    admit_handler(src, deterministic=True)
    # A tenant handler is still allowed its clock: the stricter rules are compute's.
    admit_handler("import datetime\nx = datetime.datetime.now()\n")


def test_execute_reports_each_way_a_function_can_fail(tmp_path) -> None:
    def run(source: str) -> dict:
        return json.loads(compute_worker.execute(source, {"k": 1}, str(tmp_path)))

    assert run(PURE)["ok"] is True
    assert run("x = 1\n")["refuse_reason"] == "function_error"
    assert run("def handle(p):\n    return [1]\n")["refuse_reason"] == "function_error"
    assert run("def handle(p):\n    raise SystemExit(3)\n")["refuse_reason"] == "function_error"
    nan = "def handle(p):\n    return {'x': float('nan')}\n"
    assert run(nan)["refuse_reason"] == "output_not_json"
    assert run("def handle(p):\n    raise MemoryError\n")["refuse_reason"] == "memory_limit_exceeded"
    assert run(CLOCK)["refuse_reason"] == "function_not_admissible"
    unprintable = (
        "class Odd(Exception):\n"
        "    def __str__(self):\n"
        "        raise ValueError('no')\n"
        "def handle(p):\n"
        "    raise Odd()\n"
    )
    try:
        admit_handler(unprintable, deterministic=True)
    except HandlerDenied:
        pass  # the scanner refuses the dunder; the fallback is then moot
    else:
        assert run(unprintable)["error"] == "Odd"


def test_execute_contains_what_the_function_does(tmp_path, monkeypatch) -> None:
    secret = tmp_path.parent / f"{tmp_path.name}-secret.key"
    secret.write_text("SECRET")
    monkeypatch.setattr(compute_worker, "admit_handler", lambda *a, **k: None)
    source = f"def handle(p):\n    return {{'s': open({str(secret)!r}).read()}}\n"
    envelope = json.loads(compute_worker.execute(source, {}, str(tmp_path / "scratch")))
    assert envelope["refuse_reason"] == "containment_violation"


def test_the_worker_refuses_a_malformed_job(monkeypatch) -> None:
    class Stdin:
        buffer = io.BytesIO(b'{"source": 1}')

    monkeypatch.setattr(sys, "stdin", Stdin)
    assert compute_worker.main() == compute_worker.EXIT_BAD_JOB


def test_the_worker_preloads_its_imports_and_reports_its_memory_cap() -> None:
    compute_worker._preload()
    assert isinstance(compute_worker._rlimit_as(), int)


# ------------------------------------------------------------------ judge


def _r(index, outcome, sha="", error=""):
    return Replica(index=index, hash_seed=index + 1, outcome=outcome, output_sha256=sha, error=error)


def test_judge_puts_the_platform_first_then_the_budget() -> None:
    both = [_r(0, "cpu_limit_exceeded"), _r(1, compute.PLATFORM_FAILURE, error="x")]
    assert judge(both).outcome == compute.PLATFORM_FAILURE
    over = [_r(0, "ok", "a"), _r(1, "wall_time_exceeded")]
    assert judge(over).outcome == "wall_time_exceeded"


def test_judge_calls_an_answer_against_an_error_a_disagreement() -> None:
    assert judge([_r(0, "ok", "a"), _r(1, "function_error")]).outcome == "replicas_disagree"
    assert judge([_r(0, "ok", "a"), _r(1, "ok", "b")]).outcome == "replicas_disagree"
    agreed = judge([_r(0, "function_error", error="E"), _r(1, "function_error", error="E")])
    assert (agreed.outcome, agreed.detail) == ("function_error", "E")
    assert judge([_r(0, "ok", "a"), _r(1, "ok", "a")]).outcome == "ok"


def test_replica_slots_are_taken_whole_or_not_at_all() -> None:
    slots = compute.ReplicaSlots(2)
    assert slots.acquire(1, timeout=0.01)
    assert not slots.acquire(2, timeout=0.05)  # one free, two needed: refuse, take nothing
    assert slots.acquire(1, timeout=0.01)
    slots.release(2)
    assert slots.acquire(2, timeout=0.01)


def test_hash_seeds_are_distinct_and_never_zero() -> None:
    seeds = compute.distinct_seeds(5)
    assert len(set(seeds)) == 5
    assert all(0 < s < 2**32 for s in seeds)



def test_a_spec_bound_format_is_deterministic_and_admitted():
    """format(x, "04x") depends on the value alone — object.__format__ raises for any
    non-empty spec — so the json-canonical reference function (which escapes code points
    with it) is a compute function, not a refusal."""
    from hestia.scan import admit_handler

    admit_handler("def handle(p):\n    return {'x': format(p['n'], '04x')}\n", deterministic=True)
    from pathlib import Path

    agents = Path(__file__).resolve().parents[2] / "hestia-agents" / "agents" / "json-canonical" / "handler.py"
    if agents.exists():
        admit_handler(agents.read_text(encoding="utf-8"), deterministic=True)
