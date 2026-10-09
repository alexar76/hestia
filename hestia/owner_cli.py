"""Owner tool: drive your own agents on a shared hearth with your own key.

    python -m hestia.owner_cli keygen --out owner.key           # once; prints the public key
    python -m hestia.owner_cli me       --hearth URL --key owner.key
    python -m hestia.owner_cli deploy   --hearth URL --key owner.key --body deploy.json
    python -m hestia.owner_cli stop     --hearth URL --key owner.key --slug my-agent
    python -m hestia.owner_cli announce --hearth URL --key owner.key --slug my-agent
    python -m hestia.owner_cli billing  --hearth URL --key owner.key --hub https://modelmarket.dev --account acct_…
    python -m hestia.owner_cli statement --hearth URL --key owner.key
    python -m hestia.owner_cli proof    --key owner.key --domain example.com   # what to publish
    python -m hestia.owner_cli domain   --hearth URL --key owner.key --domain example.com

``billing`` lets a hub sell your agents on your behalf and names your credit account there, which
receives your share of what the hub charges (``--account ""`` withdraws the choice). ``statement``
lists every call a hub was served for you, to reconcile with what it paid.

``proof`` prints what proves you control a domain — a TXT record or a /.well-known file holding
your PUBLIC key — and ``domain`` asks the hearth to check it: every agent name beginning with the
domain, zone included (``example.com.…`` or ``com.example.…``), is then yours on that hearth
(hestia/domains.py).

The key file holds the raw 32-byte Ed25519 seed, base64, mode 0600; it never leaves your
machine and is never printed. Give the operator the PUBLIC key to be admitted
(POST /v1/owners). ``deploy`` sets ``owner_pubkey`` in the body to your key — the hearth
refuses a body that names anyone else.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hestia.owners import sign_owner_request


def _load_key(path: str) -> Ed25519PrivateKey:
    seed = base64.b64decode(Path(path).read_text().strip(), validate=True)
    return Ed25519PrivateKey.from_private_bytes(seed)


def _public(key: Ed25519PrivateKey) -> str:
    return base64.b64encode(key.public_key().public_bytes_raw()).decode()


def _send(hearth: str, key: Ed25519PrivateKey, method: str, path: str, body: bytes = b"") -> int:
    import httpx

    headers = sign_owner_request(key, hearth=hearth, method=method, path=path, body=body)
    if body:
        headers["content-type"] = "application/json"
    res = httpx.request(method, hearth.rstrip("/") + path, content=body or None,
                        headers=headers, timeout=60)
    try:
        print(json.dumps(res.json(), indent=2, ensure_ascii=False))
    except ValueError:
        print(res.text)
    return 0 if res.status_code < 400 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hestia.owner_cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    keygen = sub.add_parser("keygen", help="make an owner key")
    keygen.add_argument("--out", required=True)
    proof = sub.add_parser("proof", help="print the TXT record and file that prove a domain")
    proof.add_argument("--key", required=True)
    proof.add_argument("--domain", required=True)
    for name in ("me", "deploy", "stop", "announce", "billing", "statement", "domain"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--hearth", required=True, help="the hearth's public base URL")
        cmd.add_argument("--key", required=True, help="owner key file (keygen output)")
        if name == "deploy":
            cmd.add_argument("--body", required=True, help="deploy JSON (DeployRequest)")
        if name in {"stop", "announce"}:
            cmd.add_argument("--slug", required=True)
        if name == "domain":
            cmd.add_argument("--domain", required=True, help="a domain you control, e.g. example.com")
        if name == "billing":
            cmd.add_argument("--hub", required=True, help="the hub's public URL")
            cmd.add_argument("--account", required=True, help='your credit account there, or "" to withdraw')
    args = parser.parse_args(argv)

    if args.cmd == "keygen":
        out = Path(args.out)
        if out.exists():
            print(f"{out} exists — refusing to overwrite an owner key", file=sys.stderr)
            return 1
        key = Ed25519PrivateKey.generate()
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(base64.b64encode(key.private_bytes_raw()).decode() + "\n")
        print(_public(key))
        return 0

    key = _load_key(args.key)
    if args.cmd == "proof":
        from hestia.domains import PROOF_PATH, TXT_LABEL, TXT_PREFIX, DomainError, registrable

        try:
            domain = registrable(args.domain)
        except DomainError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"DNS   TXT  {TXT_LABEL}.{domain}  \"{TXT_PREFIX}{_public(key)}\"")
        print(f"or    https://{domain}{PROOF_PATH}")
        print("      " + json.dumps({"owner_pubkeys": [_public(key)]}))
        return 0
    if args.cmd == "domain":
        body = json.dumps({"domain": args.domain}).encode()
        return _send(args.hearth, key, "POST", "/v1/owners/me/domain", body)
    if args.cmd == "me":
        return _send(args.hearth, key, "GET", "/v1/owners/me")
    if args.cmd == "statement":
        return _send(args.hearth, key, "GET", "/v1/owners/me/statement")
    if args.cmd == "billing":
        body = json.dumps({"hubs": {args.hub: args.account}}).encode()
        return _send(args.hearth, key, "POST", "/v1/owners/me/billing", body)
    if args.cmd == "deploy":
        payload = json.loads(Path(args.body).read_text())
        payload["owner_pubkey"] = _public(key)
        return _send(args.hearth, key, "POST", "/v1/tenants", json.dumps(payload).encode())
    return _send(args.hearth, key, "POST", f"/v1/tenants/{args.slug}/{args.cmd}")


if __name__ == "__main__":
    raise SystemExit(main())
