"""The hearth's pool of free public JSON-RPC endpoints, behind one name per chain.

Agents that read a chain all reach the network from the hearth's one address, so a public RPC's
per-address limit (mainnet.base.org: 25 calls a second, each call in a batch counted) is shared by
every agent and every caller. One agent that prices a token makes about fifty calls; a few buyers at
once and every chain-reading agent starts to answer "rate limited".

So the hearth answers for a chain under a name of its own (``base-rpc.hestia.modelmarket.dev``) that
an agent declares like any egress host. A request to that name never leaves for DNS: the pool

- answers ``eth_chainId`` itself, and a call pinned to a block number (``eth_call`` at a block,
  ``eth_getBlockByNumber`` with a number, ``eth_getLogs`` between two numbers, a receipt or block by
  hash once it exists) from a cache: the same call at the same block has the same answer, so a
  repeat costs no upstream call;
- sends the rest to the free endpoints in turn, each within its own calls-per-second budget, and
  ``eth_getLogs`` only to the ones that serve that many blocks in one call;
- moves a call that an endpoint refused (rate limit, range too large, any error that is not the
  contract's own revert) to the next endpoint, and rests an endpoint that answered 429, 5xx or not at
  all for ``REST_S`` seconds.

Only read methods are passed on (no transaction is ever sent from the hearth's address). Every
upstream request goes out through the egress's own client: HTTPS to a checked public address, no
redirects, responses cut at the egress cap. The pool holds no key: it is free endpoints only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

logger = logging.getLogger("hestia.rpc_pool")

READ_METHODS = frozenset({
    "eth_chainId", "net_version", "eth_blockNumber", "eth_gasPrice", "eth_maxPriorityFeePerGas",
    "eth_feeHistory", "eth_getBlockByNumber", "eth_getBlockByHash", "eth_getLogs", "eth_call",
    "eth_getTransactionReceipt", "eth_getTransactionByHash", "eth_getCode", "eth_getBalance",
    "eth_getStorageAt", "eth_getTransactionCount", "eth_estimateGas",
})
MAX_CALLS = 100  # one request from an agent
CACHE_TTL_S = 600.0  # a pinned answer older than this is read again (reorgs, bounded memory)
CACHE_MAX_ENTRIES = 20_000
CACHE_MAX_BYTES = 64 * 1024 * 1024
REST_S = 30.0
WAIT_S = 2.0  # longest a request waits for an endpoint's budget to refill

#: The free Base endpoints that answered a batch of eth_chainId, eth_call and eth_getLogs on
#: 2026-11-22. logs_range: the most blocks one eth_getLogs may span there.
DEFAULT_POOLS: dict[str, dict[str, Any]] = {
    "base-rpc.hestia.modelmarket.dev": {
        "chain_id": "0x2105",
        "upstreams": [
            {"url": "https://base-rpc.publicnode.com", "per_second": 15, "logs_range": 50_000},
            {"url": "https://mainnet.base.org", "per_second": 20, "logs_range": 0},
            {"url": "https://base.meowrpc.com", "per_second": 10, "logs_range": 10_000},
            {"url": "https://base.gateway.tenderly.co", "per_second": 10, "logs_range": 1_000},
            {"url": "https://base-mainnet.public.blastapi.io", "per_second": 10, "logs_range": 10_000},
        ],
    },
}


def _num(value: Any) -> int | None:
    """A block number given as hex (0x…) or None for a tag (latest, safe, pending…)."""
    if isinstance(value, str) and value.startswith("0x"):
        try:
            return int(value, 16)
        except ValueError:
            return None
    return None


def cacheable(method: str, params: Any) -> bool:
    """True when the answer depends only on the call itself, not on when it is asked."""
    p = params if isinstance(params, list) else []
    if method in ("eth_call", "eth_getCode", "eth_getBalance", "eth_getTransactionCount"):
        return len(p) >= 2 and _num(p[-1]) is not None
    if method == "eth_getStorageAt":
        return len(p) >= 3 and _num(p[2]) is not None
    if method == "eth_getBlockByNumber":
        return len(p) >= 1 and _num(p[0]) is not None
    if method == "eth_getLogs":
        f = p[0] if p and isinstance(p[0], dict) else {}
        return bool(f.get("blockHash")) or (_num(f.get("fromBlock")) is not None
                                             and _num(f.get("toBlock")) is not None)
    return method in ("eth_getBlockByHash", "eth_getTransactionReceipt", "eth_getTransactionByHash")


def logs_span(method: str, params: Any) -> int:
    """Blocks one eth_getLogs spans (0 for anything else or a blockHash filter)."""
    if method != "eth_getLogs" or not isinstance(params, list) or not params or not isinstance(params[0], dict):
        return 0
    f = params[0]
    lo, hi = _num(f.get("fromBlock")), _num(f.get("toBlock"))
    if f.get("blockHash"):
        return 0
    if lo is None or hi is None:
        return 1 << 30  # an open range: only an endpoint that serves the widest
    return max(0, hi - lo + 1)


class Cache:
    """Answers to pinned calls, newest kept, bounded by count and bytes. Thread-safe."""

    def __init__(self, *, ttl_s: float = CACHE_TTL_S, max_entries: int = CACHE_MAX_ENTRIES,
                 max_bytes: int = CACHE_MAX_BYTES) -> None:
        self.ttl_s, self.max_entries, self.max_bytes = ttl_s, max_entries, max_bytes
        self._items: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()
        self.hits = self.misses = 0

    def get(self, key: str, now: float | None = None) -> Any:
        now = time.monotonic() if now is None else now
        with self._lock:
            item = self._items.get(key)
            if item is None or item[0] < now:
                if item is not None:
                    self._drop(key)
                self.misses += 1
                return _MISSING
            self._items.move_to_end(key)
            self.hits += 1
            return json.loads(item[1])

    def put(self, key: str, result: Any, now: float | None = None) -> None:
        text = json.dumps(result, separators=(",", ":"))
        if len(text) > self.max_bytes // 64:
            return
        now = time.monotonic() if now is None else now
        with self._lock:
            if key in self._items:
                self._drop(key)
            self._items[key] = (now + self.ttl_s, text)
            self._bytes += len(text) + len(key)
            while self._items and (len(self._items) > self.max_entries or self._bytes > self.max_bytes):
                self._drop(next(iter(self._items)))

    def _drop(self, key: str) -> None:
        _, text = self._items.pop(key)
        self._bytes -= len(text) + len(key)


_MISSING = object()


@dataclass
class Upstream:
    url: str
    per_second: float
    logs_range: int = 0
    tokens: float = 0.0
    stamp: float = 0.0
    resting_until: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def host(self) -> str:
        return (urlsplit(self.url).hostname or "").lower()

    def available(self, now: float) -> float:
        with self.lock:
            self._refill(now)
            return 0.0 if now < self.resting_until else self.tokens

    def take(self, n: int, now: float) -> bool:
        with self.lock:
            self._refill(now)
            if now < self.resting_until or self.tokens < n:
                return False
            self.tokens -= n
            return True

    def rest(self, now: float) -> None:
        with self.lock:
            self.resting_until = now + REST_S

    def _refill(self, now: float) -> None:
        burst = max(1.0, self.per_second * 2)
        if self.stamp == 0.0:
            self.tokens, self.stamp = burst, now
            return
        self.tokens = min(burst, self.tokens + (now - self.stamp) * self.per_second)
        self.stamp = now


Send = Callable[[str, str, str], Awaitable[dict[str, Any]]]  # (host, url, body) -> egress reply


def _retryable(error: Any) -> bool:
    """An error another endpoint may not give: anything but the contract's own revert."""
    text = json.dumps(error).lower()
    return "revert" not in text


