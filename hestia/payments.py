"""Non-custodial payment verification for priced tenants.

The buyer pays the TENANT OWNER directly, on chain. This hearth never holds a
key, never takes a cut and never touches the money: it reads the chain and
decides whether a call has been paid for. That is the only arrangement where an
operator can host other people's providers without becoming a money transmitter
for them.

Verification is deliberately narrow — an ERC-20 Transfer, to the address the
tenant published, of at least the asking price, confirmed, and not already
spent on another call.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

# keccak256("Transfer(address,address,uint256)")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
# keccak256("AuthorizationUsed(address,bytes32)") — EIP-3009. The token contract
# emits this when it accepts a `transferWithAuthorization`, and the nonce it
# records is the one the payer SIGNED. That is what binds a payment to one call:
# the hearth mints the nonce, so a transfer carrying it cannot be a transfer that
# was made for something else.
AUTHORIZATION_USED_TOPIC = (
    "0x98de503528ee59b575ef0c0a2576a82497bfc029a5685b209e9ec333479b10a5"
)

_TX_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_NONCE = re.compile(r"^0x[0-9a-fA-F]{64}$")


def is_nonce(value: str) -> bool:
    return bool(_NONCE.match((value or "").strip()))


class PaymentError(RuntimeError):
    """The call has not been paid for. Carries a reason the buyer can act on."""


@dataclass(frozen=True)
class PaymentTerms:
    """What a buyer must do, in the units the chain uses."""

    chain: str
    token: str
    token_contract: str
    decimals: int
    pay_to: str
    amount_units: int
    amount_usd: float
    min_confirmations: int
    chain_id: int = 8453
    eip712_name: str = "USD Coin"
    eip712_version: str = "2"

    def as_x402_accept(self) -> dict[str, Any]:
        """One entry of an x402 `accepts` array.

        The shape the hub's own x402 module emits, so a client that already
        speaks x402 to the hub needs no second code path for a hearth.
        """
        return {
            "scheme": "exact",
            "network": self.chain,
            "maxAmountRequired": str(self.amount_units),
            "asset": self.token_contract,
            "payTo": self.pay_to,
            "resource": "",
            "description": f"{self.amount_usd} {self.token}",
            "mimeType": "application/json",
            "maxTimeoutSeconds": 300,
            # `extra` carries the EIP-712 domain of the asset, which is what a
            # buyer needs to sign a transferWithAuthorization the token will
            # accept. x402 clients already read name/version from here.
            "extra": {
                "name": self.eip712_name,
                "version": self.eip712_version,
                "decimals": self.decimals,
                "symbol": self.token,
                "chainId": self.chain_id,
                "verifyingContract": self.token_contract,
            },
        }


def is_address(value: str) -> bool:
    return bool(_ADDRESS.match((value or "").strip()))


def to_units(amount_usd: float, decimals: int) -> int:
    """Dollars → token base units, rounded up.

    Rounding up by design: rounding a $0.0015 call down to 1500 units would let
    a buyer underpay by a hair on every single call, and the provider carries
    the loss. The buyer overpays by at most one base unit.
    """
    scaled = amount_usd * (10**decimals)
    units = int(scaled)
    return units + 1 if scaled - units > 1e-9 else units


def endpoints(rpc_url: str) -> list[str]:
    """Split the configured value into endpoints, in order of preference."""
    return [part.strip() for part in (rpc_url or "").split(",") if part.strip()]


def _rpc(url: str, method: str, params: list[Any], timeout: float) -> Any:
    response = httpx.post(
        url,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=timeout,
    )
    response.raise_for_status()
    body = response.json()
    if "error" in body:
        raise PaymentError(f"chain rpc refused {method}: {body['error']}")
    return body.get("result")


def _topic_address(topic: str) -> str:
    """A 32-byte log topic carries a 20-byte address in its low bytes."""
    return "0x" + (topic or "")[-40:]


def verify_transfer(
    *,
    rpc_url: str,
    tx_hash: str,
    terms: PaymentTerms,
    timeout: float = 10.0,
    max_age_s: int = 0,
    require_nonce: str = "",
) -> dict[str, Any]:
    """Confirm `tx_hash` paid `terms`. Raises PaymentError with the reason.

    Returns the settled facts — never the caller's claim about them.

    `max_age_s`, when > 0, refuses a transfer whose block is older than that many
    seconds. On its own a raw ERC-20 transfer carries nothing binding it to THIS
    call — no nonce, no sender restriction — so if the payout address also
    receives funds from other sources (the shared treasury does), an old,
    unrelated transfer of at least the price could otherwise be replayed as a
    free call. A freshness window shrinks that to transfers made in the last few
    minutes; on its own it is a mitigation, not a binding. Fail-closed: if the
    block's age cannot be read, the payment is refused rather than assumed fresh.

    `require_nonce`, when set, is what actually binds the payment to ONE call:
    the transaction must carry an EIP-3009 `AuthorizationUsed` log from the token
    contract for exactly that nonce. The payer signed the nonce the hearth minted
    when it answered 402, and the token contract verified that signature and
    recorded it — so the hearth needs no key, no gas and no signature-recovery
    library to know this payment was made for this call, and a transfer made for
    anything else (including an unrelated deposit to the same shared payout
    address) can never satisfy it.
    """
    if not _TX_HASH.match((tx_hash or "").strip()):
        raise PaymentError("payment reference must be a 0x transaction hash")
    tx_hash = tx_hash.strip().lower()

    # Try each endpoint until one HAS the receipt. A provider that 403s a method
    # (publicnode refuses eth_getTransactionReceipt) or lags a block behind would
    # otherwise silently break every paid call — one endpoint is a single point
    # of failure for the money path. A node that answers "no such transaction"
    # is not authoritative either: another may already have it.
    urls = endpoints(rpc_url)
    if not urls:
        raise PaymentError("no chain endpoint is configured")
    receipt = None
    used = ""
    transport_error: Exception | None = None
    for url in urls:
        try:
            found = _rpc(url, "eth_getTransactionReceipt", [tx_hash], timeout)
        except (httpx.HTTPError, PaymentError) as exc:
            transport_error = exc
            continue
        if found:
            receipt, used = found, url
            break
    if receipt is None:
        if transport_error is not None and len(urls) == 1:
            raise transport_error
        raise PaymentError("transaction is not on chain yet")
    if str(receipt.get("status", "")).lower() not in ("0x1", "1"):
        raise PaymentError("transaction reverted")

    head = int(_rpc(used, "eth_blockNumber", [], timeout), 16)
    mined = int(receipt.get("blockNumber", "0x0"), 16)
    confirmations = max(0, head - mined + 1)
    if confirmations < terms.min_confirmations:
        raise PaymentError(
            f"payment has {confirmations} confirmation(s); "
            f"{terms.min_confirmations} required"
        )

    if max_age_s > 0:
        # Ask the SAME endpoint that served the receipt for the block, so a node
        # that answered the receipt answers this too. Fail-closed on anything
        # unreadable: a payment whose age we cannot establish is refused.
        block = _rpc(used, "eth_getBlockByNumber", [hex(mined), False], timeout)
        raw_ts = (block or {}).get("timestamp")
        if raw_ts is None:
            raise PaymentError("cannot establish payment age; refusing")
        try:
            mined_at = int(str(raw_ts), 16)
        except (TypeError, ValueError) as exc:
            raise PaymentError("cannot establish payment age; refusing") from exc
        age = time.time() - mined_at
        if age > max_age_s:
            raise PaymentError(
                f"payment is {int(age)}s old; must be within {max_age_s}s of the "
                "call (an old transfer cannot be replayed as a fresh payment)"
            )

    want_to = terms.pay_to.lower()
    want_token = terms.token_contract.lower()
    paid = 0
    authorizer = ""
    nonce_seen = False
    want_nonce = (require_nonce or "").strip().lower()
    for log in receipt.get("logs") or []:
        topics = log.get("topics") or []
        if not topics:
            continue
        if str(log.get("address", "")).lower() != want_token:
            continue
        topic0 = (topics[0] or "").lower()
        if want_nonce and topic0 == AUTHORIZATION_USED_TOPIC and len(topics) >= 3:
            # topics[2] is the bytes32 nonce the payer signed and the token
            # contract verified. Matching it is the whole binding.
            if (topics[2] or "").lower() == want_nonce:
                nonce_seen = True
                authorizer = _topic_address(topics[1]).lower()
            continue
        if topic0 != TRANSFER_TOPIC or len(topics) < 3:
            continue
        if _topic_address(topics[2]).lower() != want_to:
            continue
        # Sum every matching transfer in the tx: a router may split one payment
        # across several Transfer events to the same recipient.
        paid += int(log.get("data") or "0x0", 16)

    if want_nonce and not nonce_seen:
        raise PaymentError(
            "payment is not bound to this call: the transaction carries no "
            "EIP-3009 authorization for the nonce this hearth issued. Pay with "
            "transferWithAuthorization using the nonce from the 402."
        )

    if paid == 0:
        raise PaymentError(
            f"no {terms.token} transfer to {terms.pay_to} found in that transaction"
        )
    if paid < terms.amount_units:
        raise PaymentError(
            f"paid {paid} base units, price is {terms.amount_units}"
        )
    return {
        "tx_hash": tx_hash,
        "paid_units": paid,
        "confirmations": confirmations,
        "block_number": mined,
        "chain": terms.chain,
        "token": terms.token,
        "pay_to": terms.pay_to,
        "nonce": want_nonce,
        "authorizer": authorizer,
        "verified_at": time.time(),
    }
