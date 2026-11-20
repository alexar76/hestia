"""WebAssembly sandbox for template handlers: CPython or QuickJS compiled to WASI, run by wasmtime.

Two languages share every rule below. A Python handler runs in CPython for WASI; a JavaScript
handler (one classic script that defines `handle(payload)`, since 2026-11-20) runs in QuickJS-ng
for WASI, which is given no preopened directory at all.

This is the isolation boundary the AST scanner never was. A handler runs inside a fresh
WebAssembly instance per call, and the instance can reach only what this module hands it:

- the standard library, preopened READ-ONLY at /lib, and nothing else of the filesystem;
- no network (WASI preview 1 has no sockets), no processes, no native code, no host env
  (two fixed variables are all it sees);
- a linear-memory ceiling (`memory_mb`), a wall-clock deadline enforced by epoch
  interruption, and a cap on what it may write back.

Signing keys never enter the sandbox: the caller signs the result outside. A sandbox
escape would need a bug in wasmtime itself, and this runs in its own container with no
network and no secrets (hestia.wasm_runner) so that even that lands nowhere.

One engine and one compiled module per process; one Store (and instance) per call, so a
call cannot see another call's memory. A single ticker thread advances the engine epoch,
and each call gets a deadline in ticks — a per-call timer would interrupt every other
call sharing the engine.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_MEMORY_MB = 256
DEFAULT_TIMEOUT_S = 10.0
DEFAULT_MAX_OUTPUT = 4 * 1024 * 1024
TICK_S = 0.05

LANGUAGES = ("python", "javascript")
# QuickJS's own stack limit, kept below the WebAssembly stack: deep recursion then raises a
# RangeError the handler can see, instead of a trap that ends the call.
JS_STACK_KB = 256

# Runs inside the sandbox. It is the only code there besides the handler, and it trusts
# nothing the handler returns: the result must be a JSON object, or the call fails.
GUEST = r"""
import json, sys
_PENDING = []
class _NeedFetch(BaseException):
    pass
class Response:
    def __init__(self, raw):
        self.status = int(raw.get("status") or 0)
        self.headers = dict(raw.get("headers") or {})
        self.text = str(raw.get("text") or "")
        self.error = raw.get("error")
    @property
    def ok(self):
        return 200 <= self.status < 300
    def json(self):
        return json.loads(self.text)
def _key(method, url, headers, body):
    pairs = sorted((str(k).lower(), str(v)) for k, v in (headers or {}).items())
    return json.dumps([str(method or "GET").upper(), url, pairs, body],
                      ensure_ascii=False, separators=(",", ":"))
def _make_fetch(fetched):
    # The sandbox has no sockets. fetch() returns a response the hearth already fetched for
    # this call, or records the request and ends the run; the hearth fetches it (only from
    # this agent's declared egress hosts) and runs the handler again from the start.
    def fetch(url, method="GET", headers=None, body=None, json=None):
        if json is not None:
            body = _dumps(json)
            headers = dict(headers or {})
            headers.setdefault("content-type", "application/json")
        hdrs = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        key = _key(method, url, hdrs, body)
        if key in fetched:
            raw = fetched[key]
            if raw.get("refused"):
                raise RuntimeError("egress refused: " + str(raw["refused"])[:200])
            return Response(raw)
        _PENDING.append({"method": str(method).upper(), "url": url, "headers": hdrs, "body": body})
        raise _NeedFetch()
    return fetch
