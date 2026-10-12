# PMT Host templates
Use pmt-server init for schema-validated configuration generation and pmt-server plan for service/firewall command generation. These templates document values; they contain no credential, claim key or TLS private key.
A Windows release uses <app_root>/venv/Scripts/python.exe; Linux uses <app_root>/venv/bin/python. Runtime host-config.json stays outside the plugin cache.
Generated task/systemd definitions must use the reviewed config, account and file references. Do not apply a template directly to an operating Host.
