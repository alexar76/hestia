"""One compute replica: ``python -m hestia.compute_worker``.

Reads one job from stdin — ``{"source": <handler.py text>, "input": <object>}`` —
runs ``handle(input)`` exactly once, writes one canonical JSON envelope to stdout,
and exits. No HTTP, no key, no ledger. The parent (``hestia.compute``) holds the
signing key, measures this process from the outside (``os.wait4``) and decides
what the answer is worth; nothing this process says about its own resource use
is believed.

What this process is given, and why:

* a fresh, empty 0700 working directory, and nothing else on disk it may open —
  the containment guard allows the interpreter's library roots plus that
  directory, so ``provider.key`` and every ``tenant.key`` are unreadable even
  under a scanner miss (a tenant process can read its OWN key; this one has none);
* rlimits the parent set before exec (CPU, address space, files, no core);
* an environment built from nothing: ``PYTHONHASHSEED`` differs per replica on
  purpose so hash-order dependence shows up as disagreement, and ``TZ`` is UTC.

The source is admitted again here, under the deterministic rules, even though the
parent already did it: this module must never run source it has not checked
itself, however it was started.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from hestia.handler_guard import HandlerContainmentError, guarded
from hestia.scan import ALLOWED_IMPORTS, HandlerDenied, admit_handler

try:  # POSIX only; absent on Windows.
    import resource
except ImportError:  # pragma: no cover - platform without rlimits
    resource = None  # type: ignore[assignment]

# The job could not be read, or this module failed before running the function.
# Distinct from 0 (an envelope was written) so the parent can tell a platform
# fault — which gives the buyer their payment back — from the function's own.
EXIT_BAD_JOB = 70

# Every refuse_reason this module can put in an envelope. The parent treats
# anything else as a replica that did not answer.
WORKER_REASONS = frozenset(
    {
        "function_not_admissible",
        "function_error",
        "memory_limit_exceeded",
        "output_not_json",
        "containment_violation",
    }
)

CANONICALIZATION = "json-sorted-compact-utf8@v1"


def canonical_bytes(value: Any) -> bytes:
    """The bytes a compute digest is taken over.

    Sorted keys, no whitespace, UTF-8, and no NaN/Infinity (they are not JSON, and
    a parser elsewhere would read them differently or not at all). The same
    encoding the hearth already hashes a tenant call's input with, so an
    ``input_sha256`` means the same thing on both paths. It is not RFC 8785:
    float formatting and key order follow CPython, which is why a receipt names
    its ``python_version``.
    """
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _refusal(reason: str, error: str, extra: dict[str, Any]) -> bytes:
    return canonical_bytes({"ok": False, "refuse_reason": reason, "error": error[:300], **extra})


def _describe(exc: BaseException) -> str:
    # A handler-defined exception can carry a __str__ of its own that raises; the
    # envelope must still be written.
    try:
        return f"{type(exc).__name__}: {exc}"
    except Exception:  # noqa: BLE001
        return type(exc).__name__


def execute(
    source: str, payload: dict[str, Any], workdir: str, extra: dict[str, Any] | None = None
) -> bytes:
    """Run ``handle(payload)`` once, contained; return the envelope bytes.

    Everything that touches an object the function produced — calling it,
    formatting its exception, serialising its output — happens inside the guard,
    because each of those can run the function's own code (a method on a class it
    defined). Only plain bytes leave the block.
    """
    extra = dict(extra or {})
    try:
        admit_handler(source, deterministic=True)
    except HandlerDenied as exc:
        return _refusal("function_not_admissible", str(exc), extra)
    code = compile(source, "handler.py", "exec")
    namespace: dict[str, Any] = {}
    with guarded(workdir):
        try:
            exec(code, namespace)  # noqa: S102 — AST-admitted AND contained
            handle = namespace.get("handle")
            if not callable(handle):
                return _refusal("function_error", "handler.py must define handle(payload)", extra)
            out = handle(payload)
            if not isinstance(out, dict):
                return _refusal("function_error", "handle must return an object", extra)
            try:
                return canonical_bytes({"ok": True, "output": out, **extra})
            except (TypeError, ValueError, RecursionError) as exc:
                return _refusal("output_not_json", _describe(exc), extra)
        except MemoryError:
            return _refusal("memory_limit_exceeded", "the function ran out of memory", extra)
        except HandlerContainmentError as exc:
            return _refusal("containment_violation", str(exc), extra)
        except BaseException as exc:  # noqa: BLE001 — SystemExit too: the envelope must be written
            return _refusal("function_error", _describe(exc), extra)


def _preload() -> None:
    """Import every module a function may import, before containment is armed.

    A first import under the guard walks sys.path and lists directories, and a
    directory outside the library roots (the package root this module was loaded
    from) is refused — so whether a function's `import statistics` worked could
    depend on a cache's state. Loaded here, those imports are dictionary lookups.
    """
    import importlib

    for name in sorted(ALLOWED_IMPORTS):
        importlib.import_module(name)


def _rlimit_as() -> int:
    """The address-space limit actually in force here, or -1 for none.

    Reported, not assumed: macOS refuses RLIMIT_AS, and the receipt says whether
    memory was capped by the kernel or only measured afterwards."""
    if resource is None:  # pragma: no cover
        return -1
    soft = resource.getrlimit(resource.RLIMIT_AS)[0]
    return -1 if soft == resource.RLIM_INFINITY else int(soft)


def _claim_stdout() -> int:
    """Keep stdout for the envelope alone; point fd 1 at nothing for everyone else.

    `print` is an admitted builtin. Without this, one print in a function puts
    bytes in front of the envelope, the parent reads "no envelope", and the
    function's own chatter is booked as the hearth's failure — which gives the
    buyer's payment back, so the same transaction could buy the same work again.
    """
    channel = os.dup(1)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.close(devnull)
    return channel


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def main() -> int:
    workdir = os.getcwd()
    try:
        job = json.loads(sys.stdin.buffer.read())
        source = job["source"]
        payload = job["input"]
        if not isinstance(source, str) or not isinstance(payload, dict):
            raise ValueError("a job carries a source string and an input object")
        _preload()
        channel = _claim_stdout()
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"compute_worker: {_describe(exc)}\n")
        return EXIT_BAD_JOB
    extra = {"rlimit_as": _rlimit_as()}
    # Armed for the rest of this process and never disarmed. The function can
    # leave work behind — a finalizer, a cycle the collector reaches later — and
    # that runs contained too. `execute` nests its own guard inside this one.
    guarded(workdir).__enter__()
    _write_all(channel, execute(source, payload, workdir, extra))
    # No interpreter shutdown: nothing the function left runs after this line.
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
