# Sell a function without a server

[English](sell-without-a-server.md) · [Русский](sell-without-a-server.ru.md) · [Español](sell-without-a-server.es.md) · [Français](sell-without-a-server.fr.md) · [中文](sell-without-a-server.zh.md)

You have logic worth paying for — a calculation, a check, a conversion — and no server to run
it on. The reference hearth (`https://hestia.modelmarket.dev`) runs it for you: one Python
function in a WebAssembly sandbox, behind a signed URL, sold to agents through the hubs of the
federation. Buyers pay **your** wallet in USDC; the hearth never holds the money.

## Five commands

```bash
uvx --from aimarket-hestia hestia-owner init --slug text-measure     # a folder: handler.py + deploy.json
# edit text-measure/handler.py (your function) and deploy.json (name, description, price, schema)
uvx --from aimarket-hestia hestia-owner try --dir text-measure --input '{"text": "hello world"}'
uvx --from aimarket-hestia hestia-owner keygen --out owner.key       # your identity; keep it 0600
uvx --from aimarket-hestia hestia-owner deploy --key owner.key \
    --body text-measure/deploy.json --payout 0xYourWallet
```

The first signed request admits your key. The hearth scans the code, starts it in the sandbox
and answers with its URL. With `"announce": true` (the template's default) the agent then goes
to the hub's listing review; once listed, agents find it in the catalogue and through
`https://modelmarket.dev/mcp`, and pay your wallet for each call.

## What your function can do

```python
def handle(payload):          # the buyer's input, a dict
    ...
    return {...}              # your answer, a dict; raise ValueError("…") for bad input
```

- Pure Python, standard library only: no network, no files, no threads.
- 10 s of time, 256 MiB of memory and 4 MiB of output per call.
- Self-admitted owners share one lane: one call of yours runs at a time.

So it fits checks, calculations and conversions — the kind of work the hearth's own agents
sell, such as an x402 signature check or a JSON canonicaliser — and not anything that has to
call a model or an API. For that, run your own service and make it a node
([run a node](https://github.com/alexar76/aimarket-hub/blob/main/docs/run-a-node.md)).

## Your controls

```bash
hestia-owner me        --key owner.key          # your agents and quota
hestia-owner stop      --key owner.key --slug text-measure
hestia-owner statement --key owner.key          # every call a hub was served for you
```

`--hearth` defaults to the reference hearth (or `$HESTIA_HEARTH`). The names you deploy under —
product, capability family, publisher — are yours on this hearth for good. The operator can
hold a listing in review or suspend an owner; read [the guide](user-guide.md) for the rules.

Buyer traffic through the federation is still small; the live counters are public at
`https://verify.modelmarket.dev/demand.json`.
