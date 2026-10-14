"""The seller's path with hestia-owner: init a folder, try the function locally, deploy it.

Before this the only door was `python -m hestia.owner_cli` from a clone, with --hearth
required on every call and the handler pasted into a JSON string by hand.
"""
from __future__ import annotations

import json

from hestia import owner_cli
from hestia.models import DeployRequest


def test_init_writes_a_function_and_a_deploy_body_that_validates(tmp_path, capsys):
    folder = tmp_path / "text-measure"
    assert owner_cli.main(["init", "--slug", "text-measure", "--dir", str(folder)]) == 0
    body = json.loads((folder / "deploy.json").read_text())
    body = owner_cli._with_handler(body, folder / "deploy.json")
    body["owner_pubkey"] = "A" * 43 + "="
    DeployRequest.model_validate(body)  # what the hearth will check
    assert "def handle(payload)" in body["source"]["handler"]
    assert "hestia-owner try" in capsys.readouterr().out


def test_try_runs_the_function_locally(tmp_path, capsys):
    folder = tmp_path / "a"
    owner_cli.main(["init", "--slug", "a-b", "--dir", str(folder)])
    capsys.readouterr()
    assert owner_cli.main(["try", "--dir", str(folder), "--input", '{"text": "hello big world"}']) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"characters": 15, "words": 3, "longest_word": "hello"}
    assert owner_cli.main(["try", "--dir", str(folder), "--input", "{}"]) == 1
    assert "send the text" in capsys.readouterr().out


def test_init_refuses_to_overwrite(tmp_path):
    folder = tmp_path / "x"
    folder.mkdir()
    (folder / "handler.py").write_text("keep me")
    assert owner_cli.main(["init", "--slug", "x", "--dir", str(folder)]) == 1
    assert (folder / "handler.py").read_text() == "keep me"


def test_hearth_defaults_to_the_reference_one(monkeypatch):
    monkeypatch.delenv("HESTIA_HEARTH", raising=False)
    sent = {}

    def fake_send(hearth, key, method, path, body=b""):
        sent.update(hearth=hearth, path=path, body=json.loads(body or b"{}"))
        return 0

    monkeypatch.setattr(owner_cli, "_send", fake_send)
    monkeypatch.setattr(owner_cli, "_load_key", lambda _p: owner_cli.Ed25519PrivateKey.generate())
    assert owner_cli.main(["me", "--key", "k"]) == 0
    assert sent["hearth"] == owner_cli.DEFAULT_HEARTH


def test_deploy_fills_the_handler_and_the_payout(tmp_path, monkeypatch):
    folder = tmp_path / "s"
    owner_cli.main(["init", "--slug", "s-agent", "--dir", str(folder)])
    sent = {}

    def fake_send(hearth, key, method, path, body=b""):
        sent.update(path=path, body=json.loads(body))
        return 0

    monkeypatch.setattr(owner_cli, "_send", fake_send)
    monkeypatch.setattr(owner_cli, "_load_key", lambda _p: owner_cli.Ed25519PrivateKey.generate())
    assert owner_cli.main(["deploy", "--key", "k", "--body", str(folder / "deploy.json"),
                           "--payout", "0x" + "ab" * 20]) == 0
    assert sent["path"] == "/v1/tenants"
    assert "def handle" in sent["body"]["source"]["handler"]
    assert sent["body"]["payout_address"] == "0x" + "ab" * 20
    assert len(sent["body"]["owner_pubkey"]) == 44


def test_the_template_passes_the_hearths_own_admission():
    from hestia import scan

    scan.admit_handler(owner_cli._HANDLER)
    scan.lint_wasm_handler(owner_cli._HANDLER)
    scan.admit_handler(owner_cli._HANDLER, deterministic=True)
