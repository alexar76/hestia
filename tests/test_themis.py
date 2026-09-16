from __future__ import annotations

import pytest

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


def test_themis_unreachable_fails_closed() -> None:
    with pytest.raises(AdmissionDenied, match="unreachable"):
        admit(url="http://127.0.0.1:9", capability=_cap(), admit_review=False, timeout=0.2)


def test_themis_private_url_fails_closed() -> None:
    with pytest.raises(AdmissionDenied, match="non-public"):
        admit(url="https://169.254.169.254/", capability=_cap(), admit_review=False)