def _dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
def _main():
    request = json.loads(sys.stdin.read())
    fetched = request.get("fetched") or {}
    namespace = {"__name__": "handler", "fetch": _make_fetch(fetched), "Response": Response,
                 "CALL_NONCE": str(request.get("seed") or "")}
    try:
        exec(compile(request["source"], "handler.py", "exec"), namespace)
        handle = namespace.get("handle")
        if not callable(handle):
            raise RuntimeError("handler.py must define handle(payload)")
        result = handle(request["payload"])
        if not _PENDING and not isinstance(result, dict):
            raise ValueError("handler must return an object")
        text = json.dumps({"ok": True, "result": result}, ensure_ascii=False,
                          allow_nan=False, separators=(",", ":"))
    except RecursionError:
        text = json.dumps({"ok": False, "kind": "error", "error": "recursion too deep"})
    except MemoryError:
        text = json.dumps({"ok": False, "kind": "memory", "error": "out of memory"})
    except BaseException as exc:
        message = str(exc)[:300] or type(exc).__name__
        text = json.dumps({"ok": False, "kind": "error", "error": message},
                          ensure_ascii=False)
    if _PENDING:
        # Asked for a response it does not have yet, even if it then caught the signal.
        text = json.dumps({"ok": False, "kind": "fetch", "request": _PENDING[0]},
                          ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write("\x1e" + text)
_main()
"""


# The JavaScript guest: the same protocol as GUEST. The handler's source arrives on stdin with the
# payload, is evaluated as a classic script in the global scope, and must leave `handle` (or
# `handler.handle`, an esbuild --format=iife --global-name=handler bundle) behind. `handle` may be
# async. fetch(url, {method, headers, body, json}) answers from the responses the hearth already
# made, or records the request and ends the run, exactly like the Python guest; the request key
# matches hestia.egress.request_key byte for byte. The engine's std/os modules can still be
# imported by a handler; under WASI with no preopened directory, no environment and no sockets
# they reach nothing but this process's own output, which the guest writes last and then exits.
JS_GUEST = r"""
"use strict";
const _std = std, _os = os;
delete globalThis.std; delete globalThis.os; delete globalThis.bjson;
const _pending = [];
let _answered = false;
function _answer(value) {
  if (_answered) return;
  _answered = true;
  let text;
  if (_pending.length) {
    text = JSON.stringify({ ok: false, kind: "fetch", request: _pending[0] });
  } else {
    text = JSON.stringify(value);
  }
  _std.out.puts("\x1e" + text);
  _std.out.flush();
  _std.exit(0);
}
function _cmp(a, b) {
  const x = Array.from(a, (c) => c.codePointAt(0)), y = Array.from(b, (c) => c.codePointAt(0));
  for (let i = 0; i < Math.min(x.length, y.length); i++) if (x[i] !== y[i]) return x[i] - y[i];
  return x.length - y.length;
}
function _key(method, url, headers, body) {
  const pairs = Object.entries(headers || {}).map(([k, v]) => [String(k).toLowerCase(), String(v)])
    .sort((p, q) => _cmp(p[0], q[0]) || _cmp(p[1], q[1]));
  return JSON.stringify([String(method || "GET").toUpperCase(), url, pairs, body === undefined ? null : body]);
}
class Response {
  constructor(raw) {
    this.status = Number(raw.status || 0);
    const h = {};
    for (const [k, v] of Object.entries(raw.headers || {})) h[String(k).toLowerCase()] = String(v);
    this.headers = { get: (name) => (String(name).toLowerCase() in h ? h[String(name).toLowerCase()] : null),
                     entries: () => Object.entries(h) };
    this._text = String(raw.text || "");
    this.error = raw.error || null;
  }
  get ok() { return this.status >= 200 && this.status < 300; }
  text() { return Promise.resolve(this._text); }
  json() { return Promise.resolve().then(() => JSON.parse(this._text)); }
}
class _NeedFetch extends Error {}
let _fetched = {};
globalThis.fetch = function fetch(url, init) {
  init = init || {};
  let body = init.body === undefined ? null : init.body;
  let headers = Object.assign({}, init.headers || {});
  if (init.json !== undefined) {
    body = JSON.stringify(init.json);
    if (!Object.keys(headers).some((k) => k.toLowerCase() === "content-type")) headers["content-type"] = "application/json";
  }
  const hdrs = {};
  for (const [k, v] of Object.entries(headers)) hdrs[String(k).toLowerCase()] = String(v);
  const method = String(init.method || "GET").toUpperCase();
  const key = _key(method, url, hdrs, body);
  if (Object.prototype.hasOwnProperty.call(_fetched, key)) {
    const raw = _fetched[key];
    if (raw.refused) return Promise.reject(new Error("egress refused: " + String(raw.refused).slice(0, 200)));
    return Promise.resolve(new Response(raw));
  }
  _pending.push({ method, url, headers: hdrs, body });
  throw new _NeedFetch("fetch: the hearth fetches this and runs the handler again");
};
globalThis.Response = Response;
function _failure(e) {
  if (e instanceof InternalError && /out of memory/.test(String(e.message))) return { ok: false, kind: "memory", error: "out of memory" };
  const message = (e && e.message !== undefined ? String(e.message) : String(e)).slice(0, 300) || (e && e.name) || "error";
  return { ok: false, kind: "error", error: message };
}
let _request;
try {
  _request = JSON.parse(_std.in.readAsString());
  _fetched = _request.fetched || {};
  globalThis.CALL_NONCE = String(_request.seed || "");
} catch (e) {
  _answer({ ok: false, kind: "error", error: "unreadable request" });
}
(async () => {
  _std.evalScript(_request.source);
  const handle = _std.evalScript("typeof handle === 'function' ? handle : (typeof handler === 'object' && handler && typeof handler.handle === 'function' ? handler.handle : undefined)");
  if (typeof handle !== "function") throw new Error("the handler must define handle(payload)");
  const result = await handle(_request.payload);
  if (!_pending.length && (result === null || typeof result !== "object" || Array.isArray(result))) {
    throw new Error("handler must return an object");
  }
  return result;
})().then((result) => {
  let text;
  try { text = JSON.stringify(result); } catch (e) { return _answer({ ok: false, kind: "error", error: "result is not JSON: " + e.message }); }
  _answer({ ok: true, result: JSON.parse(text) });
}, (e) => _answer(_failure(e)));
// The sandbox has no I/O to wait for, so every promise a handler can settle settles in the job
// queue, which runs before any timer. A promise still pending at this timer never will: a
// handler bug, answered as one rather than left to look like a dead sandbox.
_os.setTimeout(() => _answer({ ok: false, kind: "error",
  error: "handle returned a promise that never settles (the sandbox has no timers or I/O to await)" }), 0);
"""


@dataclass(frozen=True)
class Outcome:
    """What one sandboxed call produced. `kind`: ok, error, timeout, memory, output, crash."""

    kind: str
    result: dict[str, Any] | None = None
    error: str = ""
    wall_ms: int = 0
    # kind == "fetch": the request the handler asked the hearth to make (hestia.egress).
    fetch: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.kind == "ok"

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "wall_ms": self.wall_ms}
        if self.ok:
            out["result"] = self.result
        elif self.kind == "fetch":
            out["fetch"] = self.fetch
        else:
            out["error"] = self.error
        return out


def default_paths() -> tuple[Path, Path]:
    root = Path(os.environ.get("HESTIA_WASM_ROOT", "/opt/python-wasi"))
    return root / "python.wasm", root / "lib"


def default_qjs() -> Path:
    return Path(os.environ.get("HESTIA_QJS_WASM", "/opt/qjs-wasi/qjs-wasi.wasm"))


class Sandbox:
    def __init__(
        self,
        python_wasm: Path | None = None,
        stdlib: Path | None = None,
        *,
        qjs_wasm: Path | None = None,
        memory_mb: int = DEFAULT_MEMORY_MB,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_output: int = DEFAULT_MAX_OUTPUT,
    ) -> None:
        import wasmtime

        wasm_path, lib_path = default_paths()
        self.python_wasm = Path(python_wasm or wasm_path)
        self.stdlib = Path(stdlib or lib_path)
        self.qjs_wasm = Path(qjs_wasm or default_qjs())
        have_python = self.python_wasm.is_file() and self.stdlib.is_dir()
        have_js = self.qjs_wasm.is_file()
        if not have_python and not have_js:
            raise RuntimeError(
                f"neither CPython for WASI ({self.python_wasm.parent}: python.wasm + lib/) nor "
                f"QuickJS for WASI ({self.qjs_wasm}) is installed; see Dockerfile"
            )
        self.memory_mb = memory_mb
        self.timeout_s = timeout_s
        self.max_output = max_output
        self._wasmtime = wasmtime
        config = wasmtime.Config()
        config.epoch_interruption = True
        self._engine = wasmtime.Engine(config)
        self._modules: dict[str, Any] = {}
        if have_python:
            self._modules["python"] = wasmtime.Module.from_file(self._engine, str(self.python_wasm))
        if have_js:
            self._modules["javascript"] = wasmtime.Module.from_file(self._engine, str(self.qjs_wasm))
        self._linker = wasmtime.Linker(self._engine)
        self._linker.define_wasi()
        self._stop = threading.Event()
        self._ticker = threading.Thread(target=self._tick, name="wasm-epoch", daemon=True)
        self._ticker.start()

    def _tick(self) -> None:
        while not self._stop.wait(TICK_S):
            self._engine.increment_epoch()

    def close(self) -> None:
        self._stop.set()

    @property
    def languages(self) -> list[str]:
        return [lang for lang in LANGUAGES if lang in self._modules]

    def run(self, source: str, payload: Any, *, timeout_s: float | None = None,
            fetched: dict[str, Any] | None = None, language: str = "python",
            seed: str = "") -> Outcome:
        wasmtime = self._wasmtime
        module = self._modules.get(language)
        if module is None:
            return self._done("crash", time.perf_counter(),
                              error=f"this sandbox runs no {language} handlers")
        budget = self.timeout_s if timeout_s is None else min(timeout_s, self.timeout_s)
        store = wasmtime.Store(self._engine)
        store.set_limits(memory_size=self.memory_mb * 1024 * 1024)
        store.set_epoch_deadline(max(1, int(budget / TICK_S) + 1))
        written = bytearray()
        overflow = [False]

        def sink(data: bytes) -> int:
            if len(written) + len(data) > self.max_output:
                overflow[0] = True
                return len(data)  # swallowed: the guest keeps running until its deadline
            written.extend(data)
            return len(data)

        started = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="hestia-call-") as tmp:
            request = Path(tmp) / "request.json"
            request.write_text(json.dumps({"source": source, "payload": payload,
                                           "fetched": fetched or {}, "seed": seed},
                                          ensure_ascii=False), encoding="utf-8")
            wasi = wasmtime.WasiConfig()
            if language == "javascript":
                # No preopened directory and no environment: nothing of the host is visible.
                wasi.argv = ["qjs", "--std", "--stack-size", str(JS_STACK_KB), "-e", JS_GUEST]
            else:
                wasi.argv = ["python", "-I", "-S", "-B", "-c", GUEST]
                wasi.env = [("PYTHONHASHSEED", "0"), ("PYTHONHOME", "/")]
                wasi.preopen_dir(str(self.stdlib), "/lib", False)
            wasi.stdin_file = str(request)
            wasi.stdout_custom = sink
            wasi.stderr_custom = lambda data: len(data)
            store.set_wasi(wasi)
            try:
                instance = self._linker.instantiate(store, module)
                instance.exports(store)["_start"](store)
            except wasmtime.ExitTrap as exc:
                if exc.code != 0 and not written:
                    return self._done("crash", started, error=f"interpreter exited {exc.code}")
            except wasmtime.Trap as exc:
                text = str(exc)
                if "interrupt" in text or "epoch" in text:
                    return self._done("timeout", started,
                                      error=f"handler exceeded its {budget:g}s budget")
                if "call stack exhausted" in text:
                    return self._done("error", started, error="call stack exhausted (recursion too deep)")
                return self._done("crash", started, error="sandbox trap: " + text.splitlines()[0][:200])
            except wasmtime.WasmtimeError as exc:
                return self._done("crash", started, error="sandbox error: " + str(exc)[:200])
        if overflow[0]:
            return self._done("output", started,
                              error=f"handler wrote more than {self.max_output} bytes")
        marker = written.rfind(b"\x1e")
        if marker < 0:
            return self._done("crash", started, error="handler produced no answer")
        try:
            answer = json.loads(written[marker + 1:].decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self._done("crash", started, error="handler answer is not JSON")
        if answer.get("ok") is True and isinstance(answer.get("result"), dict):
            return self._done("ok", started, result=answer["result"])
        if answer.get("kind") == "fetch" and isinstance(answer.get("request"), dict):
            return Outcome(kind="fetch", fetch=answer["request"],
                           wall_ms=int((time.perf_counter() - started) * 1000))
        kind = answer.get("kind") if answer.get("kind") in ("error", "memory") else "error"
        return self._done(kind, started, error=str(answer.get("error") or "handler failed")[:300])

    @staticmethod
    def _done(kind: str, started: float, *, result: dict[str, Any] | None = None,
              error: str = "") -> Outcome:
        return Outcome(kind=kind, result=result, error=error,
                       wall_ms=int((time.perf_counter() - started) * 1000))
