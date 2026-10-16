"""HESTIA's public face: the landing page, health, the well-known document, the signed
manifest, search, the hearth description and the compute catalogue.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from hestia import __version__
from hestia.compute import (
    COMPUTE_RUN,
    COMPUTE_VERIFY,
    Limits,
    METHODS,
    published_functions,
    REPLICAS,
    runtime_facts,
)
from hestia.config import Settings
from hestia.names import DOMAIN, holder_of
from hestia.runtime.stub import StubRuntime
from hestia.runtime.wasm import WasmRuntime
from hestia.signing import ProviderSigner
from hestia.store import TenantStore


def register(
    *,
    _offered: Callable[..., Any],
    _suspended: Callable[..., Any],
    app: FastAPI,
    compute_limits: Limits,
    landing_dir: Any,
    settings: Settings,
    signer: ProviderSigner,
    store: TenantStore,
    stub: StubRuntime | WasmRuntime,
) -> SimpleNamespace:
    """Attach the landing, discovery and catalogue routes to ``app``."""
    # app.py's module-level helpers; imported here because app.py imports this module.
    from hestia.app import (
        CAPS,
        PRODUCT_ID,
    )

    @app.get("/")
    def landing() -> FileResponse:
        index = landing_dir / "index.html"
        if not index.is_file():
            raise HTTPException(status_code=404, detail="landing missing")
        return FileResponse(index)

    @app.get("/hearth.js")
    def landing_hearth_js() -> FileResponse:
        script = landing_dir / "hearth.js"
        if not script.is_file():
            raise HTTPException(status_code=404, detail="landing missing")
        return FileResponse(script, media_type="text/javascript")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "service": "hestia",
            "version": __version__,
            "runtime": settings.runtime,
            "profile": settings.profile,
            "ledger": store.backend_type,
            "payments": "on" if settings.payments_enabled else "off",
            "tenants": store.count(),
        }

    @app.get("/.well-known/ai-market.json")
    def well_known() -> dict[str, Any]:
        document: dict[str, Any] = {
            "protocol": "aimarket/2",
            # A hub validates this document against a schema that REQUIRES the plural
            # list. Publishing only the singular `protocol` failed `well_known_schema`
            # on the federation assay ("'protocol_versions' is a required property"),
            # and a failed assay is never approved, so the hearth's agents could not
            # reach a catalogue however well the manifest itself was formed.
            "protocol_versions": ["v2"],
            "name": "HESTIA",
            "hub_url": settings.public_base,
            # The crawler pins a peer by `signer_public_key` and finds the
            # catalogue through `manifest_url`. Without them a hub cannot pin
            # this hearth, and an unpinned peer is never indexed.
            "signer_public_key": signer.public_key_b64,
            "manifest_url": f"{settings.public_base}/ai-market/v2/manifest",
            # Hub routing reads `mcp_endpoint` as "POST the v2 envelope here".
            # Without it a Hub falls back to `/capabilities/{product}/{cap}/invoke`,
            # which this host does not serve — a paid catalogue sale then 502s
            # after the seller has already been paid on-chain.
            "mcp_endpoint": f"{settings.public_base}/ai-market/v2/invoke",
            "provider_pubkey": signer.public_key_b64,
            "capabilities": [c["capability_id"] for c in CAPS]
            + [c["capability_id"] for c in _compute_caps()],
            # Pending-hub rows show this number as "claimed"; absent, every hub
            # displayed "claimed 0" for a hearth that sells host + tenant tools.
            "capabilities_count": len(CAPS) + len(_compute_caps()) + len(_tenant_tools()),
        }
        # Signed whole, hybrid when HESTIA_PQC is on: a hub pins the PQ key out of
        # this block, and relays a peer's gossip only when the document verifies.
        document["signature"] = signer.sign_object(document)
        return document

    def _tenant_tools() -> list[dict[str, Any]]:
        """Running tenants, in the shape a hub indexes.

        A hub stores no per-tool URL: it routes by calling this peer's single
        invoke endpoint with a capability_id, so every tool advertises the same
        `invoke_url` and dispatch happens here.

        The gate is "running", not "announced". `/v1/hearth` already publishes
        every running tenant, so gating the manifest on a hub handshake would
        only make two public views of this hearth disagree — and `announced`
        cannot even be set on a hearth with no HESTIA_HUB_URL, which is how the
        reference host is configured. What a hub does with this document is
        still its own decision: it indexes a peer only after the operator there
        pins it.
        """
        tools = []
        suspended = _suspended()
        proven = _proven_domains()
        for row in store.list_running():
            if not _offered(row, suspended):
                continue  # runs at its own door; not offered to hubs until reviewed or listed
            cap = dict(row.capability)
            cap.pop("provider_pubkey", None)
            tool = {
                **cap,
                "invoke_url": f"{settings.public_base}/ai-market/v2/invoke",
                "hearth_url": row.public_url,
                "provider_pubkey": signer.public_key_b64,
                "payout_address": row.payout_address,
                "publisher_id": row.payout_address or cap.get("publisher_id") or "hestia",
            }
            owned = proven.get(holder_of(row.owner_pubkey))
            if owned:
                # A domain its owner proved to this hearth (hestia/domains.py): a buyer can
                # check who answers for the agent, which a free-text publisher_id cannot say.
                tool["publisher_domain"] = owned[0]
            tools.append(tool)
        return tools

    def _proven_domains() -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for name in store.names(DOMAIN):
            out.setdefault(name["holder"], []).append(name["name"])
        return {holder: sorted(names) for holder, names in out.items()}

    def _compute_caps() -> list[dict[str, Any]]:
        """The two compute SKUs, only on a hearth that serves them.

        `payout_address` is the dedicated compute wallet: a hub's seller_pay_to
        reads it, so the hub's own 402 names it and the hub stays the only till.
        Without it (hub-key-only hearths) a host cap has no payee on the market
        rail and the hub can only sell it on credits. The published limits are
        the price's terms: the hub bills per call, never per CPU-second, so a
        buyer needs to know what one call can use.
        """
        if not settings.compute_enabled:
            return []
        payee = settings.compute_payout_address
        input_schema = {
            "type": "object",
            "required": ["slug", "input"],
            "properties": {
                "slug": {"type": "string", "description": "a function listed by GET /v1/compute"},
                "function_sha256": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                    "description": "refuse the call unless the function is exactly these bytes",
                },
                "input": {"type": "object", "description": "what handle(payload) receives"},
            },
        }
        caps = [
            {
                "capability_id": COMPUTE_RUN,
                "name": "Metered compute (one replica)",
                "description": (
                    "Run an operator-published pure function (GET /v1/compute) once on your "
                    "input, under the published CPU, memory and wall-clock limits. Returns the "
                    "output and a signed receipt binding the function, input and output digests "
                    "to kernel-measured CPU time and peak memory. Your data, never your code."
                ),
                "price_per_call_usd": settings.compute_run_price_usd,
            },
            {
                "capability_id": COMPUTE_VERIFY,
                "name": "Replicated compute (two replicas)",
                "description": (
                    "Run the function twice in parallel with different hash seeds and sign "
                    "only if both produce identical output bytes; otherwise ok:false, "
                    "replicas_disagree, nothing signed. One operator runs both replicas: a "
                    "consistency check, not third-party verification (see docs/COMPUTE.md)."
                ),
                "price_per_call_usd": settings.compute_verify_price_usd,
            },
        ]
        for cap in caps:
            cap.update(
                {
                    "product_id": PRODUCT_ID,
                    "input_schema": input_schema,
                    "output_schema": {"type": "object"},
                    "limits": compute_limits.public(),
                    "replicas": REPLICAS[cap["capability_id"]],
                    "method": METHODS[cap["capability_id"]],
                    "payout_address": payee,
                    "publisher_id": payee or "hestia",
                }
            )
        return caps

    @app.get("/ai-market/v2/manifest")
    def manifest() -> dict[str, Any]:
        tools = []
        for cap in CAPS:
            tools.append(
                {
                    **cap,
                    "product_id": PRODUCT_ID,
                    "invoke_url": f"{settings.public_base}/ai-market/v2/invoke",
                    "publisher_id": "hestia",
                    "provider_pubkey": signer.public_key_b64,
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object"},
                }
            )
        for cap in _compute_caps():
            tools.append(
                {
                    **cap,
                    "invoke_url": f"{settings.public_base}/ai-market/v2/invoke",
                    "provider_pubkey": signer.public_key_b64,
                }
            )
        tools.extend(_tenant_tools())
        document: dict[str, Any] = {
            "protocol": "aimarket/2",
            "protocol_version": "v2",
            "name": "HESTIA",
            "hub_url": settings.public_base,
            "total_capabilities": len(tools),
            "capabilities_count": len(tools),
            # Freshness is signed: a hub rejects a replayed manifest by its age.
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "tools": tools,
            "by_hub": {},
        }
        document["signature"] = signer.sign_manifest(document)
        return document

    @app.get("/ai-market/v2/search")
    def search(q: str = "") -> dict[str, Any]:
        needle = q.lower().strip()
        tools = [
            t
            for t in manifest()["tools"]
            if not needle
            or needle in t["capability_id"]
            or needle in t["name"].lower()
            or needle in t["description"].lower()
        ]
        return {"total": len(tools), "tools": tools}

    @app.get("/v1/hearth")
    def hearth() -> dict[str, Any]:
        owners = {o["pubkey"]: o for o in store.list_owners()}
        proven = _proven_domains()
        rows = []
        for row in store.list_running():
            owner_record = owners.get(holder_of(row.owner_pubkey))
            if owner_record is not None and owner_record["status"] != "active":
                continue  # a suspended owner's agents are not hosted here while suspended
            item = row.public_dict()
            # Whose agent this is: the hearth operator's own (no admitted owner — the reference
            # agents carry the placeholder key), or an admitted owner's, by the label the
            # operator gave it. A dashboard counts "ours" against "third-party" from this.
            item["owner"] = ({"kind": "owner", "label": str(owner_record.get("label") or "")[:80]}
                             if owner_record else {"kind": "operator"})
            if owner_record and proven.get(owner_record["pubkey"]):
                item["owner"]["domains"] = proven[owner_record["pubkey"]]
            rows.append(item)
        by_owner = sum(1 for r in rows if r["owner"]["kind"] == "owner")
        return {"ok": True, "hearth": settings.public_base, "tenants": rows,
                "running": len(rows), "operator_tenants": len(rows) - by_owner,
                "owner_tenants": by_owner}

    @app.get("/v1/compute")
    def compute_catalogue() -> dict[str, Any]:
        """What compute this hearth sells: functions, their digests, limits, prices.

        Free and signed. A buyer pins `function_sha256` from here, and a
        cross-hearth check compares it with another hearth's copy of the same
        function before paying either of them.
        """
        if not settings.compute_enabled:
            return {"ok": True, "enabled": False}
        document: dict[str, Any] = {
            "ok": True,
            "enabled": True,
            "hearth": settings.public_base,
            "provider_pubkey": signer.public_key_b64,
            "capabilities": [
                {
                    "capability_id": cap["capability_id"],
                    "price_per_call_usd": cap["price_per_call_usd"],
                    "replicas": cap["replicas"],
                    "method": cap["method"],
                }
                for cap in _compute_caps()
            ],
            "limits": compute_limits.public(),
            "payout_address": settings.compute_payout_address,
            "same_operator": True,
            **runtime_facts(),
            "functions": [fn.public() for fn in published_functions(settings, store, stub)],
        }
        document["signature"] = signer.sign_object(document)
        return document

    return SimpleNamespace(
        _compute_caps=_compute_caps,
        hearth=hearth,
    )
