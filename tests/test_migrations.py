from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from hestia.db import is_postgres_url, open_ledger
from hestia.migrations import (
    MIGRATION_001_TENANTS,
    MIGRATIONS,
    REVISION_001_COLUMNS,
    TENANT_COLUMNS,
    MigrationError,
    apply_migrations,
    current_version,
    statements_for,
    tenant_columns,
)
from hestia.store import TenantRow, TenantStore

HEAD = MIGRATIONS[-1][0]


def test_no_alembic_in_tree() -> None:
    root = Path(__file__).resolve().parents[1]
    assert not (root / "hestia" / "schema_alembic.py").exists()
    assert not (root / "hestia" / "alembic").exists()
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert "alembic" not in pyproject
    assert "sqlalchemy" not in pyproject


def test_url_helpers() -> None:
    from hestia.db import PostgresLedger, translate_placeholders

    assert is_postgres_url("postgresql://u:p@db:5432/hestia")
    assert is_postgres_url("postgres://u:p@db:5432/hestia")
    assert not is_postgres_url("")
    assert not is_postgres_url("sqlite:///tmp/x.db")
    assert translate_placeholders("SELECT * FROM tenants WHERE slug = ?") == (
        "SELECT * FROM tenants WHERE slug = %s"
    )
    with pytest.raises(RuntimeError, match="postgresql"):
        open_ledger(Path("/tmp/x.db"), "sqlite:///tmp/x.db")
    with pytest.raises(ValueError, match="options="):
        PostgresLedger("postgresql://u:p@db/hestia?options=-csearch_path=evil")


def test_tenant_row_matches_migration_contract() -> None:
    mapped = tuple(
        "capability_json" if name == "capability" else name for name in TenantRow.__dataclass_fields__
    )
    assert mapped == TENANT_COLUMNS
    # Revision 001 is shipped and immutable, so it carries its own columns and
    # not today's. Checking TENANT_COLUMNS against it broke the moment a later
    # revision added one — what the contract actually means is that 001 holds
    # exactly what 001 created, and that the APPLIED schema matches the full
    # list, which `_assert_tenant_columns` enforces against a real ledger.
    for col in REVISION_001_COLUMNS:
        assert col in MIGRATION_001_TENANTS
    assert set(REVISION_001_COLUMNS) <= set(TENANT_COLUMNS)
    later = set(TENANT_COLUMNS) - set(REVISION_001_COLUMNS)
    added_by_revisions = " ".join(
        stmt
        for _version, _name, statements in MIGRATIONS[1:]
        for stmt in statements_for(statements, "postgresql")
    )
    for col in later:
        assert col in added_by_revisions, f"{col} is in the contract but no revision adds it"
    assert "CREATE TABLE IF NOT EXISTS tenants" not in MIGRATION_001_TENANTS
    assert "CREATE TABLE tenants" in MIGRATION_001_TENANTS


def test_sqlite_upgrade_creates_tenants(tmp_path) -> None:
    backend = open_ledger(tmp_path / "hestia.db", "")
    try:
        assert apply_migrations(backend) == HEAD
        assert current_version(backend) == HEAD
        assert tenant_columns(backend) == TENANT_COLUMNS
        assert apply_migrations(backend) == HEAD
    finally:
        backend.close()


def test_stamp_pre_migration_sqlite(tmp_path) -> None:
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute(MIGRATION_001_TENANTS)
    conn.execute(
        "INSERT INTO tenants (slug, status, capability_json, source_kind, owner_pubkey, "
        "listen_url, public_url, created_at, updated_at) "
        "VALUES ('legacy', 'stopped', '{}', 'template', 'p', 'http://x', 'http://y', 1, 1)"
    )
    conn.commit()
    conn.close()
    store = TenantStore.sqlite(path)
    try:
        row = store.get("legacy")
        assert row is not None
        assert row.slug == "legacy"
        assert store.schema_version == HEAD
        store.upsert(
            TenantRow(
                slug="legacy",
                status="stopped",
                capability={"capability_id": "x.y@v1"},
                source_kind="template",
                owner_pubkey="p",
                listen_url="http://x",
                public_url="http://y",
                announced=0,
                note="",
                created_at=1.0,
                updated_at=2.0,
                last_error="",
                image_digest="",
            )
        )
        assert store.get("legacy") is not None
    finally:
        store.close()


def test_stamp_ignores_leftover_alembic_version(tmp_path) -> None:
    path = tmp_path / "hybrid.db"
    conn = sqlite3.connect(path)
    conn.execute(MIGRATION_001_TENANTS)
    conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
    conn.execute("INSERT INTO alembic_version (version_num) VALUES ('0001_tenants')")
    conn.commit()
    conn.close()
    store = TenantStore.sqlite(path)
    try:
        assert store.schema_version == HEAD
        assert store.backend_type == "sqlite"
    finally:
        store.close()


def test_legacy_wrong_columns_fail_loud(tmp_path) -> None:
    path = tmp_path / "bad.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE tenants (slug TEXT PRIMARY KEY, extra TEXT NOT NULL)")
    conn.commit()
    conn.close()
    with pytest.raises(MigrationError, match="revision 001"):
        TenantStore.sqlite(path)


def test_migrations_cli_status(tmp_path, monkeypatch) -> None:
    from hestia.migrations import main

    monkeypatch.setenv("HESTIA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HESTIA_RUNTIME", "stub")
    monkeypatch.delenv("HESTIA_DATABASE_URL", raising=False)
    monkeypatch.delenv("HESTIA_REPLICAS", raising=False)
    monkeypatch.delenv("HESTIA_REQUIRE_SANDBOX", raising=False)
    monkeypatch.delenv("HESTIA_DOCKER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("HESTIA_ALLOW_HOST_DOCKER", raising=False)
    assert main(["up"]) == 0
    assert main(["status"]) == 0
    assert main(["nope"]) == 2


def _pg_url() -> str:
    return (os.environ.get("HESTIA_TEST_DATABASE_URL") or "").strip()


@pytest.mark.skipif(not _pg_url(), reason="set HESTIA_TEST_DATABASE_URL to a scratch Postgres")
def test_postgres_columns_match_contract(tmp_path) -> None:
    store = TenantStore.open(data_dir=tmp_path, database_url=_pg_url())
    try:
        assert tenant_columns(store.backend) == TENANT_COLUMNS
        assert store.version == HEAD
    finally:
        store.close()
