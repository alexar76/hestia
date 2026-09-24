"""Subprocess runtime: sealed stub on loopback. Default for tests and laptops.

Isolation here is process + filesystem hygiene, not cgroups. The IsolationProfile
numbers are documented; this runtime ignores them on purpose.
"""

from __future__ import annotations

import atexit
import os
import socket
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

from hestia.models import CapabilitySpec
from hestia.policy import (
    TENANT_NETWORK_LABEL,
    TENANT_RUN_LABEL,
    IsolationProfile,
    tenant_network_name,
)
from hestia.runtime import RunningTenant
from hestia.scan import admit_handler

DEFAULT_MEMORY_CAP_MB = 512

try:  # POSIX only; absent on Windows.
    import resource
except ImportError:  # pragma: no cover - platform without rlimits
    resource = None  # type: ignore[assignment]


def _stub_rlimit_preexec():
    """preexec_fn for a stub tenant. Never false-kills a healthy tenant.

    Always disables core dumps (a crash must not spill tenant memory to disk)
    and caps address space. The cap defaults to 512 MiB rather than off: these
    handlers are pure functions over a 256 KiB body, so the ceiling is far above
    anything legitimate and far below what it takes to hurt the host. Set
    HESTIA_STUB_MEMORY_CAP_MB=0 to disable it, and note that RLIMIT_AS counts
    virtual address space, so a handler mapping large files would need it raised.
    Real cgroup limits are still HESTIA_RUNTIME=docker.
    """
    if resource is None:
        return None
    cap_mb = DEFAULT_MEMORY_CAP_MB
    try:
        cap_mb = int(os.environ.get("HESTIA_STUB_MEMORY_CAP_MB", str(DEFAULT_MEMORY_CAP_MB)))
    except ValueError:
        cap_mb = DEFAULT_MEMORY_CAP_MB

    def _apply() -> None:
        try:
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        except (ValueError, OSError):
            pass
        if cap_mb > 0:
            limit = cap_mb * 1024 * 1024
            try:
                resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
            except (ValueError, OSError):
                pass

    return _apply


class StubRuntime:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self._procs: dict[str, subprocess.Popen[str]] = {}
        atexit.register(self._stop_all)

    def _stop_all(self) -> None:
        for handle in list(self._procs):
            self.stop(handle)

    def start(
        self,
        *,
        slug: str,
        capability: CapabilitySpec,
        handler: str,
        image_digest: str,
        profile: IsolationProfile,
        env: dict[str, str],
    ) -> RunningTenant:
        del image_digest, profile  # isolation is documented; stub cannot enforce cgroups
        tenant_dir = self.data_dir / "tenants" / slug
        tenant_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(tenant_dir, 0o700)
        (tenant_dir / "tmp").mkdir(exist_ok=True, mode=0o700)
        cap_file = tenant_dir / "capability.json"
        cap_file.write_text(capability.model_dump_json(), encoding="utf-8")
        os.chmod(cap_file, 0o600)
        handler_file = tenant_dir / "handler.py"
        if handler.strip():
            admit_handler(handler)
            handler_file.write_text(handler, encoding="utf-8")
            os.chmod(handler_file, 0o600)
        elif handler_file.exists():
            handler_file.unlink()
        key_file = tenant_dir / "tenant.key"
        port = _free_port()
        child_env = minimal_tenant_env(
            slug=slug,
            cap_file=cap_file,
            key_file=key_file,
            handler_file=handler_file if handler_file.exists() else None,
            bind="127.0.0.1",
            port=port,
            home=tenant_dir,
            extra=env,
        )
        proc = subprocess.Popen(  # noqa: S603 — argv is fixed to this interpreter + module
            [sys.executable, "-m", "hestia.tenant_stub"],
            cwd=str(tenant_dir),
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            preexec_fn=_stub_rlimit_preexec() if os.name == "posix" else None,
        )
        deadline = time.time() + 5
        listen_url = f"http://127.0.0.1:{port}"
        while time.time() < deadline:
            if proc.poll() is not None:
                err = (proc.stderr.read() if proc.stderr else "")[-800:]
                raise RuntimeError(f"tenant process exited: {err}")
            if _port_open(port):
                self._procs[slug] = proc
                return RunningTenant(slug=slug, listen_url=listen_url, handle=slug)
            time.sleep(0.05)
        proc.kill()
        raise RuntimeError("tenant did not become healthy")

    def stored_handler(self, slug: str) -> str:
        """The handler source already on disk for `slug`, or "" for a sealed stub.

        A restart must hand `start` the SAME source it had, because `start`
        deletes handler.py when it is given an empty one — restoring a template
        tenant without this would silently demote it to a sealed stub that
        answers a different thing under the same capability id.
        """
        path = self.data_dir / "tenants" / slug / "handler.py"
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8")

    def stop(self, handle: str) -> None:
        proc = self._procs.pop(handle, None)
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


def minimal_tenant_env(
    *,
    slug: str,
    cap_file: Path,
    key_file: Path,
    handler_file: Path | None,
    bind: str,
    port: int,
    home: Path,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Env for the stub child. Operator secrets are never inherited.

    Only HESTIA_TENANT_* (plus capability/handler file paths) and a minimal
    PATH/HOME/TMPDIR needed to start the same interpreter. PYTHONPATH is omitted
    so the child uses the interpreter's site-packages, not the operator's extras.
    """
    env = {
        "PATH": os.environ.get("PATH") or "/usr/bin:/bin",
        "HOME": str(home),
        "TMPDIR": str(home / "tmp"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "HESTIA_TENANT_SLUG": slug,
        "HESTIA_CAPABILITY_FILE": str(cap_file),
        "HESTIA_TENANT_KEY": str(key_file),
        "HESTIA_TENANT_BIND": bind,
        "HESTIA_TENANT_PORT": str(port),
        # Wall-clock budget for one invoke, enforced inside the tenant.
        "HESTIA_TENANT_CALL_TIMEOUT_S": os.environ.get(
            "HESTIA_TENANT_CALL_TIMEOUT_S", "10"
        ),
    }
    if handler_file is not None:
        env["HESTIA_HANDLER_FILE"] = str(handler_file)
    for key, value in (extra or {}).items():
        if _is_tenant_extra(key):
            env[key] = value
    return env


def _is_tenant_extra(key: str) -> bool:
    if key in {"HESTIA_DEPLOY_TOKEN", "HESTIA_HUB_URL", "HESTIA_THEMIS_URL", "HESTIA_DATA_DIR"}:
        return False
    if key.startswith("AIMARKET_"):
        return False
    return key.startswith("HESTIA_TENANT_")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.15)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def tenant_image_ref(digest: str) -> str:
    """The only image reference a tenant container is ever started from."""
    return f"hestia-tenant@{digest}"


def docker_spec_for_tests(
    *,
    docker_bin: str,
    slug: str,
    digest: str,
    profile: IsolationProfile,
    run_id: str = "",
) -> list[str]:
    """Build the locked argv for this slug. `profile.network` is the PREFIX; the
    container joins its own `<prefix>-<slug>` network, never the prefix itself.

    Kept here as a pure function so callers and tests exercise the same argv
    policy without requiring a Docker daemon.
    """
    image = tenant_image_ref(digest)
    isolated = replace(profile, network=tenant_network_name(slug, profile.network))
    labels = {TENANT_NETWORK_LABEL: slug}
    if run_id:
        labels[TENANT_RUN_LABEL] = run_id
    return isolated.docker_argv(
        docker_bin=docker_bin,
        name=f"hestia-{slug}",
        image=image,
        env={"HOST": "0.0.0.0"},
        labels=labels,
    )
