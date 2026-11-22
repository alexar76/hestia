# Changelog

Notable changes to HESTIA (`aimarket-hestia`). Earlier releases: the git history.

## Unreleased

### Added
- **`handler_sha256` on the public roster** (`/v1/hearth`, sandbox runtime): the SHA-256 of the handler file every
  call hands the sandbox, Python or JavaScript, recomputed after each deploy. A published copy of an agent's source
  can be checked against the code that answers.
- **`/agents/`**: the operator's agent catalogue in five languages (built by hestia-agents
  `scripts/build_catalog.py` into `docs/agents/`, copied into the image): each agent's purpose, reachable hosts,
  sandbox and egress limits, handler source with its SHA-256, tests and recorded replies; the page compares every
  digest with the roster when it opens. Sources, tests and Markdown are served as `text/plain`; `/agents` redirects
  to `/agents/`. The landing links it.

## 0.2.0 — 2026-11-03

### Added
- **Network for agents through the hearth** (`HESTIA_TENANT_EGRESS=1`, needs `HESTIA_RUNTIME=wasm`; off by default).
  The sandbox still has no sockets: a handler calls `fetch()`, the hearth makes the request and runs the handler again
  with the response (at most 6 rounds per call). Only the hosts the deploy names in `egress_hosts` (at most 8 exact
  public DNS names, listed publicly on `/v1/hearth`) are reachable, over https/443, GET or POST, with request and
  response caps (64 KiB / 256 KiB), 5 s per request, no redirects, no proxies, no cookies. The hearth resolves the
  name itself, refuses it when any answer is non-public, and connects to the address it checked. Budgets per agent
  (`HESTIA_TENANT_EGRESS_PER_MINUTE`, 60) and per hearth (`HESTIA_TENANT_EGRESS_HEARTH_PER_MINUTE`, 600). See
  "Network for agents" in the README.

### Security
- Egress judged IPv6 answers by their own flags, so a name resolving to a NAT64 form of a loopback or metadata
  address (`64:ff9b::7f00:1`, `64:ff9b::a9fe:a9fe`) passed. IPv4-mapped, 6to4 and NAT64 addresses are now judged by
  the IPv4 address they carry, and the local-use prefix `64:ff9b:1::/48` is refused.
