"""Versioned tenant-ledger migrations.

House style matches Hub: SQLite-shaped DDL, a ``schema_migrations`` bookkeeping
table, fail-loud apply. The production contract is the revision list — never
``CREATE TABLE IF NOT EXISTS tenants`` inside the control plane.

Revision 001 matches the historical SQLite ledger exactly. A pre-migration
SQLite file with those columns is stamped, not rebuilt.
"""

from __future__ import annotations

import sys
import time

from hestia.db import LedgerBackend

# Canonical tenant columns. Tests fail if TenantRow, SQLite, or Postgres drift.
TENANT_COLUMNS: tuple[str, ...] = (
    "slug",
    "status",
    "capability_json",
    "source_kind",
    "owner_pubkey",
    "listen_url",
    "public_url",
    "announced",
    "note",
    "created_at",
    "updated_at",
    "last_error",
    "image_digest",
    "payout_address",
    "listing",
    "listing_reasons",
    "listing_by",
)

# What revision 001 alone created. Later revisions extend TENANT_COLUMNS; a
# legacy ledger is stamped against this.
REVISION_001_COLUMNS: tuple[str, ...] = (
    "slug", "status", "capability_json", "source_kind", "owner_pubkey",
    "listen_url", "public_url", "announced", "note", "created_at",
    "updated_at", "last_error", "image_digest",
)

MIGRATION_001_TENANTS = """
CREATE TABLE tenants (
    slug TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    capability_json TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    owner_pubkey TEXT NOT NULL,
    listen_url TEXT NOT NULL,
    public_url TEXT NOT NULL,
    announced INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    last_error TEXT NOT NULL DEFAULT '',
    image_digest TEXT NOT NULL DEFAULT ''
)
""".strip()

# Postgres REAL is float4: ~128 s of granularity at a current unix timestamp, so
# created_at/updated_at drifted by up to a minute and writes seconds apart could
# land on the same value. SQLite REAL is a 8-byte double, so this only ever bit
# the Postgres ledger. DOUBLE PRECISION is exact on Postgres and keeps REAL
# affinity on SQLite (its rule 4 matches the "DOUB" in the type name).
MIGRATION_002_TIME_PRECISION: dict[str, tuple[str, ...]] = {
    "postgresql": (
        "ALTER TABLE tenants ALTER COLUMN created_at TYPE DOUBLE PRECISION",
        "ALTER TABLE tenants ALTER COLUMN updated_at TYPE DOUBLE PRECISION",
        "ALTER TABLE schema_migrations ALTER COLUMN applied_at TYPE DOUBLE PRECISION",
    ),
    "sqlite": (),
}

# Where a priced tenant's money goes, and which payments have already been spent.
# The hearth never holds the funds: the buyer pays the tenant owner directly and
# this only records that a given transaction has been used, so one payment
# cannot buy two calls.
MIGRATION_003_PAYOUT: tuple[str, ...] = (
    "ALTER TABLE tenants ADD COLUMN payout_address TEXT NOT NULL DEFAULT ''",
    """
    CREATE TABLE tenant_payments (
        tx_hash TEXT PRIMARY KEY,
        slug TEXT NOT NULL,
        chain TEXT NOT NULL,
        token TEXT NOT NULL,
        paid_units TEXT NOT NULL,
        pay_to TEXT NOT NULL,
        spent_at DOUBLE PRECISION NOT NULL
    )
    """.strip(),
)

# What binds a payment to ONE call. A bare ERC-20 transfer carries nothing that
# names the call it pays for, so a transfer to a payout address that also
# receives funds from elsewhere (a shared treasury does) could be replayed as a
# free call. The hearth mints an invoice with a random nonce when it answers 402;
# the buyer signs an EIP-3009 authorization over THAT nonce, and the token
# contract records it on chain as `AuthorizationUsed`. The nonce is the primary
# key, so consuming an invoice is the database's decision, not a read-then-write.
MIGRATION_004_INVOICES: tuple[str, ...] = (
    """
    CREATE TABLE tenant_invoices (
        nonce TEXT PRIMARY KEY,
        slug TEXT NOT NULL,
        capability_id TEXT NOT NULL,
        chain TEXT NOT NULL,
        token TEXT NOT NULL,
        token_contract TEXT NOT NULL,
        pay_to TEXT NOT NULL,
        amount_units TEXT NOT NULL,
        issued_at DOUBLE PRECISION NOT NULL,
        expires_at DOUBLE PRECISION NOT NULL,
        consumed_at DOUBLE PRECISION NOT NULL DEFAULT 0,
        tx_hash TEXT NOT NULL DEFAULT ''
    )
    """.strip(),
)

