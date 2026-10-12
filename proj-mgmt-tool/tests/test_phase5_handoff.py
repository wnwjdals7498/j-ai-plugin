from copy import deepcopy
import hashlib
import json
import uuid

import pytest

from pmt.errors import PmtError
from pmt.handoff import build_handoff, load_handoff, validate_handoff

def ident():
    return str(uuid.uuid4())

def document():
    project = ident()
    return build_handoff(host_url="https://pmt.example:8765", namespace_id=ident(),
        device={"device_id": ident(), "actor": "developer", "permissions": ["read", "write"],
                "scopes": [project], "credential": {"delivery": "separate", "env": "PMT_HOST_CREDENTIAL"}},
        projects=[{"name": "example", "project_id": project, "repositories": [
            {"name": "repo", "repository_id": ident(), "remote": "https://github.com/example/repo.git",
             "graph_path": "docs/pmt-docs/graph.json"}]}])

def test_roundtrip_strict_json_and_isolated_result(tmp_path):
    value = document()
    path = tmp_path / "handoff.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    assert load_handoff(path) == value
    result = validate_handoff(value)
    result["device"]["permissions"].append("admin")
    assert value["device"]["permissions"] == ["read", "write"]

@pytest.mark.parametrize("version", [2, True, "1", None])
def test_unknown_version_is_rejected(version):
    value = document()
    value["version"] = version
    with pytest.raises(PmtError) as error:
        validate_handoff(value)
    assert error.value.code == "handoff_version_unsupported"

@pytest.mark.parametrize("url", ["http://pmt.example:8765", "http://127.0.0.1:18765",
    "https://user:password@pmt.example", "https://pmt.example/?secret=x",
    "https://pmt.example/a/../b", "https://pmt.example:0", "https://pmt.example/\n"])
def test_unsafe_host_endpoint_rejected(url):
    value = document()
    value["host"]["url"] = url
    with pytest.raises(PmtError) as error:
        validate_handoff(value)
    assert error.value.code == "handoff_invalid"

def test_loopback_http_requires_explicit_test_mode():
    value = document()
    value["host"]["url"] = "http://127.0.0.1:18765"
    assert validate_handoff(value, allow_loopback_http=True) == value

@pytest.mark.parametrize("field,value", [("core", "0.5"), ("db_schema", 4),
    ("graph_schema", True), ("protocol", [True]), ("protocol", [1, 2])])
def test_compatibility_checked_before_connect(field, value):
    doc = document()
    doc["host"]["compatibility"][field] = value
    with pytest.raises(PmtError) as error:
        validate_handoff(doc)
    assert error.value.code == "incompatible"

@pytest.mark.parametrize("target", ["root", "host", "device", "credential", "repository"])
def test_secret_fields_never_accepted_or_echoed(target):
    value = document()
    node = {"root": value, "host": value["host"], "device": value["device"],
        "credential": value["device"]["credential"],
        "repository": value["projects"][0]["repositories"][0]}[target]
    node["secret_value"] = "synthetic-secret-must-not-appear"
    with pytest.raises(PmtError) as error:
        validate_handoff(value)
    assert error.value.code == "handoff_invalid"
    assert "synthetic-secret" not in str(error.value)

@pytest.mark.parametrize("value", ["bad-id", "FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF", None])
def test_noncanonical_device_id_rejected(value):
    doc = document()
    doc["device"]["device_id"] = value
    with pytest.raises(PmtError):
        validate_handoff(doc)

@pytest.mark.parametrize("remote", ["https://token@github.com/example/repo",
    "https://user:password@github.com/example/repo", "https://github.com/repo?token=x",
    "file:///local/repo"])
def test_repository_remote_cannot_transport_credentials(remote):
    value = document()
    value["projects"][0]["repositories"][0]["remote"] = remote
    with pytest.raises(PmtError):
        validate_handoff(value)

def test_separate_delivery_and_bounded_graph_path_required():
    value = document()
    value["device"]["credential"]["delivery"] = "inline"
    with pytest.raises(PmtError):
        validate_handoff(value)
    value = document()
    value["projects"][0]["repositories"][0]["graph_path"] = "../secrets"
    with pytest.raises(PmtError):
        validate_handoff(value)

def test_ca_digest_and_certificate_validation(tmp_path):
    from test_phase3_host_network import _certificate
    cert, _ = _certificate(tmp_path, "public-ca")
    value = document()
    pem = cert.read_text(encoding="utf-8")
    value["host"].update(ca_pem=pem, ca_sha256=hashlib.sha256(pem.encode()).hexdigest())
    assert validate_handoff(value) == value
    value["host"]["ca_sha256"] = "0" * 64
    with pytest.raises(PmtError) as error:
        validate_handoff(value)
    assert error.value.code == "handoff_ca_mismatch"
    value["host"].pop("ca_sha256")
    with pytest.raises(PmtError):
        validate_handoff(value)

def test_duplicate_keys_and_oversized_input_rejected(tmp_path):
    path = tmp_path / "handoff.json"
    path.write_text('{"format":"pmt-handoff","format":"other"}')
    with pytest.raises(PmtError) as error:
        load_handoff(path)
    assert error.value.code == "handoff_invalid"
    path.write_bytes(b" " * (1024 * 1024 + 1))
    with pytest.raises(PmtError):
        load_handoff(path)

def test_wildcard_bootstrap_device_is_not_a_client_handoff():
    value = document()
    value["device"]["scopes"] = ["*"]
    with pytest.raises(PmtError):
        validate_handoff(value)


@pytest.mark.parametrize("projects", [False, {}, "invalid"])
def test_builder_does_not_silently_default_invalid_optional_values(projects):
    doc = document()
    with pytest.raises(PmtError):
        build_handoff(host_url=doc["host"]["url"], namespace_id=doc["namespace_id"],
            device=doc["device"], projects=projects)
