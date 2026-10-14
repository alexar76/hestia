from tests.conftest import auth, client, deploy_payload


def test_admin_page_is_served(tmp_path) -> None:
    api = client(tmp_path)
    page = api.get("/ui/admin/")
    assert page.status_code == 200
    assert b"HESTIA" in page.content
    assert b"watch" in page.content
    assert api.get("/ui/admin").status_code == 200
    assets = api.get("/ui/assets/admin.js")
    assert assets.status_code == 200
    assert b"HESTIA_DEPLOY_TOKEN" in assets.content


def test_admin_overview_requires_token(tmp_path) -> None:
    api = client(tmp_path)
    assert api.get("/v1/admin/overview").status_code == 401
    assert api.get("/v1/admin/overview", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_admin_overview_empty_token_never_opens(tmp_path) -> None:
    api = client(tmp_path, deploy_token="")
    res = api.get("/v1/admin/overview", headers={"Authorization": "Bearer "})
    assert res.status_code == 401


def test_admin_overview_empty_expected_rejects_typed_bearer(tmp_path) -> None:
    api = client(tmp_path, deploy_token="")
    res = api.get("/v1/admin/overview", headers={"Authorization": "Bearer typed-anyway"})
    assert res.status_code == 401


def test_admin_overview_accepts_matching_bearer(tmp_path) -> None:
    api = client(tmp_path, deploy_token="local-secret-token")
    res = api.get("/v1/admin/overview", headers={"Authorization": "Bearer local-secret-token"})
    assert res.status_code == 200
    assert res.json()["ok"] is True


def test_admin_overview_wrong_length_is_401_not_500(tmp_path) -> None:
    api = client(tmp_path, deploy_token="short")
    res = api.get("/v1/admin/overview", headers={"Authorization": "Bearer a-much-longer-guess"})
    assert res.status_code == 401


def test_admin_overview_empty_hearth(tmp_path) -> None:
    api = client(tmp_path)
    res = api.get("/v1/admin/overview", headers=auth(api))
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["tenants"] == 0
    assert body["running"] == 0
    assert body["stopped"] == 0
    assert body["runtime"] == "stub"
    assert body["hearth"] == "http://127.0.0.1:9480"
    assert "listen_url" not in body
    assert "deploy_token" not in body


def test_admin_overview_one_tenant_after_deploy(tmp_path) -> None:
    api = client(tmp_path)
    created = api.post("/v1/tenants", json=deploy_payload(), headers=auth(api))
    assert created.status_code == 200, created.text
    body = api.get("/v1/admin/overview", headers=auth(api)).json()
    assert body["tenants"] == 1
    assert body["running"] == 1
    assert body["stopped"] == 0
    assert "listen_url" not in body
    listed = api.get("/v1/tenants", headers=auth(api)).json()
    assert "listen_url" not in listed["tenants"][0]
    api.post("/v1/tenants/demo-echo/stop", headers=auth(api))
    after = api.get("/v1/admin/overview", headers=auth(api)).json()
    assert after["running"] == 0
    assert after["stopped"] == 1
    assert after["tenants"] == 1
