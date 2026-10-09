"""Per-tenant Docker networks: ownership, addresses, lifecycle.

Every test drives the real policy and runtime code against FakeEngine, which
keeps networks and containers as state. A check removed from the code shows up
here as a network or container that should not exist.
"""

from __future__ import annotations

import ipaddress
import subprocess

import pytest

from hestia.models import CapabilitySpec
from hestia.policy import (
    TENANT_NETWORK_LABEL,
    TENANT_RUN_LABEL,
    IsolationProfile,
    assert_safe_docker_argv,
    ensure_isolated_tenant_network,
    inspect_tenant_network,
    retire_legacy_tenant_network,
    tenant_network_problem,
)
from hestia.runtime.docker import DockerRuntime
from hestia.subnets import (
    parse_subnet_pool,
    pick_tenant_subnet,
    pool_capacity,
    tenant_bridge_name,
)
from tests.conftest import capability
from tests.fake_engine import FakeEngine

DIGEST = "sha256:" + ("ab" * 32)
POOL = parse_subnet_pool("10.231.0.0/16")


def _profile() -> IsolationProfile:
    return IsolationProfile(
        cpus="0.5",
        memory_mb=256,
        pids=64,
        user="65532:65532",
        read_only=True,
        no_new_privileges=True,
        cap_drop=("ALL",),
        tmpfs_mb=32,
        network="hestia-tenants",
        egress_allowlist=(),
    )


def _start(runtime: DockerRuntime, slug: str):
    return runtime.start(
        slug=slug,
        capability=CapabilitySpec.model_validate(capability()),
        handler="",
        image_digest=DIGEST,
        profile=_profile(),
        env={},
    )


def _runtime(**kwargs) -> DockerRuntime:
    return DockerRuntime("docker", frozenset({DIGEST}), **kwargs)


def _good_network_kwargs(name: str, slug: str, subnet: str = "10.231.9.0/29") -> dict:
    return {
        "internal": True,
        "labels": {TENANT_NETWORK_LABEL: slug},
        "options": {"com.docker.network.bridge.name": tenant_bridge_name(name)},
        "subnet": subnet,
    }


@pytest.fixture
def engine(monkeypatch) -> FakeEngine:
    return FakeEngine().install(monkeypatch)


# ------------------------------------------------------------------ addresses


def test_bridge_names_fit_the_kernel_limit_and_share_one_prefix() -> None:
    names = {tenant_bridge_name(f"hestia-tenants-{slug}") for slug in ("alpha", "beta", "x" * 48)}
    assert len(names) == 3
    assert all(len(name) <= 15 and name.startswith("hst") for name in names)
    assert tenant_bridge_name("hestia-tenants-alpha") == tenant_bridge_name("hestia-tenants-alpha")


@pytest.mark.parametrize(
    "value",
    ["", "not-a-net", "10.231.0.1/16", "fd00::/64", "8.8.0.0/16", "127.0.0.0/8", "169.254.0.0/16", "10.0.0.0/30"],
)
def test_subnet_pool_refuses_what_is_not_a_private_ipv4_range(value) -> None:
    with pytest.raises(RuntimeError, match="HESTIA_TENANT_SUBNET_POOL"):
        parse_subnet_pool(value)


def test_subnet_pool_capacity() -> None:
    assert pool_capacity(POOL) == 8192
    assert pool_capacity(parse_subnet_pool("10.231.0.0/29")) == 1


def test_pick_skips_taken_refused_and_reserved_ranges() -> None:
    first = pick_tenant_subnet(POOL, taken=[])
    assert first == ipaddress.ip_network("10.231.0.0/29")
    assert pick_tenant_subnet(POOL, taken=[first]) == ipaddress.ip_network("10.231.0.8/29")
    assert pick_tenant_subnet(POOL, taken=[], skip=[first]) == ipaddress.ip_network("10.231.0.8/29")
    # A live row still points at 10.231.0.2: that /29 is not handed out again.
    assert pick_tenant_subnet(POOL, taken=[], reserved_hosts=["10.231.0.2", "junk"]) == (
        ipaddress.ip_network("10.231.0.8/29")
    )
    # A wide network elsewhere on the engine that overlaps the pool is avoided.
    assert pick_tenant_subnet(POOL, taken=[ipaddress.ip_network("10.231.0.0/24")]) == (
        ipaddress.ip_network("10.231.1.0/29")
    )
    with pytest.raises(RuntimeError, match="exhausted"):
        pick_tenant_subnet(parse_subnet_pool("10.231.0.0/29"), taken=[first])


