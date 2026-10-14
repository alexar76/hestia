"""Verified compute: buyer DATA through an operator-published function, measured.

Two host capabilities:

* ``hestia.compute.run@v1`` runs the function once;
* ``hestia.compute.verify@v1`` runs it twice in parallel, deliberately perturbed
  (a different ``PYTHONHASHSEED`` each, a staggered start), and the hearth signs
  a compute receipt only when both replicas produced the same canonical output
  bytes. When they do not, the answer is ``ok: false`` with
  ``refuse_reason: replicas_disagree`` and nothing is signed.

What that proves, and what it does not, is ``docs/COMPUTE.md``. The receipt says
the most important part itself: ``same_operator: true``. Two replicas under one
operator and one key catch nondeterminism and transient faults, not a dishonest
operator; the rung that does is the same function on a second, independently
keyed hearth (``hestia-agents cross-verify``).

Buyers bring data, never code. The function is the ``handler.py`` of a running
template tenant the operator has published for compute (HESTIA_COMPUTE_FUNCTIONS),
pinned by ``function_sha256`` = sha256 of its exact bytes, and it must pass the
deterministic admission (``scan.admit_handler(..., deterministic=True)``). Taking
Python from a buyer would be remote code execution by design — the AST scan is an
allow-list, not a sandbox.

Each replica is a fresh ``python -S -P -B`` running ``hestia.compute_worker``:

* in an empty 0700 working directory, removed afterwards, holding no key — and
  never a tenant directory, where a tenant's own ``tenant.key`` is readable;
* under RLIMIT_CPU, RLIMIT_AS, RLIMIT_NOFILE, RLIMIT_FSIZE=0 and RLIMIT_CORE=0,
  set before exec;
* against a wall-clock deadline this process enforces by killing it;
* measured by the kernel: CPU time and peak RSS come from ``os.wait4`` when the
  replica is reaped, so the function never reports its own usage. The budget is
  also checked against those numbers, because RLIMIT_CPU counts whole seconds
  and macOS does not enforce RLIMIT_AS at all.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import platform
import re
import secrets
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import hestia
from hestia import __version__
from hestia.compute_worker import (
    CANONICALIZATION,
    EXIT_BAD_JOB,
    WORKER_REASONS,
    canonical_bytes,
)
from hestia.payments import is_address
from hestia.scan import HandlerDenied, admit_handler
from hestia.slugs import validate_slug

try:  # POSIX only; absent on Windows.
    import resource
except ImportError:  # pragma: no cover - platform without rlimits
    resource = None  # type: ignore[assignment]

COMPUTE_RUN = "hestia.compute.run@v1"
COMPUTE_VERIFY = "hestia.compute.verify@v1"
COMPUTE_CAPS = (COMPUTE_RUN, COMPUTE_VERIFY)
REPLICAS = {COMPUTE_RUN: 1, COMPUTE_VERIFY: 2}
METHOD_RUN = "hestia.run@v1"
METHOD_REPLICATE = "hestia.replicate@v1"
METHODS = {COMPUTE_RUN: METHOD_RUN, COMPUTE_VERIFY: METHOD_REPLICATE}
RECEIPT_KIND = "hestia.compute.receipt@v1"
# What executes a replica. Not "stub" (the tenant runtime) and not a container:
# a plain subprocess under rlimits, which is the claim the receipt can make.
RUNTIME = "subprocess"

OK = "ok"
# The replica never ran the function to an answer, for a reason on the hearth's
# side (it could not start, it crashed, it wrote nothing). The buyer's payment is
# given back, exactly as a tenant 5xx gives it back on the edge.
PLATFORM_FAILURE = "platform_failure"
DISAGREE = "replicas_disagree"
# The input consumed its budget. Billed, like a tenant's 504: the work was done.
LIMIT_REASONS = (
    "cpu_limit_exceeded",
    "wall_time_exceeded",
    "memory_limit_exceeded",
    "output_too_large",
)

NOFILE = 32
STDERR_CAP = 8 * 1024
# Longest a replica's pipes are drained after it was reaped. A replica cannot
# fork (the guard refuses it), so the only writer is gone; this is a bound, not
# an expected wait.
DRAIN_S = 0.5
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
# The package this module was loaded from. The replica imports the worker from
# the SAME tree — so the admission rules and containment it runs under are the
# version the receipt names — and appends it after the standard library, so
# nothing in it can shadow a stdlib module.
PACKAGE_ROOT = str(Path(hestia.__file__).resolve().parent.parent)
_BOOTSTRAP = (
    f"import sys; sys.path.append({PACKAGE_ROOT!r}); "
    "from hestia.compute_worker import main; raise SystemExit(main())"
)


class ComputeRefused(Exception):
    """A compute request refused before anything ran or was paid for."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


