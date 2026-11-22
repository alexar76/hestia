"""What the hearth publishes about the code it runs: each agent's handler_sha256 on the public
roster, and the operator's agent catalogue at /agents/ (docs/agents, built by
hestia-agents/scripts/build_catalog.py) whose sources those digests can be checked against."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tests.conftest import auth, client, deploy_payload
from tests.test_wasm_runtime import HANDLER, js_payload, scripted_runner, wasm_client  # noqa: F401

CATALOGUE = Path(__file__).resolve().parent.parent / "docs" / "agents"


def _roster(c) -> dict[str, dict]:
    return {t["slug"]: t for t in c.get("/v1/hearth").json()["tenants"]}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_the_roster_gives_the_digest_of_the_handler_each_call_runs(tmp_path, scripted_runner) -> None:  # noqa: F811
    socket_path, sandbox = scripted_runner
    c = wasm_client(tmp_path, socket_path)
    assert c.post("/v1/tenants", json=deploy_payload(handler=HANDLER), headers=auth(c)).status_code == 200
    js = js_payload(slug="js-agent")
    js["capability"]["capability_id"] = "demo.js@v1"
    assert c.post("/v1/tenants", json=js, headers=auth(c)).status_code == 200
    roster = _roster(c)
    assert roster["demo-echo"]["handler_sha256"] == _sha(HANDLER)
    assert roster["js-agent"]["handler_sha256"] == _sha(js["source"]["handler"])
    # The digest is of the very bytes the sandbox is handed on a call.
    assert c.post("/t/demo-echo/invoke", json={}).status_code == 200
    assert _sha(sandbox.calls[-1][0]) == roster["demo-echo"]["handler_sha256"]


def test_a_redeploy_changes_the_digest_even_at_the_same_size_and_instant(tmp_path, scripted_runner) -> None:  # noqa: F811
    socket_path, _ = scripted_runner
    c = wasm_client(tmp_path, socket_path)
    first = "def handle(p):\n    return {'a': 1}\n"
    second = "def handle(p):\n    return {'b': 1}\n"
    assert len(first) == len(second)
    assert c.post("/v1/tenants", json=deploy_payload(handler=first), headers=auth(c)).status_code == 200
    assert _roster(c)["demo-echo"]["handler_sha256"] == _sha(first)
    handler = tmp_path / "tenants" / "demo-echo" / "handler.py"
    stamp = handler.stat()
    assert c.post("/v1/tenants", json=deploy_payload(handler=second), headers=auth(c)).status_code == 200
    # Pretend the file system could not tell the two writes apart.
    import os

    os.utime(handler, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert _roster(c)["demo-echo"]["handler_sha256"] == _sha(second)


def test_a_restarted_hearth_reports_the_same_digest(tmp_path, scripted_runner) -> None:  # noqa: F811
    socket_path, _ = scripted_runner
    c = wasm_client(tmp_path, socket_path)
    assert c.post("/v1/tenants", json=deploy_payload(handler=HANDLER), headers=auth(c)).status_code == 200
    again = wasm_client(tmp_path, socket_path)
    assert _roster(again)["demo-echo"]["handler_sha256"] == _sha(HANDLER)


def test_a_hearth_without_the_sandbox_publishes_no_digest(tmp_path) -> None:
    c = client(tmp_path)
    assert c.post("/v1/tenants", json=deploy_payload(), headers=auth(c)).status_code == 200
    assert "handler_sha256" not in _roster(c)["demo-echo"]


# ------------------------------------------------------------------ /agents/

needs_catalogue = pytest.mark.skipif(not (CATALOGUE / "catalog.json").is_file(),
                                     reason="docs/agents is built by hestia-agents/scripts/build_catalog.py")


@needs_catalogue
def test_the_catalogue_is_served_at_agents(tmp_path) -> None:
    c = client(tmp_path)
    moved = c.get("/agents", follow_redirects=False)
    assert moved.status_code == 308 and moved.headers["location"] == "/agents/"
    page = c.get("/agents/")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert '<script src="live.js"></script>' in page.text
    for lang in ("ru", "es", "fr", "zh"):
        assert c.get(f"/agents/index.{lang}.html").status_code == 200
    script = c.get("/agents/live.js")
    assert script.status_code == 200 and "javascript" in script.headers["content-type"]


@needs_catalogue
def test_published_sources_are_text_and_match_their_digests(tmp_path) -> None:
    c = client(tmp_path)
    catalog = c.get("/agents/catalog.json")
    assert catalog.headers["content-type"].startswith("application/json")
    agents = catalog.json()["agents"]
    assert agents
    sums = {}
    listing = c.get("/agents/SHA256SUMS")
    assert listing.headers["content-type"] == "text/plain; charset=utf-8"
    for line in listing.text.splitlines():
        digest, name = line.split("  ", 1)
        sums[name] = digest
    for agent in agents:
        served = c.get("/agents/" + agent["source_file"])
        # Shown, never run: a handler.js is not served as a script, a .py not as a download.
        assert served.status_code == 200
        assert served.headers["content-type"] == "text/plain; charset=utf-8", agent["source_file"]
        digest = hashlib.sha256(served.content).hexdigest()
        assert digest == agent["handler_sha256"] == sums[agent["source_file"]]
    for name in ("tests/test_agents.py", "docs/AGENTS.md", "docs/AGENTS.zh.md"):
        served = c.get("/agents/" + name)
        assert served.status_code == 200 and served.headers["content-type"] == "text/plain; charset=utf-8"
    data = next(n for n in sums if n.startswith("tests/") and n.endswith(".json"))
    assert c.get("/agents/" + data).headers["content-type"] == "text/plain; charset=utf-8"


@needs_catalogue
def test_the_catalogue_serves_nothing_outside_its_folder(tmp_path) -> None:
    c = client(tmp_path)
    for path in ("/agents/../hestia/app.py", "/agents/%2e%2e/%2e%2e/hestia/app.py",
                 "/agents/..%2f..%2fhestia%2fapp.py", "/agents/no-such-file.py"):
        answer = c.get(path)
        assert answer.status_code == 404 or "def build_app" not in answer.text, path
    assert c.get("/agents/src/").status_code == 404  # no directory listing
    json.loads(c.get("/agents/catalog.json").text)
