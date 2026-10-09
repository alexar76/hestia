"""WebAssembly runtime: template tenants with no process of their own.

A tenant here is three files in its directory — capability.json, handler.py and
tenant.key — and nothing that runs between calls. A call reaches the hearth, which hands
the handler's source and the payload to the sandbox runner (hestia.wasm_runner: its own
container, no network, no secrets) over a Unix socket, gets the result back, and signs it
with the tenant's own key here. The handler never shares a process, a filesystem or a key
with the hearth; an idle agent costs disk, not memory, so the hearth can hold hundreds.

Answers keep the stub's contract byte for byte: 200 {ok, result, provider_pubkey,
signature}, 400 for a handler error, 504 for a handler that ran out of time, and the same
signing canonical — a tenant moved from the stub runtime keeps its key and its receipts.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from hestia.models import CapabilitySpec
from hestia.policy import IsolationProfile
from hestia.runtime import RunningTenant
from hestia.scan import lint_wasm_handler
from hestia.signing import ProviderSigner

SCHEME = "wasm://"

# Outcome kind -> (HTTP status, whether it is the platform's fault). The fault decides
# whether a buyer's payment claim is released: a handler that raised or ran out of time
# on the buyer's input is billed, like a 400 / 504 from a stub; a dead runner is not.
_STATUS = {
    "error": 400,
    "memory": 400,
    "timeout": 504,
    "output": 502,
    "crash": 502,
}


LANE_BUSY = "the sandbox lane for self-admitted owners' agents is busy; retry"


def is_wasm(listen_url: str) -> bool:
    return (listen_url or "").startswith(SCHEME)


class OpenLane:
    """The runner slots that agents of self-admitted owners share.

    A key admits itself for free (HESTIA_OPEN_OWNERS), so a cap per owner holds nobody back:
    ten keys are ten owners. The bound is on all of them together. Their calls hold at most
    `slots` of the runner's slots, which the hearth keeps below the runner's worker count, so
    the operator's agents and those of owners it admitted always find one free. Within the
    lane an owner runs one call at a time, so one owner cannot fill a wider lane alone.

    Thread-safe and free of any event loop: the hearth and its tests may call from several.
    """

    def __init__(self, slots: int, wait_s: float) -> None:
        self.slots = max(1, int(slots))
        self.wait_s = max(0.0, float(wait_s))
        self._lock = threading.Lock()
        self._owners: set[str] = set()

    def _try_enter(self, owner: str) -> bool:
        with self._lock:
            if len(self._owners) >= self.slots or owner in self._owners:
                return False
            self._owners.add(owner)
            return True

    async def enter(self, owner: str) -> bool:
        """Take a slot for `owner`, waiting up to wait_s. False: the lane stayed full."""
        deadline = time.monotonic() + self.wait_s
        while not self._try_enter(owner):
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.05)
        return True

    def leave(self, owner: str) -> None:
        with self._lock:
            self._owners.discard(owner)

    @property
    def busy(self) -> int:
        with self._lock:
            return len(self._owners)


class WasmRuntime:
    """Same start/stop/stored_handler surface as StubRuntime, plus invoke and health."""

    def __init__(
        self,
        data_dir: Path,
        socket_path: str,
        *,
        timeout_s: float = 10.0,
        open_slots: int = 1,
        open_wait_s: float = 2.0,
    ) -> None:
        self.data_dir = data_dir
        self.socket_path = socket_path
        self.timeout_s = timeout_s
        self.open_lane = OpenLane(open_slots, open_wait_s)
        self._signers: dict[str, ProviderSigner] = {}

    def _dir(self, slug: str) -> Path:
        return self.data_dir / "tenants" / slug

    def start(
        self,
        *,
        slug: str,
        capability: CapabilitySpec,
        handler: str,
        image_digest: str,
        profile: IsolationProfile,
        env: dict[str, str],
    ) -> RunningTenant:
        del image_digest, profile, env  # the sandbox sets its own limits; no process to start
        tenant_dir = self._dir(slug)
        tenant_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(tenant_dir, 0o700)
        cap_file = tenant_dir / "capability.json"
        cap_file.write_text(capability.model_dump_json(), encoding="utf-8")
        os.chmod(cap_file, 0o600)
        handler_file = tenant_dir / "handler.py"
        if handler.strip():
            lint_wasm_handler(handler)
            handler_file.write_text(handler, encoding="utf-8")
            os.chmod(handler_file, 0o600)
        elif handler_file.exists():
            handler_file.unlink()
        # Creates the tenant's key on first start and keeps it for ever after, exactly as
        # the stub did: a tenant moved here signs with the identity buyers pinned.
        self._signers[slug] = ProviderSigner(tenant_dir / "tenant.key", pqc=False)
        return RunningTenant(slug=slug, listen_url=SCHEME + slug, handle=slug)

    def stop(self, handle: str) -> None:
        self._signers.pop(handle, None)  # nothing runs between calls

    def stored_handler(self, slug: str) -> str:
        path = self._dir(slug) / "handler.py"
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def health(self, slug: str) -> dict[str, Any]:
        return {"ok": True, "slug": slug, "status": "running", "runtime": "wasm"}

    def _signer(self, slug: str) -> ProviderSigner:
        signer = self._signers.get(slug)
        if signer is None:
            signer = ProviderSigner(self._dir(slug) / "tenant.key", pqc=False)
            self._signers[slug] = signer
        return signer

    async def _run(self, source: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        transport = httpx.AsyncHTTPTransport(uds=self.socket_path)
        async with httpx.AsyncClient(transport=transport, timeout=self.timeout_s + 15.0) as client:
            response = await client.post("http://runner/run", json={
                "source": source, "payload": payload, "timeout_s": self.timeout_s})
        try:
            body = response.json()
        except ValueError:
            body = {}
        return response.status_code, body if isinstance(body, dict) else {}

    async def invoke(
        self,
        slug: str,
        capability: dict[str, Any],
        body: dict[str, Any],
        *,
        open_owner: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """(status, answer) for one call, in the stub's shapes.

        `open_owner` names the self-admitted owner whose agent this is; its handler then runs
        in the open lane, and a full lane answers 503 (a platform refusal: a claimed payment
        is released). None: the operator's agent or one of an owner it admitted.
        """
        source = self.stored_handler(slug)
        if not source:
            result: dict[str, Any] = {
                "echo": body,
                "capability_id": capability["capability_id"],
                "hearth": "hestia",
                "note": "sealed stub — no custom handler",
            }
        else:
            if open_owner is not None and not await self.open_lane.enter(open_owner):
                return 503, {"ok": False, "error": LANE_BUSY}
            try:
                status, outcome = await self._run(source, body)
            except httpx.HTTPError:
                return 502, {"ok": False, "error": "sandbox runner unreachable"}
            finally:
                if open_owner is not None:
                    self.open_lane.leave(open_owner)
            if status == 503:
                return 503, {"ok": False, "error": "sandbox runner busy; retry"}
            if status != 200:
                return 502, {"ok": False, "error": "sandbox runner refused the call"}
            kind = outcome.get("kind")
            if kind != "ok" or not isinstance(outcome.get("result"), dict):
                code = _STATUS.get(str(kind), 502)
                return code, {"ok": False, "error": str(outcome.get("error") or kind)[:300]}
            result = outcome["result"]
        signer = self._signer(slug)
        return 200, {
            "ok": True,
            "result": result,
            "provider_pubkey": signer.public_key_b64,
            "signature": signer.sign_result(
                result,
                capability_id=capability["capability_id"],
                product_id=capability["product_id"],
                input_payload=body,
            ),
        }
