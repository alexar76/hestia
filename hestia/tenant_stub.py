"""Sealed tenant process. Serves /health and /invoke from OUR code, not theirs.

A template handler may compute the result payload, but only after AST admission.
The HTTP server, signing, and filesystem stay inside this module.
"""

from __future__ import annotations

import json
import os
import runpy
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


def main() -> None:
    from hestia.signing import ProviderSigner

    scrub_operator_env()
    slug = os.environ["HESTIA_TENANT_SLUG"]
    cap_path = Path(os.environ["HESTIA_CAPABILITY_FILE"])
    key_path = Path(os.environ["HESTIA_TENANT_KEY"])
    handler_path = Path(os.environ.get("HESTIA_HANDLER_FILE", ""))
    bind_host = os.environ.get("HESTIA_TENANT_BIND", "127.0.0.1")
    port = int(os.environ.get("HESTIA_TENANT_PORT", "0"))
    capability = json.loads(cap_path.read_text(encoding="utf-8"))
    signer = ProviderSigner(key_path)
    handler_fn = _load_handler(handler_path) if handler_path.name else None

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: object) -> None:  # noqa: A003
            sys.stderr.write(f"tenant[{slug}] " + (fmt % args) + "\n")

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
            if length > 256 * 1024:
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
                result = _invoke(capability, body, handler_fn)
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

    httpd = ThreadingHTTPServer((bind_host, port), Handler)
    bound = httpd.server_address[1]
    sys.stdout.write(json.dumps({"listen_port": bound}) + "\n")
    sys.stdout.flush()
    httpd.serve_forever()


def _load_handler(path: Path):
    if not path.is_file():
        return None
    from hestia.scan import admit_handler

    source = path.read_text(encoding="utf-8")
    admit_handler(source)
    ns = runpy.run_path(str(path), run_name="tenant_handler")
    fn = ns.get("handle")
    if not callable(fn):
        raise RuntimeError("handler.py must define handle(payload)")
    return fn


def _invoke(capability: dict[str, Any], body: dict[str, Any], handler_fn) -> dict[str, Any]:
    if handler_fn is not None:
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