# ---------------------------------------------------------------- settings


def settings_problem(settings: Any) -> str:
    """Why these settings cannot serve compute, or "" when they can (or it is off).

    Fail-loud at boot, like HESTIA_PAYMENTS_ENABLED without an RPC: a compute cap
    that can never be paid for would either refuse every buyer or, worse, be
    served free by whoever reads the config wrong.
    """
    if not settings.compute_enabled:
        return ""
    payee = settings.compute_payout_address
    if payee and not is_address(payee):
        return "HESTIA_COMPUTE_PAYOUT_ADDRESS must be a 0x EVM address"
    if payee and not settings.payment_rpc_url:
        return (
            "HESTIA_COMPUTE_PAYOUT_ADDRESS requires HESTIA_PAYMENT_RPC_URL: a compute "
            "payment is verified on chain, never taken on the caller's word"
        )
    if not payee and not settings.compute_hub_keys:
        return (
            "HESTIA_COMPUTE_ENABLED=1 needs a way to be paid: HESTIA_COMPUTE_PAYOUT_ADDRESS "
            "(+ HESTIA_PAYMENT_RPC_URL) and/or HESTIA_COMPUTE_HUB_KEYS"
        )
    if any(len(key) < 24 for key in settings.compute_hub_keys):
        return "every HESTIA_COMPUTE_HUB_KEYS entry must be at least 24 characters"
    for slug in settings.compute_functions:
        try:
            validate_slug(slug)
        except ValueError as exc:
            return f"HESTIA_COMPUTE_FUNCTIONS: {exc}"
    if settings.require_sandbox and not settings.compute_allow_unsandboxed:
        return (
            "HESTIA_REQUIRE_SANDBOX / HESTIA_PROFILE=prod refuses compute: replicas are "
            "plain subprocesses under rlimits, not a sandbox. Set "
            "HESTIA_COMPUTE_ALLOW_UNSANDBOXED=1 to accept that explicitly."
        )
    if settings.compute_max_concurrent < 2:
        return "HESTIA_COMPUTE_MAX_CONCURRENT must be at least 2 (a verify job runs two replicas)"
    if not 100 <= settings.compute_cpu_ms <= 60_000:
        return "HESTIA_COMPUTE_CPU_MS must be between 100 and 60000"
    if not 64 <= settings.compute_memory_mb <= 4096:
        return "HESTIA_COMPUTE_MEMORY_MB must be between 64 and 4096"
    # Two replicas run in parallel, and the whole job has to come back inside the
    # hub's 30 s peer timeout with a chain lookup in front of it.
    if not 100 <= settings.compute_wall_ms <= 15_000:
        return "HESTIA_COMPUTE_WALL_S must be between 0.1 and 15"
    if settings.compute_max_output_bytes < 1024:
        return "HESTIA_COMPUTE_MAX_OUTPUT_BYTES must be at least 1024"
    for price in (settings.compute_run_price_usd, settings.compute_verify_price_usd):
        if not 0 < price <= 100:
            return "compute prices must be above 0 and at most 100 USD"
    return ""


@dataclass(frozen=True)
class Limits:
    cpu_ms: int
    memory_mb: int
    wall_ms: int
    max_output_bytes: int
    nofile: int = NOFILE

    @classmethod
    def from_settings(cls, settings: Any) -> Limits:
        return cls(
            cpu_ms=settings.compute_cpu_ms,
            memory_mb=settings.compute_memory_mb,
            wall_ms=settings.compute_wall_ms,
            max_output_bytes=settings.compute_max_output_bytes,
        )

    def public(self) -> dict[str, int]:
        return {
            "cpu_ms": self.cpu_ms,
            "memory_mb": self.memory_mb,
            "wall_ms": self.wall_ms,
            "max_output_bytes": self.max_output_bytes,
            "nofile": self.nofile,
        }


