"""The Hestia *box* must not be a shell onto the host."""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.yml"
DOCKERFILE = ROOT / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"
MONOREPO = ROOT.parent
NGINX = MONOREPO / "deploy" / "nginx" / "hestia.modelmarket.dev.conf"
DEPLOY = MONOREPO / "scripts" / "deploy_hestia.sh"


def test_dockerfile_has_no_docker_cli() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "FROM docker:" not in text
    assert "dockercli" not in text
    assert "docker.sock" not in text
    assert "USER 65532:65532" in text
    assert "HESTIA_RUNTIME=stub" in text
    assert "HESTIA_ALLOW_HOST_DOCKER=0" in text


def test_compose_hestia_cannot_shell_the_host() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    assert text.count("  hestia:") == 1
    assert "/var/run/docker.sock" not in text
    assert "/run/docker.sock" not in text
    assert "pid: host" not in text
    assert "ipc: host" not in text
    assert "uts: host" not in text
    assert "network_mode:" not in text
    assert "- /:" not in text
    assert "cap_add:" not in text
    assert "privileged: true" not in text
    assert "docker:27-dind" not in text
    assert "dind:" not in text
    assert "HESTIA_RUNTIME: stub" in text
    assert 'HESTIA_ALLOW_HOST_DOCKER: "0"' in text
    assert 'user: "65532:65532"' in text
    assert "privileged: false" in text
    assert "read_only: true" in text
    assert "cap_drop:" in text
    assert "- ALL" in text
    assert "no-new-privileges:true" in text
    assert "127.0.0.1:9480:9480" in text
    assert "hestia-data:/data" in text
    assert "- /tmp" in text
    assert "HESTIA_PUBLIC_BASE: \"${HESTIA_PUBLIC_BASE:-http://127.0.0.1:9480}\"" in text


def test_dockerignore_keeps_readme_for_docker_build() -> None:
    text = DOCKERIGNORE.read_text(encoding="utf-8")
    assert "README.md" not in text.splitlines()
    assert "*.md" not in text.splitlines()
    assert "docs" in text
    assert "tests" in text
    assert ".env" in text
    assert "data" in text


def test_fleet_deploy_artifacts_when_present() -> None:
    if not NGINX.is_file() or not DEPLOY.is_file():
        pytest.skip("satellite export — nginx/deploy live in the monorepo")
    nginx = NGINX.read_text(encoding="utf-8")
    assert "127.0.0.1:9480" in nginx
    assert "hestia.modelmarket.dev" in nginx
    assert "docker.sock" not in nginx
    assert "privileged" not in nginx
    script = DEPLOY.read_text(encoding="utf-8")
    assert "HESTIA_DEPLOY_TOKEN" in script
    assert "hestia.modelmarket.dev" in script
    assert "/var/run/docker.sock" not in script
    assert "Do not invent a box" in nginx
