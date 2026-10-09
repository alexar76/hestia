"""capability_id is an identifier and a rendered label: reject markup at the door."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hestia.models import CapabilitySpec


def _spec(cap_id: str) -> CapabilitySpec:
    return CapabilitySpec(
        product_id="p",
        capability_id=cap_id,
        name="n",
        description="d",
        price_per_call_usd=0,
        input_schema={},
        output_schema={},
        publisher_id="pub",
    )


@pytest.mark.parametrize("cap_id", ["hestia.host.deploy@v1", "demo.echo@v1", "a-b_c@1.2.0+build"])
def test_valid_capability_ids(cap_id: str) -> None:
    assert _spec(cap_id).capability_id == cap_id


@pytest.mark.parametrize(
    "cap_id",
    [
        "x<svg/onload=alert(1)>@v1",  # the stored-XSS payload the public console once rendered
        'a"@v1',
        "a'@v1",
        "no-version",
        "@v1",
        "a@",
        "a b@v1",
    ],
)
def test_markup_and_malformed_capability_ids_refused(cap_id: str) -> None:
    with pytest.raises(ValidationError):
        _spec(cap_id)
