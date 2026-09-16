"""Wire types. Extra fields are rejected — a dossier is not a dump."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from hestia.slugs import validate_slug


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
        if "@" not in value or " " in value:
            raise ValueError("capability_id must look like name@version")
        return value

    @field_validator("input_schema", "output_schema")
    @classmethod
    def _schema_object(cls, value: dict[str, Any]) -> dict[str, Any]:
        if value.get("type") not in {None, "object"}:
            raise ValueError("schema type must be object when present")
        return value


class TemplateSource(StrictModel):
    kind: Literal["template"] = "template"
    handler: str = Field(default="", max_length=32_000)


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

    @field_validator("slug")
    @classmethod
    def _slug(cls, value: str) -> str:
        return validate_slug(value)
