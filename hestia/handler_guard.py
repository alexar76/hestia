"""Runtime containment for an admitted template handler.

The AST scanner (`hestia.scan`) is a static allow-list, and a static allow-list
over untrusted Python cannot be complete: an allowed module re-exports other
modules as ordinary attributes, so the module graph reaches `os`/`sys`/`builtins`
and, from there, `open`/`system`/`eval`. Every round of that whack-a-mole is one
missed gadget away from reading `/data/provider.key`.

This module is the second layer, and the one that actually bounds the blast
radius. It installs a CPython audit hook (PEP 578) — which fires in the C
implementation of the sensitive operation itself and cannot be removed or
monkey-patched away — and, while a handler is running, refuses the operations a
pure deterministic handler never performs:

  * opening / listing a path outside the interpreter's own library and the
    tenant's own directory (so `provider.key`, every sibling `tenant.key`, and
    `/etc/*` are unreadable even under full in-process code execution);
  * spawning a process (`os.system`, `subprocess`, `os.exec*`/`os.spawn*`);
  * opening a socket (no exfiltration channel);
  * loading native code (`ctypes`), or mutating the filesystem / process.

It does NOT block `import`, `exec` or `compile`: the import machinery and several
stdlib modules use all three at load time, and an attacker who can already run
admitted Python gains nothing by running more of it — every dangerous *effect*
is gated above. This is containment, not a sandbox; `HESTIA_RUNTIME=docker` with
gVisor is still the real isolation. But it means a scanner miss is no longer an
automatic key theft.
"""

from __future__ import annotations

import os
import sys

# Events whose *effect* a pure handler never needs. Matched by exact name or by
# prefix. These do not fire during ordinary arithmetic/JSON/hashlib work.
_BLOCK_EXACT = frozenset(
    {
        "os.system",
        "os.putenv",
        "os.unsetenv",
        "os.remove",
        "os.rename",
        "os.rmdir",
        "os.mkdir",
        "os.makedirs",
        "os.link",
        "os.symlink",
        "os.chmod",
        "os.chown",
        "os.fchmod",
        "os.fchown",
        "os.lchmod",
        "os.truncate",
        "os.kill",
        "os.killpg",
        "os.setuid",
        "os.setgid",
        "os.setegid",
        "os.seteuid",
        "os.setreuid",
        "os.setregid",
        "os.chdir",
        "os.fchdir",
        "os.chroot",
        "os.fork",
        "os.forkpty",
        "os.startfile",
        "os.add_dll_directory",
        "os.putenv",
        "os.rename",
        "cpython.run_command",
        "cpython.run_module",
        "code.__new__",
    }
)

_BLOCK_PREFIXES = (
    "subprocess.",
    "socket.",
    "ctypes.",
    "winreg.",
    "os.exec",
    "os.spawn",
    "os.posix_spawn",
    "shutil.",
    "pty.",
    "fcntl.",
    "mmap.",
    "msvcrt.",
    "multiprocessing.",
    "asyncio.",
    "ssl.",
    "http.client.",
    "urllib.",
    "ftplib.",
    "smtplib.",
    "poplib.",
    "imaplib.",
    "nntplib.",
    "telnetlib.",
    "webbrowser.",
    "glob.",
)

# Path-scoped events: allowed only under an interpreter library root or the
# tenant's own directory. `open` covers builtins.open / io.* / os.open.
_PATH_SCOPED = frozenset({"open", "os.scandir", "os.listdir"})


class HandlerContainmentError(RuntimeError):
    """A handler attempted an operation containment forbids."""


class _Guard:
    """Process-global switch the single installed audit hook reads.

    The tenant server is single-threaded (the per-call SIGALRM deadline requires
    it), so one flag is enough; there is no concurrent handler to interleave."""

    def __init__(self) -> None:
        self.armed = False
        self.tenant_dir = ""
        # Interpreter library roots: where `import` legitimately reads .py/.so.
        roots = {
            sys.prefix,
            sys.base_prefix,
            getattr(sys, "exec_prefix", sys.prefix),
            getattr(sys, "base_exec_prefix", sys.base_prefix),
        }
        # The directory the stdlib itself lives in (covers unusual layouts, e.g.
        # a relocated uv-managed CPython whose prefix is not an ancestor).
        roots.add(os.path.dirname(os.__file__))
        self.lib_roots = tuple(
            os.path.normpath(r) for r in roots if r
        )

    def allow_path(self, raw: object) -> bool:
        if not isinstance(raw, (str, bytes)):
            # A file descriptor (int) or None: the fd was obtained through some
            # earlier `open`, which was itself scoped. Allow the follow-on op.
            return True
        try:
            path = os.fsdecode(raw)
        except Exception:
            return False
        if not path:
            return False
        # Resolve relative paths against the tenant cwd, then normalise so
        # `../../provider.key` cannot climb out under a benign-looking prefix.
        if not os.path.isabs(path):
            path = os.path.join(os.getcwd(), path)
        path = os.path.normpath(path)
        roots = self.lib_roots + ((self.tenant_dir,) if self.tenant_dir else ())
        for root in roots:
            if path == root or path.startswith(root + os.sep):
                return True
        return False


_GUARD = _Guard()
_INSTALLED = False


def _hook(event: str, args: tuple) -> None:
    guard = _GUARD
    if not guard.armed:
        return
    if event in _PATH_SCOPED:
        target = args[0] if args else None
        if not guard.allow_path(target):
            raise HandlerContainmentError(
                f"handler is not allowed to access {target!r}"
            )
        return
    if event in _BLOCK_EXACT or event.startswith(_BLOCK_PREFIXES):
        raise HandlerContainmentError(
            f"handler is not allowed to perform {event!r}"
        )


def install() -> None:
    """Install the audit hook once. Safe to call repeatedly."""
    global _INSTALLED
    if _INSTALLED:
        return
    sys.addaudithook(_hook)
    _INSTALLED = True


class guarded:
    """Arm containment for the duration of a `with` block.

    Nesting keeps the innermost tenant_dir and restores the previous state on
    exit, so a call guarded inside an already-guarded load stays contained."""

    def __init__(self, tenant_dir: str | os.PathLike[str]) -> None:
        self.tenant_dir = os.path.normpath(str(tenant_dir))
        self._prev_armed = False
        self._prev_dir = ""

    def __enter__(self) -> "guarded":
        install()
        self._prev_armed = _GUARD.armed
        self._prev_dir = _GUARD.tenant_dir
        _GUARD.tenant_dir = self.tenant_dir
        _GUARD.armed = True
        return self

    def __exit__(self, *exc: object) -> None:
        _GUARD.armed = self._prev_armed
        _GUARD.tenant_dir = self._prev_dir