# ----------------------------------------------------------------- start/argv


def test_each_tenant_gets_its_own_verified_network(engine) -> None:
    runtime = _runtime()
    alpha = _start(runtime, "alpha")
    beta = _start(runtime, "beta")

    nets = engine.tenant_networks()
    assert set(nets) == {"hestia-tenants-alpha", "hestia-tenants-beta"}
    subnets = {name: net["IPAM"]["Config"][0]["Subnet"] for name, net in nets.items()}
    assert len(set(subnets.values())) == 2
    for name, net in nets.items():
        assert net["Internal"] is True and net["EnableIPv6"] is False
        assert ipaddress.ip_network(subnets[name]).subnet_of(POOL)
        assert net["Options"]["com.docker.network.bridge.name"] == tenant_bridge_name(name)
    assert engine.containers["hestia-alpha"]["NetworkSettings"]["Networks"].keys() == {"hestia-tenants-alpha"}
    assert engine.containers["hestia-beta"]["NetworkSettings"]["Networks"].keys() == {"hestia-tenants-beta"}
    assert ipaddress.ip_address(alpha.listen_url.split("//")[1].split(":")[0]) in ipaddress.ip_network(
        subnets["hestia-tenants-alpha"]
    )
    assert alpha.listen_url != beta.listen_url
    runs = [c for c in engine.calls if c[1:2] == ["run"]]
    for call, slug in zip(runs, ("alpha", "beta"), strict=True):
        assert call[call.index("--network") + 1] == f"hestia-tenants-{slug}"
        assert "hestia-tenants" not in call  # never the former shared bridge
        assert f"{TENANT_NETWORK_LABEL}={slug}" in call
        assert any(item.startswith(f"{TENANT_RUN_LABEL}=") for item in call)
    creates = [c for c in engine.calls if c[1:3] == ["network", "create"]]
    assert all("--internal" in c and "--ipv6=false" in c and "--subnet" in c for c in creates)


@pytest.mark.parametrize(
    "mutation",
    ["unlabelled", "other-slug", "foreign-running", "foreign-stopped"],
)
def test_a_network_that_is_not_ours_alone_is_refused(engine, mutation) -> None:
    name = "hestia-tenants-alpha"
    kwargs = _good_network_kwargs(name, "alpha")
    if mutation == "unlabelled":
        kwargs["labels"] = {}
    elif mutation == "other-slug":
        kwargs["labels"] = {TENANT_NETWORK_LABEL: "beta"}
    engine.add_network(name, **kwargs)
    if mutation == "foreign-running":
        engine.add_container("intruder", name, running=True)
    elif mutation == "foreign-stopped":
        # network inspect does not list a stopped container; ps -a does.
        engine.add_container("intruder", name, running=False)

    with pytest.raises(RuntimeError, match="cannot be used"):
        _start(_runtime(), "alpha")
    assert "hestia-alpha" not in engine.containers
    assert name in engine.networks  # someone else's network is never removed


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("Internal", False),
        ("EnableIPv6", True),
        ("Driver", "macvlan"),
        ("Options", {"com.docker.network.bridge.name": "br-whatever"}),
        ("Options", "gateway-mode"),
        ("IPAM", {"Config": [{"Subnet": "172.30.0.0/16"}]}),
        ("IPAM", {"Config": [{"Subnet": "10.231.0.0/28"}]}),
        ("IPAM", {"Config": [{"Subnet": "10.9.0.0/29"}]}),  # a /29, but outside the pool
        ("IPAM", {"Config": [{"Subnet": "10.231.9.0/29"}, {"Subnet": "10.231.9.8/29"}]}),
        ("Scope", "swarm"),
    ],
)
def test_each_isolation_check_is_enforced(field, value) -> None:
    name = "hestia-tenants-alpha"
    engine = FakeEngine()
    network = engine.add_network(name, **_good_network_kwargs(name, "alpha"))
    assert tenant_network_problem(network, slug="alpha", name=name, pool=POOL, attached=[]) == ""
    if value == "gateway-mode":
        network["Options"]["com.docker.network.bridge.gateway_mode_ipv4"] = "routed"
    else:
        network[field] = value
    assert tenant_network_problem(network, slug="alpha", name=name, pool=POOL, attached=[])


