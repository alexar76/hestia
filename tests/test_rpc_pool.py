"""The hearth's pool of free RPC endpoints (hestia.rpc_pool) and its place in egress.

An agent declares the pool's name like any host; the hearth answers it without DNS: pinned calls from
a cache, the rest from free endpoints in turn, a refused call moved to the next endpoint, and JSON-RPC
calls counted one by one against the agent's and the hearth's budgets."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from hestia.egress import Egress, EgressRefused, EgressRequest
from hestia.rpc_pool import _MISSING, Cache, RpcPool, cacheable, logs_span

NAME = "base-rpc.hestia.modelmarket.dev"
POOL = {NAME: {"chain_id": "0x2105", "upstreams": [
    {"url": "https://a.example.org", "per_second": 100, "logs_range": 1000},
    {"url": "https://b.example.org", "per_second": 100, "logs_range": 50000},
]}}
PUBLIC = "93.184.215.14"


def run(coro):
    return asyncio.run(coro)


class Upstreams:
    """Scripted endpoints: per host, a function of the batch they get."""

    def __init__(self, **behaviour):
        self.behaviour = behaviour
        self.seen: list[tuple[str, list]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host if request.url.host not in (PUBLIC,) else request.headers["host"]
        batch = json.loads(request.content)
        self.seen.append((host, batch))
        return self.behaviour[host.split(".")[0]](batch)


def ok(result_of):
    def answer(batch):
        return httpx.Response(200, json=[{"jsonrpc": "2.0", "id": c["id"], "result": result_of(c)} for c in batch])
    return answer


def egress(upstreams, **kw):
    return Egress(resolve=lambda h: [PUBLIC], transport=httpx.MockTransport(upstreams),
                  rpc_pool=RpcPool(POOL), **kw)


def call(e, body, slug="agent"):
    req = EgressRequest.parse({"url": f"https://{NAME}/", "method": "POST", "body": json.dumps(body),
                               "headers": {"content-type": "application/json"}}, [NAME])
    reply = run(e.fetch(slug, req))
    return reply, json.loads(reply["text"])


CALL_AT = {"jsonrpc": "2.0", "id": 7, "method": "eth_call",
           "params": [{"to": "0x" + "1" * 40, "data": "0x313ce567"}, "0x3a0000"]}


def test_chain_id_is_answered_by_the_hearth_and_a_pinned_call_once_from_upstream():
    ups = Upstreams(a=ok(lambda c: "0x06"), b=ok(lambda c: "0x06"))
    e = egress(ups)
    _, first = call(e, [{"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}, CALL_AT])
    assert first == [{"jsonrpc": "2.0", "id": 1, "result": "0x2105"}, {"jsonrpc": "2.0", "id": 7, "result": "0x06"}]
    _, again = call(e, CALL_AT)
    assert again == {"jsonrpc": "2.0", "id": 7, "result": "0x06"}
    assert len(ups.seen) == 1 and [c["method"] for c in ups.seen[0][1]] == ["eth_call"]


def test_a_call_at_latest_is_never_cached():
    ups = Upstreams(a=ok(lambda c: "0x10"), b=ok(lambda c: "0x10"))
    e = egress(ups)
    latest = dict(CALL_AT, params=[CALL_AT["params"][0], "latest"])
    call(e, latest)
    call(e, latest)
    assert len(ups.seen) == 2


def test_a_rate_limited_endpoint_rests_and_the_next_one_answers():
    ups = Upstreams(a=lambda b: httpx.Response(429), b=ok(lambda c: "0x2"))
    e = egress(ups)
    for _ in range(3):
        _, out = call(e, dict(CALL_AT, params=[CALL_AT["params"][0], "latest"]))
        assert out["result"] == "0x2"
    assert [h for h, _ in ups.seen].count("a.example.org") == 1  # rested after its 429


def test_a_refused_call_moves_on_but_a_revert_is_an_answer():
    def a(batch):
        return httpx.Response(200, json=[
            {"jsonrpc": "2.0", "id": c["id"], "error": {"code": -32005, "message": "request limit reached"}}
            if c["method"] == "eth_getBalance" else
            {"jsonrpc": "2.0", "id": c["id"], "error": {"code": 3, "message": "execution reverted"}}
            for c in batch])

    ups = Upstreams(a=a, b=ok(lambda c: "0x9"))
    e = egress(ups)
    pool = e.rpc_pool.chains[NAME]
    pool._order = lambda span, now: [u for u in pool.upstreams]  # a first, then b
    _, out = call(e, [{"jsonrpc": "2.0", "id": 1, "method": "eth_getBalance", "params": ["0x" + "2" * 40, "latest"]},
                      dict(CALL_AT, id=2)])
    assert out[0] == {"jsonrpc": "2.0", "id": 1, "result": "0x9"}
    assert out[1]["error"]["message"] == "execution reverted"
    assert [len(batch) for _, batch in ups.seen] == [2, 1]


def test_wide_logs_go_only_where_that_range_is_served():
    ups = Upstreams(a=ok(lambda c: []), b=ok(lambda c: []))
    e = egress(ups)
    logs = {"jsonrpc": "2.0", "id": 3, "method": "eth_getLogs",
            "params": [{"fromBlock": hex(1), "toBlock": hex(50000), "address": "0x" + "3" * 40}]}
    _, out = call(e, logs)
    assert out["result"] == [] and [h for h, _ in ups.seen] == ["b.example.org"]


def test_writes_and_odd_bodies_are_refused_without_an_upstream_call():
    ups = Upstreams(a=ok(lambda c: "0x"), b=ok(lambda c: "0x"))
    e = egress(ups)
    reply, out = call(e, {"jsonrpc": "2.0", "id": 1, "method": "eth_sendRawTransaction", "params": ["0x00"]})
    assert reply["status"] == 400 and "not a read method" in out["error"]["message"]
    reply, out = call(e, [{"id": 1}])
    assert reply["status"] == 400
    assert ups.seen == []


def test_every_call_of_a_batch_counts_against_the_budget():
    ups = Upstreams(a=ok(lambda c: "0x1"), b=ok(lambda c: "0x1"))
    e = egress(ups, rpc_calls_per_minute=10)
    batch = [dict(CALL_AT, id=i, params=[CALL_AT["params"][0], hex(100 + i)]) for i in range(8)]
    call(e, batch)
    with pytest.raises(EgressRefused, match="RPC budget"):
        call(e, batch)
    call(e, batch[:2], slug="another-agent")


def test_the_budget_counts_rpc_calls_to_any_host_not_only_the_pool():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=[])

    e = Egress(resolve=lambda h: [PUBLIC], transport=httpx.MockTransport(handler), rpc_calls_per_minute=5)
    body = json.dumps([{"jsonrpc": "2.0", "id": i, "method": "eth_blockNumber", "params": []} for i in range(4)])
    req = EgressRequest.parse({"url": "https://mainnet.base.org/", "method": "POST", "body": body}, ["mainnet.base.org"])
    run(e.fetch("x", req))
    with pytest.raises(EgressRefused, match="RPC budget"):
        run(e.fetch("x", req))
    plain = EgressRequest.parse({"url": "https://mainnet.base.org/"}, ["mainnet.base.org"])
    run(e.fetch("x", plain))  # a GET is not JSON-RPC: only the request budget applies
    assert len(seen) == 2


@pytest.mark.parametrize(("method", "params", "pinned"), [
    ("eth_call", [{}, "0x10"], True),
    ("eth_call", [{}, "latest"], False),
    ("eth_getBlockByNumber", ["0x10", False], True),
    ("eth_getBlockByNumber", ["latest", False], False),
    ("eth_getLogs", [{"fromBlock": "0x1", "toBlock": "0x2"}], True),
    ("eth_getLogs", [{"fromBlock": "0x1"}], False),
    ("eth_getTransactionReceipt", ["0xab"], True),
    ("eth_blockNumber", [], False),
])
def test_what_is_cacheable(method, params, pinned):
    assert cacheable(method, params) is pinned


def test_logs_span_and_cache_bounds():
    assert logs_span("eth_getLogs", [{"fromBlock": "0x1", "toBlock": "0xa"}]) == 10
    assert logs_span("eth_getLogs", [{"fromBlock": "latest"}]) == 1 << 30
    assert logs_span("eth_call", []) == 0
    c = Cache(max_entries=2)
    for k in "abc":
        c.put(k, k)
    assert c.get("a") is _MISSING and c.get("c") == "c"  # the oldest fell out
    expired = Cache(ttl_s=1)
    expired.put("k", 1, now=0)
    assert expired.get("k", now=0.5) == 1 and expired.get("k", now=2) is _MISSING


def test_a_pool_upstream_must_be_https_443():
    with pytest.raises(ValueError, match="https on port 443"):
        RpcPool({NAME: {"chain_id": "0x1", "upstreams": [{"url": "http://x.example.org"}]}})


def test_hestia_log_levels_give_the_pool_its_info_lines():
    import io
    import logging

    from hestia.__main__ import DEFAULT_LOG_LEVELS, configure_logging

    configure_logging(DEFAULT_LOG_LEVELS)
    (handler,) = [h for h in logging.getLogger("hestia").handlers if getattr(h, "_hestia", False)]
    stream = io.StringIO()
    previous = handler.setStream(stream)  # the handler keeps the stderr it was made with
    try:
        logging.getLogger("hestia.rpc_pool").info("rpc pool line")
        logging.getLogger("hestia.egress").info("egress line")
        logging.getLogger("hestia.egress").warning("egress warning")
        err = stream.getvalue()
        assert "INFO hestia.rpc_pool: rpc pool line" in err and "egress line" not in err
        assert "WARNING hestia.egress: egress warning" in err
        configure_logging(DEFAULT_LOG_LEVELS)  # a second call adds no second handler
        assert sum(getattr(h, "_hestia", False) for h in logging.getLogger("hestia").handlers) == 1
        with pytest.raises(ValueError, match="only hestia loggers"):
            configure_logging("uvicorn=DEBUG")
    finally:
        handler.setStream(previous)
        logging.getLogger("hestia.rpc_pool").setLevel(logging.NOTSET)
        logging.getLogger("hestia").removeHandler(handler)
        logging.getLogger("hestia").propagate = True
        logging.getLogger("hestia").setLevel(logging.NOTSET)


def test_pool_requests_spend_rpc_calls_not_the_request_budget():
    ups = Upstreams(a=ok(lambda c: "0x1"), b=ok(lambda c: "0x1"))

    def handler(request):
        if request.headers["host"] == "api.example.org":
            return httpx.Response(200, text="plain")
        return ups(request)

    e = Egress(resolve=lambda h: [PUBLIC], transport=httpx.MockTransport(handler), rpc_pool=RpcPool(POOL),
               per_minute=1, rpc_calls_per_minute=100)
    for block in range(5):  # five requests, one request allowed per minute: the pool is not counted
        call(e, dict(CALL_AT, params=[CALL_AT["params"][0], hex(200 + block)]))
    plain = EgressRequest.parse({"url": "https://api.example.org/"}, ["api.example.org"])
    run(e.fetch("agent", plain))
    with pytest.raises(EgressRefused, match="egress budget"):
        run(e.fetch("agent", plain))


def test_a_batch_larger_than_an_endpoints_burst_goes_to_another():
    from hestia.rpc_pool import DEFAULT_POOLS

    base = next(u for u in DEFAULT_POOLS[NAME]["upstreams"] if u["url"] == "https://mainnet.base.org")
    assert base["per_second"] * 2 < 16  # a 16-call batch never fits its burst
    pool = RpcPool({NAME: {"chain_id": "0x2105", "upstreams": [
        {"url": "https://a.example.org", "per_second": 6}, {"url": "https://b.example.org", "per_second": 15}]}})
    chain = pool.chains[NAME]
    a, b = chain.upstreams
    assert not a.take(16, now=1.0) and b.take(16, now=1.0)


def test_a_refused_receipt_moves_on_without_resting_the_endpoint():
    # publicnode answers a receipt it does not know yet with 403; another endpoint says null (not mined).
    ups = Upstreams(a=lambda b: httpx.Response(403, json={"jsonrpc": "2.0", "id": None, "error": {
        "code": -32602, "message": "Archive requests require a personal token"}}), b=ok(lambda c: None))
    e = egress(ups)
    pool = e.rpc_pool.chains[NAME]
    pool._order = lambda span, now: [u for u in pool.upstreams]  # a first, then b
    receipt = {"jsonrpc": "2.0", "id": 4, "method": "eth_getTransactionReceipt", "params": ["0x" + "12" * 32]}
    _, out = call(e, receipt)
    assert out == {"jsonrpc": "2.0", "id": 4, "result": None}
    assert pool.upstreams[0].resting_until == 0.0  # not rested: it still serves everything else
    assert [h for h, _ in ups.seen] == ["a.example.org", "b.example.org"]


def test_an_endpoint_gets_no_more_than_its_max_batch():
    sizes = []

    def small(batch):
        sizes.append(("a", len(batch)))
        if len(batch) > 4:
            return httpx.Response(400, text="Bad Request")
        return httpx.Response(200, json=[{"jsonrpc": "2.0", "id": c["id"], "result": "0x1"} for c in batch])

    def big(batch):
        sizes.append(("b", len(batch)))
        return httpx.Response(429)

    pool = {NAME: {"chain_id": "0x2105", "upstreams": [
        {"url": "https://a.example.org", "per_second": 100, "max_batch": 4},
        {"url": "https://b.example.org", "per_second": 100}]}}
    e = Egress(resolve=lambda h: [PUBLIC], transport=httpx.MockTransport(Upstreams(a=small, b=big)),
               rpc_pool=RpcPool(pool))
    batch = [dict(CALL_AT, id=i, params=[CALL_AT["params"][0], "latest"]) for i in range(10)]
    _, out = call(e, batch)
    assert [o["result"] for o in out] == ["0x1"] * 10
    assert sizes[0] == ("b", 10)                       # the whole batch to the one that takes it whole
    assert [n for h, n in sizes if h == "a"] == [4, 4, 2]  # then pieces the small one accepts
