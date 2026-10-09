import pytest

from hestia.ssrf import assert_public_https


def test_https_public_hostname() -> None:
    try:
        assert assert_public_https("https://example.com/").startswith("https://")
    except ValueError as exc:
        if "does not resolve" in str(exc):
            pytest.skip("dns unavailable")
        raise


def test_userinfo_refused() -> None:
    with pytest.raises(ValueError):
        assert_public_https("https://a:b@example.com/")


def test_http_refused_outside_tests() -> None:
    with pytest.raises(ValueError):
        assert_public_https("http://example.com/")


def test_loopback_http_allowed_when_opted_in() -> None:
    assert_public_https("http://127.0.0.1:9480/", allow_http_loopback=True)


def test_ftp_and_empty_host_refused() -> None:
    with pytest.raises(ValueError):
        assert_public_https("ftp://example.com/")
    with pytest.raises(ValueError):
        assert_public_https("https:///")
    with pytest.raises(ValueError):
        assert_public_https("https://localhost/")
