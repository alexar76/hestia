"""A hostile payload must not be able to burn a core forever.

The stub is not a sandbox and never will be — it is a subprocess with rlimits
and a per-call deadline. What these lock down is that the deadline actually
fires on the case it exists for: a caller-supplied regular expression with
catastrophic backtracking, which CPython cannot abort between bytecodes because
the whole match is one C call.
"""

from __future__ import annotations

import time

import pytest

from hestia.runtime.stub import DEFAULT_MEMORY_CAP_MB, minimal_tenant_env
from hestia.tenant_stub import CallTimeout, call_timeout_s, run_with_deadline
from tests.conftest import auth, client, deploy_payload

# The handler a paying caller can reach: it compiles a pattern the caller sends.
REGEX_HANDLER = (
    "import re\n"
    "def handle(payload):\n"
    "    pattern = payload.get('pattern', '')\n"
    "    subject = payload.get('subject', '')\n"
    "    return {'matched': re.fullmatch(pattern, subject) is not None}\n"
)

# 6 characters. Against 41 characters of input this runs effectively forever.
EVIL_PATTERN = "(a+)+$"
EVIL_SUBJECT = "a" * 40 + "!"


def test_the_deadline_aborts_catastrophic_backtracking() -> None:
    import re

    started = time.time()
    with pytest.raises(CallTimeout):
        run_with_deadline(lambda: re.fullmatch(EVIL_PATTERN, EVIL_SUBJECT), 1)
    # It must actually stop, and stop near the budget rather than at some
    # multiple of it.
    assert time.time() - started < 5


def test_a_finished_call_clears_the_alarm() -> None:
    # A budget left armed would fire during the NEXT call and kill innocent work.
    assert run_with_deadline(lambda: 2 + 2, 5) == 4
    time.sleep(0.2)
    assert run_with_deadline(lambda: 2 + 2, 5) == 4


def test_the_deadline_can_be_disabled_but_is_on_by_default(monkeypatch) -> None:
    assert call_timeout_s() == 10
    monkeypatch.setenv("HESTIA_TENANT_CALL_TIMEOUT_S", "0")
    assert call_timeout_s() == 0
    # Disabled means the call runs unguarded, not that it is refused.
    assert run_with_deadline(lambda: "done", 0) == "done"
    monkeypatch.setenv("HESTIA_TENANT_CALL_TIMEOUT_S", "nonsense")
    assert call_timeout_s() == 10


def test_the_memory_cap_is_on_by_default() -> None:
    # An opt-in ceiling protects nobody; this asserts the default is a number,
    # not zero.
    assert DEFAULT_MEMORY_CAP_MB >= 128


def test_the_budget_reaches_the_child_env(tmp_path) -> None:
    env = minimal_tenant_env(
        slug="x",
        cap_file=tmp_path / "c.json",
        key_file=tmp_path / "k",
        handler_file=None,
        bind="127.0.0.1",
        port=1,
        home=tmp_path,
    )
    assert env["HESTIA_TENANT_CALL_TIMEOUT_S"] == "10"
    # And the operator's secrets still do not.
    assert "HESTIA_DEPLOY_TOKEN" not in env


def test_a_hostile_payload_gets_504_and_the_agent_survives(tmp_path) -> None:
    """End to end through a real tenant process: the call is cut off, the agent
    keeps serving, and the caller is told what happened."""
    api = client(tmp_path)
    assert api.post(
        "/v1/tenants", json=deploy_payload("re-agent", REGEX_HANDLER), headers=auth(api)
    ).status_code == 200

    started = time.time()
    res = api.post(
        "/t/re-agent/invoke",
        json={"pattern": EVIL_PATTERN, "subject": EVIL_SUBJECT},
    )
    elapsed = time.time() - started

    assert res.status_code == 504
    assert "budget" in res.json()["error"]
    # The default budget is 10s and the edge gives up at 20s; the point is that
    # the tenant answers rather than the edge timing out.
    assert elapsed < 20

    # And the agent is still alive for the next caller.
    ok = api.post("/t/re-agent/invoke", json={"pattern": "a+", "subject": "aaa"})
    assert ok.status_code == 200
    assert ok.json()["result"] == {"matched": True}
