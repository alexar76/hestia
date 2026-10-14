import pytest

from hestia.slugs import RESERVED, validate_slug


@pytest.mark.parametrize("slug", ["ok-agent", "ok1", "weather-bot-west"])
def test_good_slugs(slug: str) -> None:
    assert validate_slug(slug) == slug


@pytest.mark.parametrize(
    "slug",
    ["", "A", "1abc", "-abc", "ab", "ab--cd", "admin", "well-known", "AB", "has space"],
)
def test_bad_slugs(slug: str) -> None:
    with pytest.raises(ValueError):
        validate_slug(slug)


def test_reserved_contains_path_segments() -> None:
    assert "ui" in RESERVED and "t" in RESERVED
