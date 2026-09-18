"""Sealed tenant process. Serves /health and /invoke from OUR code, not theirs.

A template handler may compute the result payload, but only after AST admission.
The HTTP server, signing, and filesystem stay inside this module.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

# This file is executed as `python -m hestia.tenant_stub`. Imports stay in-tree
# (scan, signing) plus cryptography already installed next to Hestia.


_DROP_EXACT = frozenset(
    {
        "HESTIA_DEPLOY_TOKEN",
        "HESTIA_HUB_URL",
        "HESTIA_THEMIS_URL",
        "HESTIA_AUTO_ANNOUNCE",
        "HESTIA_DATA_DIR",
        "HESTIA_ALLOW_IMAGE_DIGESTS",
    }
)
_KEEP_PREFIXES = ("HESTIA_TENANT_",)
_KEEP_EXACT = frozenset({"HESTIA_CAPABILITY_FILE", "HESTIA_HANDLER_FILE"})


def scrub_operator_env(environ: dict[str, str] | None = None) -> None:
    """Drop operator secrets if a parent leaked them into this process."""
    env = os.environ if environ is None else environ
    for key in list(env):
        if key in _KEEP_EXACT or key.startswith(_KEEP_PREFIXES):
            continue
        if key in _DROP_EXACT or key.startswith("AIMARKET_"):
            del env[key]
            continue
        upper = key.upper()
        if upper.endswith(("_TOKEN", "_SECRET", "_PASSWORD", "_API_KEY")):
            del env[key]


DEFAULT_CALL_TIMEOUT_S = 10


class CallTimeout(Exception):
    """A handler ran past its per-call budget."""


def call_timeout_s() -> int:
    """Seconds one invoke may burn. 0 disables the guard (not advised)."""
    try:
        value = int(os.environ.get("HESTIA_TENANT_CALL_TIMEOUT_S", str(DEFAULT_CALL_TIMEOUT_S)))
    except ValueError:
        return DEFAULT_CALL_TIMEOUT_S
    return max(0, value)


def deadline_armable() -> bool:
    """Whether the per-call alarm can actually be set here.

    `signal.signal` raises outside the main thread, and that exception used to
    be swallowed by the generic handler below — turning EVERY invoke into a 400
    the moment the server ran on a worker thread. The guard has to be explicit
    rather than discovered.
    """
    return (
        hasattr(signal, "SIGALRM")
        and threading.current_thread() is threading.main_thread()
    )


def run_with_deadline(fn, seconds: int):
    """Run `fn` under a wall-clock budget, aborting it if it overruns.

    SIGALRM is the mechanism, which is why this server is single-threaded: a
    signal is delivered to the main thread, so a handler running anywhere else
    could not be interrupted. CPython checks for pending signals inside long
    regex matching too, which is the case that matters — a caller-supplied
    pattern like `(a+)+$` against 41 characters otherwise pegs a core forever.

    Not a sandbox, and not a substitute for cgroups: it bounds ONE call. A
    handler that leaks memory or forks is still the operator's problem, which is
    what HESTIA_RUNTIME=docker is for.
    """
    if seconds <= 0 or not deadline_armable():
        return fn()

    def _fire(_signum, _frame):
        raise CallTimeout(f"handler exceeded its {seconds}s budget")

    previous = signal.signal(signal.SIGALRM, _fire)
    signal.alarm(seconds)
    try:
        return fn()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def main() -> None:
    from hestia.limits import resolve_max_body_bytes
    from hestia.signing import ProviderSigner

    scrub_operator_env()
    slug = os.environ["HESTIA_TENANT_SLUG"]
    cap_path = Path(os.environ["HESTIA_CAPABILITY_FILE"])
    key_path = Path(os.environ["HESTIA_TENANT_KEY"])
    handler_path = Path(os.environ.get("HESTIA_HANDLER_FILE", ""))
    bind_host = os.environ.get("HESTIA_TENANT_BIND", "127.0.0.1")
    port = int(os.environ.get("HESTIA_TENANT_PORT", "0"))
    # Same ceiling as Settings.max_body_bytes / BodyLimitMiddleware.
    max_body_bytes = resolve_max_body_bytes(
        os.environ.get("HESTIA_TENANT_MAX_BODY_BYTES")
        or os.environ.get("HESTIA_MAX_BODY_BYTES")
    )
    capability = json.loads(cap_path.read_text(encoding="utf-8"))
    signer = ProviderSigner(key_path)
    handler_fn = _load_handler(handler_path) if handler_path.name else None
    budget = call_timeout_s()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: object) -> None:  # noqa: A003
            # No per-request access log: a sealed tenant's stderr has no reader,
            # and a line per request filled the 64 KiB pipe buffer and blocked the
            # tenant on its next write. Errors still reach stderr as tracebacks.
            return

        def _send(self, code: int, payload: dict[str, Any]) -> None:
            raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            if self.path.split("?", 1)[0] == "/health":
                self._send(200, {"ok": True, "slug": slug, "status": "running"})
                return
            self._send(404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path != "/invoke":
                self._send(404, {"ok": False, "error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or "0")
            if length > max_body_bytes:
                self._send(413, {"ok": False, "error": "body too large"})
                return
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode())
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send(400, {"ok": False, "error": "invalid json"})
                return
            if not isinstance(body, dict):
                self._send(400, {"ok": False, "error": "body must be an object"})
                return
            try:
                result = run_with_deadline(
                    lambda: _invoke(
                        capability, body, handler_fn, handler_dir=handler_path.parent
                    ),
                    budget,
                )
            except CallTimeout as exc:
                # 504, not 400: the input was not refused, the work was cut off.
                self._send(504, {"ok": False, "error": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001 — tenant must fail closed, not crash
                self._send(400, {"ok": False, "error": str(exc)[:300]})
                return
            signature = signer.sign_result(
                result,
                capability_id=capability["capability_id"],
                product_id=capability["product_id"],
                input_payload=body,
            )
            self._send(
                200,
                {
                    "ok": True,
                    "result": result,
                    "provider_pubkey": signer.public_key_b64,
                    "signature": signature,
                },
            )

    # Single-threaded on purpose: the per-call deadline above is delivered by
    # SIGALRM, and a signal only interrupts the main thread. Concurrency across
    # agents is unaffected — every agent is its own process.
    if budget > 0 and not deadline_armable():
        # Loud, because an unguarded tenant is exactly what this exists to stop.
        sys.stderr.write(
            f"tenant[{slug}] WARNING: per-call deadline cannot be armed off the "
            "main thread; calls run unbounded\n"
        )
    httpd = HTTPServer((bind_host, port), Handler)
    bound = httpd.server_address[1]
    sys.stdout.write(json.dumps({"listen_port": bound}) + "\n")
    sys.stdout.flush()
    httpd.serve_forever()


def _load_handler(path: Path):
    if not path.is_file():
        return None
    from hestia.handler_guard import guarded
    from hestia.scan import admit_handler

    source = path.read_text(encoding="utf-8")
    admit_handler(source)
    # Compile from the already-read source and exec in a fresh namespace rather
    # than runpy.run_path, which would re-open the file — the containment guard
    # (armed below) refuses opens outside the tenant dir, and re-reading the
    # handler is an open we do not need. Top-level handler code runs contained:
    # its imports are fine, but an import-time attempt to read a key or spawn a
    # process fails closed here, before the tenant ever serves a request.
    code = compile(source, str(path), "exec")
    ns: dict[str, Any] = {}
    with guarded(path.parent):
        exec(code, ns)  # noqa: S102 — source is AST-admitted AND runs contained
    fn = ns.get("handle")
    if not callable(fn):
        raise RuntimeError("handler.py must define handle(payload)")
    return fn


def _invoke(
    capability: dict[str, Any],
    body: dict[str, Any],
    handler_fn,
    handler_dir: Path | None = None,
) -> dict[str, Any]:
    if handler_fn is not None:
        from hestia.handler_guard import guarded

        # Every call runs contained: even if a gadget slipped the scanner, the
        # handler cannot read a sibling's key, shell out, or open a socket.
        if handler_dir is not None:
            with guarded(handler_dir):
                out = handler_fn(body)
        else:
            out = handler_fn(body)
        if not isinstance(out, dict):
            raise ValueError("handler must return an object")
        return out
    return {
        "echo": body,
        "capability_id": capability["capability_id"],
        "hearth": "hestia",
        "note": "sealed stub — no custom handler",
    }


if __name__ == "__main__":
    main()
