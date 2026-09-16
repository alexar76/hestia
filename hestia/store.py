"""SQLite tenant ledger. Process-local on purpose — see HESTIA_REPLICAS."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
        }


class TenantStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tenants (
                slug TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                capability_json TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                owner_pubkey TEXT NOT NULL,
                listen_url TEXT NOT NULL,
                public_url TEXT NOT NULL,
                announced INTEGER NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                last_error TEXT NOT NULL DEFAULT '',
                image_digest TEXT NOT NULL DEFAULT ''
            );
            """
        )
        self._conn.commit()

    def count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM tenants").fetchone()
        return int(row["n"])

    def get(self, slug: str) -> TenantRow | None:
        row = self._conn.execute("SELECT * FROM tenants WHERE slug = ?", (slug,)).fetchone()
        return _row(row) if row else None

    def list_running(self) -> list[TenantRow]:
        rows = self._conn.execute(
            "SELECT * FROM tenants WHERE status = 'running' ORDER BY slug"
        ).fetchall()
        return [_row(r) for r in rows]

    def list_all(self) -> list[TenantRow]:
        rows = self._conn.execute("SELECT * FROM tenants ORDER BY slug").fetchall()
        return [_row(r) for r in rows]

    def upsert(self, tenant: TenantRow) -> None:
        self._conn.execute(
            """
            INSERT INTO tenants (
                slug, status, capability_json, source_kind, owner_pubkey,
                listen_url, public_url, announced, note, created_at, updated_at,
                last_error, image_digest
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                image_digest=excluded.image_digest
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
            ),
        )
        self._conn.commit()

    def set_status(self, slug: str, status: str, *, error: str = "", announced: int | None = None) -> None:
        now = time.time()
        if announced is None:
            self._conn.execute(
                "UPDATE tenants SET status=?, last_error=?, updated_at=? WHERE slug=?",
                (status, error, now, slug),
            )
        else:
            self._conn.execute(
                "UPDATE tenants SET status=?, last_error=?, announced=?, updated_at=? WHERE slug=?",
                (status, error, announced, now, slug),
            )
        self._conn.commit()


def _row(row: sqlite3.Row) -> TenantRow:
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
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_error=row["last_error"],
        image_digest=row["image_digest"],
    )
