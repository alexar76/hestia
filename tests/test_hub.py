import pytest

from hestia.hub import AnnounceError, announce


def test_private_hub_url_is_refused() -> None:
    with pytest.raises(AnnounceError, match="non-public"):
        announce(hub_url="https://10.0.0.8", hearth_url="https://example.com")


def test_userinfo_hub_url_is_refused() -> None:
    with pytest.raises(AnnounceError):
        announce(hub_url="https://a:b@example.com", hearth_url="http://127.0.0.1:9480")