# Who may change which tenant (hestia/owners.py). An owner is an Ed25519 key the
# operator admitted (or, with HESTIA_OPEN_OWNERS, one that admitted itself; such an owner
# may not run code). A nonce is spent per owner by the primary key, so a replayed request
# loses at the database, not in a read-then-write.
MIGRATION_005_OWNERS: tuple[str, ...] = (
    """
    CREATE TABLE owners (
        pubkey TEXT PRIMARY KEY,
        label TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL,
        max_tenants INTEGER NOT NULL,
        may_run_code INTEGER NOT NULL DEFAULT 1,
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL
    )
    """.strip(),
    """
    CREATE TABLE owner_nonces (
        owner_nonce TEXT PRIMARY KEY,
        seen_at DOUBLE PRECISION NOT NULL
    )
    """.strip(),
)

# Hubs that sell a tenant on its owner's behalf (hestia/hub_billing.py): which hub credits which
# account of the owner, and every call a hub was served without an on-chain payment — the rows an
# owner reconciles with what the hub paid.
MIGRATION_006_HUB_BILLING: tuple[str, ...] = (
    "ALTER TABLE owners ADD COLUMN billing_json TEXT NOT NULL DEFAULT '{}'",
    """
    CREATE TABLE hub_billed_calls (
        id TEXT PRIMARY KEY,
        hub TEXT NOT NULL,
        slug TEXT NOT NULL,
        owner_pubkey TEXT NOT NULL,
        capability_id TEXT NOT NULL,
        bill_to_account TEXT NOT NULL,
        price_usd DOUBLE PRECISION NOT NULL,
        input_sha256 TEXT NOT NULL,
        created_at DOUBLE PRECISION NOT NULL
    )
    """.strip(),
)

# Whether a tenant is in the public manifest hubs index (hestia/listing.py). An owner's agent
# runs as soon as it deploys; it is LISTED only after the automatic review passes or the
# operator lists it. Rows from before this revision were all listed, and stay so.
MIGRATION_007_LISTING: tuple[str, ...] = (
    "ALTER TABLE tenants ADD COLUMN listing TEXT NOT NULL DEFAULT 'listed'",
    "ALTER TABLE tenants ADD COLUMN listing_reasons TEXT NOT NULL DEFAULT '[]'",
)

# Names a hub shows buyers (capability family, product, publisher), each claimed for good by
# its first holder (hestia/names.py). The primary key is the claim: two processes on one
# ledger cannot both win a name. `listing_by` records whether the review or the operator made
# a listing decision, so an owner's redeploy cannot undo the operator's hold.
MIGRATION_008_NAMES: tuple[str, ...] = (
    """
    CREATE TABLE hearth_names (
        kind TEXT NOT NULL,
        name_key TEXT NOT NULL,
        holder TEXT NOT NULL,
        name TEXT NOT NULL,
        slug TEXT NOT NULL DEFAULT '',
        created_at DOUBLE PRECISION NOT NULL,
        PRIMARY KEY (kind, name_key)
    )
    """.strip(),
    "ALTER TABLE tenants ADD COLUMN listing_by TEXT NOT NULL DEFAULT 'review'",
)

# How an owner came to the hearth: "operator" (admitted with POST /v1/owners) or "open" (a key
# that admitted itself, HESTIA_OPEN_OWNERS). Agents of open owners share one narrow sandbox
# lane (hestia/runtime/wasm.py OpenLane), because a new key costs nothing. The default is
# "open", so a row written without the column is never trusted by accident. Before this
# revision a self-admission wrote the label "open signup"; every other ACTIVE row was written
# by the operator. A suspended row stays "open": the operator vouches again when it lifts
# the suspension (POST /v1/owners with admitted_by).
MIGRATION_009_OWNER_ADMISSION: tuple[str, ...] = (
    "ALTER TABLE owners ADD COLUMN admitted_by TEXT NOT NULL DEFAULT 'open'",
    "UPDATE owners SET admitted_by = 'operator' "
    "WHERE status = 'active' AND label <> 'open signup'",
)

# (version, name, statements). statements have no IF NOT EXISTS on product tables.
# statements is a tuple for every backend, or a dict keyed by backend_type.
MIGRATIONS: list[tuple[int, str, tuple[str, ...] | dict[str, tuple[str, ...]]]] = [
    (1, "001_tenants", (MIGRATION_001_TENANTS,)),
    (2, "002_time_precision", MIGRATION_002_TIME_PRECISION),
    (3, "003_payout_address", MIGRATION_003_PAYOUT),
    (4, "004_invoices", MIGRATION_004_INVOICES),
    (5, "005_owners", MIGRATION_005_OWNERS),
    (6, "006_hub_billing", MIGRATION_006_HUB_BILLING),
    (7, "007_listing", MIGRATION_007_LISTING),
    (8, "008_names", MIGRATION_008_NAMES),
    (9, "009_owner_admission", MIGRATION_009_OWNER_ADMISSION),
]


