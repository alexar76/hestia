"""Runtime protocol."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from hestia.models import CapabilitySpec
from hestia.policy import IsolationProfile


@dataclass(frozen=True)
class RunningTenant:
    slug: str
    listen_url: str
    handle: str


class Runtime(Protocol):
    def start(
        self,
        *,
        slug: str,
        capability: CapabilitySpec,
        handler: str,
        image_digest: str,
        profile: IsolationProfile,
        env: dict[str, str],
    ) -> RunningTenant: ...

    def stop(self, handle: str) -> None: ...
