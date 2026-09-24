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
        # "string" is intentionally absent: string.Formatter().get_field / vformat
        # walk attributes through a template string, the same escape as str.format.
        # "typing" is intentionally absent: typing.get_type_hints() eval()s a
        # string annotation, so an admitted `def f(x: "arbitrary code"): ...`
        # runs that code — the AST never sees inside the string. Removing typing
        # kills the reachable gadget; _reject_string_annotation closes the class.
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

# str.format / str.format_map read attributes through the *template string*, which
# the AST never sees ('{0.__class__.__mro__[1].__subclasses__}'.format(x)). That is
# the canonical way past a dunder-attribute blocklist, so the methods are refused
# outright. Handlers format with f-strings (whose attribute access IS scanned),
# %-formatting, or str.join instead.
_FORBIDDEN_METHODS = frozenset({"format", "format_map", "get_field", "vformat"})

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
        # Interpreter-internal attributes that hand back a frame, its globals, or
        # the builtins dict — the AST never sees inside a dict subscript, so once
        # you reach `frame.f_builtins` you read `["__import__"]("os")` and it is
        # game over. `__globals__`/`__code__`/`__traceback__` are dunders (already
        # refused), but the frame/generator/coroutine/async-gen/traceback
        # accessors below are NOT dunders and slipped straight through. Blocked
        # explicitly; the prefix rule in `_check_node` closes the rest of each class.
        "f_globals",
        "f_builtins",
        "f_locals",
        "f_back",
        "f_code",
        "f_trace",
        "gi_frame",
        "gi_code",
        "cr_frame",
        "cr_code",
        "ag_frame",
        "ag_code",
        "tb_frame",
        "tb_next",
    }
)

# Every attribute on a frame, generator, coroutine, async-generator, traceback or
# code object is interpreter-internal introspection — a category, not a handful of
# gadgets. Pure-function handlers never touch these, so the whole prefix is refused
# rather than chasing each new accessor. (`f_`/`co_` are matched by the explicit
# names above, not by prefix, so an ordinary field like `f_name`/`co_owner` on a
# handler's own object is still allowed.)
_FORBIDDEN_ATTR_PREFIXES = ("gi_", "cr_", "ag_", "tb_")

# An allow-listed module re-exports OTHER modules as plain, non-dunder attributes
# (`re.enum`, `statistics.sys`, `json.codecs`, `collections._sys`), so walking the
# module graph reaches `sys`/`os`/`builtins`/`codecs` — and from there `os.system`,
# `builtins.eval`, `codecs.open` — with no dunder in sight. Every dangerous module
# and interpreter-service name is refused AS AN ATTRIBUTE. This is a blocklist and a
# blocklist alone cannot hold (a handler still runs real Python and CPython keeps
# adding re-exports), which is exactly why `hestia.handler_guard` arms a runtime
# audit hook around the handler: a name that slips this list still cannot open a
# key file, spawn a process, or open a socket. Single-underscore attributes are
# refused wholesale below — no pure-function handler needs a private attribute, and
# that is where the private module re-exports (`_sys`, `_os`, `_collections_abc`)
# live.
_FORBIDDEN_MODULE_ATTRS = frozenset(
    {
        "sys", "os", "builtins", "codecs", "subprocess", "socket", "ctypes",
        "importlib", "imp", "marshal", "pickle", "posix", "nt", "msvcrt", "pty",
        "fcntl", "mmap", "resource", "shutil", "glob", "tempfile", "pathlib",
        "runpy", "code", "codeop", "inspect", "gc", "signal", "threading",
        "multiprocessing", "asyncio", "ssl", "urllib", "http", "ftplib", "smtplib",
        "socketserver", "selectors", "select", "platform", "sysconfig", "site",
        "warnings", "io", "enum", "random", "secrets", "operator", "copyreg",
        "weakref", "traceback", "linecache", "tokenize", "dis", "ast", "symtable",
        "webbrowser", "modules", "builtin_module_names",
    }
)


