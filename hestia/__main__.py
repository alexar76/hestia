"""`python -m hestia` — bind loopback unless HESTIA_HOST is set by the operator."""

from __future__ import annotations

import logging
import os
import sys
import time

import uvicorn

from hestia.config import Settings

#: Which hestia loggers write, and from what level: "logger=LEVEL,…". Everything under `hestia` that is not
#: named stays at WARNING, as before this setting existed.
DEFAULT_LOG_LEVELS = "hestia.rpc_pool=INFO"


def configure_logging(spec: str) -> None:
    """HESTIA_LOG_LEVELS: give the hestia loggers a handler of their own (stderr, UTC times).

    Without one their records reached Python's last-resort handler, which prints WARNING and above only,
    so an INFO line (the RPC pool's calls, cached and upstream counts) never showed in `docker logs`."""
    root = logging.getLogger("hestia")
    if not any(getattr(h, "_hestia", False) for h in root.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler._hestia = True  # type: ignore[attr-defined]
        formatter = logging.Formatter("%(asctime)sZ %(levelname)s %(name)s: %(message)s", "%Y-%m-%dT%H:%M:%S")
        formatter.converter = time.gmtime
        handler.setFormatter(formatter)
        root.addHandler(handler)
        root.propagate = False
    root.setLevel(logging.WARNING)
    for part in (spec or "").split(","):
        name, _, level = part.strip().partition("=")
        if not name.strip() or not level.strip():
            continue
        if not name.strip().startswith("hestia"):
            raise ValueError(f"HESTIA_LOG_LEVELS names {name.strip()!r}: only hestia loggers")
        logging.getLogger(name.strip()).setLevel(level.strip().upper())


def main() -> None:
    configure_logging(os.environ.get("HESTIA_LOG_LEVELS", DEFAULT_LOG_LEVELS))
    settings = Settings.from_env()
    uvicorn.run(
        "hestia.app:build_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        reload=False,
        # Explicit: left unset, uvicorn takes WEB_CONCURRENCY, and each extra worker would
        # run its own deploy lock over one ledger.
        workers=1,
    )


if __name__ == "__main__":
    main()
