"""Named, source-restricted firewall plans for the PMT Host listener."""
from __future__ import annotations

import ipaddress
import json
import shutil

from ..errors import PmtError


def _ps(value):
    return "'" + str(value).replace("'", "''") + "'"


def _rich_rule(source, port):
    network = ipaddress.ip_network(source, strict=False)
    return (f'rule family="{network.version == 6 and "ipv6" or "ipv4"}" '
            f'source address="{network}" port port="{port}" protocol="tcp" accept')


def windows_rule_script(config, *, allow_public=False, replace=False):
    port = config["listen"]["port"]
    name = f"PMT Host {port}"
    addresses = sorted(set(config["access"]["allowed_sources"]))
    profiles = ["Domain", "Private", *( ["Public"] if allow_public else [])]
    lines = []
    if replace:
        lines.append(f"Remove-NetFirewallRule -DisplayName {_ps(name)} -ErrorAction SilentlyContinue")
    address_wire = "@(" + ",".join(_ps(value) for value in addresses) + ")"
    profile_wire = ",".join(profiles)
    lines.append(
        f"New-NetFirewallRule -DisplayName {_ps(name)} -Direction Inbound -Protocol TCP "
        f"-LocalPort {port} -RemoteAddress {address_wire} -Action Allow -Profile {profile_wire} | Out-Null"
    )
    return "\n".join(lines)


def inspect_firewall_state(config, *, platform, runner, allow_public=False):
    """Read only rules for the exact PMT Host listener; preserve all others."""
    port = config["listen"]["port"]
    name = f"PMT Host {port}"
    if platform == "windows":
        addresses = sorted(set(config["access"]["allowed_sources"]))
        addr = "@(" + ",".join(_ps(value) for value in addresses) + ")"
        profile_mask = 7 if allow_public else 3
        script = "\n".join([
            f"$rules = @(Get-NetFirewallRule -DisplayName {_ps(name)} -Direction Inbound -ErrorAction SilentlyContinue)",
            "if ($rules.Count -eq 0) { '{\"exists\":false,\"matches\":false}' ; exit 0 }",
            "$r = $rules[0]; $p = Get-NetFirewallPortFilter -AssociatedNetFirewallRule $r; $a = Get-NetFirewallAddressFilter -AssociatedNetFirewallRule $r",
            f"$expectedAddresses = {addr} | Sort-Object",
            "$actualAddresses = @($a.RemoteAddress | ForEach-Object { [string]$_ } | Sort-Object)",
            f"$match = ($rules.Count -eq 1) -and ($r.Enabled -eq 'True') -and ($r.Action -eq 'Allow') -and ($p.Protocol -eq 'TCP') -and ([string]$p.LocalPort -eq '{port}')",
            f"$match = $match -and ([int]$r.Profile -eq {profile_mask}) -and (@(Compare-Object $expectedAddresses $actualAddresses).Count -eq 0)",
            "ConvertTo-Json -Compress -InputObject @{ exists = $true; matches = [bool]$match }",
        ])
        result = runner({"kind": "powershell", "script": script})
        if result is None or result.returncode != 0:
            return {"backend": "windows", "exists": False, "matches": False, "unverified": True}
        try:
            return {"backend": "windows", **json.loads(result.stdout.strip())}
        except (ValueError, TypeError):
            return {"backend": "windows", "exists": False, "matches": False, "unverified": True}

    if platform != "linux":
        return {"backend": "none"}
    if shutil.which("firewall-cmd"):
        result = runner({"kind": "exec", "argv": ["firewall-cmd", "--permanent", "--list-rich-rules"]})
        if result is None or result.returncode != 0:
            return {"backend": "firewalld", "rich_rules": [], "unverified": True}
        return {"backend": "firewalld", "rich_rules": result.stdout.splitlines()}
    if shutil.which("ufw"):
        result = runner({"kind": "exec", "argv": ["ufw", "status", "numbered"]})
        if result is None or result.returncode != 0:
            return {"backend": "ufw", "rules": [], "unverified": True}
        rules = []
        import re
        for line in result.stdout.splitlines():
            if name not in line or "ALLOW IN" not in line:
                continue
            match = re.search(r"\]\s+\d+/tcp\s+ALLOW IN\s+(\S+).*?comment\s+['\"]?PMT Host (\d+)['\"]?", line)
            if match and int(match.group(2)) == port:
                rules.append((match.group(1), port, name))
        return {"backend": "ufw", "rules": rules}
    return {"backend": "none"}


