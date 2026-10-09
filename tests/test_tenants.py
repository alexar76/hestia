from tests.conftest import auth, client, deploy_payload


def test_template_tenant_serves_signed_invoke(tmp_path) -> None:
    api = client(tmp_path)
    created = api.post("/v1/tenants", json=deploy_payload(), headers=auth(api))
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["status"] == "running"
    assert body["announced"] is False
    assert "/t/demo-echo" in body["public_url"]

    health = api.get("/t/demo-echo/health")
    assert health.status_code == 200
    assert health.json()["ok"] is True

    invoked = api.post("/t/demo-echo/invoke", json={"hello": "hearth"})
    assert invoked.status_code == 200, invoked.text
    payload = invoked.json()
    assert payload["ok"] is True
    assert payload["result"]["echo"]["hello"] == "hearth"
    assert payload["signature"]
    assert payload["provider_pubkey"]

    roster = api.get("/v1/hearth").json()["tenants"]
    assert roster[0]["slug"] == "demo-echo"

    stopped = api.post("/v1/tenants/demo-echo/stop", headers=auth(api))
    assert stopped.json()["status"] == "stopped"
    missing = api.get("/t/demo-echo/health")
    assert missing.status_code == 404


def test_dangerous_handler_never_starts(tmp_path) -> None:
    api = client(tmp_path)
    payload = deploy_payload(handler="import os\n")
    res = api.post("/v1/tenants", json=payload, headers=auth(api))
    assert res.status_code == 400
    assert api.get("/v1/hearth").json()["tenants"] == []


def test_admitted_handler_runs(tmp_path) -> None:
    api = client(tmp_path)
    handler = (
        "import math\n"
        "def handle(payload):\n"
        "    return {'doubled': int(payload.get('n', 0)) * 2}\n"
    )
    res = api.post("/v1/tenants", json=deploy_payload(handler=handler), headers=auth(api))
    assert res.status_code == 200, res.text
    invoked = api.post("/t/demo-echo/invoke", json={"n": 21})
    assert invoked.json()["result"]["doubled"] == 42
    api.post("/v1/tenants/demo-echo/stop", headers=auth(api))


def test_reserved_slug_rejected(tmp_path) -> None:
    api = client(tmp_path)
    payload = deploy_payload(slug="admin")
    res = api.post("/v1/tenants", json=payload, headers=auth(api))
    assert res.status_code == 400


def test_pinned_image_refused_without_allowlist(tmp_path) -> None:
    api = client(tmp_path)
    payload = deploy_payload()
    payload["source"] = {"kind": "image", "image_digest": "sha256:" + ("ab" * 32)}
    res = api.post("/v1/tenants", json=payload, headers=auth(api))
    assert res.status_code == 400


def test_hearth_list_capability(tmp_path) -> None:
    api = client(tmp_path)
    api.post("/v1/tenants", json=deploy_payload(), headers=auth(api))
    listed = api.post(
        "/ai-market/v2/invoke",
        json={"capability_id": "hestia.hearth.list@v1", "input": {}},
    )
    assert listed.status_code == 200
    body = listed.json()
    assert body["ok"] is True
    assert body["result"]["tenants"][0]["slug"] == "demo-echo"
    api.post("/v1/tenants/demo-echo/stop", headers=auth(api))
