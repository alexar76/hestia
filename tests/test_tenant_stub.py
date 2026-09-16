from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest

from hestia.scan import HandlerDenied
from hestia.tenant_stub import _invoke, _load_handler, main


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_invoke_echo_and_custom_handler() -> None:
    cap = {"capability_id": "demo.echo@v1", "product_id": "demo"}
    assert _invoke(cap, {"a": 1}, None)["echo"] == {"a": 1}

    def handle(payload):
        return {"n": payload["n"] + 1}

    assert _invoke(cap, {"n": 1}, handle) == {"n": 2}

    def bad(_payload):
        return ["not-an-object"]

    with pytest.raises(ValueError, match="object"):
        _invoke(cap, {}, bad)


def test_load_handler(tmp_path: Path) -> None:
    missing = tmp_path / "nope.py"
    assert _load_handler(missing) is None
    path = tmp_path / "handler.py"
    path.write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="handle"):
        _load_handler(path)
    path.write_text("def handle(payload):\n    return payload\n", encoding="utf-8")
    assert callable(_load_handler(path))
    path.write_text("import os\ndef handle(payload):\n    return payload\n", encoding="utf-8")
    with pytest.raises(HandlerDenied):
        _load_handler(path)


def test_tenant_http_server(tmp_path: Path, monkeypatch) -> None:
    cap = tmp_path / "capability.json"
    cap.write_text(
        json.dumps({"capability_id": "demo.echo@v1", "product_id": "demo"}),
        encoding="utf-8",
    )
    handler = tmp_path / "handler.py"
    handler.write_text(
        "def handle(payload):\n    return {'got': payload.get('k')}\n",
        encoding="utf-8",
    )
    port = _free_port()
    monkeypatch.setenv("HESTIA_TENANT_SLUG", "demo")
    monkeypatch.setenv("HESTIA_CAPABILITY_FILE", str(cap))
    monkeypatch.setenv("HESTIA_TENANT_KEY", str(tmp_path / "tenant.key"))
    monkeypatch.setenv("HESTIA_HANDLER_FILE", str(handler))
    monkeypatch.setenv("HESTIA_TENANT_BIND", "127.0.0.1")
    monkeypatch.setenv("HESTIA_TENANT_PORT", str(port))
    threading.Thread(target=main, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 3
    health = None
    while time.time() < deadline:
        try:
            health = httpx.get(f"{url}/health", timeout=0.2)
            if health.status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    assert health is not None and health.status_code == 200
    assert httpx.get(f"{url}/missing").status_code == 404
    assert httpx.post(f"{url}/nope", json={}).status_code == 404
    bad_json = httpx.post(
        f"{url}/invoke", content=b"{", headers={"content-type": "application/json"}
    )
    assert bad_json.status_code == 400
    not_obj = httpx.post(f"{url}/invoke", json=[1])
    assert not_obj.status_code == 400
    ok = httpx.post(f"{url}/invoke", json={"k": "hearth"})
    assert ok.status_code == 200
    body = ok.json()
    assert body["result"]["got"] == "hearth"
    assert body["signature"]
    huge = httpx.post(
        f"{url}/invoke",
        content=b"x" * (256 * 1024 + 8),
        headers={"content-type": "application/json"},
    )
    assert huge.status_code == 413