def plan_firewall(config, *, current=None, platform, backend=None, allow_public=False):
    """Compare configured allow sources with injected read-only firewall state."""
    if type(config["listen"]["port"]) is not int or not 1 <= config["listen"]["port"] <= 65535:
        raise PmtError("config_invalid", "listen.port is invalid")
    port = config["listen"]["port"]
    sources = sorted(set(config["access"]["allowed_sources"]))
    if not sources:
        return {"component": "firewall", "status": "blocked", "error_code": "firewall_sources_required",
                "message": "Firewall changes require at least one allowed source", "commands": []}
    for source in sources:
        try:
            ipaddress.ip_network(source, strict=False) if "/" in source else ipaddress.ip_address(source)
        except ValueError as exc:
            raise PmtError("config_invalid", "access.allowed_sources contains an invalid network") from exc

    current = current or {}
    if platform == "windows":
        if allow_public:
            profiles = ["Domain", "Private", "Public"]
        else:
            profiles = ["Domain", "Private"]
        desired = {"name": f"PMT Host {port}", "direction": "Inbound", "protocol": "TCP",
                   "port": port, "remote_addresses": sources, "action": "Allow", "profiles": profiles}
        if current.get("matches") is True:
            return {"component": "firewall", "status": "current", "desired": desired, "commands": []}
        script = windows_rule_script(config, allow_public=allow_public, replace=bool(current.get("exists")))
        return {"component": "firewall", "status": "planned", "desired": desired,
                "commands": [{"kind": "powershell", "script": script}]}

    if platform != "linux":
        raise PmtError("service_unsupported", "Firewall management is supported on Windows and Linux")
    if backend not in {"firewalld", "ufw"}:
        return {"component": "firewall", "status": "manual", "desired": {"port": port, "sources": sources},
                "message": "No supported firewall manager was found; apply the documented rules manually",
                "commands": []}

    commands = []
    if backend == "firewalld":
        existing = set(current.get("rich_rules", []))
        desired_rules = [_rich_rule(source, port) for source in sources]
        port_marker = f'port port="{port}" protocol="tcp"'
        conflicting = [rule for rule in existing
                       if port_marker in rule and rule not in desired_rules and rule.rstrip().endswith("accept")]
        if conflicting:
            return {"component": "firewall", "status": "blocked", "error_code": "firewall_unowned_rules",
                    "message": "Existing firewalld rules for this listener are not PMT-identifiable; inspect them before applying",
                    "commands": [], "unowned_rules": conflicting}
        for rule in desired_rules:
            if rule not in existing:
                commands.append({"kind": "exec", "argv": ["firewall-cmd", "--permanent", f"--add-rich-rule={rule}"]})
        if commands:
            commands.append({"kind": "exec", "argv": ["firewall-cmd", "--reload"]})
        desired = {"backend": backend, "port": port, "rich_rules": desired_rules}
    else:
        # UFW's comment is the stable owner label. Removal is limited to rules
        # carrying this exact PMT Host label; unrelated rules stay untouched.
        name = f"PMT Host {port}"
        existing = {tuple(item) for item in current.get("rules", [])}
        desired_rules = [(source, port, name) for source in sources]
        for source, item_port, label in desired_rules:
            if (source, item_port, label) not in existing:
                commands.append({"kind": "exec", "argv": [
                    "ufw", "allow", "from", source, "to", "any", "port", str(port),
                    "proto", "tcp", "comment", name,
                ]})
        for source, item_port, label in sorted(existing - set(desired_rules)):
            if item_port == port and label == name:
                commands.append({"kind": "exec", "argv": [
                    "ufw", "delete", "allow", "from", source, "to", "any", "port", str(port),
                    "proto", "tcp", "comment", name,
                ]})
        desired = {"backend": backend, "port": port,
                   "rules": [{"source": source, "port": port, "comment": name} for source in sources]}
    return {"component": "firewall", "status": "planned" if commands else "current",
            "desired": desired, "commands": commands}