def test_a_stale_network_of_ours_is_replaced_by_a_correct_one(engine) -> None:
    """The colleague layout (default /16 pool, no bridge name) and crash leftovers
    carry our label with nothing attached: they are rebuilt, not trusted."""
    engine.add_network(
        "hestia-tenants-alpha",
        internal=False,
        labels={TENANT_NETWORK_LABEL: "alpha"},
        subnet="172.30.0.0/16",
        ipv6=True,
    )
    _start(_runtime(), "alpha")
    net = engine.networks["hestia-tenants-alpha"]
    assert net["Internal"] is True and net["EnableIPv6"] is False
    assert ipaddress.ip_network(net["IPAM"]["Config"][0]["Subnet"]).subnet_of(POOL)


def test_create_retries_when_another_network_takes_the_subnet(engine) -> None:
    engine.on(
        lambda a: a[:2] == ["network", "create"],
        stderr="Error response from daemon: invalid pool request: Pool overlaps with other one on this address space",
        times=1,
    )
    name, created = ensure_isolated_tenant_network("docker", "alpha", pool=POOL)
    assert created
    assert engine.networks[name]["IPAM"]["Config"][0]["Subnet"] == "10.231.0.8/29"


def test_an_address_a_live_row_points_at_is_never_reused(engine) -> None:
    runtime = _runtime(reserved_hosts=lambda slug: ["10.231.0.2"] if slug == "alpha" else [])
    _start(runtime, "alpha")
    assert engine.networks["hestia-tenants-alpha"]["IPAM"]["Config"][0]["Subnet"] == "10.231.0.8/29"


def test_pool_exhaustion_fails_closed(engine) -> None:
    runtime = _runtime(subnet_pool="10.231.0.0/29")
    _start(runtime, "alpha")
    with pytest.raises(RuntimeError, match="exhausted"):
        _start(runtime, "beta")
    assert "hestia-beta" not in engine.containers


def test_our_network_is_removed_when_its_verification_fails(engine) -> None:
    name = "hestia-tenants-alpha"
    engine.on(
        lambda a: a == ["network", "inspect", name] and name in engine.networks,
        rc=0,
        stdout="not json",
        times=1,
    )
    with pytest.raises(RuntimeError, match="invalid docker network inspect"):
        ensure_isolated_tenant_network("docker", "alpha", pool=POOL)
    assert name not in engine.networks


def test_a_hanging_engine_is_an_error_not_a_crash(engine) -> None:
    def hang(args):
        raise subprocess.TimeoutExpired(args, 10)

    engine.on(lambda a: a[:2] == ["network", "inspect"], action=hang)
    with pytest.raises(RuntimeError, match="timed out"):
        inspect_tenant_network("docker", "hestia-tenants-alpha")


def test_a_start_that_fails_after_run_removes_what_it_created(engine) -> None:
    engine.reachable = False
    with pytest.raises(RuntimeError, match="not reachable"):
        _start(_runtime(), "alpha")
    assert "hestia-alpha" not in engine.containers
    assert "hestia-tenants-alpha" not in engine.networks


def test_a_created_but_never_started_container_is_removed(engine) -> None:
    # docker run creates the container, then fails to connect it.
    engine.on(
        lambda a: a[:1] == ["run"],
        action=lambda a: engine.networks.pop("hestia-tenants-alpha"),
        times=1,
    )
    with pytest.raises(RuntimeError, match="docker run failed"):
        _start(_runtime(), "alpha")
    assert "hestia-alpha" not in engine.containers


def test_a_failed_start_never_removes_a_container_it_did_not_create(engine) -> None:
    # Even one carrying this tenant's label: only the per-run label proves "mine".
    engine.add_container("hestia-alpha", "bridge", running=True, labels={TENANT_NETWORK_LABEL: "alpha"})
    with pytest.raises(RuntimeError, match="already in use"):
        _start(_runtime(), "alpha")
    assert "hestia-alpha" in engine.containers
    assert "hestia-tenants-alpha" not in engine.networks


# ----------------------------------------------------------------------- stop


