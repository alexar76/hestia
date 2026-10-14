"""Owner tool: drive your own agents on a shared hearth with your own key.

Installed as `hestia-owner` (`uvx --from aimarket-hestia hestia-owner …`). `--hearth` defaults
to $HESTIA_HEARTH, else the reference hearth https://hestia.modelmarket.dev.

    hestia-owner init --slug my-agent                           # a folder: handler.py + deploy.json
    hestia-owner try  --dir my-agent --input '{"text": "hi"}'   # run handle() locally first
    hestia-owner deploy --key owner.key --body my-agent/deploy.json --payout 0x…

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


DEFAULT_HEARTH = "https://hestia.modelmarket.dev"

_HANDLER = '''"""Your agent: one function, handle(payload) -> dict.

It runs in the hearth's WebAssembly sandbox: pure Python, standard library only (no network,
no files, no threads), 10 s and 256 MiB per call. Raise ValueError with a sentence the buyer
can act on when the input is wrong. Try it locally:  hestia-owner try --dir . --input '{"text": "a b"}'
"""


def handle(payload):
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("send the text to measure as 'text'")
    words = text.split()
    return {
        "characters": len(text),
        "words": len(words),
        "longest_word": max(words, key=len),
    }
'''


def _deploy_template(slug: str) -> dict:
    return {
        "slug": slug,
        "capability": {
            "product_id": slug,
            "capability_id": f"{slug.replace('-', '.')}.run@v1",
            "name": "Text measure",
            "description": "Counts characters and words in a text and names its longest word.",
            "price_per_call_usd": 0.001,
            "input_schema": {"type": "object", "required": ["text"],
                             "properties": {"text": {"type": "string"}}},
            "output_schema": {"type": "object"},
            "publisher_id": slug,
            "provider_pubkey": "",
        },
        "source": {"kind": "template", "handler": ""},
        "owner_pubkey": "",
        "payout_address": "",
        "announce": True,
        "note": "",
    }


def _with_handler(payload: dict, body_path: Path) -> dict:
    """An empty template handler is filled from handler.py beside deploy.json."""
    source = payload.get("source") or {}
    if source.get("kind", "template") == "template" and not source.get("handler"):
        handler = body_path.parent / "handler.py"
        if handler.is_file():
            source["handler"] = handler.read_text(encoding="utf-8")
            payload["source"] = source
    return payload


def _try(folder: Path, raw_input: str) -> int:
    """Run handle() on one input, here, before anything is deployed."""
    import runpy

    handler = folder / "handler.py"
    if not handler.is_file():
        print(f"{handler} not found", file=sys.stderr)
        return 1
    payload = json.loads(raw_input or "{}")
    namespace = runpy.run_path(str(handler))
    try:
        result = namespace["handle"](payload)
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hestia-owner")
    sub = parser.add_subparsers(dest="cmd", required=True)
    init = sub.add_parser("init", help="start an agent: a folder with handler.py and deploy.json")
    init.add_argument("--slug", required=True, help="lowercase name, e.g. text-measure")
    init.add_argument("--dir", default="", help="folder to create (default: the slug)")
    trial = sub.add_parser("try", help="run handle() locally on one input")
    trial.add_argument("--dir", default=".")
    trial.add_argument("--input", default="{}", help="the payload as JSON")
    keygen = sub.add_parser("keygen", help="make an owner key")
    keygen.add_argument("--out", required=True)
    proof = sub.add_parser("proof", help="print the TXT record and file that prove a domain")
    proof.add_argument("--key", required=True)
    proof.add_argument("--domain", required=True)
    for name in ("me", "deploy", "stop", "announce", "billing", "statement", "domain"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--hearth", default=os.environ.get("HESTIA_HEARTH") or DEFAULT_HEARTH,
                         help="the hearth's public base URL (default: $HESTIA_HEARTH or the reference hearth)")
        cmd.add_argument("--key", required=True, help="owner key file (keygen output)")
        if name == "deploy":
            cmd.add_argument("--body", required=True, help="deploy JSON (DeployRequest)")
            cmd.add_argument("--payout", default="",
                             help="your wallet (0x…): buyers pay it directly; the hearth never holds it")
        if name in {"stop", "announce"}:
            cmd.add_argument("--slug", required=True)
        if name == "domain":
            cmd.add_argument("--domain", required=True, help="a domain you control, e.g. example.com")
        if name == "billing":
            cmd.add_argument("--hub", required=True, help="the hub's public URL")
            cmd.add_argument("--account", required=True, help='your credit account there, or "" to withdraw')
    args = parser.parse_args(argv)

    if args.cmd == "init":
        folder = Path(args.dir or args.slug)
        if folder.exists() and any(folder.iterdir()):
            print(f"{folder} exists and is not empty — refusing to overwrite it", file=sys.stderr)
            return 1
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "handler.py").write_text(_HANDLER, encoding="utf-8")
        (folder / "deploy.json").write_text(
            json.dumps(_deploy_template(args.slug), indent=2) + "\n", encoding="utf-8")
        print(f"{folder}/handler.py   your function — edit it, then: hestia-owner try --dir {folder} --input '{{\"text\": \"hi there\"}}'")
        print(f"{folder}/deploy.json  name, price, schema — then: hestia-owner keygen --out owner.key")
        print(f"                       and: hestia-owner deploy --key owner.key --body {folder}/deploy.json --payout 0x…")
        return 0
    if args.cmd == "try":
        return _try(Path(args.dir), args.input)

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
        body_path = Path(args.body)
        payload = _with_handler(json.loads(body_path.read_text()), body_path)
        payload["owner_pubkey"] = _public(key)
        if args.payout:
            payload["payout_address"] = args.payout
        return _send(args.hearth, key, "POST", "/v1/tenants", json.dumps(payload).encode())
    return _send(args.hearth, key, "POST", f"/v1/tenants/{args.slug}/{args.cmd}")


def entry() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    raise SystemExit(main())