def price_of(settings: Any, capability_id: str) -> float:
    if capability_id == COMPUTE_VERIFY:
        return float(settings.compute_verify_price_usd)
    return float(settings.compute_run_price_usd)


def hub_key_matches(presented: str, keys: tuple[str, ...]) -> bool:
    """Constant-time against every configured key — no early exit that times which."""
    matched = False
    raw = presented.encode()
    for key in keys:
        matched |= hmac.compare_digest(raw, key.encode())
    return matched


# ---------------------------------------------------------------- requests


def sha256_hex(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def function_sha256(source: str) -> str:
    return sha256_hex(source.encode("utf-8"))


@dataclass(frozen=True)
class ComputeRequest:
    slug: str
    function_sha256: str  # "" when the buyer did not pin one
    payload: dict[str, Any]
    input_sha256: str


def parse_request(body: Any) -> ComputeRequest:
    """``{"slug", "function_sha256"?, "input"}`` — refused (400) before anything runs."""
    if not isinstance(body, dict):
        raise ComputeRefused(400, "compute input must be an object")
    unknown = set(body) - {"slug", "function_sha256", "input"}
    if unknown:
        raise ComputeRefused(400, f"unknown compute field(s): {', '.join(sorted(unknown))}")
    try:
        slug = validate_slug(str(body.get("slug") or ""))
    except ValueError as exc:
        raise ComputeRefused(400, f"slug: {exc}") from None
    pinned = str(body.get("function_sha256") or "").strip().lower()
    if pinned and not _SHA256.match(pinned):
        raise ComputeRefused(400, "function_sha256 must be 64 lowercase hex characters")
    payload = body.get("input")
    if not isinstance(payload, dict):
        raise ComputeRefused(400, "input must be a JSON object: it is what handle(payload) receives")
    try:
        digest = sha256_hex(canonical_bytes(payload))
    except (TypeError, ValueError, RecursionError):
        # NaN/Infinity or a lone surrogate: valid to Python's parser, not JSON, and
        # not something two machines would hash the same way.
        raise ComputeRefused(400, "input is not canonical JSON (NaN, Infinity or a lone surrogate)") from None
    return ComputeRequest(slug=slug, function_sha256=pinned, payload=payload, input_sha256=digest)


@dataclass(frozen=True)
class ComputeFunction:
    slug: str
    capability_id: str
    source: str
    function_sha256: str
    refused: str  # "" when the deterministic admission passes

    def public(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "capability_id": self.capability_id,
            "function_sha256": self.function_sha256,
            "admissible": not self.refused,
            "refused": self.refused,
        }


def _function_from(row: Any, source: str) -> ComputeFunction:
    refused = ""
    try:
        admit_handler(source, deterministic=True)
    except HandlerDenied as exc:
        refused = str(exc)
    return ComputeFunction(
        slug=row.slug,
        capability_id=str(row.capability.get("capability_id") or ""),
        source=source,
        function_sha256=function_sha256(source),
        refused=refused,
    )


def published_functions(settings: Any, store: Any, stub: Any) -> list[ComputeFunction]:
    """Every published slug that is a running template tenant with a handler.

    Running, because stopping a tenant is how an operator withdraws it: the
    handler file stays on disk after a stop, and serving it anyway would keep
    selling a function the public roster no longer lists.
    """
    rows = {
        row.slug: row
        for row in store.list_running()
        if row.source_kind == "template"
    }
    functions = []
    for slug in settings.compute_functions:
        row = rows.get(slug)
        if row is None:
            continue
        source = stub.stored_handler(slug)
        if source.strip():
            functions.append(_function_from(row, source))
    return functions


def find_function(settings: Any, store: Any, stub: Any, slug: str) -> ComputeFunction | None:
    if slug not in settings.compute_functions:
        return None
    row = store.get(slug)
    if row is None or row.status != "running" or row.source_kind != "template":
        return None
    # Read once. These exact bytes are hashed, admitted and handed to the
    # replicas, so a redeploy racing this call cannot make the receipt name one
    # function while the replicas ran another.
    source = stub.stored_handler(slug)
    if not source.strip():
        return None
    return _function_from(row, source)


# ---------------------------------------------------------------- replicas


def rlimit_preexec(limits: Limits):
    """preexec_fn for a replica: the ceilings, set in the child before exec.

    RLIMIT_CPU is whole seconds, so the soft limit rounds the budget UP (SIGXCPU)
    and the hard limit is one second past it (SIGKILL); the millisecond budget is
    then enforced on the kernel's measurement. RLIMIT_FSIZE=0 because a replica
    has no reason to write a byte to disk (pipes are not files). A limit the
    platform refuses — macOS will not set RLIMIT_AS — is skipped, never fatal:
    the peak-RSS check still applies, and the receipt says which one held.
    """
    if resource is None:  # pragma: no cover - platform without rlimits
        return None
    cpu_s = max(1, math.ceil(limits.cpu_ms / 1000))
    memory = limits.memory_mb * 1024 * 1024
    table = (
        (resource.RLIMIT_CORE, 0, 0),
        (resource.RLIMIT_CPU, cpu_s, cpu_s + 1),
        (resource.RLIMIT_NOFILE, limits.nofile, limits.nofile),
        (resource.RLIMIT_FSIZE, 0, 0),
        (resource.RLIMIT_AS, memory, memory),
    )

    def _apply() -> None:
        for which, soft, hard in table:
            try:
                resource.setrlimit(which, (soft, hard))
            except (ValueError, OSError):
                pass

    return _apply


def worker_env(workdir: str, hash_seed: int) -> dict[str, str]:
    """Built from nothing. Not one variable of the control plane's is inherited:
    no token, no key path, no RPC URL, no PYTHONPATH of the operator's."""
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": workdir,
        "TMPDIR": workdir,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        "PYTHONHASHSEED": str(hash_seed),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def worker_argv() -> list[str]:
    # -S: no site-packages and no .pth file runs at start-up. -P: nothing (not
    # even the scratch directory) is prepended to sys.path. -B: no bytecode.
    return [sys.executable, "-S", "-P", "-B", "-c", _BOOTSTRAP]


def _rss_kb(raw: int) -> int:
    # ru_maxrss is KiB on Linux and bytes on macOS.
    return int(raw) // 1024 if sys.platform == "darwin" else int(raw)


def _kill(pid: int) -> None:
    # Only ever called while the child is unreaped, so the pid is still ours.
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


@dataclass
class Replica:
    index: int
    hash_seed: int
    outcome: str = PLATFORM_FAILURE
    output: Any = None
    output_sha256: str = ""
    error: str = ""
    cpu_ms: int = 0
    max_rss_kb: int = 0
    wall_ms: int = 0
    exit_code: int | None = None
    rlimit_as: int = -1
    stderr: str = field(default="", repr=False)

    def public(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "hash_seed": self.hash_seed,
            "outcome": self.outcome,
            "output_sha256": self.output_sha256,
            "error": self.error,
            "cpu_ms": self.cpu_ms,
            "max_rss_kb": self.max_rss_kb,
            "wall_ms": self.wall_ms,
        }


def _drive(proc: subprocess.Popen, job: bytes, limits: Limits, deadline: float):
    """Feed stdin, collect stdout/stderr, enforce the deadline, reap with rusage.

    Reaping is ours, not Popen's: rusage is only available from the wait that
    reaps the child, and a kill is only ever sent while ``wait4(WNOHANG)`` still
    says the child is unreaped — so its pid cannot have been handed to anyone
    else. Returns (stdout, stderr, killed, status, rusage).
    """
    out = bytearray()
    err = bytearray()
    killed = ""
    reaped = None
    drain_until = 0.0
    pending = memoryview(job)
    sel = selectors.DefaultSelector()
    os.set_blocking(proc.stdin.fileno(), False)
    sel.register(proc.stdin, selectors.EVENT_WRITE)
    sel.register(proc.stdout, selectors.EVENT_READ)
    sel.register(proc.stderr, selectors.EVENT_READ)
    try:
        while True:
            now = time.monotonic()
            if reaped is None:
                pid, status, usage = os.wait4(proc.pid, os.WNOHANG)
                if pid:
                    reaped = (status, usage)
                    drain_until = now + DRAIN_S
                elif now >= deadline or len(out) > limits.max_output_bytes:
                    killed = "wall" if now >= deadline else "output"
                    _kill(proc.pid)
                    _, status, usage = os.wait4(proc.pid, 0)
                    reaped = (status, usage)
                    break
            if reaped is not None and (not sel.get_map() or now >= drain_until):
                break
            if not sel.get_map():
                time.sleep(0.002)  # both pipes closed; the child is exiting
                continue
            if reaped is None:
                timeout = max(0.0, min(0.05, deadline - now))
            else:
                timeout = max(0.0, drain_until - now)
            for key, _mask in sel.select(timeout):
                if key.fileobj is proc.stdin:
                    try:
                        written = os.write(key.fd, pending[:65536])
                    except BlockingIOError:
                        continue
                    except OSError:  # the child closed stdin or died early
                        written = len(pending)
                    pending = pending[written:]
                    if not pending:
                        sel.unregister(proc.stdin)
                        proc.stdin.close()
                    continue
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    sel.unregister(key.fileobj)
                    continue
                if key.fileobj is proc.stdout:
                    out += chunk
                elif len(err) < STDERR_CAP:
                    err += chunk[: STDERR_CAP - len(err)]
    except BaseException:
        if reaped is None:
            _kill(proc.pid)
            os.wait4(proc.pid, 0)
        raise
    finally:
        sel.close()
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                stream.close()
            except OSError:
                pass
    status, usage = reaped
    # Popen must not try to reap a child that is already gone.
    proc.returncode = os.waitstatus_to_exitcode(status)
    return bytes(out), bytes(err), killed, status, usage


def _classify(replica: Replica, stdout: bytes, killed: str, limits: Limits) -> None:
    code = replica.exit_code
    cpu_signals = {-signal.SIGKILL}
    if hasattr(signal, "SIGXCPU"):
        cpu_signals.add(-signal.SIGXCPU)
    if killed == "wall":
        replica.outcome = "wall_time_exceeded"
        replica.error = f"ran past the {limits.wall_ms} ms wall-clock limit"
        return
    if killed == "output":
        replica.outcome = "output_too_large"
        replica.error = f"wrote more than {limits.max_output_bytes} bytes"
        return
    cpu_soft_ms = max(1, math.ceil(limits.cpu_ms / 1000)) * 1000
    if code in cpu_signals and replica.cpu_ms >= cpu_soft_ms - 50:
        replica.outcome = "cpu_limit_exceeded"
        replica.error = f"killed at the {limits.cpu_ms} ms CPU limit"
        return
    if code != 0:
        what = "refused its job" if code == EXIT_BAD_JOB else f"exited with {code}"
        replica.error = f"replica {what}: {replica.stderr[-300:]}".strip()
        return
    try:
        envelope = json.loads(stdout)
    except ValueError:
        replica.error = "replica wrote no envelope"
        return
    if not isinstance(envelope, dict):
        replica.error = "replica wrote no envelope"
        return
    rlimit_as = envelope.get("rlimit_as")
    replica.rlimit_as = rlimit_as if isinstance(rlimit_as, int) else -1
    if envelope.get("ok") is True and isinstance(envelope.get("output"), dict):
        replica.output = envelope["output"]
        # Re-derived here from the parsed value, not taken from the replica's
        # bytes: the digest is this process's statement, not the function's.
        replica.output_sha256 = sha256_hex(canonical_bytes(replica.output))
        replica.outcome = OK
    elif envelope.get("ok") is False and envelope.get("refuse_reason") in WORKER_REASONS:
        replica.outcome = str(envelope["refuse_reason"])
        replica.error = str(envelope.get("error") or "")[:300]
    else:
        replica.error = "replica wrote an envelope this hearth does not recognise"
        return
    # The kernel's numbers decide, whatever the replica said: RLIMIT_CPU fires on
    # whole seconds and RLIMIT_AS may not be enforced at all.
    if replica.cpu_ms > limits.cpu_ms:
        replica.outcome = "cpu_limit_exceeded"
        replica.error = f"used {replica.cpu_ms} ms CPU; the limit is {limits.cpu_ms} ms"
    elif replica.max_rss_kb > limits.memory_mb * 1024:
        replica.outcome = "memory_limit_exceeded"
        replica.error = (
            f"peak RSS {replica.max_rss_kb} KiB; the limit is {limits.memory_mb * 1024} KiB"
        )


def run_replica(job: bytes, limits: Limits, *, index: int, hash_seed: int) -> Replica:
    """One fresh replica in its own empty directory, measured from outside."""
    replica = Replica(index=index, hash_seed=hash_seed)
    try:
        workdir = tempfile.mkdtemp(prefix="hestia-compute-")
    except OSError as exc:
        replica.error = f"no scratch directory: {exc}"
        return replica
    try:
        os.chmod(workdir, 0o700)
        started = time.monotonic()
        try:
            proc = subprocess.Popen(  # noqa: S603 — fixed argv: this interpreter + our module
                worker_argv(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=workdir,
                env=worker_env(workdir, hash_seed),
                preexec_fn=rlimit_preexec(limits),
                close_fds=True,
            )
        except OSError as exc:
            replica.error = f"replica did not start: {exc}"
            return replica
        stdout, stderr, killed, status, usage = _drive(
            proc, job, limits, started + limits.wall_ms / 1000
        )
        if not killed and len(stdout) > limits.max_output_bytes:
            # It wrote past the ceiling and exited before the next check could
            # kill it. The bytes are here, but they are over budget all the same.
            killed = "output"
        replica.wall_ms = int((time.monotonic() - started) * 1000)
        replica.cpu_ms = int(round((usage.ru_utime + usage.ru_stime) * 1000))
        replica.max_rss_kb = _rss_kb(usage.ru_maxrss)
        replica.exit_code = os.waitstatus_to_exitcode(status)
        replica.stderr = stderr.decode("utf-8", errors="replace")
        _classify(replica, stdout, killed, limits)
        return replica
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def distinct_seeds(count: int) -> list[int]:
    """PYTHONHASHSEED values, all different and none 0 (0 turns hashing randomisation off)."""
    seeds: list[int] = []
    while len(seeds) < count:
        seed = secrets.randbelow(2**32 - 1) + 1
        if seed not in seeds:
            seeds.append(seed)
    return seeds


def run_replicas(
    source: str, payload: dict[str, Any], limits: Limits, count: int
) -> list[Replica]:
    """`count` replicas of one job, in parallel, each with its own hash seed.

    Every replica after the first starts 5-30 ms later, at random. The admission
    already refuses every clock it can see; the stagger is for one it cannot, so
    two replicas are less likely to read the same instant and agree by accident.
    """
    job = canonical_bytes({"source": source, "input": payload})
    seeds = distinct_seeds(count)
    results: list[Replica | None] = [None] * count

    def _one(index: int) -> None:
        if index:
            time.sleep((5 + secrets.randbelow(26)) / 1000)
        try:
            results[index] = run_replica(job, limits, index=index, hash_seed=seeds[index])
        except Exception as exc:  # noqa: BLE001 — one replica must not lose the other's result
            results[index] = Replica(index=index, hash_seed=seeds[index], error=str(exc)[:300])

    if count == 1:
        _one(0)
    else:
        threads = [threading.Thread(target=_one, args=(i,), daemon=True) for i in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    return [r for r in results if r is not None]


@dataclass(frozen=True)
class Judgement:
    outcome: str
    detail: str
    output: Any = None
    output_sha256: str = ""


def judge(replicas: list[Replica]) -> Judgement:
    """What the replicas add up to. Order matters:

    1. any replica the hearth failed → a platform failure (payment given back);
    2. any replica over budget → that limit (the input consumed its budget);
    3. replicas that answered differently — different bytes, or one answered and
       one raised → ``replicas_disagree``;
    4. replicas that all raised → the function's own error;
    5. otherwise, one output every replica produced byte-for-byte.
    """
    for replica in replicas:
        if replica.outcome == PLATFORM_FAILURE:
            return Judgement(PLATFORM_FAILURE, f"replica {replica.index}: {replica.error}")
    for replica in replicas:
        if replica.outcome in LIMIT_REASONS:
            return Judgement(replica.outcome, f"replica {replica.index}: {replica.error}")
    if len({(r.outcome, r.output_sha256) for r in replicas}) > 1:
        described = "; ".join(
            f"replica {r.index}: {r.outcome}"
            + (f" {r.output_sha256[:16]}…" if r.output_sha256 else "")
            for r in replicas
        )
        return Judgement(DISAGREE, f"the replicas did not produce the same output ({described})")
    first = replicas[0]
    if first.outcome != OK:
        return Judgement(first.outcome, first.error)
    return Judgement(OK, "", first.output, first.output_sha256)


class ReplicaSlots:
    """How many replica processes may run at once, host-wide.

    Taken atomically per job (a verify job needs two), so two verify jobs cannot
    each hold one slot and wait forever for the other's.
    """

    def __init__(self, capacity: int) -> None:
        self._free = capacity
        self._cond = threading.Condition()

    def acquire(self, count: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._free < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            self._free -= count
            return True

    def release(self, count: int) -> None:
        with self._cond:
            self._free += count
            self._cond.notify_all()


# ---------------------------------------------------------------- receipts


def runtime_facts() -> dict[str, str]:
    return {
        "runtime": RUNTIME,
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        # libm and CPython builds differ across machines; a cross-hearth
        # comparison that disagrees should be read against these.
        "platform": f"{sys.platform}-{platform.machine()}",
        "hestia_version": __version__,
    }


def build_receipt(
    *,
    capability_id: str,
    hearth: str,
    function: ComputeFunction,
    request: ComputeRequest,
    judgement: Judgement,
    replicas: list[Replica],
    limits: Limits,
) -> dict[str, Any]:
    """The unsigned compute receipt. Integers and strings only, so any JSON
    canonicalisation (the hearth's own, RFC 8785 for an AWR verdict) agrees on it."""
    capped = all(r.rlimit_as == limits.memory_mb * 1024 * 1024 for r in replicas)
    return {
        "kind": RECEIPT_KIND,
        "id": f"urn:uuid:{uuid.uuid4()}",
        "capability_id": capability_id,
        "method": METHODS[capability_id],
        "hearth": hearth,
        "slug": function.slug,
        "function_sha256": function.function_sha256,
        "input_sha256": request.input_sha256,
        "output_sha256": judgement.output_sha256,
        "canonicalization": CANONICALIZATION,
        "replicas": len(replicas),
        "hash_seeds": [r.hash_seed for r in replicas],
        "cpu_ms": [r.cpu_ms for r in replicas],
        "max_rss_kb": [r.max_rss_kb for r in replicas],
        "wall_ms": [r.wall_ms for r in replicas],
        # `memory_capped_by_kernel` false means the memory limit held only as a
        # measurement after the fact (macOS refuses RLIMIT_AS).
        "limits": {**limits.public(), "memory_capped_by_kernel": capped},
        **runtime_facts(),
        # One operator ran every replica and holds the key that signs this. Read
        # it as "consistent on this host", never as "verified by a third party".
        "same_operator": True,
        "issued_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def refusal_body(
    *,
    capability_id: str,
    function: ComputeFunction,
    request: ComputeRequest,
    judgement: Judgement,
    replicas: list[Replica],
    limits: Limits,
) -> dict[str, Any]:
    """An honest `ok: false`: what ran, what each replica measured — unsigned.

    A hub reads `ok: false` as a refusal and does not bill its credits rail for
    it; the `refuse_reason` is what tells the caller what happened."""
    return {
        "ok": False,
        "refuse_reason": judgement.outcome,
        "error": judgement.outcome,
        "detail": judgement.detail,
        "capability_id": capability_id,
        "slug": function.slug,
        "function_sha256": function.function_sha256,
        "input_sha256": request.input_sha256,
        "replicas": [r.public() for r in replicas],
        "limits": limits.public(),
        "signed": False,
    }
