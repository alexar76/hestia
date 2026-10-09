
import pytest

from hestia.models import CapabilitySpec
from hestia.policy import IsolationProfile
from hestia.runtime.docker import DockerRuntime


def test_capability_id_and_schema_rejected() -> None:
    from pydantic import ValidationError

    from hestia.models import CapabilitySpec, PinnedImageSource

    with pytest.raises(ValidationError):
        CapabilitySpec(
            product_id="x",
            capability_id="no-version",
            name="x",
            description="enough",
            price_per_call_usd=0,
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            publisher_id="t",
        )
    with pytest.raises(ValidationError):
        CapabilitySpec(
            product_id="x",
            capability_id="x.y@v1",
            name="x",
            description="enough",
            price_per_call_usd=0,
            input_schema={"type": "array"},
            output_schema={"type": "object"},
            publisher_id="t",
        )
    with pytest.raises(ValidationError):
        PinnedImageSource(image_digest="sha256:zzzz")
    with pytest.raises(ValidationError):
        PinnedImageSource(image_digest="sha256:" + ("gg" * 32))
    runtime = DockerRuntime("docker", frozenset())
    with pytest.raises(RuntimeError, match="not on HESTIA_ALLOW_IMAGE_DIGESTS"):
        runtime.start(
            slug="x",
            capability=CapabilitySpec(
                product_id="x",
                capability_id="x.y@v1",
                name="x",
                description="enough",
                price_per_call_usd=0,
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                publisher_id="t",
            ),
            handler="",
            image_digest="sha256:" + ("ab" * 32),
            profile=IsolationProfile(
                cpus="0.5",
                memory_mb=256,
                pids=64,
                user="65532:65532",
                read_only=True,
                no_new_privileges=True,
                cap_drop=("ALL",),
                tmpfs_mb=32,
                network="none",
                egress_allowlist=(),
            ),
            env={},
        )


def test_start_refuses_a_tenant_without_an_address_on_its_network(monkeypatch) -> None:
    """There is no `http://hestia-<slug>:8080` fallback any more: a host process
    cannot resolve that name, so it only ever produced a dead listen_url."""
    from tests.fake_engine import FakeEngine

    engine = FakeEngine().install(monkeypatch)
    real = engine._run_container

    def no_address(rest):
        rc, out, err = real(rest)
        for net in engine.containers["hestia-noaddr"]["NetworkSettings"]["Networks"].values():
            net["IPAddress"] = ""
        return rc, out, err

    engine._run_container = no_address
    digest = "sha256:" + ("ab" * 32)
    runtime = DockerRuntime("docker", frozenset({digest}))
    with pytest.raises(RuntimeError, match="no IPv4 address"):
        runtime.start(
            slug="noaddr",
            capability=CapabilitySpec(
                product_id="x",
                capability_id="x.y@v1",
                name="x",
                description="enough",
                price_per_call_usd=0,
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                publisher_id="t",
            ),
            handler="",
            image_digest=digest,
            profile=IsolationProfile(
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
            ),
            env={},
        )
    # What that start created is gone again.
    assert "hestia-noaddr" not in engine.containers
    assert "hestia-tenants-noaddr" not in engine.networks


def test_docker_missing_binary(monkeypatch) -> None:
    monkeypatch.setattr("hestia.runtime.docker.shutil.which", lambda _bin: None)
    digest = "sha256:" + ("ab" * 32)
    runtime = DockerRuntime("docker", frozenset({digest}))
    with pytest.raises(RuntimeError, match="not on PATH"):
        runtime.start(
            slug="x",
            capability=CapabilitySpec(
                product_id="x",
                capability_id="x.y@v1",
                name="x",
                description="enough",
                price_per_call_usd=0,
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                publisher_id="t",
            ),
            handler="",
            image_digest=digest,
            profile=IsolationProfile(
                cpus="0.5",
                memory_mb=256,
                pids=64,
                user="65532:65532",
                read_only=True,
                no_new_privileges=True,
                cap_drop=("ALL",),
                tmpfs_mb=32,
                network="none",
                egress_allowlist=(),
            ),
            env={},
        )
