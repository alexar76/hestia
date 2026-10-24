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

    # A fresh schema, as test_owners' client does: clearing tenants alone left the names a
    # previous test's owner held (demo.echo), and this operator deploy of it was refused.
    import psycopg

    with psycopg.connect(_pg_url(), autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    api = client(tmp_path, database_url=_pg_url())
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


def test_duplicate_keys_are_recognised_by_code_not_by_message() -> None:
    """A Postgres that answers in another language turned a replayed owner nonce into a 500:
    the store read "duplicate key" out of the message text."""
    import sqlite3

    from hestia.db import is_unique_violation

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (k TEXT PRIMARY KEY, u TEXT UNIQUE, n TEXT NOT NULL)")
    conn.execute("INSERT INTO t VALUES ('a', 'x', 'v')")
    for sql in ("INSERT INTO t VALUES ('a', 'y', 'v')", "INSERT INTO t VALUES ('b', 'x', 'v')"):
        with pytest.raises(sqlite3.IntegrityError) as dup:
            conn.execute(sql)
        assert is_unique_violation(dup.value), sql
    with pytest.raises(sqlite3.IntegrityError) as not_null:
        conn.execute("INSERT INTO t VALUES ('c', 'z', NULL)")
    assert not is_unique_violation(not_null.value)

    class PgError(Exception):
        def __init__(self, sqlstate: str, message: str) -> None:
            super().__init__(message)
            self.sqlstate = sqlstate

    ru = "повторяющееся значение ключа нарушает ограничение уникальности"
    assert is_unique_violation(PgError("23505", ru))
    assert not is_unique_violation(PgError("23502", "null value violates not-null constraint"))
    assert not is_unique_violation(RuntimeError("duplicate key"))
