"""Stdlib HTTP transport for the operation-level StorePort.

This module contains no Host implementation. It sends protocol-v1 requests to
an explicitly configured server and validates the returned core envelope.
"""
from __future__ import annotations

import ipaddress
import hashlib
import os
import re
import ssl
import time
import warnings
from contextlib import closing
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import (HTTPRedirectHandler, HTTPSHandler, Request,
                            build_opener)
import uuid

from .errors import PmtError
from .util import canonical_json, strict_json_loads

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_MAX_REQUEST = 1024 * 1024
_MAX_RESPONSE = 1024 * 1024
_CORE_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}$")
_OPERATION = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_LOCAL_CORE = (0, 3)


def _identity(value, field):
    if (not isinstance(value, str) or not value.strip() or len(value) > 200
            or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)):
        raise PmtError("invalid_identity", f"{field} must be nonempty text without control characters")
    return value


def _uuid(value, field):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise PmtError("invalid_id", f"{field} must be a canonical UUID") from exc
    return value


def _validate_endpoint(endpoint, allow_loopback_http):
    if not isinstance(endpoint, str) or not endpoint or any(ord(c) < 0x21 for c in endpoint):
        raise ValueError("endpoint must be an absolute HTTPS URL")
    try:
        parts = urlsplit(endpoint)
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise ValueError("endpoint URL is invalid") from exc
    decoded_path = unquote(parts.path)
    if (parts.scheme not in {"https", "http"} or not host or parts.username is not None
            or parts.password is not None or parts.query or parts.fragment):
        raise ValueError("endpoint must not contain credentials, query or fragment")
    if ("\\" in endpoint or "\\" in decoded_path
            or any(part in {".", ".."} for part in decoded_path.split("/"))):
        raise ValueError("endpoint path contains an unsafe segment")
    if parts.scheme == "http":
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValueError("plain HTTP is limited to literal loopback addresses") from exc
        if not allow_loopback_http or not address.is_loopback:
            raise ValueError("plain HTTP requires explicit loopback test mode")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("endpoint port is invalid")
    path = parts.path.rstrip("/")
    return f"{parts.scheme}://{parts.netloc}{path}"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HttpStore:
    """HTTP implementation of execute/get_request_result/check_compatibility.

    Credentials are referenced by environment variable name and read only
    immediately before a request. They are never included in diagnostics.
    """

    def __init__(self, endpoint, credential_env, device_id, environment_id,
                 namespace_id, ca_file=None, timeout=10.0,
                 allow_loopback_http=False, observer=None):
        self.endpoint = _validate_endpoint(endpoint, allow_loopback_http)
        if not isinstance(credential_env, str) or not _ENV_NAME.fullmatch(credential_env):
            raise PmtError("credential_reference_invalid", "credential_env must be an environment variable name")
        self.credential_env = credential_env
        self.device_id = _uuid(device_id, "device_id")
        self.environment_id = _uuid(environment_id, "environment_id")
        self.namespace_id = _uuid(namespace_id, "namespace_id")
        if type(timeout) not in (int, float) or timeout <= 0 or timeout > 300:
            raise PmtError("timeout_invalid", "timeout must be greater than 0 and at most 300 seconds")
        self.timeout = float(timeout)
        self.observer = observer
        context = ssl.create_default_context(cafile=ca_file)
        self._opener = build_opener(_NoRedirect(), HTTPSHandler(context=context))

    def _credential(self):
        value = os.environ.get(self.credential_env)
        if (not isinstance(value, str) or not value
                or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)):
            raise PmtError("credential_unavailable", "Configured credential is unavailable", 3)
        return value

    def _url(self, path):
        return self.endpoint + path

    def _headers(self, session_id):
        headers = {
            "Authorization": "Bearer " + self._credential(),
            "X-PMT-Device": self.device_id,
            "X-PMT-Environment": self.environment_id,
            "X-PMT-Namespace": self.namespace_id,
            "Accept": "application/json",
        }
        if session_id:
            headers["X-PMT-Session"] = session_id
        return headers

    def _emit(self, request_id, operation, status, request_bytes, response_bytes, started):
        if self.observer is None:
            return
        if not isinstance(operation, str) or not _OPERATION.fullmatch(operation):
            operation = "unknown"
        event = {"request_id": request_id, "operation": operation, "http_status": status,
                 "request_bytes": request_bytes, "response_bytes": response_bytes,
                 "elapsed_ms": max(0, int((time.monotonic() - started) * 1000))}
        try:
            self.observer(event)
        except Exception:
            warnings.warn("HttpStore observer failed; transport operation result is unchanged",
                          RuntimeWarning, stacklevel=2)

    def _request(self, method, path, *, session_id=None, body=None, request_id=None,
                 operation="", request_fingerprint=None):
        if path not in {"/api/v1/operations", "/api/v1/compatibility", "/api/v1/sessions"} and not re.fullmatch(
                r"/api/v1/requests/[0-9a-f-]{36}", path):
            raise PmtError("remote_path_invalid", "Unsupported remote API path", 2)
        raw = None if body is None else canonical_json(body).encode("utf-8")
        if raw is not None and len(raw) > _MAX_REQUEST:
            raise PmtError("request_too_large", "HTTP request exceeds 1 MiB", 2)
        headers = self._headers(session_id)
        if raw is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        if request_fingerprint is not None:
            if not isinstance(request_fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", request_fingerprint):
                raise PmtError("request_fingerprint_invalid", "Expected request fingerprint is invalid", 2)
            headers["X-PMT-Request-Fingerprint"] = request_fingerprint
        req = Request(self._url(path), data=raw, headers=headers, method=method)
        started = time.monotonic()
        status = None
        response_bytes = 0
        try:
            try:
                response = self._opener.open(req, timeout=self.timeout)
            except HTTPError as exc:
                response = exc
            with closing(response) as response_stream:
                status = response_stream.getcode()
                data = response_stream.read(_MAX_RESPONSE + 1)
            response_bytes = len(data)
            if response_bytes > _MAX_RESPONSE:
                raise PmtError("remote_response_too_large", "Remote response exceeds 1 MiB", 3)
            if status is not None and 300 <= status < 400:
                return status, None, response_bytes
            try:
                value = strict_json_loads(data, max_bytes=_MAX_RESPONSE)
            except PmtError:
                raise PmtError("remote_response_invalid", "Remote response is not valid UTF-8 JSON", 3)
            return status, value, response_bytes
        except PmtError:
            raise
        except (URLError, TimeoutError, OSError) as exc:
            raise PmtError("remote_unavailable", "Remote storage could not be reached", 3,
                           True, {"effect": "unknown"}) from exc
        finally:
            self._emit(request_id, operation, status, len(raw or b""), response_bytes, started)

    @staticmethod
    def _valid_envelope(value, request_id):
        if not (isinstance(value, dict)
                and set(value) == {"protocol_version", "request_id", "ok", "result", "error", "warnings"}
                and type(value.get("protocol_version")) is int and value["protocol_version"] == 1
                and value.get("request_id") == request_id and type(value.get("ok")) is bool
                and isinstance(value.get("warnings"), list)
                and all(isinstance(item, str) for item in value["warnings"])):
            return False
        error = value["error"]
        if value["ok"]:
            return error is None
        return (isinstance(error, dict) and isinstance(error.get("code"), str) and bool(error["code"])
                and isinstance(error.get("message"), str) and type(error.get("retryable")) is bool
                and ("details" not in error or error["details"] is None or isinstance(error["details"], dict)))

    def _operation(self, request):
        if not isinstance(request, dict):
            raise PmtError("invalid_request", "Request must be an object")
        request_id = _uuid(request.get("request_id"), "request_id")
        session_id = _identity(request.get("session_id"), "session_id")
        actor = _identity(request.get("actor"), "actor")
        body = dict(request)
        body.setdefault("protocol_version", 1)
        status, value, size = self._request("POST", "/api/v1/operations", session_id=session_id,
                                            body=body, request_id=request_id,
                                            operation=request.get("operation", ""))
        if status not in {200, 201, 202, 400, 401, 403, 409, 503}:
            raise PmtError("remote_http_error", "Remote server returned an unsupported HTTP status", 3,
                           details={"http_status": status})
        if not isinstance(value, dict) or set(value) != {"api_version", "envelope", "exit_code"}:
            raise PmtError("remote_response_invalid", "Remote operation response has an invalid shape", 3)
        if type(value.get("api_version")) is not int or value["api_version"] != 1:
            raise PmtError("remote_api_version_unsupported", "Remote API version is unsupported", 3)
        exit_code = value.get("exit_code")
        envelope = value.get("envelope")
        if (type(exit_code) is not int or not 0 <= exit_code <= 5
                or not self._valid_envelope(envelope, request_id)):
            raise PmtError("remote_response_invalid", "Remote operation envelope is invalid", 3)
        success_status = status in {200, 201, 202}
        if (envelope["ok"] is not (exit_code == 0)
                or (exit_code == 0) is not success_status):
            raise PmtError("remote_response_invalid", "Remote status, envelope and exit code disagree", 3)
        return envelope, exit_code

    def execute(self, request):
        from .host.execution_metadata import validate_execution_metadata
        validate_execution_metadata(request)
        return self._operation(request)

    def register_session(self, session_id):
        session_id = _identity(session_id, "session_id")
        status, value, _ = self._request("POST", "/api/v1/sessions", body={
            "session_id": session_id, "environment_id": self.environment_id},
            session_id=session_id, request_id=session_id, operation="register_session")
        if status not in {200, 201}:
            raise self._remote_error(status, value)
        if not isinstance(value, dict) or value.get("api_version") != 1 or value.get("session_id") != session_id:
            raise PmtError("remote_response_invalid", "Session registration response is invalid", 3)
        return value

    def _resource_request(self, method, path, session_id, *, content=None, metadata=None,
                          maximum_bytes=8 * 1024 * 1024, metadata_header="X-PMT-Resource-Metadata", content_type="application/octet-stream"):
        """Resource bytes use a separate bound; authentication/redirect rules stay shared."""
        _identity(session_id, "session_id")
        limit = maximum_bytes
        if content is not None and (not isinstance(content, bytes) or len(content) > limit):
            raise PmtError("resource_too_large", "Resource must be bytes within 8 MiB", 2)
        headers = self._headers(session_id)
        if metadata is not None:
            encoded = canonical_json(metadata)
            if len(encoded.encode()) > 4096:
                raise PmtError("resource_metadata_too_large", "Resource metadata exceeds its bound", 2)
            headers[metadata_header] = encoded
            headers["Content-Type"] = content_type
        started = time.monotonic()
        status, size = None, 0
        try:
            req = Request(self._url(path), data=content, headers=headers, method=method)
            try:
                stream = self._opener.open(req, timeout=self.timeout)
            except HTTPError as error:
                stream = error
            with closing(stream):
                status = stream.getcode()
                maximum = limit if method == "GET" and status == 200 else _MAX_RESPONSE
                data = stream.read(maximum + 1)
                response_headers = stream.headers
            size = len(data)
            if size > maximum:
                raise PmtError("remote_response_too_large", "Resource response exceeds its bound", 3)
            if status != 200:
                if 300 <= status < 400:
                    raise PmtError("remote_redirect_forbidden", "Storage redirects are not followed", 3)
                value = strict_json_loads(data, max_bytes=_MAX_RESPONSE)
                raise self._remote_error(status, value)
            return data, response_headers
        except PmtError:
            raise
        except (URLError, TimeoutError, OSError) as error:
            raise PmtError("remote_unavailable", "Resource storage could not be reached", 4, True,
                           {"effect": "unknown"}) from error
        finally:
            self._emit(metadata.get("request_id") if metadata else None, "resource_transfer", status,
                       len(content or b""), size, started)

    def publish_resource(self, metadata, content, *, session_id):
        if not isinstance(metadata, dict) or set(metadata) - {"request_id", "scope_id", "purpose", "sha256", "size"}:
            raise PmtError("host_input_invalid", "Resource metadata is invalid", 2)
        value = dict(metadata)
        _uuid(value.get("request_id"), "request_id")
        _uuid(value.get("scope_id"), "scope_id")
        if value.get("purpose") not in {"evidence", "result", "graph_snapshot", "verification_snapshot"}:
            raise PmtError("host_resource_purpose_invalid", "Resource purpose is unsupported", 2)
        if "size" in value and type(value["size"]) is not int:
            raise PmtError("host_input_invalid", "Resource size must be an integer", 2)
        if not isinstance(content, bytes):
            raise PmtError("host_input_invalid", "Resource content must be bytes", 2)
        digest = hashlib.sha256(content).hexdigest()
        if value.get("sha256", digest) != digest or value.get("size", len(content)) != len(content):
            raise PmtError("resource_hash_mismatch", "Resource size or hash does not match", 2)
        value.update(sha256=digest, size=len(content))
        data, _ = self._resource_request("POST", "/api/v1/resources", session_id, content=content, metadata=value)
        result = strict_json_loads(data, max_bytes=_MAX_RESPONSE)
        artifact = result.get("artifact_ref") if isinstance(result, dict) else None
        receipt = result.get("receipt_ref") if isinstance(result, dict) else None
        if (not isinstance(result, dict) or result.get("api_version") != 1 or not isinstance(artifact, dict)
                or artifact.get("sha256") != digest or artifact.get("size") != len(content)
                or artifact.get("scope_id") != value["scope_id"] or artifact.get("purpose") != value.get("purpose")
                or not isinstance(receipt, dict) or receipt.get("request_id") != value["request_id"]):
            raise PmtError("remote_response_invalid", "Resource publication receipt is invalid", 3)
        _uuid(artifact.get("id"), "resource_id")
        return result

    def read_resource(self, resource_id, *, session_id, expected_sha256, scope_id, run_id=None):
        _uuid(resource_id, "resource_id")
        _uuid(scope_id, "scope_id")
        if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise PmtError("resource_hash_invalid", "Expected resource hash must be SHA-256", 2)
        path = "/api/v1/resources/" + resource_id
        if run_id is not None:
            path += "?run_id=" + _uuid(run_id, "run_id")
        data, headers = self._resource_request("GET", path, session_id)
        if (headers.get("X-PMT-SHA256") != expected_sha256 or hashlib.sha256(data).hexdigest() != expected_sha256
                or headers.get("X-PMT-Scope") != scope_id or headers.get("X-PMT-Resource") != resource_id):
            raise PmtError("resource_hash_mismatch", "Resource bytes or binding do not match the expected receipt", 3)
        return {"content": data, "sha256": expected_sha256, "size": len(data),
                "scope_id": scope_id, "resource_id": resource_id, "purpose": headers.get("X-PMT-Purpose")}

    def import_bundle(self, metadata, content, *, session_id):
        if not isinstance(metadata, dict) or set(metadata) != {"bundle_id", "manifest_sha256"}:
            raise PmtError("transfer_metadata_invalid", "Transfer metadata is invalid", 2)
        _uuid(metadata["bundle_id"], "bundle_id")
        if not isinstance(metadata["manifest_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", metadata["manifest_sha256"]):
            raise PmtError("transfer_metadata_invalid", "Manifest hash must be SHA-256", 2)
        data, _ = self._resource_request("POST", "/api/v1/transfers/import", session_id, content=content,
            metadata=metadata, maximum_bytes=64 * 1024 * 1024, metadata_header="X-PMT-Transfer-Metadata",
            content_type="application/vnd.pmt.migration+zip")
        value = strict_json_loads(data, max_bytes=_MAX_RESPONSE)
        if not isinstance(value, dict) or value.get("api_version") != 1 or not isinstance(value.get("receipt"), dict):
            raise PmtError("remote_response_invalid", "Import receipt is invalid", 3)
        return value["receipt"]

    def create_backup(self, request_id, *, session_id):
        _uuid(request_id, "request_id")
        data, _ = self._resource_request("POST", "/api/v1/transfers/backup", session_id,
            content=canonical_json({"request_id": request_id}).encode(), metadata={}, content_type="application/json")
        value = strict_json_loads(data, max_bytes=_MAX_RESPONSE)
        if not isinstance(value, dict) or value.get("api_version") != 1 or not isinstance(value.get("download_ref"), str):
            raise PmtError("remote_response_invalid", "Backup receipt is invalid", 3)
        _uuid(value["download_ref"], "download_ref")
        return value

    def download_bundle(self, download_ref, *, session_id, expected_manifest_sha256):
        _uuid(download_ref, "download_ref")
        data, headers = self._resource_request("GET", "/api/v1/transfers/download/" + download_ref, session_id,
                                             maximum_bytes=64 * 1024 * 1024)
        digest = hashlib.sha256(data).hexdigest()
        if (headers.get("X-PMT-Download-Ref") != download_ref or headers.get("X-PMT-Manifest-SHA256") != expected_manifest_sha256
                or headers.get("X-PMT-Bundle-SHA256") != digest):
            raise PmtError("transfer_hash_mismatch", "Downloaded bundle or manifest binding does not match", 3)
        return {"content": data, "manifest_sha256": expected_manifest_sha256, "bundle_sha256": digest,
                "download_ref": download_ref, "size_bytes": len(data)}

    def get_request_result(self, request_id, actor=None, session_id=None, *, expected_request=None):
        request_id = _uuid(request_id, "request_id")
        actor = _identity(actor, "actor")
        session_id = _identity(session_id, "session_id")
        expected_fingerprint = None
        if expected_request is not None:
            if (not isinstance(expected_request, dict) or expected_request.get("request_id") != request_id
                    or expected_request.get("actor") != actor or expected_request.get("session_id") != session_id):
                raise PmtError("request_lookup_invalid", "Expected request identity does not match lookup", 2)
            from .db import semantic_request_fingerprint
            from .service import normalize_lookup_request
            expected_fingerprint = semantic_request_fingerprint(normalize_lookup_request(expected_request))
        status, value, _ = self._request("GET", f"/api/v1/requests/{request_id}",
                                         session_id=session_id, request_id=request_id,
                                         operation="get_request_result",
                                         request_fingerprint=expected_fingerprint)
        if status != 200:
            raise self._remote_error(status, value)
        if (not isinstance(value, dict) or set(value) != {"api_version", "actor", "session_id", "envelope", "exit_code"}
                or type(value.get("api_version")) is not int or value["api_version"] != 1
                or value.get("actor") != actor or value.get("session_id") != session_id):
            raise PmtError("remote_response_invalid", "Request result is invalid or belongs to another owner", 3)
        if value["envelope"] is None and value["exit_code"] is None:
            return None
        if (type(value["exit_code"]) is not int or not 0 <= value["exit_code"] <= 5
                or not self._valid_envelope(value["envelope"], request_id)
                or value["envelope"]["ok"] is not (value["exit_code"] == 0)):
            raise PmtError("remote_response_invalid", "Request result envelope is invalid", 3)
        return value["envelope"], value["exit_code"]

    @staticmethod
    def _remote_error(status, value):
        if isinstance(value, dict) and isinstance(value.get("error"), dict):
            error = value["error"]
            code = error.get("code") if isinstance(error.get("code"), str) else "remote_request_failed"
            message = error.get("message") if isinstance(error.get("message"), str) else "Remote request failed"
            retryable = error.get("retryable") is True
            details = error.get("details") if isinstance(error.get("details"), dict) else None
            exit_code = error.get("exit_code") if type(error.get("exit_code")) is int else (3 if status >= 500 else 2)
            return PmtError(code, message, exit_code, retryable, details)
        return PmtError("remote_request_failed", "Remote request failed", 3 if status >= 500 else 2,
                        status >= 500, {"http_status": status})

    def check_compatibility(self):
        status, value, _ = self._request("GET", "/api/v1/compatibility", operation="check_compatibility")
        if status != 200:
            raise self._remote_error(status, value)
        required = {"api_version", "core_version", "db_schema", "graph_schema", "protocol_versions",
                    "namespace_id", "device_id", "actor", "scopes", "permissions"}
        if not isinstance(value, dict) or set(value) != required:
            raise PmtError("remote_response_invalid", "Compatibility response has an invalid shape", 3)
        if (type(value["api_version"]) is not int or value["api_version"] != 1
                or not isinstance(value["core_version"], str) or not _CORE_VERSION.fullmatch(value["core_version"])
                or type(value["db_schema"]) is not int or value["db_schema"] < 1
                or type(value["graph_schema"]) is not int or value["graph_schema"] < 1
                or not isinstance(value["protocol_versions"], list)
                or any(type(item) is not int or item < 1 for item in value["protocol_versions"])
                or value["namespace_id"] != self.namespace_id or value["device_id"] != self.device_id
                or not isinstance(value["actor"], str) or not value["actor"]
                or not isinstance(value["scopes"], list) or any(not isinstance(x, str) for x in value["scopes"])
                or not isinstance(value["permissions"], list)
                or any(not isinstance(x, str) for x in value["permissions"])):
            raise PmtError("remote_compatibility_mismatch", "Remote identity or compatibility is invalid", 3)
        from . import __version__
        from .db import SCHEMA_VERSION
        from .planning.graph import SCHEMA_VERSION as GRAPH_SCHEMA_VERSION
        local_major_minor = tuple(int(item) for item in __version__.split(".")[:2])
        remote_parts = value["core_version"].split(".")
        remote_major_minor = tuple(int(item) for item in remote_parts[:2]) if len(remote_parts) >= 2 and all(
            item.isdigit() for item in remote_parts[:2]) else None
        expected = {"core_major_minor": list(local_major_minor), "db_schema": SCHEMA_VERSION,
                    "graph_schema": GRAPH_SCHEMA_VERSION, "protocol_version": 1}
        mismatches = []
        if remote_major_minor != local_major_minor:
            mismatches.append("core_major_minor")
        if value["db_schema"] != SCHEMA_VERSION:
            mismatches.append("db_schema")
        if value["graph_schema"] != GRAPH_SCHEMA_VERSION:
            mismatches.append("graph_schema")
        if 1 not in value["protocol_versions"]:
            mismatches.append("protocol_version")
        return {"compatible": not mismatches, "incompatibilities": mismatches,
                "expected": expected, **value, "storage": "http"}
