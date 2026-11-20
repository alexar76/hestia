"""Wire types. Extra fields are rejected — a dossier is not a dump."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from hestia.slugs import validate_slug

# name@version — an identifier and a URL/label fragment. No HTML metacharacters,
# quotes or whitespace, so it can never carry markup into a console that renders it.
_CAPABILITY_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*@[a-zA-Z0-9][a-zA-Z0-9._+-]*$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CapabilitySpec(StrictModel):
    product_id: str = Field(min_length=1, max_length=80)
    capability_id: str = Field(min_length=3, max_length=120)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=2000)
    price_per_call_usd: float = Field(ge=0, le=100)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    publisher_id: str = Field(min_length=1, max_length=80)
    provider_pubkey: str = ""

    @field_validator("capability_id")
    @classmethod
    def _cap_id(cls, value: str) -> str:
        # fullmatch: `$` alone also matches before a trailing newline, and "x@v1\n" would
        # then run beside "x@v1" as a second agent.
        if not _CAPABILITY_ID.fullmatch(value):
            raise ValueError(
                "capability_id must look like name@version "
                "([a-zA-Z0-9._-]+@[a-zA-Z0-9._+-]+, no spaces or markup)"
            )
        return value

    @field_validator("input_schema", "output_schema")
    @classmethod
    def _schema_object(cls, value: dict[str, Any]) -> dict[str, Any]:
        if value.get("type") not in {None, "object"}:
            raise ValueError("schema type must be object when present")
        return value


# A handler for the WebAssembly sandbox may be one bundled file, in either language; the whole
# deploy request must still fit HESTIA_MAX_BODY_BYTES (256 KiB by default, the same in the
# hearth's nginx), JSON escaping included, hence 200 000 characters and not more. The stub
# runtime keeps the old 32 000 for Python (hestia.scan.admit_handler reads every node).
MAX_PYTHON_HANDLER = 32_000
MAX_JS_HANDLER = 200_000
MAX_SANDBOX_HANDLER = MAX_JS_HANDLER


class TemplateSource(StrictModel):
    kind: Literal["template"] = "template"
    language: Literal["python", "javascript"] = "python"
    handler: str = Field(default="", max_length=MAX_JS_HANDLER)

    @model_validator(mode="after")
    def _size_for_language(self) -> "TemplateSource":
        if len(self.handler) > MAX_SANDBOX_HANDLER:
            raise ValueError(f"a {self.language} handler is at most {MAX_SANDBOX_HANDLER} characters")
        return self


class PinnedImageSource(StrictModel):
    kind: Literal["image"] = "image"
    image_digest: str = Field(min_length=71, max_length=80)

    @field_validator("image_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        lowered = value.strip().lower()
        if not lowered.startswith("sha256:") or len(lowered) != 71:
            raise ValueError("image_digest must be sha256:<64 hex>")
        hexpart = lowered.split(":", 1)[1]
        if any(c not in "0123456789abcdef" for c in hexpart):
            raise ValueError("image_digest must be sha256:<64 hex>")
        return lowered


class DeployRequest(StrictModel):
    slug: str
    capability: CapabilitySpec
    source: TemplateSource | PinnedImageSource
    owner_pubkey: str = Field(min_length=40, max_length=80)
    announce: bool = False
    note: str = Field(default="", max_length=280)
    # Where this tenant's owner is paid. The hearth verifies payments to it and
    # never holds the funds, so it is the owner's address, not the operator's.
    payout_address: str = Field(default="", max_length=42)
    # hestia.egress: exact public DNS names this agent's handler may fetch from, through the
    # hearth. Public on the roster. Empty: no network, as before.
    egress_hosts: list[str] = Field(default_factory=list, max_length=8)
    # hestia.egress, caller-named hosts: top-level input fields whose value (a host name, or an
    # https:// URL) adds that one host for that one call. The caller, not the agent, chooses where
    # the call reaches: "check this domain". Public on the roster. At most three fields.
    egress_input_fields: list[str] = Field(default_factory=list, max_length=3)

    @field_validator("egress_hosts")
    @classmethod
    def _egress(cls, value: list[str]) -> list[str]:
        from hestia.egress import normalize_hosts

        return normalize_hosts(value)

    @field_validator("egress_input_fields")
    @classmethod
    def _egress_fields(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        for name in value:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name or ""):
                raise ValueError(f"egress input field {name!r} must be an identifier")
            if name not in out:
                out.append(name)
        return out

    @field_validator("payout_address")
    @classmethod
    def _payout(cls, value: str) -> str:
        raw = (value or "").strip()
        if not raw:
            return ""
        if not re.match(r"^0x[0-9a-fA-F]{40}$", raw):
            raise ValueError("payout_address must be a 0x EVM address")
        return raw

    @field_validator("slug")
    @classmethod
    def _slug(cls, value: str) -> str:
        return validate_slug(value)
