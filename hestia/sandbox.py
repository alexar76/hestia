"""WebAssembly sandbox for template handlers: CPython compiled to WASI, run by wasmtime.

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

# Runs inside the sandbox. It is the only code there besides the handler, and it trusts
# nothing the handler returns: the result must be a JSON object, or the call fails.
GUEST = r"""
import json, sys
def _main():
    request = json.loads(sys.stdin.read())
    namespace = {"__name__": "handler"}
    try:
        exec(compile(request["source"], "handler.py", "exec"), namespace)
        handle = namespace.get("handle")
        if not callable(handle):
            raise RuntimeError("handler.py must define handle(payload)")
        result = handle(request["payload"])
        if not isinstance(result, dict):
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
    sys.stdout.write("\x1e" + text)
_main()
"""


@dataclass(frozen=True)
class Outcome:
    """What one sandboxed call produced. `kind`: ok, error, timeout, memory, output, crash."""

    kind: str
    result: dict[str, Any] | None = None
    error: str = ""
    wall_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.kind == "ok"

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "wall_ms": self.wall_ms}
        if self.ok:
            out["result"] = self.result
        else:
            out["error"] = self.error
        return out


def default_paths() -> tuple[Path, Path]:
    root = Path(os.environ.get("HESTIA_WASM_ROOT", "/opt/python-wasi"))
    return root / "python.wasm", root / "lib"


class Sandbox:
    def __init__(
        self,
        python_wasm: Path | None = None,
        stdlib: Path | None = None,
        *,
        memory_mb: int = DEFAULT_MEMORY_MB,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_output: int = DEFAULT_MAX_OUTPUT,
    ) -> None:
        import wasmtime

        wasm_path, lib_path = default_paths()
        self.python_wasm = Path(python_wasm or wasm_path)
        self.stdlib = Path(stdlib or lib_path)
        if not self.python_wasm.is_file() or not self.stdlib.is_dir():
            raise RuntimeError(
                f"CPython for WASI is not installed at {self.python_wasm.parent} "
                "(python.wasm + lib/); see Dockerfile"
            )
        self.memory_mb = memory_mb
        self.timeout_s = timeout_s
        self.max_output = max_output
        self._wasmtime = wasmtime
        config = wasmtime.Config()
        config.epoch_interruption = True
        self._engine = wasmtime.Engine(config)
        self._module = wasmtime.Module.from_file(self._engine, str(self.python_wasm))
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

    def run(self, source: str, payload: Any, *, timeout_s: float | None = None) -> Outcome:
        wasmtime = self._wasmtime
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
            request.write_text(json.dumps({"source": source, "payload": payload},
                                          ensure_ascii=False), encoding="utf-8")
            wasi = wasmtime.WasiConfig()
            wasi.argv = ["python", "-I", "-S", "-B", "-c", GUEST]
            wasi.env = [("PYTHONHASHSEED", "0"), ("PYTHONHOME", "/")]
            wasi.preopen_dir(str(self.stdlib), "/lib", False)
            wasi.stdin_file = str(request)
            wasi.stdout_custom = sink
            wasi.stderr_custom = lambda data: len(data)
            store.set_wasi(wasi)
            try:
                instance = self._linker.instantiate(store, self._module)
                instance.exports(store)["_start"](store)
            except wasmtime.ExitTrap as exc:
                if exc.code != 0 and not written:
                    return self._done("crash", started, error=f"interpreter exited {exc.code}")
            except wasmtime.Trap as exc:
                text = str(exc)
                if "interrupt" in text or "epoch" in text:
                    return self._done("timeout", started,
                                      error=f"handler exceeded its {budget:g}s budget")
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
        kind = answer.get("kind") if answer.get("kind") in ("error", "memory") else "error"
        return self._done(kind, started, error=str(answer.get("error") or "handler failed")[:300])

    @staticmethod
    def _done(kind: str, started: float, *, result: dict[str, Any] | None = None,
              error: str = "") -> Outcome:
        return Outcome(kind=kind, result=result, error=error,
                       wall_ms=int((time.perf_counter() - started) * 1000))
