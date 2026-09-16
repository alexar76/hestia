"""AST admission for template handlers. Unknown nodes fail closed.

This is an allowlist parser, not a sandbox. Admitted source still runs as Python
via runpy in the tenant process. Do not describe it as a VM or seccomp jail.
"""

from __future__ import annotations

import ast

ALLOWED_IMPORTS = frozenset(
    {
        "collections",
        "copy",
        "datetime",
        "decimal",
        "functools",
        "hashlib",
        "itertools",
        "json",
        "math",
        "numbers",
        "re",
        "statistics",
        "string",
        "typing",
        "unicodedata",
    }
)

# operator.attrgetter / methodcaller are classic AST-escape gadgets — not allowed.
_FORBIDDEN_NAMES = frozenset(
    {
        "__import__",
        "breakpoint",
        "classmethod",
        "compile",
        "copyright",
        "credits",
        "delattr",
        "dir",
        "eval",
        "exec",
        "exit",
        "getattr",
        "globals",
        "help",
        "input",
        "license",
        "locals",
        "memoryview",
        "open",
        "quit",
        "setattr",
        "staticmethod",
        "super",
        "type",
        "vars",
        "__builtins__",
    }
)

_FORBIDDEN_ATTRS = frozenset(
    {
        "__class__",
        "__dict__",
        "__globals__",
        "__mro__",
        "__subclasses__",
        "__code__",
        "__bases__",
        "__getattribute__",
        "__getattr__",
        "__setattr__",
        "__delattr__",
        "__reduce__",
        "__reduce_ex__",
    }
)


class HandlerDenied(ValueError):
    pass


def admit_handler(source: str) -> ast.Module:
    if len(source) > 32_000:
        raise HandlerDenied("handler is larger than 32 KiB")
    try:
        tree = ast.parse(source, filename="handler.py", mode="exec")
    except SyntaxError as exc:
        raise HandlerDenied(f"handler is not valid Python: {exc.msg}") from exc
    for node in ast.walk(tree):
        _check_node(node)
    return tree


def _is_dunder(name: str) -> bool:
    return len(name) >= 4 and name.startswith("__") and name.endswith("__")


def _deny_identifier(name: str | None, *, what: str) -> None:
    if not name:
        return
    if name in _FORBIDDEN_NAMES or _is_dunder(name):
        raise HandlerDenied(f"{what} '{name}' is refused")


def _check_node(node: ast.AST) -> None:
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        names = [alias.name.split(".", 1)[0] for alias in node.names]
        if isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".", 1)[0]
            if node.level != 0:
                raise HandlerDenied("relative imports are refused")
            names.append(root)
        for name in names:
            if name not in ALLOWED_IMPORTS:
                raise HandlerDenied(f"import of '{name}' is refused")
        return
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        raise HandlerDenied("global/nonlocal is refused")
    if isinstance(node, ast.Name):
        _deny_identifier(node.id, what="name")
        return
    if isinstance(node, ast.Attribute):
        if node.attr in _FORBIDDEN_ATTRS or _is_dunder(node.attr):
            raise HandlerDenied(f"attribute '{node.attr}' is refused")
        return
    if isinstance(node, ast.arg):
        _deny_identifier(node.arg, what="argument")
        return
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FORBIDDEN_NAMES:
        raise HandlerDenied(f"call to '{node.func.id}' is refused")
