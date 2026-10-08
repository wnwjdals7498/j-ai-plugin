"""Resolve systemd credential paths in memory without changing Host configuration."""
from __future__ import annotations
import copy
import os
from pathlib import Path
from ..errors import PmtError
from .service import credential_source_map


def resolve_runtime_config(config, config_root, *, environ=None):
    """Use service credential aliases. Missing delivered files never fall back.
    Existing consumers continue to enforce ownership, ACL and TLS validation.
    """
    resolved = copy.deepcopy(config)
    if config["service"]["kind"] != "systemd":
        references = [config["claim_key"]["source"],
                      *[entry["source"] for entry in config["claim_key"].get("retained", [])]]
        values = [source.get("path", "") for source in references]
        values.extend(config["tls"].values())
        if any("${CREDENTIALS_DIRECTORY}" in value for value in values if isinstance(value, str)):
            raise PmtError("service_unsupported", "Credential-directory references require a systemd service")
        return resolved
    sources = credential_source_map(config, config_root)
    environment = os.environ if environ is None else environ
    directory = environment.get("CREDENTIALS_DIRECTORY")
    if directory is not None:
        if (not isinstance(directory, str) or not directory or
                any(ord(character) < 32 for character in directory) or
                "$" in directory or "~" in directory or
                not Path(directory).is_absolute() or ".." in Path(directory).parts):
            raise PmtError("host_key_unavailable", "Systemd credential directory must be an absolute path")
        sources = {alias: str(Path(directory) / alias) for alias in sources}
    claim = resolved["claim_key"]
    claim["source"]["path"] = sources[f"claim-{claim['key_id']}"]
    for retained in claim.get("retained", []):
        retained["source"]["path"] = sources[f"claim-{retained['key_id']}"]
    if resolved["tls"].get("key_file"):
        resolved["tls"]["key_file"] = sources["tls-key"]
    for name in ("cert_file", "ca_file"):
        if "${CREDENTIALS_DIRECTORY}" in resolved["tls"].get(name, ""):
            raise PmtError("service_unsupported", "Public TLS certificates must use absolute file paths")
    return resolved