def _reply(status: int, payload: Any) -> dict[str, Any]:
    return {"status": status, "headers": {"content-type": "application/json"},
            "text": json.dumps(payload, separators=(",", ":"))}


class Chain:
    def __init__(self, name: str, chain_id: str, upstreams: list[Upstream], cache: Cache) -> None:
        self.name, self.chain_id, self.upstreams, self.cache = name, chain_id, upstreams, cache
        self._turn = 0
        self.upstream_calls = 0

    def _key(self, method: str, params: Any) -> str:
        return self.name + "|" + method + "|" + json.dumps(params, sort_keys=True, separators=(",", ":"))

    def _order(self, span: int, now: float) -> list[Upstream]:
        able = [u for u in self.upstreams if span == 0 or u.logs_range >= span]
        self._turn += 1
        shift = self._turn % max(1, len(able))
        rotated = able[shift:] + able[:shift]
        return sorted(rotated, key=lambda u: -u.available(now) if span == 0 else -u.logs_range)

    async def answer(self, calls: list[dict[str, Any]], send: Send,
                     clock: Callable[[], float] = time.monotonic) -> list[dict[str, Any]]:
        """One answer per call, in order, with the caller's ids."""
        out: list[dict[str, Any] | None] = [None] * len(calls)
        pending: list[int] = []
        for i, c in enumerate(calls):
            method, params = c["method"], c.get("params", [])
            if method == "eth_chainId":
                out[i] = {"jsonrpc": "2.0", "id": c.get("id"), "result": self.chain_id}
            elif cacheable(method, params):
                hit = self.cache.get(self._key(method, params))
                if hit is _MISSING:
                    pending.append(i)
                else:
                    out[i] = {"jsonrpc": "2.0", "id": c.get("id"), "result": hit}
            else:
                pending.append(i)
        # Calls that need the same kind of endpoint go together: wide eth_getLogs apart.
        groups: dict[int, list[int]] = {}
        for i in pending:
            span = logs_span(calls[i]["method"], calls[i].get("params"))
            groups.setdefault(0 if span <= 1_000 else span, []).append(i)
        for span, idx in groups.items():
            await self._send_group(calls, idx, span, out, send, clock)
        return [o if o is not None else {"jsonrpc": "2.0", "id": calls[i].get("id"),
                                         "error": {"code": -32603, "message": "no endpoint answered"}}
                for i, o in enumerate(out)]

    async def _send_group(self, calls, idx, span, out, send, clock) -> None:
        left = list(idx)
        tried: set[str] = set()
        deadline = clock() + WAIT_S
        while left:
            now = clock()
            order = [u for u in self._order(span, now) if u.url not in tried]
            if not order:
                return
            upstream = next((u for u in order if u.take(len(left), now)), None)
            if upstream is None:
                if clock() >= deadline:
                    upstream = order[0]  # out of patience: the endpoint will say if it minds
                else:
                    await asyncio.sleep(0.2)
                    continue
            tried.add(upstream.url)
            body = [{"jsonrpc": "2.0", "id": n, "method": calls[i]["method"],
                     "params": calls[i].get("params", [])} for n, i in enumerate(left)]
            self.upstream_calls += len(left)
            reply = await send(upstream.host, upstream.url, json.dumps(body, separators=(",", ":")))
            status = int(reply.get("status") or 0)
            if status == 429 or status >= 500 or status == 0 or reply.get("error"):
                upstream.rest(clock())
                logger.info("rpc pool %s: %s rested %.0f s (status %s, %d calls moved on)", self.name,
                            upstream.host, REST_S, status, len(left))
                continue
            try:
                answers = json.loads(reply.get("text") or "")
            except ValueError:
                upstream.rest(clock())
                continue
            if isinstance(answers, dict):  # an endpoint that answers a batch with one error
                upstream.rest(clock())
                continue
            by_id = {a.get("id"): a for a in answers if isinstance(a, dict)} if isinstance(answers, list) else {}
            still = []
            for n, i in enumerate(left):
                a = by_id.get(n)
                call = calls[i]
                if a is None or ("error" in a and _retryable(a["error"])):
                    still.append(i)
                    if a is not None:
                        out[i] = {"jsonrpc": "2.0", "id": call.get("id"), "error": a["error"]}
                    continue
                if "error" in a:
                    out[i] = {"jsonrpc": "2.0", "id": call.get("id"), "error": a["error"]}
                    continue
                result = a.get("result")
                out[i] = {"jsonrpc": "2.0", "id": call.get("id"), "result": result}
                if result is not None and cacheable(call["method"], call.get("params", [])):
                    self.cache.put(self._key(call["method"], call.get("params", [])), result)
            if still:
                logger.info("rpc pool %s: %s refused %d of %d calls; trying the next endpoint", self.name,
                            upstream.host, len(still), len(left))
            left = still


