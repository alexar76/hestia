import pytest

from hestia.scan import HandlerDenied, admit_handler


def test_empty_handler_is_valid_python_module() -> None:
    admit_handler("")


def test_json_math_handler_is_admitted() -> None:
    src = (
        "import json, math\n"
        "def handle(payload):\n"
        "    return {'n': math.floor(payload.get('n', 0))}\n"
    )
    admit_handler(src)


@pytest.mark.parametrize(
    "src",
    [
        "import os\n",
        "import subprocess\n",
        "eval('1')\n",
        "exec('x=1')\n",
        "open('/etc/passwd')\n",
        "from pathlib import Path\n",
        "__import__('os')\n",
        "x.__class__\n",
        "import socket as s\n",
        "import operator\n",
        "__builtins__\n",
        "global x\n",
        "setattr(x, 'a', 1)\n",
        "x.__getattribute__('__class__')\n",
    ],
)
def test_dangerous_handlers_are_refused(src: str) -> None:
    with pytest.raises(HandlerDenied):
        admit_handler(src)


def test_handler_too_large_and_syntax() -> None:
    with pytest.raises(HandlerDenied, match="larger"):
        admit_handler("x=1\n" * 20_000)
    with pytest.raises(HandlerDenied, match="not valid"):
        admit_handler("def (")
    with pytest.raises(HandlerDenied, match="relative"):
        admit_handler("from .x import y\n")


@pytest.mark.parametrize(
    "src",
    [
        # str.format / format_map walk attributes through the template string, which
        # the AST never sees — the canonical way past a dunder blocklist.
        "def handle(p):\n    return {'x': '{0.__class__.__base__}'.format(p)}\n",
        "def handle(p):\n    return {'x': '{a.__class__}'.format_map({'a': p})}\n",
        "def handle(p):\n    return {'x': str.format('{0.__class__}', p)}\n",
        "def handle(p):\n    return {'x': '{}'.format(p)}\n",
    ],
)
def test_format_gadget_is_refused(src: str) -> None:
    with pytest.raises(HandlerDenied, match="format"):
        admit_handler(src)


def test_fstring_attribute_access_still_scanned() -> None:
    # f-strings keep attribute access visible to the AST, so the dunder rule holds.
    with pytest.raises(HandlerDenied, match="__class__"):
        admit_handler("def handle(p):\n    return {'x': f'{p.__class__}'}\n")


def test_string_formatter_gadget_is_refused() -> None:
    # string.Formatter().get_field walks attributes through the field string too.
    with pytest.raises(HandlerDenied):
        admit_handler("import string\ndef handle(p):\n    return {'x': string.Formatter().get_field('0.__class__', [p], {})}\n")
    with pytest.raises(HandlerDenied, match="import of 'string'"):
        admit_handler("import string\ndef handle(p):\n    return {'x': string.digits}\n")


def test_from_import_checks_the_module_not_the_symbol() -> None:
    """`from decimal import Decimal` names one module: decimal.

    The symbol used to be checked against the module allow-list, so every
    from-import of an allowed module was refused and the error named the symbol
    as though it were the module.
    """
    admit_handler("from decimal import Decimal\ndef handle(p):\n    return {}\n")
    admit_handler("from json import dumps, loads\ndef handle(p):\n    return {}\n")
    admit_handler("def handle(p):\n    from hashlib import sha256\n    return {}\n")


def test_from_import_still_refuses_a_module_off_the_allowlist() -> None:
    with pytest.raises(HandlerDenied, match="import of 'os'"):
        admit_handler("from os import path\ndef handle(p):\n    return {}\n")
    with pytest.raises(HandlerDenied, match="import of 'subprocess'"):
        admit_handler("from subprocess import run\ndef handle(p):\n    return {}\n")


def test_star_import_is_refused() -> None:
    # Reachable only once from-imports work at all: a star import binds whatever
    # the module re-exports, including the modules it imported itself.
    with pytest.raises(HandlerDenied, match="import \\*"):
        admit_handler("from json import *\ndef handle(p):\n    return {}\n")


def test_import_alias_cannot_bind_a_refused_name() -> None:
    # An alias is the one binding form that never appears as an ast.Name.
    with pytest.raises(HandlerDenied, match="import alias '__class__'"):
        admit_handler("import json as __class__\ndef handle(p):\n    return {}\n")
    with pytest.raises(HandlerDenied, match="import alias 'getattr'"):
        admit_handler("from decimal import Decimal as getattr\ndef handle(p):\n    return {}\n")


def test_string_annotation_rce_gadget_is_refused() -> None:
    """typing.get_type_hints() eval()s a string annotation, so an admitted
    `def f(x: "code")` used to run arbitrary Python the AST never inspected."""
    with pytest.raises(HandlerDenied, match="typing"):
        admit_handler(
            'import typing\n'
            'def _g(x: "__import__(\'os\').getpid()"):\n    return x\n'
            'def handle(p):\n    typing.get_type_hints(_g)\n    return {}\n'
        )


def test_string_annotations_are_refused_everywhere() -> None:
    # A string annotation is deferred source; refuse it in every position, so
    # the class is closed even if some other resolver evaluates it.
    for src in (
        'def _g(x: "open(\'x\')"):\n    return x\ndef handle(p):\n    return {}\n',
        'def handle(p) -> "__import__(\'os\')":\n    return {}\n',
        'def handle(p):\n    y: "open(\'x\')" = 1\n    return {}\n',
        'def handle(p):\n    z: "int" = int(p.get(\'n\', 0))\n    return {\'z\': z}\n',
    ):
        with pytest.raises(HandlerDenied, match="string annotation"):
            admit_handler(src)


