"""Who may use which name on this hearth: capability families, products and publishers.

A hub indexes this hearth's manifest and shows buyers what it says — the capability id, the
product it belongs to, the publisher who answers for it — and the routed invoke dispatches by
capability id. So these names are what a stranger would borrow to pass an agent off as one
buyers already trust, or to take its routed calls. Each one is claimed by the first holder
that deploys under it and stays theirs for good: a stopped agent, a renamed one and a
suspended owner keep their names until the operator releases them
(POST /v1/admin/names/release).

Names are compared folded (hestia/listing.py): case, separators, look-alike letters and a
trailing version do not make a new family, so `merkle-proof@v2`, `merkle_proof.v2@v1` and
`Merkle.Pr0of@v1` are all the family of `merkle.proof@v1`. The publisher a hub shows is the
payout address when there is one, so a payout address is a publisher name too.

  - family     the capability id before `@`; `hestia*` is the hearth's own, closed to everyone;
  - product    product_id;
  - publisher  publisher_id and payout_address;
  - prefix     a reservation the operator makes: every family, product and publisher whose
               folded name starts with it belongs to its holder (`hestia`, plus
               HESTIA_RESERVED_PREFIXES, are the operator's at boot);
  - domain     a domain its owner proved (hestia/domains.py), zone included: every family,
               product and publisher that BEGINS with the domain — `attestedmemory.net.deal`, or
               reversed, `net.attestedmemory.deal`, as whole words, not letters — is the
               owner's. The bare word is not: attestedmemory.com may be someone else's.

A name a holder already has stays theirs when a prefix or a domain covering it is reserved
later: reservations only decide names nobody holds yet.

A holder is a canonical owner key, or "operator" for agents under the placeholder key. The
claim is the database's (a primary key on kind + folded name), so two processes on one ledger
cannot both win a name.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from hestia.listing import family_key, skeleton
from hestia.owners import OwnerProofError, canonical_owner_key

logger = logging.getLogger("hestia.names")

FAMILY = "family"
PRODUCT = "product"
PUBLISHER = "publisher"
PREFIX = "prefix"
DOMAIN = "domain"
KINDS = (FAMILY, PRODUCT, PUBLISHER, PREFIX, DOMAIN)
RESERVABLE = (FAMILY, PRODUCT, PUBLISHER, PREFIX)   # a domain comes only with its proof
OPERATOR = "operator"
HEARTH_PREFIX = "hestia"
CLOSED = skeleton(HEARTH_PREFIX)


class NameTaken(Exception):
    """A name this deploy would use belongs to another holder."""


def holder_of(owner_pubkey: str) -> str:
    """Whose names a row's agent uses: its owner's, or the operator's (placeholder key)."""
    try:
        return canonical_owner_key(owner_pubkey)
    except OwnerProofError:
        return OPERATOR


def key_for(kind: str, name: str) -> str:
    if kind == FAMILY:
        return family_key(name)
    return skeleton(name.strip().lower() if kind == DOMAIN else name)


def word_covers(word_key: str, text: str) -> bool:
    """True when `text` begins with these words as whole words: its first segments, folded and
    joined, are the key (`attested-memory.net.deal` for attestedmemory.net), not merely its
    letters (`attestedmemorynetwork.deal`)."""
    joined = ""
    for segment in re.split(r"[\W_]+", text):
        if not segment:
            continue
        joined += skeleton(segment)
        if joined == word_key:
            return True
        if not word_key.startswith(joined):
            return False
    return False


def claims_for(capability: dict[str, Any], payout_address: str = "") -> list[tuple[str, str, str]]:
    """(kind, folded key, name as written) for every name an agent would use."""
    cap_id = str(capability.get("capability_id") or "")
    out = [(FAMILY, family_key(cap_id), cap_id.split("@", 1)[0])]
    product = str(capability.get("product_id") or "").strip()
    if product:
        out.append((PRODUCT, skeleton(product), product))
    for publisher in (str(capability.get("publisher_id") or "").strip(), payout_address.strip()):
        if publisher:
            out.append((PUBLISHER, skeleton(publisher), publisher))
    seen: set[tuple[str, str]] = set()
    unique = []
    for kind, key, text in out:
        if key and (kind, key) not in seen:
            seen.add((kind, key))
            unique.append((kind, key, text))
    return unique


def _refusal(kind: str, text: str, held: dict[str, Any]) -> str:
    theirs = held.get("name") or text
    if kind == FAMILY:
        return (f"the {text} capabilities belong to another owner on this hearth"
                + ("" if theirs == text else f" (as {theirs})"))
    return (f"the {kind} {text} belongs to another owner on this hearth"
            + ("" if theirs == text else f" (as {theirs})"))


def check(store: Any, holder: str, claims: list[tuple[str, str, str]]) -> None:
    """Raise NameTaken if any of these names is closed or another holder's."""
    prefixes = store.names(PREFIX)
    proven = store.names(DOMAIN)
    for kind, key, text in claims:
        if kind == FAMILY and key.startswith(CLOSED):
            raise NameTaken(f"{text}: hestia.* is this hearth's own namespace")
        held = store.name_holder(kind, key)
        if held is not None:
            if held["holder"] != holder:
                raise NameTaken(_refusal(kind, text, held))
            continue   # already this holder's: a reservation made since does not take it back
        for prefix in prefixes:
            if key.startswith(prefix["name_key"]) and prefix["holder"] != holder:
                raise NameTaken(
                    f"{kind} names starting with '{prefix['name']}' are reserved on this hearth")
        for owned in proven:
            if owned["holder"] != holder and domain_covers(owned["name"], text):
                raise NameTaken(f"{kind} names beginning with {owned['name']} (or "
                                f"{reverse(owned['name'])}) belong to the owner of {owned['name']}")


