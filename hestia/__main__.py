"""`python -m hestia` — bind loopback unless HESTIA_HOST is set by the operator."""

from __future__ import annotations

import uvicorn

from hestia.config import Settings


def main() -> None:
    settings = Settings.from_env()
    uvicorn.run(
        "hestia.app:build_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
