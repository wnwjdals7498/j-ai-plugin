---
name: pmt-server
description: Install, configure, diagnose, and operate a PMT storage Host using its installed host venv and pmt-server CLI. Use on the Host administrator computer for server tasks; client connection and project work use the pmt skill.
---

# PMT Server

Use the Host venv selected by PMT_SERVER_PYTHON. The plugin contains guidance and launchers; install proj-mgmt-tool[host] separately from the same release. PMT_HOST_CONFIG_ROOT identifies the folder containing host-config.json. Claude SessionStart exports only these two paths; Codex has no server Hook, so set them explicitly before using bin/pmt-server or bin/pmt-server.cmd.

Read the applicable [Windows procedure](references/setup-windows.md) or [Linux procedure](references/setup-linux.md). For an existing Host, first inspect its paths, namespace, device list, key references, account, and automatic-start definition. Preserve the prior service definition and a verified backup before an authorized transition. Use isolated paths and a separate loopback port for development tests.

Use this order as dependencies permit:

1. Install the host release in its own venv; verify pmt-server version.
2. Plan init (or init --adopt for an existing Host); apply only within the authorized server scope.
3. Register the existing TLS certificate/key; check SAN, pair, chain and expiry. No CA-generation command is provided.
4. Run doctor, then serve for an authorized manual health and authenticated compatibility check.
5. Register projects/repositories, issue scoped client devices, create handoff JSON and a separate protected credential file.
6. Review plan for service and firewall; apply only after authorization for those operating changes. Verify restart, duplicate prevention and allowed-source access on the actual machine.
7. Run backup and restore-check; upgrade only with a verified backup and rollback path.

Credentials, claim keys and TLS private keys stay out of chat, logs, Git and handoff JSON. Prefer --credential-out; never copy the Host claim key to a client. Normal client grants use actual project UUIDs and read,write,runtime,review. Temporary bootstrap devices are revoked even when project creation fails.

For a failure, inspect the structured error, config validation, doctor, redacted logs, then status. Stop on active claims/runs or incompatible schema. Do not overwrite state to make a check pass. Generated commands and fixtures are evidence about a plan; they do not establish actual service, reboot or firewall success.

Historical operating context: [Windows Host](references/windows-host.md) and [deployment order](references/deployment-order.md). Current Phase5 procedures and observed evidence take precedence over old command examples.
