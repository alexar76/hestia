"""The WebAssembly sandbox itself, against the escapes a hostile handler would try.

Runs only where wasmtime and CPython-for-WASI are installed (the hearth image puts them at
/opt/python-wasi; elsewhere set HESTIA_WASM_ROOT). Every attack below runs with NO help from
the AST scanner: the sandbox is the boundary, and these tests are the proof it holds alone.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

wasmtime = pytest.importorskip("wasmtime")

ROOT = Path(os.environ.get("HESTIA_WASM_ROOT", "/opt/python-wasi"))
if not (ROOT / "python.wasm").is_file():
    pytest.skip("CPython for WASI is not installed (HESTIA_WASM_ROOT)", allow_module_level=True)

from hestia.sandbox import Sandbox  # noqa: E402


@pytest.fixture(scope="module")
def sandbox():
    box = Sandbox(ROOT / "python.wasm", ROOT / "lib", memory_mb=128, timeout_s=3.0,
                  max_output=64 * 1024)
    yield box
    box.close()


def handler(body: str) -> str:
    return body.strip() + "\n"


def test_a_plain_handler_runs(sandbox) -> None:
    out = sandbox.run(handler("def handle(p):\n    return {'echo': p, 'n': len(p)}"), {"a": [1, 2]})
    assert out.kind == "ok" and out.result == {"echo": {"a": [1, 2]}, "n": 1}


def test_any_stdlib_module_works(sandbox) -> None:
    source = handler("""
import base64, difflib, struct, typing, string, fractions, heapq, re, json, hashlib
def handle(p):
    d = list(difflib.ndiff(['a'], ['b']))
    return {'b64': base64.b64encode(b'hi').decode(), 'diff': d, 'z': struct.calcsize('<I'),
            'f': str(fractions.Fraction(3, 6)), 'h': hashlib.sha256(b'').hexdigest()[:8]}
""")
    out = sandbox.run(source, {})
    assert out.kind == "ok" and out.result["b64"] == "aGk=" and out.result["f"] == "1/2"


@pytest.mark.parametrize(
    ("name", "body"),
    [
        ("host file", "def handle(p):\n    return {'x': open('/etc/passwd').read()}"),
        ("host root", "import os\ndef handle(p):\n    return {'x': os.listdir('/')}"),
        ("key file", "def handle(p):\n    return {'x': open('/data/provider.key').read()}"),
        ("write stdlib", "def handle(p):\n    open('/lib/python3.14/x.py', 'w').write('x')\n"
                         "    return {}"),
        ("network", "import socket\ndef handle(p):\n"
                    "    socket.create_connection(('1.1.1.1', 80), timeout=1)\n    return {}"),
        ("process", "import subprocess\ndef handle(p):\n    subprocess.run(['ls'])\n    return {}"),
        ("native code", "import ctypes\ndef handle(p):\n    return {}"),
        ("shell", "import os\ndef handle(p):\n    return {'x': os.system('id')}"),
    ],
)
def test_escapes_fail_inside_the_sandbox(sandbox, name, body) -> None:
    out = sandbox.run(handler(body), {})
    assert out.kind == "error", (name, out)


def test_the_environment_is_empty(sandbox) -> None:
    os.environ["HESTIA_DEPLOY_TOKEN"] = "must-not-leak"
    try:
        out = sandbox.run(handler("import os\ndef handle(p):\n    return dict(os.environ)"), {})
    finally:
        del os.environ["HESTIA_DEPLOY_TOKEN"]
    assert out.kind == "ok" and out.result == {"PYTHONHASHSEED": "0", "PYTHONHOME": "/"}


def test_memory_is_capped(sandbox) -> None:
    out = sandbox.run(handler("def handle(p):\n    return {'n': len(bytearray(1 << 30))}"), {})
    assert out.kind == "memory"


def test_time_is_capped(sandbox) -> None:
    started = time.perf_counter()
    out = sandbox.run(handler("def handle(p):\n    while True:\n        pass"), {}, timeout_s=1.0)
    assert out.kind == "timeout" and time.perf_counter() - started < 3.0


def test_output_is_capped(sandbox) -> None:
    out = sandbox.run(handler("import sys\ndef handle(p):\n    sys.stdout.write('x' * 10**6)\n"
                              "    return {}"), {})
    assert out.kind == "output"


def test_crashes_and_bad_results(sandbox) -> None:
    assert sandbox.run(handler("import os\ndef handle(p):\n    os._exit(0)"), {}).kind == "crash"
    assert sandbox.run(handler("def handle(p):\n    return [1]"), {}).error == \
        "handler must return an object"
    assert sandbox.run(handler("def handle(p):\n    raise ValueError('bad input')"), {}).error \
        == "bad input"
    assert sandbox.run(handler("x = 1"), {}).error == "handler.py must define handle(payload)"
    assert sandbox.run(handler("def handle(p):\n    return {'x': float('nan')}"), {}).kind == \
        "error"


def test_recursion_is_an_error_not_a_crash(sandbox) -> None:
    out = sandbox.run(handler("def f(n):\n    return f(n + 1)\ndef handle(p):\n    return f(0)"), {})
    assert out.kind == "error" and "recursion" in out.error


def test_calls_do_not_share_state(sandbox) -> None:
    source = handler("import builtins\ndef handle(p):\n"
                     "    seen = getattr(builtins, 'seen', 0)\n    builtins.seen = seen + 1\n"
                     "    return {'seen': seen}")
    assert [sandbox.run(source, {}).result["seen"] for _ in range(3)] == [0, 0, 0]


def test_one_slow_call_does_not_cut_off_its_neighbours(sandbox) -> None:
    results = {}

    def call(key, source, timeout):
        results[key] = sandbox.run(source, {}, timeout_s=timeout)

    slow = threading.Thread(target=call, args=(
        "slow", handler("def handle(p):\n    while True:\n        pass"), 1.0))
    quick = [threading.Thread(target=call, args=(
        f"quick{i}", handler("import time\ndef handle(p):\n    t = time.time()\n"
                             "    while time.time() - t < 1.5:\n        pass\n    return {}"), 3.0))
             for i in range(2)]
    for thread in [slow, *quick]:
        thread.start()
    for thread in [slow, *quick]:
        thread.join()
    assert results["slow"].kind == "timeout"
    assert all(results[f"quick{i}"].kind == "ok" for i in range(2))
