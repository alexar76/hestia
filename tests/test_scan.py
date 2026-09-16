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