def statements_for(statements: tuple[str, ...] | dict[str, tuple[str, ...]], backend_type: str) -> tuple[str, ...]:
    if isinstance(statements, dict):
        return statements.get(backend_type, ())
    return statements


class MigrationError(RuntimeError):
    """Schema apply failed. Do not start the hearth on a half-migrated ledger."""


def apply_migrations(backend: LedgerBackend) -> int:
    """Apply pending revisions. Returns the head version. Fail loud."""
    try:
        _ensure_bookkeeping(backend)
        _stamp_legacy_ledger(backend)
        applied = applied_versions(backend)
        head = 0
        for version, name, statements in MIGRATIONS:
            head = version
            if version in applied:
                continue
            try:
                backend.execute_script(statements_for(statements, backend.backend_type))
                backend.execute_write(
                    "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                    (version, name, time.time()),
                )
            except Exception as exc:
                raise MigrationError(f"migration {name} failed: {exc}") from exc
        _assert_tenant_columns(backend)
        return head
    except MigrationError:
        raise
    except Exception as exc:
        raise MigrationError(f"ledger migrate failed: {exc}") from exc


def applied_versions(backend: LedgerBackend) -> set[int]:
    rows = backend.execute("SELECT version FROM schema_migrations")
    return {int(row["version"]) for row in rows}


def current_version(backend: LedgerBackend) -> int:
    rows = backend.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations")
    if not rows:
        return 0
    return int(rows[0]["v"] or 0)


def tenant_columns(backend: LedgerBackend) -> tuple[str, ...]:
    if backend.backend_type == "sqlite":
        rows = backend.execute("PRAGMA table_info(tenants)")
        return tuple(str(row["name"]) for row in rows)
    rows = backend.execute(
        "SELECT column_name AS name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'tenants' "
        "ORDER BY ordinal_position"
    )
    return tuple(str(row["name"]) for row in rows)


def _ensure_bookkeeping(backend: LedgerBackend) -> None:
    backend.execute_script(
        (
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at DOUBLE PRECISION NOT NULL
            )
            """.strip(),
        )
    )


def _table_names(backend: LedgerBackend) -> set[str]:
    if backend.backend_type == "sqlite":
        rows = backend.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        return {str(row["name"]) for row in rows}
    rows = backend.execute(
        "SELECT tablename AS name FROM pg_tables WHERE schemaname = current_schema()"
    )
    return {str(row["name"]) for row in rows}


def _stamp_legacy_ledger(backend: LedgerBackend) -> None:
    """Pre-migration ledger already has ``tenants``. Stamp 001; do not CREATE.

    Covers historical SQLite files and a live Postgres that was created by the
    short-lived Alembic experiment. Extra leftover tables (``alembic_version``)
    are ignored.
    """
    names = _table_names(backend)
    if "tenants" not in names:
        return
    if 1 in applied_versions(backend):
        return
    cols = tenant_columns(backend)
    # A pre-migration ledger predates every later revision, so it is stamped
    # against revision 001's own column list, not today's contract.
    if cols != REVISION_001_COLUMNS:
        raise MigrationError(
            f"legacy tenants columns {cols} do not match revision 001 {REVISION_001_COLUMNS}"
        )
    backend.execute_write(
        "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
        (1, "001_tenants", time.time()),
    )


def _assert_tenant_columns(backend: LedgerBackend) -> None:
    cols = tenant_columns(backend)
    if cols != TENANT_COLUMNS:
        raise MigrationError(
            f"tenants columns {cols} do not match contract {TENANT_COLUMNS}"
        )


def main(argv: list[str] | None = None) -> int:
    from hestia.config import Settings
    from hestia.db import open_ledger

    args = list(sys.argv[1:] if argv is None else argv)
    settings = Settings.from_env()
    backend = open_ledger(settings.data_dir / "hestia.db", settings.database_url)
    try:
        if not args or args[0] in {"up", "apply"}:
            version = apply_migrations(backend)
            print(f"ledger={backend.backend_type} version={version}")
            return 0
        if args[0] == "status":
            apply_migrations(backend)
            print(
                f"ledger={backend.backend_type} version={current_version(backend)} "
                f"applied={sorted(applied_versions(backend))}"
            )
            return 0
        print("usage: python -m hestia.migrations [up|status]", file=sys.stderr)
        return 2
    finally:
        backend.close()


if __name__ == "__main__":
    raise SystemExit(main())
