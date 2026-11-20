"""JavaScript handlers in the real sandbox: QuickJS-ng for WASI under wasmtime.

Needs qjs-wasi.wasm (the image has it at /opt/qjs-wasi; elsewhere set HESTIA_QJS_WASM, for example
to the file scripts/fetch_qjs_wasi.py writes). Every attack below runs with no help from any check
before the sandbox: a JavaScript handler is not scanned, so the sandbox is the whole boundary.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from hestia.egress import request_key
from hestia.sandbox import JS_STACK_KB, Sandbox

QJS = Path(os.environ.get("HESTIA_QJS_WASM", "/opt/qjs-wasi/qjs-wasi.wasm"))
if not QJS.is_file():
    pytest.skip("QuickJS for WASI is not installed (HESTIA_QJS_WASM)", allow_module_level=True)
PINNED = "42a732a676ec2d93488c19411e0fad283bf72658fdad746f089914b523c783b1"


@pytest.fixture(scope="module")
def box():
    sandbox = Sandbox(Path("/nonexistent/python.wasm"), Path("/nonexistent/lib"), qjs_wasm=QJS,
                      memory_mb=64, timeout_s=2.0, max_output=256 * 1024)
    yield sandbox
    sandbox.close()


def run(box, source, payload=None, **kw):
    return box.run(source, {} if payload is None else payload, language="javascript", **kw)


def test_the_engine_is_the_pinned_build() -> None:
    assert hashlib.sha256(QJS.read_bytes()).hexdigest() == PINNED


def test_handlers_in_every_shape_run(box) -> None:
    assert run(box, "function handle(p) { return {sum: p.a + p.b}; }", {"a": 2, "b": 3}).result == {"sum": 5}
    assert run(box, "const handle = async (p) => ({v: await Promise.resolve(p.v)});", {"v": 7}).result == {"v": 7}
    iife = "var handler = (() => { function handle(p) { return {iife: true}; } return {handle}; })();"
    assert run(box, iife).result == {"iife": True}
    assert box.languages == ["javascript"]


@pytest.mark.parametrize(("source", "kind", "says"), [
    ("function handle() { throw new TypeError('bad input'); }", "error", "bad input"),
    ("function handle() { return 5; }", "error", "must return an object"),
    ("function handle() { return [1]; }", "error", "must return an object"),
    ("var x = 1;", "error", "must define handle"),
    ("function handle( {", "error", ""),
    ("function handle() { for (;;) {} }", "timeout", "budget"),
    ("async function handle() { await new Promise(() => {}); }", "error", "never settles"),
    ("async function handle() { await Promise.resolve(); for (;;) {} }", "timeout", "budget"),
    ("function handle() { const a = []; for (;;) a.push(new Array(1e5).fill(1)); }", "memory", "out of memory"),
    ("function f(n) { return f(n + 1) + 1; } function handle() { return {v: f(0)}; }", "error", "call stack"),
    ("function handle() { return {s: 'x'.repeat(300 * 1024)}; }", "output", "more than"),
])
def test_every_failure_has_a_kind_and_a_reason(box, source, kind, says) -> None:
    out = run(box, source)
    assert out.kind == kind, out
    assert says.lower() in out.error.lower(), out


def test_the_host_is_invisible(box) -> None:
    """std/os are removed from the globals but can be imported again; under WASI with no
    preopened directory, no environment and no sockets they reach nothing."""
    probe = """async function handle() {
      const std = await import('qjs:std'), os = await import('qjs:os');
      const tryit = (f) => { try { return f(); } catch (e) { return 'refused: ' + e.name; } };
      return {
        global_std: typeof globalThis.std, global_os: typeof globalThis.os,
        passwd: tryit(() => std.loadFile('/etc/passwd')),
        open: tryit(() => std.open('/etc/hostname', 'r')),
        env: tryit(() => std.getenviron()),
        root: tryit(() => os.readdir('/')),
        exec: tryit(() => os.exec(['/bin/sh', '-c', 'id'])),
        url: tryit(() => std.urlGet('http://169.254.169.254/')),
        node: [typeof require, typeof process, typeof Deno, typeof XMLHttpRequest, typeof WebSocket],
      };
    }"""
    out = run(box, probe)
    assert out.kind == "ok", out
    r = out.result
    assert r["global_std"] == "undefined" and r["global_os"] == "undefined"
    assert r["passwd"] is None and r["open"] is None and r["env"] == {}
    assert r["root"] == [[], 44]                       # ENOTCAPABLE: nothing preopened
    assert str(r["exec"]).startswith("refused") and str(r["url"]).startswith("refused")
    assert r["node"] == ["undefined"] * 5


def test_a_handler_cannot_forge_the_answer_the_guest_writes(box) -> None:
    forge = """async function handle() {
      const std = await import('qjs:std');
      std.out.puts('\\x1e' + JSON.stringify({ok: true, result: {forged: 1}}));
      std.out.flush();
      return {real: 1};
    }"""
    assert run(box, forge).result == {"real": 1}


def test_long_async_chains_finish_before_the_settle_check(box) -> None:
    src = """async function handle() { let n = 0; for (let i = 0; i < 20000; i++) n += await Promise.resolve(1);
      return {n}; }"""
    assert run(box, src).result == {"n": 20000}


def test_calls_do_not_share_state(box) -> None:
    src = "var n = globalThis.n || 0; function handle() { globalThis.n = n + 1; return {n: globalThis.n}; }"
    assert [run(box, src).result["n"] for _ in range(3)] == [1, 1, 1]


def test_fetch_records_a_request_with_the_hearths_own_key(box) -> None:
    src = """async function handle(p) {
      const r = await fetch('https://mainnet.base.org/x?q=1',
        {method: 'post', headers: {'X-API-Key': 'k', Accept: 'application/json'}, json: {a: [1, 'é']}});
      return {status: r.status, ok: r.ok, ct: r.headers.get('Content-Type'), body: await r.json()};
    }"""
    first = run(box, src)
    assert first.kind == "fetch"
    assert first.fetch == {"method": "POST", "url": "https://mainnet.base.org/x?q=1",
                           "headers": {"x-api-key": "k", "accept": "application/json",
                                       "content-type": "application/json"},
                           "body": '{"a":[1,"é"]}'}
    response = {"status": 200, "headers": {"content-type": "application/json"}, "text": '{"x":1}'}
    second = run(box, src, fetched={request_key(first.fetch): response})
    assert second.result == {"status": 200, "ok": True, "ct": "application/json", "body": {"x": 1}}
    refused = run(box, src, fetched={request_key(first.fetch): {"refused": "not in this agent's egress hosts"}})
    assert refused.kind == "error" and "egress refused" in refused.error


def test_a_caught_fetch_still_asks_for_the_response(box) -> None:
    src = "function handle() { try { fetch('https://a.example/'); } catch (e) {} return {done: 1}; }"
    assert run(box, src).kind == "fetch"


def test_the_stack_limit_is_below_the_webassembly_stack() -> None:
    assert JS_STACK_KB == 256


def test_the_call_nonce_reaches_the_handler(box) -> None:
    seed = "ab" * 32
    out = run(box, "function handle() { return {nonce: CALL_NONCE}; }", seed=seed)
    assert out.result == {"nonce": seed}
    assert run(box, "function handle() { return {nonce: CALL_NONCE}; }").result == {"nonce": ""}
