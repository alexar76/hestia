# Changelog

Notable changes to HESTIA (`aimarket-hestia`). Earlier releases: the git history.

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
