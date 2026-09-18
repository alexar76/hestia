"""The runtime containment guard is the backstop for a scanner miss.

These tests call blocked operations directly (as if a gadget had defeated the AST
scanner) and assert the audit hook refuses the dangerous effect while leaving a
pure handler's own work untouched.
"""

from __future__ import annotations

import os

import pytest

from hestia.handler_guard import HandlerContainmentError, guarded, install


@pytest.fixture()
def tenant_tree(tmp_path):
    data = tmp_path / "data"
    (data / "tenants" / "victim").mkdir(parents=True)
    (data / "tenants" / "attacker").mkdir(parents=True)
    (data / "provider.key").write_text("PROVIDER_SECRET")
    (data / "tenants" / "victim" / "tenant.key").write_text("VICTIM_SECRET")
    install()
    return data


def test_reading_provider_key_is_blocked(tenant_tree):
    attacker = tenant_tree / "tenants" / "attacker"
    with pytest.raises(HandlerContainmentError):
        with guarded(attacker):
            open(tenant_tree / "provider.key").read()


def test_reading_sibling_tenant_key_is_blocked(tenant_tree):
    attacker = tenant_tree / "tenants" / "attacker"
    with pytest.raises(HandlerContainmentError):
        with guarded(attacker):
            open(tenant_tree / "tenants" / "victim" / "tenant.key").read()


def test_path_climb_out_of_tenant_dir_is_blocked(tenant_tree):
    attacker = tenant_tree / "tenants" / "attacker"
    with pytest.raises(HandlerContainmentError):
        with guarded(attacker):
            open(os.path.join(str(attacker), "..", "..", "provider.key")).read()


def test_spawn_and_socket_and_native_are_blocked(tenant_tree):
    attacker = tenant_tree / "tenants" / "attacker"
    for fn in (
        lambda: os.system("echo x"),
        lambda: __import__("subprocess").run(["echo", "x"]),
        lambda: __import__("socket").getaddrinfo("example.com", 80),
        lambda: os.remove(str(tenant_tree / "provider.key")),
        lambda: os.scandir(str(tenant_tree)),
    ):
        with pytest.raises(HandlerContainmentError):
            with guarded(attacker):
                fn()


def test_pure_handler_work_is_allowed(tenant_tree):
    attacker = tenant_tree / "tenants" / "attacker"
    import decimal
    import hashlib
    import json

    with guarded(attacker):
        # arithmetic / hashing / json / a not-yet-imported stdlib module
        assert hashlib.sha256(b"x").hexdigest()
        assert json.dumps({"a": 1}) == '{"a": 1}'
        assert str(decimal.Decimal("1.5")) == "1.5"
        assert __import__("statistics").mean([1, 2, 3]) == 2
        # its own directory is writable/readable
        scratch = attacker / "scratch"
        scratch.write_text("ok")
        assert scratch.read_text() == "ok"


def test_guard_disarms_on_exit(tenant_tree):
    attacker = tenant_tree / "tenants" / "attacker"
    with guarded(attacker):
        pass
    # Outside the block the control plane's own file access is unrestricted again.
    assert (tenant_tree / "provider.key").read_text() == "PROVIDER_SECRET"


def test_nested_guard_restores_previous_dir(tenant_tree):
    outer = tenant_tree / "tenants" / "attacker"
    inner = tenant_tree / "tenants" / "victim"
    with guarded(outer):
        with guarded(inner):
            # inner tenant may touch its own dir
            (inner / "s").write_text("x")
        # back to outer: the inner dir is now off-limits again
        with pytest.raises(HandlerContainmentError):
            open(inner / "tenant.key").read()
