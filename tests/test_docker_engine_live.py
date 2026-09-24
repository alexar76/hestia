"""Per-tenant networks against a REAL Docker engine. Opt in: HESTIA_TEST_DOCKER_ENGINE=1.

Everything the fake engine models is asserted here on the real thing: each
tenant on its own Internal /29 with IPv6 off, one tenant unable to reach another,
the engine host able to reach both, and an upgrade moving tenants off the former
shared bridge. Two things are swapped, both test-only: the image (a local build
has no repo digest for `hestia-tenant@sha256:…`), and the reachability probe,
which runs from the engine host's network namespace because on Docker Desktop
this process is outside the VM that owns the bridges.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from hestia.app import reconcile_docker_tenants
from hestia.models import CapabilitySpec
from hestia.policy import IsolationProfile, tenant_network_problem
from hestia.runtime import docker as docker_runtime
from hestia.runtime.docker import DockerRuntime
from hestia.store import TenantRow, TenantStore
from hestia.subnets import parse_subnet_pool
from tests.conftest import capability

pytestmark = pytest.mark.skipif(
    os.environ.get("HESTIA_TEST_DOCKER_ENGINE") != "1",
    reason="set HESTIA_TEST_DOCKER_ENGINE=1 to run against a real Docker engine",
)

PREFIX = "hstlive"
POOL = "10.239.0.0/24"
IMAGE = "hestia-live-tenant:test"
DIGEST = "sha256:" + ("cd" * 32)
# Unmistakable names: this test removes its own containers before and after.
SLUGS = ("hstlive-alpha", "hstlive-beta")


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check, timeout=120)


def _cleanup() -> None:
    names = [f"hestia-{s}" for s in SLUGS]
    _docker("rm", "-f", *names, check=False)
    nets = [f"{PREFIX}-{s}" for s in SLUGS] + [PREFIX]
    _docker("network", "rm", *nets, check=False)


@pytest.fixture
def live(monkeypatch, tmp_path):
    context = tmp_path / "image"
    context.mkdir()
    (context / "Dockerfile").write_text(
        "FROM busybox:latest\n"
        "RUN mkdir /www && echo tenant > /www/index.html\n"
        'CMD ["httpd", "-f", "-p", "8080", "-h", "/www"]\n'
    )
    _docker("build", "-q", "-t", IMAGE, str(context))
    _cleanup()
    real_spec = docker_runtime.docker_spec_for_tests

    def spec(**kwargs):
        argv = real_spec(**kwargs)
        return [*argv[:-1], IMAGE]

    def reachable(self, address, port):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            probe = _docker(
                "run", "--rm", "--net=host", "busybox", "nc", "-z", "-w", "1", address, str(port),
                check=False,
            )
            if probe.returncode == 0:
                return
            time.sleep(0.5)
        raise RuntimeError(f"{address}:{port} not reachable from the engine host")

    monkeypatch.setattr(docker_runtime, "docker_spec_for_tests", spec)
    monkeypatch.setattr(DockerRuntime, "_wait_reachable", reachable)
    yield DockerRuntime(
        "docker", frozenset({DIGEST}), network_prefix=PREFIX, subnet_pool=POOL
    )
    _cleanup()
    _docker("rmi", "-f", IMAGE, check=False)


def _profile() -> IsolationProfile:
    return IsolationProfile(
        cpus="0.5", memory_mb=128, pids=64, user="65532:65532", read_only=True,
        no_new_privileges=True, cap_drop=("ALL",), tmpfs_mb=8, network=PREFIX,
        egress_allowlist=(),
    )


def _start(runtime: DockerRuntime, slug: str):
    return runtime.start(
        slug=slug, capability=CapabilitySpec.model_validate(capability()), handler="",
        image_digest=DIGEST, profile=_profile(), env={},
    )


def _reach(from_container: str, url: str) -> bool:
    return _docker("exec", from_container, "wget", "-qO-", "-T", "2", url, check=False).returncode == 0


def _host_reach(url: str) -> bool:
    return _docker("run", "--rm", "--net=host", "busybox", "wget", "-qO-", "-T", "3", url, check=False).returncode == 0


def test_real_engine_tenants_are_isolated_from_each_other(live) -> None:
    alpha = _start(live, SLUGS[0])
    beta = _start(live, SLUGS[1])
    pool = parse_subnet_pool(POOL)
    for slug in SLUGS:
        name = f"{PREFIX}-{slug}"
        network = json.loads(_docker("network", "inspect", name).stdout)[0]
        attached = _docker("ps", "-a", "--filter", f"network={name}", "--format", "{{.Names}}").stdout.split()
        assert tenant_network_problem(network, slug=slug, name=name, pool=pool, attached=attached) == ""

    assert _host_reach(f"{alpha.listen_url}/") and _host_reach(f"{beta.listen_url}/")
    assert not _reach(f"hestia-{SLUGS[0]}", f"{beta.listen_url}/")
    assert not _reach(f"hestia-{SLUGS[1]}", f"{alpha.listen_url}/")
    assert not _reach(f"hestia-{SLUGS[0]}", "http://1.1.1.1/")

    live.stop(f"hestia-{SLUGS[0]}")
    assert _docker("network", "inspect", f"{PREFIX}-{SLUGS[0]}", check=False).returncode != 0


def test_real_engine_upgrade_moves_tenants_off_the_shared_bridge(live, tmp_path: Path) -> None:
    _docker("network", "create", "--internal", PREFIX)
    for slug in SLUGS:
        _docker("run", "-d", "--name", f"hestia-{slug}", "--user", "65532:65532", "--network", PREFIX, IMAGE)
    time.sleep(1)
    beta_ip = _docker("container", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", f"hestia-{SLUGS[1]}").stdout.strip()
    assert _reach(f"hestia-{SLUGS[0]}", f"http://{beta_ip}:8080/")  # the original hole

    store = TenantStore.open(data_dir=tmp_path)
    now = time.time()
    for slug in SLUGS:
        store.upsert(TenantRow(
            slug=slug, status="running", capability=capability(capability_id=f"{slug}.c@v1"),
            source_kind="image", owner_pubkey="", listen_url="", public_url="", announced=0,
            note="", created_at=now, updated_at=now, last_error="", image_digest=DIGEST,
        ))
    report = reconcile_docker_tenants(store, live, _profile())

    assert sorted(report["restored"]) == list(SLUGS)
    assert report["legacy_network"]["removed"] is True
    alpha, beta = store.get(SLUGS[0]), store.get(SLUGS[1])
    assert not _reach(f"hestia-{SLUGS[0]}", f"{beta.listen_url}/")
    assert _host_reach(f"{alpha.listen_url}/")


FIREWALL_SCENARIO = r"""
set -e
[ "$(readlink /proc/self/ns/net)" != "$(readlink /proc/1/ns/net)" ] || { echo SAME-NETNS; exit 3; }
for b in iptables ip socat unshare nsenter; do command -v $b >/dev/null || { echo "MISSING $b"; exit 4; }; done
ip link add hstfwtest0 type bridge; ip addr add 10.99.0.1/29 dev hstfwtest0; ip link set hstfwtest0 up; ip link set lo up
socat TCP-LISTEN:18600,fork,reuseaddr SYSTEM:"echo HOST" &
unshare -n sleep 60 & TPID=$!; sleep 0.5
ip link add vethh type veth peer name vetht; ip link set vetht netns $TPID; ip link set vethh master hstfwtest0 up
nsenter -t $TPID -n ip addr add 10.99.0.2/29 dev vetht; nsenter -t $TPID -n ip link set vetht up
nsenter -t $TPID -n socat TCP-LISTEN:8080,fork,reuseaddr SYSTEM:"echo TENANT" &
sleep 0.7
t2h() { timeout 4 nsenter -t $TPID -n socat -T2 - TCP:10.99.0.1:18600,connect-timeout=2 </dev/null 2>/dev/null || echo BLOCKED; }
h2t() { timeout 4 socat -T2 - TCP:10.99.0.2:8080,connect-timeout=2 </dev/null 2>/dev/null || echo FAILED; }
fw() { sh -c "$FW" fw "$@" 2>/dev/null; }
echo "before $(t2h) $(h2t)"
fw apply; echo "applied $(t2h) $(h2t) $(fw check >/dev/null && echo ok || echo bad)"
iptables -I INPUT 1 -i hst+ -j ACCEPT; echo "early-accept $(fw check >/dev/null && echo ok || echo bad)"
fw apply; echo "reapplied $(t2h) $(fw check >/dev/null && echo ok || echo bad)"
fw remove; echo "removed $(t2h)"
kill $TPID 2>/dev/null || true
"""


def test_real_kernel_firewall_script_closes_tenant_to_host(tmp_path) -> None:
    """Runs scripts/tenant-host-firewall.sh in a throwaway network namespace (a
    privileged container's own, never the engine host's) with a bridge named
    like a tenant bridge. The engine host's firewall is not touched."""
    script = (Path(__file__).resolve().parent.parent / "scripts" / "tenant-host-firewall.sh").read_text()
    probe = _docker(
        "run", "--rm", "--privileged", "--pid=host", "-e", f"FW={script}", "busybox",
        "nsenter", "-t", "1", "-m", "--", "sh", "-c", FIREWALL_SCENARIO, check=False,
    )
    if probe.returncode in (3, 4):
        pytest.skip(f"engine host lacks what the scenario needs: {probe.stdout.strip()}")
    lines = dict(line.split(" ", 1) for line in probe.stdout.strip().splitlines() if " " in line)
    assert lines["before"] == "HOST TENANT"
    assert lines["applied"] == "BLOCKED TENANT ok"  # tenant->host dropped, edge still works
    assert lines["early-accept"] == "bad"           # an earlier ACCEPT is noticed
    assert lines["reapplied"] == "BLOCKED ok"
    assert lines["removed"] == "HOST"
