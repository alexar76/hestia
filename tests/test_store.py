from __future__ import annotations

import os

import pytest

from hestia.migrations import MIGRATIONS
from hestia.store import TenantRow, TenantStore


def _row(slug: str = "demo", *, status: str = "running", source_kind: str = "template") -> TenantRow:
    return TenantRow(
        slug=slug,
        status=status,
        capability={"capability_id": "demo.echo@v1", "name": "Demo"},
        source_kind=source_kind,
        owner_pubkey="pub",
        listen_url="http://127.0.0.1:9",
        public_url="http://127.0.0.1:9480/t/demo",
        announced=0,
        note="",
        created_at=1.0,
        updated_at=2.0,
        last_error="",
        image_digest="",
    )


def _exercise(store: TenantStore) -> None:
    assert store.count() == 0
    store.upsert(_row())
    got = store.get("demo")
    assert got is not None
    assert got.status == "running"
    assert got.capability["capability_id"] == "demo.echo@v1"
    assert store.count() == 1
    assert [r.slug for r in store.list_running()] == ["demo"]
    store.set_status("demo", "stopped", error="halt", announced=1)
    stopped = store.get("demo")
    assert stopped is not None
    assert stopped.status == "stopped"
    assert stopped.announced == 1
    assert stopped.last_error == "halt"
    store.upsert(_row(status="running"))
    store.upsert(_row("img", source_kind="image"))
    n = store.reconcile_stale_stubs("restarted")
    assert n == 1
    assert store.get("demo") is not None and store.get("demo").status == "stopped"
    assert store.get("img") is not None and store.get("img").status == "running"
    assert [r.slug for r in store.list_all()] == ["demo", "img"]
    assert store.get("demo").public_dict()["capability_id"] == "demo.echo@v1"


def test_sqlite_file_ledger(tmp_path) -> None:
    store = TenantStore.sqlite(tmp_path / "hestia.db")
    try:
        assert store.backend_type == "sqlite"
        assert store.schema_version == MIGRATIONS[-1][0]
        _exercise(store)
    finally:
        store.close()


def _pg_url() -> str:
    return (os.environ.get("HESTIA_TEST_DATABASE_URL") or "").strip()


@pytest.mark.skipif(not _pg_url(), reason="set HESTIA_TEST_DATABASE_URL to a scratch Postgres")
def test_postgres_http_round_trip(tmp_path) -> None:
    from tests.conftest import auth, client, deploy_payload

    api = client(tmp_path, database_url=_pg_url())
    api.app.state.store.backend.execute_write("DELETE FROM tenants")
    created = api.post("/v1/tenants", json=deploy_payload(slug="pg-echo"), headers=auth(api))
    assert created.status_code == 200, created.text
    status = api.get("/v1/tenants/pg-echo")
    assert status.status_code == 200
    assert status.json()["tenant"]["status"] == "running"
    knock = api.post("/v1/tenants/pg-echo/announce", headers=auth(api))
    assert knock.status_code == 400
    stopped = api.post("/v1/tenants/pg-echo/stop", headers=auth(api))
    assert stopped.json()["status"] == "stopped"
    listed = api.get("/v1/tenants", headers=auth(api)).json()
    assert listed["tenants"][0]["slug"] == "pg-echo"
    assert listed["tenants"][0]["status"] == "stopped"
    assert api.get("/health").json()["ledger"] == "postgresql"


@pytest.mark.skipif(not _pg_url(), reason="set HESTIA_TEST_DATABASE_URL to a scratch Postgres")
def test_postgres_ledger(tmp_path) -> None:
    store = TenantStore.open(data_dir=tmp_path, database_url=_pg_url())
    try:
        assert store.backend_type == "postgresql"
        store.backend.execute_write("DELETE FROM tenants")
        _exercise(store)
    finally:
        store.close()
