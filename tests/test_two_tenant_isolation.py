"""Two-tenant fail-closed isolation: keeper stays up; probe never starts forbidden code.

This is a conformance / denial check. It does not run breakout procedures.
Forbidden handlers are the same refused imports already locked in test_scan.py.
"""

from __future__ import annotations

from tests.conftest import auth, client, deploy_payload

KEEPER_HANDLER = (
    "def handle(payload):\n"
    "    return {'hello': payload.get('hello', 'hearth'), 'keeper': True}\n"
)

PROBE_HANDLER = (
    "def handle(payload):\n"
    "    return {\n"
    "        'role': 'isolation-self-check',\n"
    "        'hello': payload.get('hello', 'probe'),\n"
    "        'visible_payload_keys': sorted(payload.keys()),\n"
    "        'findings': [\n"
    "            {'category': 'sibling_fs', 'result': 'denied_at_admission'},\n"
    "            {'category': 'host_env', 'result': 'denied_at_admission'},\n"
    "            {'category': 'sibling_network', 'result': 'denied_at_admission'},\n"
    "        ],\n"
    "    }\n"
)

# Categories Hestia already refuses at AST admission. No path recipes, no payloads.
_DENIED_AT_ADMISSION = (
    ("sibling_fs", "from pathlib import Path\n"),
    ("host_env", "import os\n"),
    ("sibling_network", "import socket\n"),
)


def _keeper_payload() -> dict:
    body = deploy_payload(slug="hearth-keeper", handler=KEEPER_HANDLER)
    body["capability"]["capability_id"] = "hearth.keeper.hello@v1"
    body["capability"]["name"] = "Hearth keeper"
    body["capability"]["description"] = "Benign hello provider that stays up."
    body["note"] = "keeper — isolation fixture"
    return body


def _probe_payload(handler: str) -> dict:
    body = deploy_payload(slug="inside-probe", handler=handler)
    body["capability"]["capability_id"] = "hearth.probe.selfcheck@v1"
    body["capability"]["name"] = "Inside probe"
    body["capability"]["description"] = "Isolation self-check. Records denials only."
    body["note"] = "conformance probe — not a breakout"
    return body


def test_two_tenant_isolation_self_check(tmp_path) -> None:
    api = client(tmp_path)
    headers = auth(api)

    empty = api.get("/v1/hearth").json()
    assert empty["tenants"] == []

    created = api.post("/v1/tenants", json=_keeper_payload(), headers=headers)
    assert created.status_code == 200, created.text
    assert created.json()["status"] == "running"
    assert "/t/hearth-keeper" in created.json()["public_url"]

    health = api.get("/t/hearth-keeper/health")
    assert health.status_code == 200
    assert health.json()["slug"] == "hearth-keeper"

    hello = api.post("/t/hearth-keeper/invoke", json={"hello": "hearth"})
    assert hello.status_code == 200, hello.text
    assert hello.json()["result"]["keeper"] is True
    assert hello.json()["result"]["hello"] == "hearth"

    one = api.get("/v1/hearth").json()["tenants"]
    assert [row["slug"] for row in one] == ["hearth-keeper"]

    for category, refused_src in _DENIED_AT_ADMISSION:
        res = api.post("/v1/tenants", json=_probe_payload(refused_src), headers=headers)
        assert res.status_code == 400, category
        detail = str(res.json().get("detail", "")).lower()
        assert "refused" in detail
        still_one = api.get("/v1/hearth").json()["tenants"]
        assert [row["slug"] for row in still_one] == ["hearth-keeper"]

    probe = api.post("/v1/tenants", json=_probe_payload(PROBE_HANDLER), headers=headers)
    assert probe.status_code == 200, probe.text
    assert probe.json()["status"] == "running"

    findings = api.post("/t/inside-probe/invoke", json={"hello": "self-check"})
    assert findings.status_code == 200, findings.text
    body = findings.json()
    assert body["ok"] is True
    result = body["result"]
    assert result["role"] == "isolation-self-check"
    categories = {item["category"]: item["result"] for item in result["findings"]}
    assert categories == {
        "sibling_fs": "denied_at_admission",
        "host_env": "denied_at_admission",
        "sibling_network": "denied_at_admission",
    }
    dumped = str(body).lower()
    assert "test-token" not in dumped
    assert "hestia_deploy_token" not in dumped
    assert "provider.key" not in dumped

    keeper_after = api.get("/t/hearth-keeper/health")
    assert keeper_after.status_code == 200
    assert keeper_after.json()["ok"] is True

    roster = api.get("/v1/hearth").json()["tenants"]
    assert {row["slug"] for row in roster} == {"hearth-keeper", "inside-probe"}
    for row in roster:
        assert "listen_url" not in row

    keeper_dir = tmp_path / "tenants" / "hearth-keeper"
    probe_dir = tmp_path / "tenants" / "inside-probe"
    assert keeper_dir.is_dir()
    assert probe_dir.is_dir()
    assert (keeper_dir / "capability.json").is_file()
    assert (probe_dir / "capability.json").is_file()
    assert not (keeper_dir / "provider.key").exists()
    assert not (probe_dir / "provider.key").exists()
    assert (tmp_path / "provider.key").is_file()
    assert keeper_dir.resolve() != probe_dir.resolve()

    api.post("/v1/tenants/inside-probe/stop", headers=headers)
    api.post("/v1/tenants/hearth-keeper/stop", headers=headers)