def test_future_annotations_import_is_refused() -> None:
    # `from __future__ import annotations` stringifies EVERY annotation, which
    # would turn any annotation into an eval target.
    with pytest.raises(HandlerDenied, match="__future__"):
        admit_handler("from __future__ import annotations\ndef handle(p):\n    return {}\n")


def test_legitimate_typed_handlers_still_pass() -> None:
    admit_handler("def handle(payload: dict) -> dict:\n    return {'ok': True}\n")
    admit_handler("def handle(payload: dict) -> list:\n    return []\n")
    admit_handler("def handle(payload):\n    n: int = 1\n    return {'n': n}\n")


@pytest.mark.parametrize(
    "src",
    [
        # A generator's frame hands back the builtins dict, and a dict subscript is
        # data the AST never inspects: (x for x in ()).gi_frame.f_builtins["__import__"].
        "def handle(p):\n"
        "    g = (z for z in (1,))\n"
        "    return {'x': g.gi_frame.f_builtins['__import__']('os').getuid()}\n",
        # the frame accessor alone
        "def handle(p):\n    g = (z for z in (1,))\n    return {'x': str(g.gi_frame)}\n",
        # generator code object → nested code / consts
        "def handle(p):\n    g = (z for z in (1,))\n    return {'x': str(g.gi_code)}\n",
        # coroutine frame
        "async def _c():\n    return 1\n"
        "def handle(p):\n    return {'x': str(_c().cr_frame)}\n",
        # async-generator frame
        "async def _a():\n    yield 1\n"
        "def handle(p):\n    return {'x': str(_a().ag_frame)}\n",
        # frame internals by name, regardless of how the frame was obtained
        "def handle(p):\n    return {'x': p.f_globals}\n",
        "def handle(p):\n    return {'x': p.f_builtins}\n",
        "def handle(p):\n    return {'x': p.f_locals}\n",
        "def handle(p):\n    return {'x': p.f_back}\n",
        "def handle(p):\n    return {'x': p.f_code}\n",
        # traceback walking (a tb reached any other way still cannot be traversed)
        "def handle(p):\n    return {'x': p.tb_frame}\n",
        "def handle(p):\n    return {'x': p.tb_next}\n",
    ],
)
def test_frame_and_interpreter_internal_attributes_are_refused(src: str) -> None:
    """Reaching a frame, its globals, or the builtins dict is arbitrary code.

    `frame.f_builtins['__import__']('os')` slipped past the dunder-attribute
    blocklist because none of gi_frame/cr_frame/ag_frame/tb_frame or
    f_globals/f_builtins/f_locals/f_back/f_code are dunders. The whole
    interpreter-internals class is now refused (explicit names + gi_/cr_/ag_/tb_
    prefixes)."""
    with pytest.raises(HandlerDenied, match="refused"):
        admit_handler(src)


def test_ordinary_prefixed_fields_via_subscript_still_pass() -> None:
    # f_/co_ collide with plausible JSON keys, so they are matched by exact
    # attribute name, never by prefix. A handler reading such keys off its own
    # dict (subscript, not attribute) must still be admitted.
    admit_handler(
        "def handle(p):\n"
        "    d = {'f_name': 1, 'co_owner': 2, 'gi_x': 3}\n"
        "    return {'a': d['f_name'], 'b': d['co_owner'], 'c': d['gi_x']}\n"
    )


@pytest.mark.parametrize(
    "src",
    [
        # An allowed module re-exports other modules as ordinary attributes, so the
        # module graph reaches sys/os/builtins/codecs with no dunder at all.
        "import re\ndef handle(p):\n    return {'x': re.enum.bltns.eval('1')}\n",
        "import statistics\ndef handle(p):\n    return {'x': statistics.sys.modules['os']}\n",
        "import collections\ndef handle(p):\n    return {'x': collections._sys.modules['os']}\n",
        "import json\ndef handle(p):\n    return {'x': json.codecs.open('/x')}\n",
        "from json import codecs\ndef handle(p):\n    return {'x': codecs.open('/x')}\n",
        "import statistics\ndef handle(p):\n    return {'x': statistics.random._os.popen('id')}\n",
        # forbidden builtins reached AS AN ATTRIBUTE (never checked before)
        "import json\ndef handle(p):\n    return {'x': json.codecs.builtins.exec('1')}\n",
        # dunder read through a subscript, which the AST treats as data
        "def handle(p):\n    return {'x': p['__globals__']}\n",
        "def handle(p):\n    g = (z for z in (1,))\n    return {'x': g['__class__']}\n",
    ],
)
def test_module_graph_and_subscript_escapes_are_refused(src: str) -> None:
    """Walking an allowed module's attributes to sys/os/builtins is refused, as is
    reaching a forbidden builtin through an attribute or a dunder subscript key.
    A blocklist cannot be complete here (hestia.handler_guard is the runtime
    backstop), but every demonstrated route is closed."""
    with pytest.raises(HandlerDenied, match="refused"):
        admit_handler(src)


def test_ordinary_json_field_names_still_pass() -> None:
    # Forbidden NAMES and module names are common JSON keys; refusing them only as
    # bare identifiers/attributes (not as subscript strings) keeps handlers working.
    admit_handler(
        "def handle(p):\n"
        "    return {'a': p['type'], 'b': p.get('os'), 'c': p['input'],\n"
        "            'd': p['random'], 'e': p['modules'], 'f': p['open']}\n"
    )
