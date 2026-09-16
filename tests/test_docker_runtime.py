from unittest.mock import MagicMock

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


def test_listen_url_inspect_fallback(monkeypatch) -> None:
    digest = "sha256:" + ("ab" * 32)
    runtime = DockerRuntime("docker", frozenset({digest}))
    monkeypatch.setattr(
        "hestia.runtime.docker.subprocess.run",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("no inspect")),
    )
    assert runtime._listen_url("hestia-x") == "http://hestia-x:8080"
    monkeypatch.setattr(
        "hestia.runtime.docker.subprocess.run",
        lambda *_a, **_k: MagicMock(returncode=0, stdout="not-an-ip\n"),
    )
    assert runtime._listen_url("hestia-x") == "http://hestia-x:8080"


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
