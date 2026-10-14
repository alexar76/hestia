"""Automatic review before an owner's agent enters this hearth's public manifest.

An owner's agent runs as soon as it is deployed — callable at its own /t/{slug} door — but
hubs index this hearth's manifest, so an agent listed there is shown to every buyer of
every hub that pins the hearth. That is the step this guards. The review reads what a
buyer's model would read (name, description, schemas) and holds the listing when it finds:

  - instructions aimed at the model: <IMPORTANT>-style tags, "ignore previous
    instructions", "do not tell the user", "before using any other tool", covert actions;
  - credential paths (~/.ssh, mcp.json, wallet files, .env) and hidden characters
    (zero-width, bidi controls, tag characters) and HTML comments;
  - a word that mixes look-alike scripts (Latin with Cyrillic, Greek, Armenian, …);
  - impersonation: a capability id close to another owner's family (`merkle.proofs`,
    `merkle.proof.service` beside `merkle.proof`), or a name that reads the same as one
    already on this hearth once look-alike letters are folded.

An id in another owner's family is not held here but refused outright (hestia/names.py);
this review catches what is merely close. Deterministic patterns, not a model. A held
agent still runs at its own door; the owner sees the reasons (GET /v1/tenants) and the
operator can list it by hand. The operator's own agents are not reviewed.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

LISTED = "listed"
HELD = "held"

_RULES = (
    ("instruction tag", r"<\s*/?\s*(important|instructions?|system|secret|hidden)\b"),
    ("instruction override", r"\b(ignore|disregard|override|forget)\b.{0,40}\b(previous|prior|"
     r"above|earlier|other|all)\b.{0,30}\b(instructions?|rules|tools?|guidelines)\b"),
    ("conceals from the user", r"\b(do\s+not|don'?t|never|without)\b.{0,30}\b(tell|inform|mention|"
     r"reveal|show|notify|asking)\b.{0,30}\b(the\s+)?(user|human)\b"),
    ("pre-empts other tools", r"\bbefore\b.{0,40}\b(using|calling|invoking|running)\b.{0,40}"
     r"\b(any|other|this|the)\b.{0,20}\btools?\b"),
    ("names a credential path", r"(~/\.ssh|\bid_(rsa|ed25519|ecdsa)\b|\.aws/credentials|"
     r"/etc/(passwd|shadow)|claude_desktop_config|mcp\.json|wallet\.json|keystore|"
     r"\bseed\s+phrase\b|\bmnemonic\b|\bprivate\s+keys?\b|\.env\b)"),
    ("covert action", r"\b(secretly|silently|quietly|covertly)\b.{0,40}\b(send|forward|copy|bcc|"
     r"post|upload|include|call|read)\b"),
    ("HTML comment", r"<!--"),
)
_HIDDEN = ((0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x2064), (0x2066, 0x2069),
           (0xFEFF, 0xFEFF), (0xE0000, 0xE007F))

# Letters that read as a Latin letter, folded to it before names are compared: Cyrillic, Greek,
# Armenian, Cherokee, Lisu, Coptic, IPA and small capitals, digits that pass for letters, and
# the circle-shaped digits of other scripts. Capital I, lowercase l, digit 1, i, the vertical
# bar and every script's I-shaped letter are one class ("l"): in most UI fonts they are the
# same stroke. Fullwidth, mathematical and other compatibility forms are folded by NFKD first,
# and accents are dropped (é → e).
_LOOKALIKE: dict[str, str] = {}
for _dst, _src in (
    ("a", "аАαΑɑ⍺Ꭺꓮ"),
    ("b", "вВΒᏴᗷЬьƄƅꓐ"),
    ("c", "сСϲϹᴄⲥⲤᏟꓚ"),
    ("d", "ԁᎠᗞꓒ"),
    ("e", "еЕΕҽ℮Ꭼꓰϵ"),
    ("f", "ϝƒ"),
    ("g", "ɡցᏀԌ"),
    ("h", "һҺНΗհᏂᎻꓧ"),
    ("l", "iIl1|іІӏӀιΙɩıǀƖ׀ו"
          "اߊⵏᛁꓲ١۱Ꮮꓡ"),
    ("j", "јЈϳʝᎫꓙ"),
    ("k", "кКκΚᴋⲕᏦꓗ"),
    ("m", "МмΜᴍᎷꓟ"),
    ("n", "пոΝɴꓠη"),
    ("o", "0оОοΟօՕσᴏⲟⲞ০੦૦"
          "௦౦೦൦๐໐ဝ߀٥Ꮻꓳഠסه"),
    ("p", "рРρΡⲣⲢᴘᏢꓑ"),
    ("q", "ԛԚգզ"),
    ("r", "гᴦⲅᎡꓣ"),
    ("s", "ѕЅꜱƽᏚꓢʂ"),
    ("t", "тТτΤᴛⲧᎢꓔ"),
    ("u", "υսʋᴜꓴ"),
    ("v", "νѵѴᴠ∨Ꮩꓦ"),
    ("w", "ԝԜѡᴡᎳꓪω"),
    ("x", "хХχΧ×ꓫ"),
    ("y", "уУγүҮΥʏყᎩꓬ"),
    ("z", "ΖᴢᏃꓜ"),
):
    for _c in _src:
        _LOOKALIKE[_c] = _dst
_MULTI = (("rn", "m"), ("vv", "w"), ("cl", "d"))
# Scripts whose letters pass for Latin ones. A word mixing two of them is how a look-alike is
# built ("Bitсoin" with a Cyrillic с); a word in one script, or Latin beside Han, is not.
_LOOKALIKE_SCRIPTS = {"LATIN", "CYRILLIC", "GREEK", "ARMENIAN", "CHEROKEE", "LISU", "COPTIC"}
# A trailing version, glued or separated: merkle.proof.v2, merkle-proof2, merkleproofv2.
_VERSION_PART = re.compile(r"v?\d+")
_GLUED_VERSION = re.compile(r"(?<=[a-z])v?\d+$")


def skeleton(text: str) -> str:
    """What a reader sees: compatibility forms, case, accents, look-alike letters, separators
    and punctuation folded away. Two strings with one skeleton read as one name."""
    decomposed = unicodedata.normalize("NFKD", text)
    out = "".join(
        _LOOKALIKE.get(c, c.casefold())
        for c in decomposed
        if not _hidden(c) and unicodedata.category(c) != "Mn"
    )
    for multi, single in _MULTI:
        out = out.replace(multi, single)
    return re.sub(r"[\W_]", "", out)


def family_key(capability_id: str) -> str:
    """The family a capability id belongs to, as a reader would confuse it.

    The family is the id before `@`. A trailing version is a version, not a new family
    (`merkle.proof.v2@v1`, `merkle-proof2@v1`); separators, case and look-alike letters
    do not tell families apart (`merkle_proof`, `Merkle.Pr0of`).
    """
    family = capability_id.split("@", 1)[0].lower()
    parts = [p for p in re.split(r"[^0-9a-z]+", family) if p]
    while len(parts) > 1 and _VERSION_PART.fullmatch(parts[-1]):
        parts.pop()
    joined = "".join(parts)
    trimmed = _GLUED_VERSION.sub("", joined)
    return skeleton(trimmed if len(trimmed) >= 3 else joined)


def near(one: str, other: str) -> bool:
    """Two family keys close enough to pass for each other: one edit apart, or one the other
    with a word added (`merkleproofs`, `merkleproofservice`, `fastmerkleproof` beside
    `merkleproof`). A short key only counts a short tail: `money` is a word, not a brand."""
    if one == other:
        return True
    short, long_ = sorted((one, other), key=len)
    if len(short) < 5:
        return False
    if len(short) >= 8 and (long_.startswith(short) or long_.endswith(short)):
        return True
    if len(short) >= 6 and long_.startswith(short) and len(long_) - len(short) <= 4:
        return True
    return len(long_) - len(short) <= 1 and _edits_at_most_one(short, long_)


def _edits_at_most_one(short: str, long_: str) -> bool:
    if len(short) == len(long_):
        return sum(a != b for a, b in zip(short, long_, strict=True)) <= 1
    for i in range(len(long_)):
        if long_[:i] + long_[i + 1:] == short:
            return True
    return False


def mixed_script_word(text: str) -> str:
    """The first word that mixes look-alike scripts, or ""."""
    for word in re.findall(r"\w+", text):
        scripts = set()
        for c in word:
            if c.isalpha():
                script = unicodedata.name(c, "").split(" ", 1)[0]
                if script in _LOOKALIKE_SCRIPTS:
                    scripts.add(script)
        if len(scripts) > 1:
            return word
    return ""


def _hidden(char: str) -> bool:
    point = ord(char)
    return any(low <= point <= high for low, high in _HIDDEN)


def _strings(value: Any) -> list[str]:
    if isinstance(value, dict):
        out: list[str] = []
        for key, item in value.items():
            out.append(str(key))
            out.extend(_strings(item))
        return out
    if isinstance(value, list):
        return [s for item in value for s in _strings(item)]
    return [value] if isinstance(value, str) else []


def review(capability: dict[str, Any], *, others: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """(LISTED or HELD, reasons) for an owner's agent.

    `others` is everything on this hearth that is not this owner's: the hearth's own
    capabilities and every other owner's agents and families, running or not — an agent that
    is down today is still the one buyers know.
    """
    reasons: list[str] = []
    fields = {
        "name": str(capability.get("name") or ""),
        "description": str(capability.get("description") or ""),
        "capability_id": str(capability.get("capability_id") or ""),
        "input_schema": "\n".join(_strings(capability.get("input_schema"))),
        "output_schema": "\n".join(_strings(capability.get("output_schema"))),
    }
    for field, text in fields.items():
        for label, pattern in _RULES:
            match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
            if match:
                reasons.append(f"{field} {label}: …{text[max(0, match.start() - 30):match.end() + 30]}…")
        if any(_hidden(c) for c in text):
            reasons.append(f"{field} contains hidden characters (zero-width, bidi or tag)")
    for field in ("name", "description"):
        word = mixed_script_word(fields[field])
        if word:
            reasons.append(f"{field} mixes look-alike scripts in one word: {word}")
    mine = fields["capability_id"]
    own_family, own_name = family_key(mine), skeleton(fields["name"])
    for other in others:
        other_id = str(other.get("capability_id") or "")
        other_name = str(other.get("name") or "")
        if other_id and other_id != mine and own_family and near(own_family, family_key(other_id)):
            reasons.append(f"capability_id looks like {other_id}, already on this hearth")
        elif own_name and other_name and own_name == skeleton(other_name):
            reasons.append(f"name looks like '{other_name}', already on this hearth")
    unique = list(dict.fromkeys(reasons))
    return (HELD if unique else LISTED), unique[:20]