def test_stop_removes_the_container_then_its_network(engine) -> None:
    runtime = _runtime()
    _start(runtime, "alpha")
    runtime.stop("hestia-alpha")
    assert "hestia-alpha" not in engine.containers
    assert "hestia-tenants-alpha" not in engine.networks


def test_stop_keeps_a_network_something_else_still_uses(engine) -> None:
    runtime = _runtime()
    _start(runtime, "alpha")
    engine.add_container("debugger", "hestia-tenants-alpha", running=False)
    runtime.stop("hestia-alpha")
    assert "hestia-tenants-alpha" in engine.networks


def test_stop_raises_only_when_the_container_may_still_exist(engine) -> None:
    runtime = _runtime()
    _start(runtime, "alpha")
    engine.on(
        lambda a: a[:2] == ["rm", "-f"],
        stderr='Error response from daemon: cannot remove container "/hestia-alpha": could not kill',
        times=1,
    )
    with pytest.raises(RuntimeError, match="could not remove tenant container"):
        runtime.stop("hestia-alpha")
    assert "hestia-alpha" in engine.containers
    # A network that will not go is logged, never raised: the container is gone.
    engine.on(lambda a: a[:2] == ["network", "rm"], stderr="Error response from daemon: busy")
    runtime.stop("hestia-alpha")
    assert "hestia-alpha" not in engine.containers


@pytest.mark.parametrize("rc", [0, 1])
def test_stop_of_a_missing_container_is_fine_on_every_docker(engine, rc) -> None:
    engine.rm_missing_rc = rc
    _runtime().stop("hestia-ghost")


def test_stop_waits_out_a_removal_already_in_progress(engine) -> None:
    runtime = _runtime()
    _start(runtime, "alpha")

    def in_progress(args):
        engine.containers.pop("hestia-alpha", None)

    engine.on(
        lambda a: a[:2] == ["rm", "-f"],
        stderr="Error response from daemon: removal of container hestia-alpha is already in progress",
        times=1,
        action=None,
    )
    engine.on(lambda a: a[:2] == ["container", "inspect"], action=in_progress, times=1)
    runtime.stop("hestia-alpha")
    assert "hestia-alpha" not in engine.containers
    assert ["container", "inspect", "hestia-alpha"] in [c[1:] for c in engine.calls]
    assert "hestia-tenants-alpha" not in engine.networks


# ---------------------------------------------------------------------- state


def test_an_unreachable_engine_never_reads_as_a_missing_container(engine) -> None:
    runtime = _runtime()
    assert runtime.tenant_state("alpha") is None
    engine.on(
        lambda a: True,
        stderr="failed to connect to the docker API at tcp://dind:2376: lookup dind: no such host",
    )
    with pytest.raises(RuntimeError, match="could not inspect tenant container"):
        runtime.tenant_state("alpha")


def test_isolated_only_on_its_own_valid_network(engine) -> None:
    runtime = _runtime()
    engine.add_network("hestia-tenants", internal=True)
    engine.add_container("hestia-legacy", "hestia-tenants")
    assert runtime.is_isolated("legacy") is False
    _start(runtime, "alpha")
    assert runtime.is_isolated("alpha") is True
    engine.containers["hestia-alpha"]["NetworkSettings"]["Networks"]["hestia-tenants"] = {
        "IPAddress": "172.18.0.9"
    }
    assert runtime.is_isolated("alpha") is False


def test_retire_legacy_network_disconnects_everything_and_removes_it(engine) -> None:
    engine.add_network("hestia-tenants", internal=True)
    engine.add_container("hestia-orphan", "hestia-tenants", running=True)
    engine.add_container("hestia-stale", "hestia-tenants", running=False)
    report = retire_legacy_tenant_network("docker", "hestia-tenants")
    assert report["disconnected"] == ["hestia-orphan", "hestia-stale"]
    assert report["removed"] is True
    assert "hestia-tenants" not in engine.networks
    # Disconnected, not deleted: the ledger does not track them.
    assert engine.containers["hestia-orphan"]["NetworkSettings"]["Networks"] == {}


# ---------------------------------------------------------------------- argv


