"""Stub tenant resource guard: safe by default, real cap only when asked.

setrlimit is captured rather than applied, so the guard's intent is asserted
without mutating the test runner's own address space.
"""

from __future__ import annotations

import pytest

from hestia.runtime import stub


@pytest.mark.skipif(stub.resource is None, reason="POSIX rlimits unavailable")
def test_core_dumps_disabled_by_default(monkeypatch) -> None:
    calls: list[tuple[int, tuple[int, int]]] = []
    monkeypatch.delenv("HESTIA_STUB_MEMORY_CAP_MB", raising=False)
    monkeypatch.setattr(stub.resource, "setrlimit", lambda res, lim: calls.append((res, lim)))
    apply = stub._stub_rlimit_preexec()
    assert callable(apply)
    apply()
    assert (stub.resource.RLIMIT_CORE, (0, 0)) in calls
    # The address-space cap is now ON by default. It used to be opt-in, which
    # meant the ceiling protected nobody unless an operator had already thought
    # about it — and the people who need it most are the ones who have not.
    expected = stub.DEFAULT_MEMORY_CAP_MB * 1024 * 1024
    assert (stub.resource.RLIMIT_AS, (expected, expected)) in calls


@pytest.mark.skipif(stub.resource is None, reason="POSIX rlimits unavailable")
def test_memory_cap_can_be_turned_off(monkeypatch) -> None:
    calls: list[tuple[int, tuple[int, int]]] = []
    monkeypatch.setenv("HESTIA_STUB_MEMORY_CAP_MB", "0")
    monkeypatch.setattr(stub.resource, "setrlimit", lambda res, lim: calls.append((res, lim)))
    stub._stub_rlimit_preexec()()
    # Core dumps stay off regardless — that is not a tunable.
    assert (stub.resource.RLIMIT_CORE, (0, 0)) in calls
    assert all(res != stub.resource.RLIMIT_AS for res, _ in calls)


@pytest.mark.skipif(stub.resource is None, reason="POSIX rlimits unavailable")
def test_memory_cap_applied_when_requested(monkeypatch) -> None:
    calls: list[tuple[int, tuple[int, int]]] = []
    monkeypatch.setenv("HESTIA_STUB_MEMORY_CAP_MB", "512")
    monkeypatch.setattr(stub.resource, "setrlimit", lambda res, lim: calls.append((res, lim)))
    stub._stub_rlimit_preexec()()
    want = 512 * 1024 * 1024
    assert (stub.resource.RLIMIT_AS, (want, want)) in calls


def test_bad_cap_value_is_ignored(monkeypatch) -> None:
    monkeypatch.setenv("HESTIA_STUB_MEMORY_CAP_MB", "not-a-number")
    result = stub._stub_rlimit_preexec()
    assert result is None or callable(result)
