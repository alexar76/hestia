from pathlib import Path
import re

DOCS = Path(__file__).resolve().parents[1] / "docs"
CONSOLE = Path(__file__).resolve().parents[1] / "console"

LOCALES = {
    "workshop.md": ("host", "Not a course", "/ui/", "HESTIA_DEPLOY_TOKEN"),
    "workshop.ru.md": ("хост", "не курс", "/ui/", "HESTIA_DEPLOY_TOKEN"),
    "workshop.es.md": ("host", "No es un curso", "/ui/", "HESTIA_DEPLOY_TOKEN"),
    "workshop.fr.md": ("hôte", "pas un cours", "/ui/", "HESTIA_DEPLOY_TOKEN"),
    "workshop.zh.md": ("主机", "不是一门课", "/ui/", "HESTIA_DEPLOY_TOKEN"),
}

RETIRED_PROSE = {
    "workshop.md": (r"\bhearth\b", r"\btenant\b"),
    "workshop.ru.md": (r"очаг", r"тенант"),
    "workshop.es.md": (r"\bhogar\b", r"\binquilino\b"),
    "workshop.fr.md": (r"\bâtre\b", r"\blocataire\b"),
    "workshop.zh.md": (r"炉灶", r"租户"),
}


def _prose(text: str) -> str:
    stripped = re.sub(r"```.*?```", " ", text, flags=re.S)
    stripped = re.sub(r"`[^`]+`", " ", stripped)
    stripped = stripped.replace("/v1/hearth", " ")
    stripped = re.sub(r"«[^»]+»", " ", stripped)
    stripped = re.sub(r"“[^”]+”", " ", stripped)
    stripped = re.sub(r"「[^」]+」", " ", stripped)
    return stripped


def test_workshop_five_locales() -> None:
    for name, needles in LOCALES.items():
        text = (DOCS / name).read_text(encoding="utf-8")
        for needle in needles:
            assert needle in text, f"{name} missing {needle!r}"


def test_workshop_prose_follows_glossary() -> None:
    for name, patterns in RETIRED_PROSE.items():
        prose = _prose((DOCS / name).read_text(encoding="utf-8"))
        for pattern in patterns:
            assert not re.search(pattern, prose, flags=re.I), f"{name} still uses {pattern}"


def test_console_links_workshop(tmp_path) -> None:
    from tests.conftest import client

    api = client(tmp_path)
    page = api.get("/ui/")
    assert page.status_code == 200
    assert b"workshop.md" in page.content
    assert b'data-i="ws_h"' in page.content
    js = api.get("/ui/assets/console.js")
    assert js.status_code == 200
    assert b"workshop.ru.md" in js.content
    assert CONSOLE.joinpath("console.js").read_text(encoding="utf-8").count("nav_workshop") >= 5
