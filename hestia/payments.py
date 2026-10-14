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

import hashlib
import hmac
import re
import secrets
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


# ------------------------------------------------------------ who paid
#
# A nonce binds a payment to one call, but it cannot say WHO may redeem it: the
# token contract publishes it in AuthorizationUsed the moment the transfer is
# mined, next to the transaction hash. Anyone watching the chain then holds both
# halves of a key-less redemption and can present them before the buyer does —
# the buyer who paid is refused as "already spent".
#
# So the nonce is a COMMITMENT: nonce = sha256(secret), where the secret goes only
# to whoever asked for the quote (or is chosen by a direct buyer). The chain shows
# the commitment, never the secret, and a key-less redemption must open it. The
# hearth can check that from the chain alone — no state shared with anyone, and
# no signature-recovery library.


def mint_secret() -> str:
    return "0x" + secrets.token_hex(32)


def is_secret(value: str) -> bool:
    return bool(_NONCE.match((value or "").strip()))


def nonce_for_secret(secret: str) -> str:
    """The EIP-3009 nonce a payment secret commits to: sha256 of its 32 bytes."""
    raw = bytes.fromhex(secret.strip()[2:])
    return "0x" + hashlib.sha256(raw).hexdigest()


def opens(secret: str, nonce: str) -> bool:
    """True when `secret` is the preimage of `nonce` (constant-time compare)."""
    if not (is_secret(secret) and is_nonce(nonce)):
        return False
    return hmac.compare_digest(nonce_for_secret(secret), nonce.strip().lower())


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
    require_nonce: str,
) -> dict[str, Any]:
    """Confirm `tx_hash` paid `terms`. Raises PaymentError with the reason.

    Returns the settled facts — never the caller's claim about them.

    `require_nonce` is what binds the payment to ONE call, and it is not optional: the
    transaction must carry an EIP-3009 `AuthorizationUsed` log from the token contract
    for exactly that nonce, and only the transfer that authorization moved pays. The
    payer signed the nonce the hearth minted when it answered 402 (or the hub minted, for
    a forwarded payment), and the token contract verified that signature and recorded it
    — so the hearth needs no key, no gas and no signature-recovery library to know this
    payment was made for this call. A plain transfer to the payout address carries none
    of that: it is money from somebody, for something, and its hash is public once mined.

    `max_age_s`, when > 0, refuses a transfer whose block is older than that many
    seconds. Fail-closed: if the block's age cannot be read, the payment is refused
    rather than assumed fresh.
    """
    if not _TX_HASH.match((tx_hash or "").strip()):
        raise PaymentError("payment reference must be a 0x transaction hash")
    tx_hash = tx_hash.strip().lower()
    want_nonce = (require_nonce or "").strip().lower()
    if not is_nonce(want_nonce):
        raise PaymentError(
            "payment is not bound to a call: pay with transferWithAuthorization signed "
            "over the nonce from the 402"
        )

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
    token_logs = [
        log for log in receipt.get("logs") or []
        if (log.get("topics") or []) and str(log.get("address", "")).lower() == want_token
    ]
    paid = 0
    authorizer = ""
    nonce_seen = False
    for index, log in enumerate(token_logs):
        topics = log.get("topics") or []
        topic0 = (topics[0] or "").lower()
        if topic0 == AUTHORIZATION_USED_TOPIC and len(topics) >= 3:
            who = _topic_address(topics[1]).lower()
            if (topics[2] or "").lower() == want_nonce:
                # topics[2] is the bytes32 nonce the payer signed and the token contract
                # verified. Matching it binds the payment to this call — but only the
                # transfer THAT authorization moved may pay for it. FiatToken marks the
                # authorization used, then transfers: the token's very next log. Summing
                # every transfer to the payee let anyone holding a buyer's signed
                # authorization before it was mined bundle it with a 1-unit authorization
                # of their own and redeem the buyer's money under their own nonce.
                #
                # And every match counts, not the first: EIP-3009 nonces are per signer,
                # so anyone can sign a 1-unit authorization over the SAME nonce and put
                # it ahead of the buyer's in a bundle. The best-paying match is the one
                # that settles.
                nonce_seen = True
                moved = token_logs[index + 1] if index + 1 < len(token_logs) else {}
                moved_topics = moved.get("topics") or []
                if (
                    len(moved_topics) >= 3
                    and (moved_topics[0] or "").lower() == TRANSFER_TOPIC
                    and _topic_address(moved_topics[1]).lower() == who
                    and _topic_address(moved_topics[2]).lower() == want_to
                ):
                    units = int(moved.get("data") or "0x0", 16)
                    if units > paid:
                        paid, authorizer = units, who

    if not nonce_seen:
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