def claim(store: Any, holder: str, claims: list[tuple[str, str, str]], *, slug: str) -> None:
    """Check, then claim every name for `holder`; all or none.

    The insert is the database's decision: if another process claimed a name between the
    check and the insert, the read-back shows the other holder and this deploy is refused,
    taking back only the names it inserted itself.
    """
    check(store, holder, claims)
    inserted = [(kind, key) for kind, key, text in claims
                if store.insert_name(kind, key, holder, text, slug)]
    for kind, key, text in claims:
        held = store.name_holder(kind, key)
        if held is None or held["holder"] != holder:
            for taken_kind, taken_key in inserted:
                store.delete_name(taken_kind, taken_key, holder=holder)
            raise NameTaken(_refusal(kind, text, held or {}))


def reverse(domain: str) -> str:
    """attestedmemory.net -> net.attestedmemory: the order Java packages and the MCP Registry
    write a domain's namespace in."""
    return ".".join(reversed(domain.split(".")))


def domain_covers(domain: str, text: str) -> bool:
    """`text` begins with the domain, in either order, zone included."""
    return word_covers(skeleton(domain), text) or word_covers(skeleton(reverse(domain)), text)


def claim_domain(store: Any, holder: str, domain: str) -> str:
    """Give a proven domain's namespace to its owner, with the domain as a publisher name.

    `domain` is canonical and its control already proven (hestia/domains.py). The namespace is
    the whole domain, zone included: attestedmemory.net and attestedmemory.com are two owners'.
    Refused when it is the hearth's own or under another holder's prefix, when another owner
    proved the same domain or one that folds to it (attestedrnemory.net), or when it would cover
    a name another holder already uses — that holder was first. Returns the domain.
    """
    key = skeleton(domain)
    if key.startswith(CLOSED):
        raise NameTaken(f"{domain}: hestia is this hearth's own name")
    for prefix in store.names(PREFIX):
        if key.startswith(prefix["name_key"]) and prefix["holder"] != holder:
            raise NameTaken(f"names starting with '{prefix['name']}' are reserved on this hearth")
    held = store.name_holder(DOMAIN, key)
    if held is not None and held["holder"] != holder:
        raise NameTaken(f"{domain} already belongs to the owner of {held['name']}")
    covered = [f"{n['kind']} {n['name']}" for n in store.names()
               if n["kind"] in (FAMILY, PRODUCT, PUBLISHER) and n["holder"] != holder
               and domain_covers(domain, n["name"])]
    if covered:
        raise NameTaken(f"{domain} would cover names another holder already uses: "
                        + ", ".join(covered[:5]) + ("…" if len(covered) > 5 else ""))
    publisher = [(PUBLISHER, key, domain)]
    check(store, holder, publisher)
    if held is None and not store.insert_name(DOMAIN, key, holder, domain, ""):
        current = store.name_holder(DOMAIN, key)
        if current is None or current["holder"] != holder:
            raise NameTaken(f"{domain} was just taken by the owner of "
                            f"{(current or {}).get('name', 'another domain')}")
    claim(store, holder, publisher, slug="")
    return domain


def domains_of(store: Any, holder: str) -> list[str]:
    return sorted(n["name"] for n in store.names(DOMAIN) if n["holder"] == holder)


def seed_prefixes(store: Any, prefixes: tuple[str, ...]) -> None:
    """The operator's reserved prefixes: `hestia` always, plus HESTIA_RESERVED_PREFIXES.
    An existing reservation is left as it is — the operator may have handed one on."""
    for name in dict.fromkeys((HEARTH_PREFIX, *prefixes)):
        key = skeleton(name)
        if key:
            store.insert_name(PREFIX, key, OPERATOR, name, "")


def backfill(store: Any) -> list[tuple[str, str]]:
    """Claim the names of rows written before names were claimed, oldest first.

    Returns (slug, reason) for every row whose names another holder already has — written
    while no rule stood between them. The caller stops those; the first holder keeps the
    name, and the operator can release it if the first holder was the squatter.
    """
    conflicts = []
    for row in sorted(store.list_all(), key=lambda r: (r.created_at, r.slug)):
        holder = holder_of(row.owner_pubkey)
        try:
            claim(store, holder, claims_for(row.capability, row.payout_address), slug=row.slug)
        except NameTaken as exc:
            conflicts.append((row.slug, str(exc)))
    return conflicts


def users_of(rows: list[Any], kind: str, key: str, holder: str) -> list[str]:
    """Slugs of `holder`'s live agents that use this name."""
    out = []
    for row in rows:
        if row.status not in {"running", "quarantined"} or holder_of(row.owner_pubkey) != holder:
            continue
        if any(k == kind and fk == key for k, fk, _ in claims_for(row.capability, row.payout_address)):
            out.append(row.slug)
    return out
