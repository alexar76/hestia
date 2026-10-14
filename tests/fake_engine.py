"""A stateful stand-in for the Docker engine, driven through the real CLI calls.

Tests used to hand each subprocess.run call a canned answer, so a check could be
deleted from the code and the canned answer still satisfied the test. This
engine keeps networks and containers as state and answers the same verbs the
code sends, with the same error texts Docker 29 prints (checked against a real
engine), so the network policy is exercised end to end.
"""

from __future__ import annotations

import ipaddress
import itertools
import json
import subprocess
from collections.abc import Callable

_VALUE_FLAGS = {
    "--name",
    "--user",
    "--cpus",
    "--memory",
    "--memory-swap",
    "--pids-limit",
    "--security-opt",
    "--cap-drop",
    "--network",
    "--tmpfs",
    "--label",
    "--env",
    "--runtime",
}


class FakeEngine:
    def __init__(self) -> None:
        self.networks: dict[str, dict] = {}
        self.containers: dict[str, dict] = {}
        self.calls: list[list[str]] = []
        self.hooks: list[list] = []
        self.runtimes = {"runc"}
        self.reachable = True
        self.rm_missing_rc = 0  # Docker 29 exits 0 for `rm -f` of a missing container
        self._ids = itertools.count(1)
        self.add_network("bridge", internal=False, subnet="172.17.0.0/16", labels={})

    # ------------------------------------------------------------------ setup
    def install(self, monkeypatch) -> FakeEngine:
        monkeypatch.setattr("subprocess.run", self.run)
        monkeypatch.setattr("hestia.runtime.docker.shutil.which", lambda _bin: "/usr/bin/docker")

        def wait(runtime, address, port):
            # Reachable only if a RUNNING container really holds this address on
            # a network that still exists.
            owners = [
                name
                for name, c in self.containers.items()
                if c["State"]["Running"]
                and any(
                    net in self.networks and info.get("IPAddress") == address
                    for net, info in c["NetworkSettings"]["Networks"].items()
                )
            ]
            if not self.reachable or not owners:
                raise RuntimeError(f"tenant at {address}:{port} is not reachable from this host")

        monkeypatch.setattr("hestia.runtime.docker.DockerRuntime._wait_reachable", wait)
        return self

    def on(self, predicate: Callable[[list[str]], bool], rc: int = 1, stderr: str = "",
           stdout: str = "", times: int | None = None, action: Callable | None = None) -> None:
        """Fail (or intercept) calls matching `predicate`."""
        self.hooks.append([predicate, rc, stdout, stderr, times, action])

    def add_network(self, name: str, *, internal: bool = True, labels: dict | None = None,
                    options: dict | None = None, subnet: str | None = None,
                    ipv6: bool = False, driver: str = "bridge") -> dict:
        net_id = f"{next(self._ids):064x}"
        subnet = subnet or str(self._default_subnet())
        network = {
            "Name": name,
            "Id": net_id,
            "Driver": driver,
            "Scope": "local",
            "Internal": internal,
            "EnableIPv6": ipv6,
            "Options": dict(options or {}),
            "Labels": dict(labels or {}),
            "IPAM": {"Config": [{"Subnet": subnet, "Gateway": str(next(ipaddress.ip_network(subnet).hosts()))}]},
        }
        self.networks[name] = network
        return network

    def add_container(self, name: str, network: str | None, *, running: bool = True,
                      labels: dict | None = None) -> dict:
        networks = {}
        if network is not None:
            # Docker 29 clears the address of a container that is not running.
            networks[network] = {"IPAddress": self._next_ip(network) if running else ""}
        container = {
            "Id": f"{next(self._ids):064x}",
            "Name": f"/{name}",
            "State": {"Running": running},
            "Config": {"Labels": dict(labels or {}), "Image": ""},
            "NetworkSettings": {"Networks": networks},
        }
        self.containers[name] = container
        return container

    # ---------------------------------------------------------------- helpers
    def attached(self, network: str) -> list[str]:
        return sorted(
            name for name, c in self.containers.items() if network in c["NetworkSettings"]["Networks"]
        )

    def tenant_networks(self) -> dict[str, dict]:
        return {n: v for n, v in self.networks.items() if "dev.aicom.hestia.tenant" in v["Labels"]}

    def verbs(self) -> list[str]:
        return [" ".join(call[1:3]) for call in self.calls]

    def _subnets(self) -> list[ipaddress.IPv4Network]:
        return [ipaddress.ip_network(n["IPAM"]["Config"][0]["Subnet"]) for n in self.networks.values()]

    def _default_subnet(self) -> ipaddress.IPv4Network:
        taken = self._subnets() if hasattr(self, "networks") else []
        for second in range(17, 32):
            candidate = ipaddress.ip_network(f"172.{second}.0.0/16")
            if not any(candidate.overlaps(t) for t in taken):
                return candidate
        raise RuntimeError("all predefined address pools have been fully subnetted")

    def _next_ip(self, network: str) -> str:
        subnet = ipaddress.ip_network(self.networks[network]["IPAM"]["Config"][0]["Subnet"])
        used = {
            c["NetworkSettings"]["Networks"][network]["IPAddress"]
            for c in self.containers.values()
            if network in c["NetworkSettings"]["Networks"]
        }
        hosts = list(itertools.islice(subnet.hosts(), 1, 64))  # .1 is the gateway
        for host in hosts:
            if str(host) not in used:
                return str(host)
        raise RuntimeError("no free address")

    def _find_container(self, ref: str) -> str | None:
        for name, c in self.containers.items():
            if ref in {name, c["Id"]}:
                return name
        return None

    def _find_network(self, ref: str) -> str | None:
        """Like Docker: exact name, exact ID, then a unique ID prefix."""
        for name, n in self.networks.items():
            if ref in {name, n["Id"]}:
                return name
        matches = [name for name, n in self.networks.items() if n["Id"].startswith(ref)]
        return matches[0] if len(matches) == 1 else None

    # -------------------------------------------------------------------- CLI
    def run(self, argv, **_kwargs):
        argv = list(argv)
        self.calls.append(argv)
        args = argv[1:]
        for hook in list(self.hooks):
            predicate, rc, stdout, stderr, times, action = hook
            if predicate(args):
                if times is not None:
                    hook[4] = times - 1
                    if hook[4] <= 0:
                        self.hooks.remove(hook)
                if action is not None:
                    action(args)
                    break
                return subprocess.CompletedProcess(argv, rc, stdout, stderr)
        rc, out, err = self._dispatch(args)
        return subprocess.CompletedProcess(argv, rc, out, err)

    def _dispatch(self, args: list[str]) -> tuple[int, str, str]:
        head = args[:2]
        if head == ["network", "inspect"]:
            return self._network_inspect(args[2:])
        if head == ["network", "ls"]:
            return 0, "".join(n["Id"] + "\n" for n in self.networks.values()), ""
        if head == ["network", "create"]:
            return self._network_create(args[2:])
        if head == ["network", "rm"]:
            return self._network_rm(args[2])
        if head == ["network", "disconnect"]:
            rest = [a for a in args[2:] if a != "-f"]
            net, ref = rest[0], rest[1]
            name = self._find_container(ref)
            if name is None or net not in self.containers[name]["NetworkSettings"]["Networks"]:
                return 1, "", f"Error response from daemon: container {ref} is not connected to network {net}"
            del self.containers[name]["NetworkSettings"]["Networks"][net]
            return 0, "", ""
        if head == ["container", "inspect"]:
            name = self._find_container(args[2])
            if name is None:
                return 1, "[]\n", f"Error response from daemon: No such container: {args[2]}"
            return 0, json.dumps([self.containers[name]]), ""
        if args[:1] == ["ps"]:
            return self._ps(args[1:])
        if args[:1] == ["run"]:
            return self._run_container(args[1:])
        if args[:2] == ["rm", "-f"]:
            name = self._find_container(args[2])
            if name is None:
                return self.rm_missing_rc, "", f"Error response from daemon: No such container: {args[2]}"
            del self.containers[name]
            return 0, args[2] + "\n", ""
        if args[:1] == ["version"]:
            return 0, "29.2.1\n", ""
        if args[:1] == ["info"]:
            return 0, "".join(f"{r}\n" for r in sorted(self.runtimes)), ""
        return 1, "", f"fake engine: unsupported call {args}"

    def stop_container(self, name: str) -> None:
        """What `docker stop` does: not running, and no address any more."""
        container = self.containers[name]
        container["State"]["Running"] = False
        for info in container["NetworkSettings"]["Networks"].values():
            info["IPAddress"] = ""

    def _network_inspect(self, refs: list[str]) -> tuple[int, str, str]:
        if refs[:1] == ["-f"]:
            name = self._find_network(refs[2])
            if name is None:
                return 1, "", f"Error response from daemon: network {refs[2]} not found"
            return 0, "true\n" if self.networks[name]["Internal"] else "false\n", ""
        found, missing = [], []
        for ref in refs:
            name = self._find_network(ref)
            if name is None:
                missing.append(ref)
            else:
                found.append(dict(self.networks[name], Containers={
                    c["Id"]: {"Name": n} for n, c in self.containers.items()
                    if name in c["NetworkSettings"]["Networks"] and c["State"]["Running"]
                }))
        err = "".join(f"Error response from daemon: network {m} not found\n" for m in missing)
        return (1 if missing else 0), json.dumps(found) + "\n", err

    def _network_create(self, rest: list[str]) -> tuple[int, str, str]:
        opts: dict = {"driver": "bridge", "internal": False, "ipv6": False, "subnet": None,
                      "options": {}, "labels": {}}
        name = ""
        i = 0
        while i < len(rest):
            token = rest[i]
            if token == "--driver":
                opts["driver"] = rest[i + 1]
                i += 2
            elif token == "--internal":
                opts["internal"] = True
                i += 1
            elif token.startswith("--ipv6"):
                opts["ipv6"] = token != "--ipv6=false"
                i += 1
            elif token == "--subnet":
                opts["subnet"] = rest[i + 1]
                i += 2
            elif token in {"--opt", "-o"}:
                key, value = rest[i + 1].split("=", 1)
                opts["options"][key] = value
                i += 2
            elif token == "--label":
                key, value = rest[i + 1].split("=", 1)
                opts["labels"][key] = value
                i += 2
            else:
                name = token
                i += 1
        if name in self.networks:
            return 1, "", f"Error response from daemon: network with name {name} already exists"
        if opts["subnet"]:
            wanted = ipaddress.ip_network(opts["subnet"])
            if any(wanted.overlaps(t) for t in self._subnets()):
                return 1, "", "Error response from daemon: invalid pool request: Pool overlaps with other one on this address space"
        else:
            try:
                opts["subnet"] = str(self._default_subnet())
            except RuntimeError as exc:
                return 1, "", f"Error response from daemon: {exc}"
        self.add_network(name, internal=opts["internal"], labels=opts["labels"],
                         options=opts["options"], subnet=opts["subnet"], ipv6=opts["ipv6"],
                         driver=opts["driver"])
        return 0, self.networks[name]["Id"] + "\n", ""

    def _network_rm(self, ref: str) -> tuple[int, str, str]:
        name = self._find_network(ref)
        if name is None:
            return 1, "", f"Error response from daemon: network {ref} not found"
        if any(self.containers[c]["State"]["Running"] for c in self.attached(name)):
            return 1, "", f"Error response from daemon: error while removing network: network {name} has active endpoints"
        del self.networks[name]
        return 0, name + "\n", ""

    def _ps(self, rest: list[str]) -> tuple[int, str, str]:
        filters = [rest[i + 1] for i, t in enumerate(rest) if t == "--filter"]
        quiet = "-q" in rest
        rows = []
        for name, c in self.containers.items():
            ok = "-a" in rest or c["State"]["Running"]  # without -a, running only
            for flt in filters:
                key, value = flt.split("=", 1)
                if key == "network":
                    # Attachments are keyed by name and outlive `network rm`.
                    net = self._find_network(value) or value
                    ok = ok and net in c["NetworkSettings"]["Networks"]
                elif key == "label":
                    lk, _, lv = value.partition("=")
                    labels = c["Config"]["Labels"]
                    ok = ok and lk in labels and (not _ or labels[lk] == lv)
                else:
                    return 1, "", f"fake engine: unsupported ps filter {flt!r}"
            if ok:
                rows.append(c["Id"] if quiet else name)
        return 0, "".join(r + "\n" for r in rows), ""

    def _run_container(self, rest: list[str]) -> tuple[int, str, str]:
        values: dict[str, list[str]] = {}
        image = ""
        i = 0
        while i < len(rest):
            token = rest[i]
            if token in _VALUE_FLAGS:
                values.setdefault(token, []).append(rest[i + 1])
                i += 2
            elif token.startswith("--"):
                i += 1
            else:
                image = token
                i += 1
        name = values["--name"][0]
        labels = dict(item.split("=", 1) for item in values.get("--label", []))
        if name in self.containers:
            return 125, "", (
                "docker: Error response from daemon: Conflict. The container name "
                f'"/{name}" is already in use by container "{self.containers[name]["Id"]}".'
            )
        if "--runtime" in values and values["--runtime"][0] not in self.runtimes:
            return 125, "", "docker: Error response from daemon: unknown or invalid runtime name"
        network = values.get("--network", ["bridge"])[0]
        if network not in self.networks:
            # Docker creates the container, then fails to start it.
            self.add_container(name, None, running=False, labels=labels)
            return 125, "", f"docker: Error response from daemon: failed to set up container networking: network {network} not found"
        container = self.add_container(name, network, running=True, labels=labels)
        container["Config"]["Image"] = image
        return 0, container["Id"] + "\n", ""