class RpcPool:
    """One hearth's pools, by the name an agent declares."""

    def __init__(self, pools: dict[str, dict[str, Any]] | None = None, *, cache: Cache | None = None) -> None:
        self.cache = cache or Cache()
        self.chains: dict[str, Chain] = {}
        for name, spec in (DEFAULT_POOLS if pools is None else pools).items():
            ups = [Upstream(url=str(u["url"]), per_second=float(u.get("per_second", 10)),
                            logs_range=int(u.get("logs_range", 0))) for u in spec["upstreams"]]
            for u in ups:
                parts = urlsplit(u.url)
                if parts.scheme != "https" or parts.port not in (None, 443) or not parts.hostname:
                    raise ValueError(f"pool upstream {u.url} must be https on port 443")
            self.chains[name.lower()] = Chain(name.lower(), str(spec["chain_id"]), ups, self.cache)

    @property
    def hosts(self) -> frozenset[str]:
        return frozenset(self.chains)

    @staticmethod
    def calls(body: str | None) -> int:
        """JSON-RPC calls in a request body (a batch counts each); 0 if it is not JSON-RPC."""
        try:
            doc = json.loads(body or "")
        except ValueError:
            return 0
        if isinstance(doc, dict):
            return 1 if "method" in doc else 0
        if isinstance(doc, list):
            return sum(1 for d in doc if isinstance(d, dict) and "method" in d)
        return 0

    async def handle(self, host: str, method: str, body: str | None, send: Send) -> dict[str, Any]:
        chain = self.chains[host]
        if method != "POST":
            return _reply(405, {"jsonrpc": "2.0", "id": None,
                                "error": {"code": -32600, "message": "the pool takes JSON-RPC over POST"}})
        try:
            doc = json.loads(body or "")
        except ValueError:
            return _reply(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
        single = isinstance(doc, dict)
        calls = [doc] if single else doc
        if not isinstance(calls, list) or not calls or len(calls) > MAX_CALLS or not all(
                isinstance(c, dict) and isinstance(c.get("method"), str) for c in calls):
            return _reply(400, {"jsonrpc": "2.0", "id": None,
                                "error": {"code": -32600, "message": f"1 to {MAX_CALLS} JSON-RPC calls"}})
        refused = [c for c in calls if c["method"] not in READ_METHODS]
        if refused:
            return _reply(400, {"jsonrpc": "2.0", "id": None, "error": {
                "code": -32601, "message": f"{refused[0]['method']} is not a read method this pool passes on"}})
        before_hits, before_up = self.cache.hits, chain.upstream_calls
        answers = await chain.answer(calls, send)
        errors = sum(1 for a in answers if "error" in a)
        logger.info("rpc pool %s calls=%d cached=%d upstream=%d errors=%d", host, len(calls),
                    self.cache.hits - before_hits, chain.upstream_calls - before_up, errors)
        return _reply(200, answers[0] if single else answers)