@pytest.mark.parametrize(
    "argv",
    [
        ["docker", "run", "--name", "hestia-alpha", "--net", "hestia-tenants-alpha", "img"],
        ["docker", "run", "--name", "hestia-alpha", "--net=hestia-tenants-alpha", "img"],
        ["docker", "run", "--name", "hestia-alpha", "--network=host", "img"],
        ["docker", "run", "--name", "hestia-alpha", "--network", "hestia-tenants-alpha",
         "--network", "hestia-tenants-beta", "img"],
        # The bare prefix is the shared bridge older versions used.
        ["docker", "run", "--name", "hestia-alpha", "--network", "hestia-tenants", "img"],
        ["docker", "run", "--name", "hestia-alpha", "--network", "hestia-tenants-beta", "img"],
    ],
)
def test_argv_lock_refuses_other_network_spellings_and_shared_bridges(argv) -> None:
    with pytest.raises(RuntimeError):
        assert_safe_docker_argv(argv)


def test_argv_lock_allows_the_tenant_network_and_none() -> None:
    assert_safe_docker_argv(["docker", "run", "--name", "hestia-alpha", "--network", "hestia-tenants-alpha", "img"])
    assert_safe_docker_argv(["docker", "run", "--name", "hestia-alpha", "--network", "none", "img"])


def test_tenant_labels_are_locked() -> None:
    with pytest.raises(RuntimeError, match="label"):
        _profile().docker_argv(
            docker_bin="docker",
            name="hestia-alpha",
            image="img",
            env={},
            labels={"com.example.evil": "x --privileged"},
        )


def test_allocation_avoids_foreign_networks_in_the_pool_on_the_first_try(engine) -> None:
    engine.add_network("someone-elses", internal=False, subnet="10.231.0.0/24")
    ensure_isolated_tenant_network("docker", "alpha", pool=POOL)
    creates = [c for c in engine.calls if c[1:3] == ["network", "create"]]
    assert len(creates) == 1
    assert engine.networks["hestia-tenants-alpha"]["IPAM"]["Config"][0]["Subnet"] == "10.231.1.0/29"


def test_stop_never_removes_a_same_named_network_it_does_not_own(engine) -> None:
    engine.add_network("hestia-tenants-alpha", internal=True, labels={}, subnet="10.231.5.0/29")
    _runtime().stop("hestia-alpha")
    assert "hestia-tenants-alpha" in engine.networks


@pytest.mark.parametrize(
    "variant", ["not-internal", "labelled", "foreign-container", "id-prefix", "disconnect-fails"]
)
def test_retire_legacy_network_leaves_anything_else_alone(engine, variant) -> None:
    name = "hestia-tenants"
    if variant == "not-internal":
        engine.add_network(name, internal=False)
    elif variant == "labelled":
        engine.add_network(name, internal=True, labels={"com.example.owner": "ops"})
    else:
        engine.add_network(name, internal=True)
    engine.add_container("hestia-orphan", name)
    if variant == "foreign-container":
        engine.add_container("ops-db", name)
    if variant == "disconnect-fails":
        engine.on(lambda a: a[:2] == ["network", "disconnect"], stderr="Error response from daemon: busy")
    if variant == "id-prefix":
        # `network inspect <x>` also resolves an ID prefix: a prefix that happens to
        # start another network's ID must not take that network apart.
        net = engine.networks.pop(name)
        net["Name"] = "ops-net"
        net["Id"] = "abc" + net["Id"][3:]
        engine.networks["ops-net"] = net
        engine.containers["hestia-orphan"]["NetworkSettings"]["Networks"] = {"ops-net": {"IPAddress": "172.18.0.2"}}
        name = "abc"
    report = retire_legacy_tenant_network("docker", name)
    assert report["removed"] is False and report["errors"]
    remaining = "ops-net" if variant == "id-prefix" else name
    assert remaining in engine.networks
    if variant != "disconnect-fails":
        assert report["disconnected"] == []
        assert engine.containers["hestia-orphan"]["NetworkSettings"]["Networks"]


def test_a_same_named_network_created_during_our_create_is_verified(engine) -> None:
    """Someone else wins the create race with a network that is not isolated."""
    name = "hestia-tenants-alpha"

    def race(args):
        engine.add_network(name, internal=False, labels={TENANT_NETWORK_LABEL: "alpha"})

    engine.on(lambda a: a[:2] == ["network", "create"], action=race, times=1)
    with pytest.raises(RuntimeError, match="not a private Hestia bridge"):
        ensure_isolated_tenant_network("docker", "alpha", pool=POOL)
    assert engine.networks[name]["Internal"] is False  # not ours to remove
