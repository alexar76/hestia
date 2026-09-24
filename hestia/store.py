"""Typed tenant ledger. SQLite default; Postgres when HESTIA_DATABASE_URL is set.

A shared database is not a replica farm. Tenants still live on this host.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hestia.db import LedgerBackend, open_ledger
from hestia.migrations import apply_migrations, current_version

# Not served, not listed, and retried by the next docker reconcile (at boot or
# POST /v1/admin/reconcile). Used when an image tenant's isolation cannot be
# verified or restored, so a transient engine failure never becomes a silent,
# permanent "stopped".
QUARANTINED = "quarantined"


@dataclass
class TenantRow:
    slug: str
    status: str
    capability: dict[str, Any]
    source_kind: str
    owner_pubkey: str
    listen_url: str
    public_url: str
    announced: int
    note: str
    created_at: float
    updated_at: float
    last_error: str
    image_digest: str
    payout_address: str = ""

    def public_dict(self) -> dict[str, Any]:
        cap = self.capability
        return {
            "slug": self.slug,
            "status": self.status,
            "capability_id": cap.get("capability_id"),
            "name": cap.get("name"),
            "price_per_call_usd": cap.get("price_per_call_usd"),
            "public_url": self.public_url,
            "announced": bool(self.announced),
            "updated_at": self.updated_at,
            # Buyers need to know where a priced call is paid. The hearth never
            # holds the money, so this is the tenant owner's own address.
            "payout_address": self.payout_address,
        }


class TenantStore:
    def __init__(self, backend: LedgerBackend) -> None:
        self.backend = backend
        self.schema_version = apply_migrations(backend)

    @classmethod
    def sqlite(cls, path: Path) -> TenantStore:
        return cls(open_ledger(path, ""))

    @classmethod
    def open(cls, *, data_dir: Path, database_url: str = "") -> TenantStore:
        return cls(open_ledger(data_dir / "hestia.db", database_url))

    @property
    def backend_type(self) -> str:
        return self.backend.backend_type

    @property
    def version(self) -> int:
        return current_version(self.backend)

    def count(self) -> int:
        rows = self.backend.execute("SELECT COUNT(*) AS n FROM tenants")
        return int(rows[0]["n"])

    def get(self, slug: str) -> TenantRow | None:
        rows = self.backend.execute("SELECT * FROM tenants WHERE slug = ?", (slug,))
        return _row(rows[0]) if rows else None

    def list_running(self) -> list[TenantRow]:
        rows = self.backend.execute(
            "SELECT * FROM tenants WHERE status = 'running' ORDER BY slug"
        )
        return [_row(r) for r in rows]

    def list_all(self) -> list[TenantRow]:
        rows = self.backend.execute("SELECT * FROM tenants ORDER BY slug")
        return [_row(r) for r in rows]

    def upsert(self, tenant: TenantRow) -> None:
        self.backend.execute_write(
            """
            INSERT INTO tenants (
                slug, status, capability_json, source_kind, owner_pubkey,
                listen_url, public_url, announced, note, created_at, updated_at,
                last_error, image_digest, payout_address
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(slug) DO UPDATE SET
                status=excluded.status,
                capability_json=excluded.capability_json,
                source_kind=excluded.source_kind,
                owner_pubkey=excluded.owner_pubkey,
                listen_url=excluded.listen_url,
                public_url=excluded.public_url,
                announced=excluded.announced,
                note=excluded.note,
                updated_at=excluded.updated_at,
                last_error=excluded.last_error,
                image_digest=excluded.image_digest,
                payout_address=excluded.payout_address
            """,
            (
                tenant.slug,
                tenant.status,
                json.dumps(tenant.capability, separators=(",", ":"), sort_keys=True),
                tenant.source_kind,
                tenant.owner_pubkey,
                tenant.listen_url,
                tenant.public_url,
                1 if tenant.announced else 0,
                tenant.note,
                tenant.created_at,
                tenant.updated_at,
                tenant.last_error,
                tenant.image_digest,
                tenant.payout_address,
            ),
        )

    def reconcile_stale_stubs(self, reason: str) -> int:
        """Stop every stub row left claiming `running` by a previous process.

        Called once at start-up. A stub tenant is a child of the control-plane
        process on an ephemeral loopback port; once that process is gone the row
        is a lie and the port is reusable by anything else on the host. Image
        rows are untouched — a container outlives the control plane.
        """
        return self.backend.execute_write(
            "UPDATE tenants SET status='stopped', last_error=?, updated_at=? "
            "WHERE status='running' AND source_kind != 'image'",
            (reason, time.time()),
        )

    def claim_payment(
        self, *, tx_hash: str, slug: str, chain: str, token: str,
        paid_units: int, pay_to: str,
    ) -> bool:
        """Record a payment as spent. False when it was already used.

        The uniqueness is the whole point: a buyer who pays once must not be
        able to replay that transaction for a second call, and the check has to
        be the database's rather than a read-then-write in Python.
        """
        try:
            self.backend.execute_write(
                "INSERT INTO tenant_payments "
                "(tx_hash, slug, chain, token, paid_units, pay_to, spent_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (tx_hash.lower(), slug, chain, token, str(paid_units), pay_to.lower(), time.time()),
            )
        except Exception as exc:
            message = str(exc).lower()
            if any(k in message for k in ("unique", "constraint", "duplicate")):
                return False
            raise
        return True

    def release_payment(self, tx_hash: str) -> None:
        """Un-spend a payment so its transaction can buy a call again.

        Called only when the tenant was unreachable AFTER the payment was
        claimed — a platform failure, not the caller's. Without this an honest
        buyer whose call hit a wedged tenant would have paid and be unable to
        retry the same transaction (it would read as already spent).
        """
        self.backend.execute_write(
            "DELETE FROM tenant_payments WHERE tx_hash = ?", (tx_hash.lower(),)
        )

    def mint_invoice(
        self, *, nonce: str, slug: str, capability_id: str, chain: str, token: str,
        token_contract: str, pay_to: str, amount_units: int, ttl_s: int,
    ) -> dict[str, Any]:
        """Record what a 402 promised, so settlement checks the terms the hearth
        quoted rather than the terms the caller claims."""
        now = time.time()
        row = {
            "nonce": nonce.lower(),
            "slug": slug,
            "capability_id": capability_id,
            "chain": chain,
            "token": token,
            "token_contract": token_contract.lower(),
            "pay_to": pay_to.lower(),
            "amount_units": str(amount_units),
            "issued_at": now,
            "expires_at": now + ttl_s,
        }
        self.backend.execute_write(
            "INSERT INTO tenant_invoices "
            "(nonce, slug, capability_id, chain, token, token_contract, pay_to, "
            " amount_units, issued_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["nonce"], slug, capability_id, chain, token,
                row["token_contract"], row["pay_to"], row["amount_units"],
                now, row["expires_at"],
            ),
        )
        return row

    def invoice(self, nonce: str) -> dict[str, Any] | None:
        rows = self.backend.execute(
            "SELECT * FROM tenant_invoices WHERE nonce = ?", (nonce.lower(),)
        )
        return dict(rows[0]) if rows else None

    def consume_invoice(self, nonce: str, tx_hash: str) -> bool:
        """Spend an invoice exactly once. False when it was already spent.

        The UPDATE carries the `consumed_at = 0` test, so two calls racing on the
        same nonce are decided by the database — one row updated, one not."""
        changed = self.backend.execute_write(
            "UPDATE tenant_invoices SET consumed_at = ?, tx_hash = ? "
            "WHERE nonce = ? AND consumed_at = 0",
            (time.time(), tx_hash.lower(), nonce.lower()),
        )
        return bool(changed)

    def release_invoice(self, nonce: str) -> None:
        """Un-spend an invoice whose call then failed on the platform's side, so
        the buyer can retry with the payment they already made."""
        self.backend.execute_write(
            "UPDATE tenant_invoices SET consumed_at = 0, tx_hash = '' WHERE nonce = ?",
            (nonce.lower(),),
        )

    def payment_count(self, slug: str) -> int:
        rows = self.backend.execute(
            "SELECT COUNT(*) AS n FROM tenant_payments WHERE slug = ?", (slug,)
        )
        return int(rows[0]["n"]) if rows else 0

    def set_status(
        self, slug: str, status: str, *, error: str = "", announced: int | None = None
    ) -> None:
        now = time.time()
        if announced is None:
            self.backend.execute_write(
                "UPDATE tenants SET status=?, last_error=?, updated_at=? WHERE slug=?",
                (status, error, now, slug),
            )
            return
        self.backend.execute_write(
            "UPDATE tenants SET status=?, last_error=?, announced=?, updated_at=? WHERE slug=?",
            (status, error, announced, now, slug),
        )

    def mark_announced(self, slug: str) -> bool:
        """Record a successful announce, only if the tenant is still running.

        The announce call waits on a hub for seconds. Writing `status='running'`
        back unconditionally resurrected a row that was stopped meanwhile: its
        container and network were gone, and the address it pointed at could be
        handed to another container.
        """
        return (
            self.backend.execute_write(
                "UPDATE tenants SET announced=1, last_error='', updated_at=? "
                "WHERE slug=? AND status='running'",
                (time.time(), slug),
            )
            > 0
        )

    def record_error(self, slug: str, error: str) -> None:
        """Note an error on the row without changing whether it is served."""
        self.backend.execute_write(
            "UPDATE tenants SET last_error=?, updated_at=? WHERE slug=?",
            (error[:500], time.time(), slug),
        )

    def set_running(self, slug: str, listen_url: str) -> None:
        """Mark a tenant served at `listen_url`, touching nothing else on the row.

        Reconcile used to write back a whole row snapshot read earlier, which
        could undo an announce recorded in between."""
        self.backend.execute_write(
            "UPDATE tenants SET status='running', listen_url=?, last_error='', updated_at=? "
            "WHERE slug=?",
            (listen_url, time.time(), slug),
        )

    def close(self) -> None:
        self.backend.close()


def _row(row: dict[str, Any]) -> TenantRow:
    return TenantRow(
        slug=row["slug"],
        status=row["status"],
        capability=json.loads(row["capability_json"]),
        source_kind=row["source_kind"],
        owner_pubkey=row["owner_pubkey"],
        listen_url=row["listen_url"],
        public_url=row["public_url"],
        announced=int(row["announced"]),
        note=row["note"],
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        last_error=row["last_error"],
        image_digest=row["image_digest"],
        payout_address=row["payout_address"] or "",
    )
