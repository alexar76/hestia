"""Is a priced agent actually paid? The answer a seller must hear at deploy, not from a stranger.

A call is billed only when payments are on, the agent is priced above zero AND it names a payout
address (hestia.app_routes.invoke._terms_for): with no address the hearth serves every call free
rather than bill nobody. That is the safe default for a buyer, and a silent loss for a seller who
meant to sell: the first stranger's agent (Blackwell Case Work, 2026-11-01) sat priced at $0.001
with no address for three weeks. Hub billing (hestia.hub_billing) is the other way to be paid: a
hub the owner chose bills its own buyers and credits the owner, so only direct calls are free then.
"""

from __future__ import annotations

from typing import Iterable

from hestia.payments import is_address


def unpaid_reason(price_usd: float, payout_address: str, *, payments_enabled: bool,
                  billing_hubs: Iterable[str] = ()) -> str | None:
    """Why calls to a priced agent go unpaid, or None when nothing is lost.

    None also when payments are off on this hearth: that is the operator's choice for every agent,
    not something an owner can fix by redeploying."""
    price = float(price_usd or 0)
    if price <= 0 or not payments_enabled or is_address(payout_address or ""):
        return None
    hubs = sorted(set(billing_hubs))
    if hubs:
        return (f"priced at ${price:g} per call with no payout_address: calls straight to this hearth "
                f"are served free; calls through {', '.join(hubs)} are billed there")
    return (f"priced at ${price:g} per call with no payout_address and no hub chosen to bill through: "
            "every call is served free. Redeploy with payout_address (your own wallet; buyers pay it "
            "directly in USDC, the hearth holds nothing and takes no cut), or choose a hub with "
            "hestia-owner billing")


def agent_unpaid_reason(store, settings, *, capability: dict, payout_address: str,
                        owner_pubkey: str) -> str | None:
    """unpaid_reason for one agent on this hearth: the operator's agents may be billed by every hub
    the hearth holds a key for; an owner's by the hubs it chose (POST /v1/owners/me/billing)."""
    from hestia.names import OPERATOR, holder_of

    configured = [url for url, _ in settings.tenant_hub_keys]
    holder = holder_of(owner_pubkey)
    if holder == OPERATOR:
        hubs = configured
    else:
        chosen = store.owner_billing(holder)
        hubs = [url for url in configured if chosen.get(url)]
    return unpaid_reason(float(capability.get("price_per_call_usd") or 0), payout_address,
                         payments_enabled=settings.payments_enabled, billing_hubs=hubs)
