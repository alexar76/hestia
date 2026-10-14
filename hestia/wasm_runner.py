"""The sandbox runner: executes template handlers in WebAssembly for the hearth, nothing else.

    python -m hestia.wasm_runner

It runs in its own container with no network, no secrets and a read-only filesystem, and
listens only on a Unix socket in a volume it shares with the hearth (HESTIA_RUNNER_SOCKET).
The hearth sends a handler's source and one payload; the runner answers with the result
or why there is none. It holds no key and signs nothing: the hearth signs outside.

    POST /run     {"source": str, "payload": any, "timeout_s"?: number}
                  -> 200 {"kind": "ok", "result": {...}, "wall_ms": n}
                  -> 200 {"kind": "error" | "timeout" | "memory" | "output" | "crash", ...}
                  -> 503 busy (every worker slot taken and the queue full)
    GET  /health  -> 200 {"ok": true, ...}

Concurrency is bounded (HESTIA_RUNNER_WORKERS, default: CPU count), and a request waits at
most HESTIA_RUNNER_QUEUE_S for a slot before it is refused, so a flood of calls degrades to
503s instead of an unbounded pile of interpreters.
"""

from __future__ import annotations

import json
import os
import socket
import socketserver
import sys
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from hestia.sandbox import Sandbox

MAX_REQUEST_BYTES = 1024 * 1024
DEFAULT_SOCKET = "/run/hestia-runner/runner.sock"


class _UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def make_handler(sandbox: Sandbox, slots: threading.BoundedSemaphore, queue_s: float):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def address_string(self) -> str:  # a Unix peer has no (host, port)
            return "runner"

        def log_message(self, fmt: str, *args: object) -> None:  # noqa: A003
            return

        def _send(self, code: int, body: dict) -> None:
            raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self._send(200, {"ok": True, "service": "hestia-runner",
                                 "python": "wasi", "memory_mb": sandbox.memory_mb,
                                 "timeout_s": sandbox.timeout_s})
                return
            self._send(404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/run":
                self._send(404, {"ok": False, "error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0 or length > MAX_REQUEST_BYTES:
                self._send(413, {"ok": False, "error": "request too large"})
                return
            try:
                request = json.loads(self.rfile.read(length))
                source = request["source"]
                payload = request["payload"]
                timeout_s = float(request.get("timeout_s") or sandbox.timeout_s)
            except (ValueError, KeyError, TypeError):
                self._send(400, {"ok": False, "error": "bad request"})
                return
            if not isinstance(source, str):
                self._send(400, {"ok": False, "error": "source must be a string"})
                return
            if not slots.acquire(timeout=queue_s):
                self._send(503, {"ok": False, "error": "runner busy"})
                return
            try:
                outcome = sandbox.run(source, payload, timeout_s=timeout_s)
            finally:
                slots.release()
            self._send(200, outcome.to_json())

    return Handler


def serve(socket_path: str | None = None) -> None:
    path = Path(socket_path or os.environ.get("HESTIA_RUNNER_SOCKET", DEFAULT_SOCKET))
    workers = int(os.environ.get("HESTIA_RUNNER_WORKERS") or (os.cpu_count() or 2))
    queue_s = float(os.environ.get("HESTIA_RUNNER_QUEUE_S", "5"))
    sandbox = Sandbox(
        memory_mb=int(os.environ.get("HESTIA_RUNNER_MEMORY_MB", "256")),
        timeout_s=float(os.environ.get("HESTIA_TENANT_CALL_TIMEOUT_S", "10")),
        max_output=int(os.environ.get("HESTIA_RUNNER_MAX_OUTPUT", str(4 * 1024 * 1024))),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    server = _UnixHTTPServer(str(path), make_handler(sandbox, threading.BoundedSemaphore(workers),
                                                     queue_s))
    os.chmod(path, 0o660)
    sys.stdout.write(json.dumps({"runner": str(path), "workers": workers}) + "\n")
    sys.stdout.flush()
    try:
        server.serve_forever()
    finally:
        sandbox.close()
        server.server_close()


def probe(socket_path: str, timeout: float = 3.0) -> bool:
    """True when a runner answers /health on `socket_path` (used by the hearth and Docker)."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(socket_path)
            sock.sendall(b"GET /health HTTP/1.1\r\nHost: runner\r\nConnection: close\r\n\r\n")
            return sock.recv(64).startswith(b"HTTP/1.1 200")
    except OSError:
        return False


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--probe":
        raise SystemExit(0 if probe(os.environ.get("HESTIA_RUNNER_SOCKET", DEFAULT_SOCKET)) else 1)
    serve()
