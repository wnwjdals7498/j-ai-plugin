# Linux Host administration
Install proj-mgmt-tool[host] in a separate versioned venv. Set PMT_SERVER_PYTHON to its bin/python and PMT_HOST_CONFIG_ROOT to the Host configuration folder.
Use protected service-owned state directories and existing TLS material. plan generates pmt-host.service with file-based LoadCredential aliases claim-<key_id> and tls-key. Runtime CREDENTIALS_DIRECTORY selects the delivered files; no secret values enter unit files or environment variables.
Absolute source paths remain in host-config.json. An explicit credential-directory claim placeholder uses HostConfigRoot/secrets/claim-<key_id>.key as its offline source, and the TLS placeholder uses HostConfigRoot/tls/tls-key. Missing/insecure sources block the plan; no directory scan chooses a private key.
Inspect the service/firewall plan before applying authorized system changes. Verify actual systemd start/restart, duplicate locking, TLS compatibility and allowed-source access on Linux; Windows fixture results cannot establish these checks.
Use the repository's current proj-mgmt-tool/docs/phase5/setup-linux.md for installation, owner/mode and operating checks.
