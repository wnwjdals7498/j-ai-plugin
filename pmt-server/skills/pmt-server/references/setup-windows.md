# Windows Host administration
Install the selected commit's proj-mgmt-tool[host] in a separate release venv. Set PMT_SERVER_PYTHON to its Scripts/python.exe and PMT_HOST_CONFIG_ROOT to the Host configuration folder.
Use init --adopt only after a read-only survey of an existing Host, and show the dry-run first. Preserve the old task XML and stop/backup only with authorization. Apply adoption, backup, restore-check and doctor before manual serve.
Register an existing TLS pair with tls register; distribute only its public CA. device issue --credential-out stores a separate protected token; handoff JSON carries endpoint, namespace, grants, project/repository and optional public CA only.
Inspect plan --only service,firewall before replacing automatic-start definitions. Disable an old task/rule instead of deleting it. A service install, restart, reboot, key-store migration, or production device change requires the user's operating authorization.
Use the repository's current proj-mgmt-tool/docs/phase5/setup-windows.md and codex-phase5-host.md for the complete adoption gates and actual verification checklist. This reference does not authorize a deployment.
