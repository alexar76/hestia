from unittest.mock import MagicMock

import pytest

from hestia.hub import AnnounceError, announce
from hestia.models import CapabilitySpec
from hestia.themis import AdmissionDenied, admit


def _cap() -> CapabilitySpec:
    return CapabilitySpec(
        product_id="x",
        capability_id="x.y@v1",
        name="x",
        description="enough text",
        price_per_call_usd=0.001,
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        publisher_id="t",
    )


def test_announce_success(monkeypatch) -> None:
    response = MagicMock()
    response.json.return_value = {"ok": True}
    monkeypatch.setattr("hestia.hub.httpx.post", lambda *a, **k: response)
    body = announce(hub_url="http://127.0.0.1:1", hearth_url="http://127.0.0.1:9480")
    assert body == {"ok": True}


def test_announce_http_error(monkeypatch) -> None:
    import httpx

    def boom(*_a, **_k):
        raise httpx.ConnectError("nope")

    monkeypatch.setattr("hestia.hub.httpx.post", boom)
    with pytest.raises(AnnounceError, match="failed"):
        announce(hub_url="http://127.0.0.1:1", hearth_url="http://127.0.0.1:9480")


@pytest.mark.parametrize(
    ("decision", "admit_review", "ok"),
    [
        ("approve", False, True),
        ("review", True, True),
        ("review", False, False),
        ("reject", False, False),
        ("", False, False),
    ],
)
def test_themis_decisions(monkeypatch, decision, admit_review, ok) -> None:
    response = MagicMock()
    response.json.return_value = {"result": {"decision": decision}}
    monkeypatch.setattr("hestia.themis.httpx.post", lambda *a, **k: response)
    if ok:
        admit(url="http://127.0.0.1:9", capability=_cap(), admit_review=admit_review)
        return
    with pytest.raises(AdmissionDenied):
        admit(url="http://127.0.0.1:9", capability=_cap(), admit_review=admit_review)
