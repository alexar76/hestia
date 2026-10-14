"""The body ceiling must survive a request that declares no length.

A Content-Length test alone is not a ceiling: chunked requests carry no such
header, and `/ai-market/v2/invoke` parses its body before it can know whether
the capability needs auth.
"""

from __future__ import annotations

from hestia.limits import DEFAULT_MAX_BODY_BYTES
from tests.conftest import client

CAP = "hestia.hearth.list@v1"


def _chunked(total: int):
    pad = b"A" * 8192
    sent = 0

    def gen():
        nonlocal sent
        yield b'{"capability_id":"' + CAP.encode() + b'","pad":"'
        while sent < total:
            yield pad
            sent += len(pad)
        yield b'"}'

    return gen()


def test_chunked_body_over_the_cap_is_refused(tmp_path) -> None:
    api = client(tmp_path)
    res = api.post("/ai-market/v2/invoke", content=_chunked(4 * 1024 * 1024))
    assert res.status_code == 413
    assert res.json()["error"] == "body too large"


def test_chunked_body_under_the_cap_still_works(tmp_path) -> None:
    api = client(tmp_path)
    res = api.post("/ai-market/v2/invoke", content=_chunked(8192))
    assert res.status_code == 200
    assert res.json()["result"]["ok"] is True


def test_declared_length_over_the_cap_is_refused(tmp_path) -> None:
    api = client(tmp_path)
    res = api.post("/ai-market/v2/invoke", content=b"x" * (DEFAULT_MAX_BODY_BYTES + 8))
    assert res.status_code == 413


def test_custom_max_body_bytes_is_honored(tmp_path) -> None:
    api = client(tmp_path, max_body_bytes=1024)
    over = api.post("/ai-market/v2/invoke", content=b"x" * 2048)
    assert over.status_code == 413
    under = api.post("/ai-market/v2/invoke", json={"capability_id": CAP})
    assert under.status_code == 200


def test_unparsable_content_length_is_a_400_not_a_crash(tmp_path) -> None:
    api = client(tmp_path)
    res = api.request(
        "POST",
        "/ai-market/v2/invoke",
        content=b"{}",
        headers={"Content-Length": "not-a-number"},
    )
    assert res.status_code == 400
    assert res.json()["error"] == "bad content-length"


def test_plain_get_is_untouched(tmp_path) -> None:
    api = client(tmp_path)
    assert api.get("/health").status_code == 200
