from tests.conftest import client, deploy_payload


def test_health_and_manifest(tmp_path) -> None:
    api = client(tmp_path)
    health = api.get("/health").json()
    assert health["ok"] is True
    assert health["service"] == "hestia"
    assert health["ledger"] == "sqlite"
    well = api.get("/.well-known/ai-market.json").json()
    assert "hestia.host.deploy@v1" in well["capabilities"]
    man = api.get("/ai-market/v2/manifest").json()
    assert man["total_capabilities"] == 4
    search = api.get("/ai-market/v2/search", params={"q": "hearth"}).json()
    assert search["total"] >= 1


def test_deploy_requires_token(tmp_path) -> None:
    api = client(tmp_path)
    res = api.post("/v1/tenants", json=deploy_payload())
    assert res.status_code == 401


def test_empty_token_never_opens_the_hearth(tmp_path) -> None:
    api = client(tmp_path, deploy_token="")
    headers = {"Authorization": "Bearer "}
    assert api.post("/v1/tenants", json=deploy_payload(), headers=headers).status_code == 401
    assert api.post("/v1/tenants/demo-echo/stop", headers=headers).status_code == 401
    assert api.post("/v1/tenants/demo-echo/announce", headers=headers).status_code == 401
    sku = api.post(
        "/ai-market/v2/invoke",
        headers=headers,
        json={"capability_id": "hestia.host.deploy@v1", "input": deploy_payload()},
    )
    assert sku.status_code == 401


def test_hearth_starts_empty(tmp_path) -> None:
    api = client(tmp_path)
    body = api.get("/v1/hearth").json()
    assert body["tenants"] == []
