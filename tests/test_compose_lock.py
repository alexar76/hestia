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
    assert "HESTIA_WASM_ROOT=/opt/python-wasi" in text
    assert "fetch_python_wasi.py /opt/python-wasi" in text
    assert "HESTIA_ALLOW_HOST_DOCKER=0" in text
    assert "COPY docs/landing" in text
    assert '".[postgres,pqc,wasm]"' in text
    assert "HESTIA_PQC=1" in text
    assert "alembic" not in text
    assert "sqlalchemy" not in text


def test_compose_hestia_cannot_shell_the_host() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    assert text.count("  hestia:") == 1
    assert "/var/run/docker.sock" not in text
    assert "/run/docker.sock" not in text
    assert "pid: host" not in text
    assert "ipc: host" not in text
    assert "uts: host" not in text
    # The only network_mode is the sandbox runner's "none": no host networking anywhere.
    assert "network_mode: host" not in text
    assert text.count("network_mode:") == 1 and "network_mode: none" in text
    assert "- /:" not in text
    assert "cap_add:" not in text
    assert "privileged: true" not in text
    assert "docker:27-dind" not in text
    assert "dind:" not in text
    assert 'HESTIA_RUNTIME: "${HESTIA_RUNTIME:-stub}"' in text
    assert 'HESTIA_ALLOW_HOST_DOCKER: "0"' in text
    assert 'HESTIA_MAX_TENANTS: "${HESTIA_MAX_TENANTS:-8}"' in text
    assert 'user: "65532:65532"' in text
    assert "privileged: false" in text
    assert "read_only: true" in text
    assert "cap_drop:" in text
    assert "- ALL" in text
    assert "no-new-privileges:true" in text
    assert "127.0.0.1:9480:9480" in text
    assert "hestia-data:/data" in text
    assert "- /tmp" in text
    assert 'HESTIA_PUBLIC_BASE: "${HESTIA_PUBLIC_BASE:-http://127.0.0.1:9480}"' in text
    assert 'HESTIA_DATABASE_URL: "${HESTIA_DATABASE_URL:-}"' in text
    assert "hestia-postgres" not in text
    assert "HESTIA_PROFILE" not in text
    assert "HESTIA_REQUIRE_SANDBOX" not in text


def test_dockerignore_keeps_readme_for_docker_build() -> None:
    text = DOCKERIGNORE.read_text(encoding="utf-8")
    assert "README.md" not in text.splitlines()
    assert "*.md" not in text.splitlines()
    assert "docs" in text
    assert "!docs/landing/" in text
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
    assert "HESTIA_POSTGRES_PASSWORD" in script
    assert "docker-compose.postgres.yml" in script
    assert "chmod 600" in script
    assert "hestia.modelmarket.dev" in script
    assert "/var/run/docker.sock" not in script
    assert "Live cert already present" in script
    assert "host_ips" in script
    assert "--remote" in script
    assert "Do not invent a box" in nginx
    # A wasm hearth needs the runner's compose profile, or the runner is neither built nor
    # restarted and the hearth keeps calling an old one.
    assert "--profile wasm" in script and "hestia-runner" in script
    # Migrations apply on boot: the ledger is dumped and images tagged before anything changes.
    assert "pg_dump" in script and "pre-deploy-" in script
    # A host the A record does not point at would run a second ledger.
    assert "--new-host" in script and "getent ahostsv4" in script
    # hestia/.env wins; the remote wrapper never digs a token out of files or containers.
    remote = script.split("install_remote() {", 1)[1].split("\n}\n", 1)[0]
    assert "HESTIA_DEPLOY_TOKEN" not in remote and "docker inspect" not in remote
    assert "docker network create" not in script


def test_postgres_overlay_is_dedicated_and_unpublished() -> None:
    overlay = ROOT / "docker-compose.postgres.yml"
    text = overlay.read_text(encoding="utf-8")
    assert "hestia-postgres:" in text
    assert "POSTGRES_DB: hestia" in text
    assert "pg_isready" in text
    assert "HESTIA_DATABASE_URL:" in text
    assert "hestia-postgres:5432/hestia" in text
    assert "/var/run/docker.sock" not in text
    assert "privileged: true" not in text
    assert "5432:5432" not in text
    assert 'HESTIA_RUNTIME: "${HESTIA_RUNTIME:-stub}"' in text
    assert 'HESTIA_ALLOW_HOST_DOCKER: "0"' in text
    assert "HESTIA_REPLICAS" not in text
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "alembic" not in pyproject
    assert "sqlalchemy" not in pyproject
    assert not (ROOT / "hestia" / "schema_alembic.py").exists()
    assert not (ROOT / "hestia" / "alembic").exists()


def test_the_sandbox_runner_gets_no_network_and_no_secrets() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    runner = text.split("  hestia-runner:", 1)[1].split("\nvolumes:", 1)[0]
    assert "network_mode: none" in runner
    assert 'user: "65532:65532"' in runner and "read_only: true" in runner
    assert "- ALL" in runner and "no-new-privileges:true" in runner
    assert "hestia-data" not in runner, "the runner must not see the hearth's data or keys"
    for secret in ("TOKEN", "DATABASE_URL", "KEY", "SECRET", "PASSWORD", "env_file"):
        assert secret not in runner, secret


def test_cpu_has_a_ceiling_like_memory_and_pids() -> None:
    """A handler may spin to its deadline. Without a CPU ceiling a few of them take whole
    cores of a host that runs other services too."""
    text = COMPOSE.read_text(encoding="utf-8")
    hearth = text.split("  hestia:", 1)[1].split("  hestia-runner:", 1)[0]
    runner = text.split("  hestia-runner:", 1)[1].split("\nvolumes:", 1)[0]
    assert "cpus: ${HESTIA_CPUS:-2}" in hearth
    assert "cpus: ${HESTIA_RUNNER_CPUS:-1.5}" in runner
    # The hearth sizes the open lane against the runner's workers: one variable for both.
    workers = 'HESTIA_RUNNER_WORKERS: "${HESTIA_RUNNER_WORKERS:-2}"'
    assert workers in hearth and workers in runner
    assert 'HESTIA_OPEN_LANE_SLOTS: "${HESTIA_OPEN_LANE_SLOTS:-1}"' in hearth