def _forbidden_string_key(node: ast.AST) -> str | None:
    """A subscript index that is a dunder string constant.

    The AST never looks inside a subscript, so `mapping['__globals__']` and
    `frame.f_builtins['__import__']` are data to it — the canonical way to read a
    dunder off an object's `__dict__` or the builtins mapping without writing the
    dunder as an attribute. A dunder is never a legitimate JSON field name, so it
    is refused. (Forbidden *names* and module names are NOT refused here: `type`,
    `os`, `input` are ordinary JSON keys, and the builtins/`sys.modules` mappings
    that would make such a key dangerous are already unreachable — every accessor
    that yields them is refused at the attribute layer.)"""
    key = node
    if isinstance(key, ast.Constant) and isinstance(key.value, str):
        if _is_dunder(key.value):
            return key.value
    return None


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


def _reject_string_annotation(annotation: ast.AST | None) -> None:
    """Refuse a string (forward-reference) annotation anywhere in a signature.

    `def f(x: "code")` stores "code" as a plain string Constant that the AST walk
    treats as data — then typing.get_type_hints() (or any forward-ref resolver)
    eval()s it, running arbitrary Python the scanner never inspected. A real
    annotation is a Name or a subscript of Names (`dict`, `list[int]`); a string
    anywhere inside one is code in disguise, so the whole subtree is refused.
    """
    if annotation is None:
        return
    for sub in ast.walk(annotation):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            raise HandlerDenied(
                "string annotations are refused: a forward-reference annotation is "
                "source the scanner cannot see and a resolver would eval()"
            )


def _check_node(node: ast.AST) -> None:
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        if isinstance(node, ast.ImportFrom):
            if node.level != 0:
                raise HandlerDenied("relative imports are refused")
            for alias in node.names:
                # A star import binds whatever the module re-exports, which for a
                # module without __all__ includes the modules it imported itself.
                if alias.name == "*":
                    raise HandlerDenied("'from ... import *' is refused")
            # In `from x import y` only `x` names a module. Checking `y` against
            # the module allow-list refused every from-import of an allowed
            # module and reported the symbol as though it were the module.
            names = [(node.module or "").split(".", 1)[0]]
        else:
            names = [alias.name.split(".", 1)[0] for alias in node.names]
        for name in names:
            if name not in ALLOWED_IMPORTS:
                raise HandlerDenied(f"import of '{name}' is refused")
        for alias in node.names:
            # An import alias is a binding, like a function argument, and is the
            # one binding form that never shows up as an ast.Name.
            _deny_identifier(alias.asname, what="import alias")
        return
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        raise HandlerDenied("global/nonlocal is refused")
    if isinstance(node, ast.Name):
        _deny_identifier(node.id, what="name")
        return
    if isinstance(node, ast.Attribute):
        if (
            node.attr in _FORBIDDEN_ATTRS
            or _is_dunder(node.attr)
            or node.attr.startswith(_FORBIDDEN_ATTR_PREFIXES)
            # A single leading underscore is a private/interpreter name. Nothing a
            # pure-function handler legitimately reaches starts with one, and the
            # private module re-exports (`_sys`, `_os`, `_collections_abc`) do.
            or (node.attr.startswith("_") and node.attr != "_")
            # `.eval` / `.open` / `.system` reached as an attribute is exactly as
            # dangerous as the bare name; the Attribute branch used to check only
            # the dunder/attr sets and let every forbidden NAME through as an attr.
            or node.attr in _FORBIDDEN_NAMES
            or node.attr in _FORBIDDEN_MODULE_ATTRS
        ):
            raise HandlerDenied(f"attribute '{node.attr}' is refused")
        if node.attr in _FORBIDDEN_METHODS:
            raise HandlerDenied(f"method '{node.attr}' is refused (format-string attribute walk)")
        return
    if isinstance(node, ast.Subscript):
        forbidden = _forbidden_string_key(node.slice)
        if forbidden is not None:
            raise HandlerDenied(f"subscript key '{forbidden}' is refused")
        return
    if isinstance(node, ast.arg):
        _deny_identifier(node.arg, what="argument")
        _reject_string_annotation(node.annotation)
        return
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        _reject_string_annotation(node.returns)
        return
    if isinstance(node, ast.AnnAssign):
        _reject_string_annotation(node.annotation)
        return
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FORBIDDEN_NAMES:
        raise HandlerDenied(f"call to '{node.func.id}' is refused")
