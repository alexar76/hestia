"""Fetch CPython for WASI, check it against a pinned SHA-256, unpack it. Used by the Dockerfile.

    python scripts/fetch_python_wasi.py /opt/python-wasi

The build is Brett Cannon's cpython-wasi-build release (a CPython core developer's CI
artefact): python.wasm and its standard library. The digest below is the one GitHub
publishes for the asset, checked again here; a mismatch aborts the image build.
"""

from __future__ import annotations

import hashlib
import io
import sys
import urllib.request
import zipfile
from pathlib import Path

VERSION = "3.14.7"
URL = (f"https://github.com/brettcannon/cpython-wasi-build/releases/download/v{VERSION}/"
       f"python-{VERSION}-wasi_sdk-24.zip")
SHA256 = "2e064d3fb8172471d39d741348efa722349c40b96301f69968dff714999c584b"


def main(target: str) -> None:
    with urllib.request.urlopen(URL, timeout=120) as response:  # noqa: S310 — pinned https URL
        data = response.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != SHA256:
        raise SystemExit(f"python-wasi digest {digest} != pinned {SHA256}: refusing it")
    root = Path(target)
    root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for member in archive.namelist():
            if member.startswith("/") or ".." in Path(member).parts:
                raise SystemExit(f"unsafe path in archive: {member}")
        archive.extractall(root)
    if not (root / "python.wasm").is_file() or not (root / "lib").is_dir():
        raise SystemExit("archive did not contain python.wasm and lib/")
    print(f"CPython {VERSION} for WASI at {root} ({len(data)} bytes, sha256 {digest[:12]}…)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/opt/python-wasi")
