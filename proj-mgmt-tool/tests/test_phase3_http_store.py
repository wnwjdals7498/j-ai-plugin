import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import uuid

import pytest

from pmt.errors import PmtError
from pmt.http_store import HttpStore


DEVICE = str(uuid.uuid4())
ENVIRONMENT = str(uuid.uuid4())
NAMESPACE = str(uuid.uuid4())
SESSION = str(uuid.uuid4())
REQUEST = str(uuid.uuid4())


class Fixture:
    def __init__(self):
        self.requests = []
        self.status = 200
        self.body = None
        self.redirect = False
        self.notfound = False
        self.server = None

    def __enter__(self):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                size = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(size)
                fixture.requests.append((self.command, self.path, dict(self.headers), body))
                if fixture.redirect:
                    self.send_response(302)
                    self.send_header("Location", "/attacker")
                    self.end_headers()
                    return
                payload = fixture.body
                if payload is None and self.path == "/api/v1/operations":
                    decoded = json.loads(body)
                    envelope = {"protocol_version": 1, "request_id": decoded["request_id"], "ok": True,
                                "result": {"accepted": True}, "error": None, "warnings": []}
                    payload = {"api_version": 1, "envelope": envelope, "exit_code": 0}
                if payload is None:
                    payload = {"api_version": 1, "session_id": SESSION}
                self._reply(fixture.status, payload)

            def do_GET(self):
                fixture.requests.append((self.command, self.path, dict(self.headers), b""))
                payload = fixture.body
                if payload is None and self.path == "/api/v1/compatibility":
                    payload = {"api_version": 1, "core_version": "0.3.0", "db_schema": 4,
                               "graph_schema": 1, "protocol_versions": [1], "namespace_id": NAMESPACE,
                               "device_id": DEVICE, "actor": "fixture-user", "scopes": [], "permissions": []}
                elif payload is None:
                    if fixture.notfound:
                        payload = {"api_version": 1, "actor": "fixture-user", "session_id": SESSION,
                                   "envelope": None, "exit_code": None}
                    else:
                        envelope = {"protocol_version": 1, "request_id": REQUEST, "ok": True,
                                    "result": {"replayed": True}, "error": None, "warnings": []}
                        payload = {"api_version": 1, "actor": "fixture-user", "session_id": SESSION,
                                   "envelope": envelope, "exit_code": 0}
                self._reply(fixture.status, payload)

            def _reply(self, status, payload):
                raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.store = HttpStore(f"http://127.0.0.1:{self.server.server_port}", "PMT_FIXTURE_TOKEN",
                               DEVICE, ENVIRONMENT, NAMESPACE, timeout=2,
                               allow_loopback_http=True)
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def request():
    return {"protocol_version": 1, "operation": "read_context", "request_id": REQUEST,
            "actor": "fixture-user", "session_id": SESSION, "payload": {}}


def test_execute_sends_fixed_headers_and_preserves_port_shape(monkeypatch):
    monkeypatch.setenv("PMT_FIXTURE_TOKEN", "fixture-secret-do-not-log")
    observations = []
    with Fixture() as fixture:
        fixture.store.observer = observations.append
        envelope, exit_code = fixture.store.execute(request())
    assert exit_code == 0
    assert envelope["result"] == {"accepted": True}
    method, path, headers, raw = fixture.requests[0]
    assert (method, path) == ("POST", "/api/v1/operations")
    assert headers["Authorization"] == "Bearer fixture-secret-do-not-log"
    normalized_headers = {key.casefold(): value for key, value in headers.items()}
    assert normalized_headers["x-pmt-session"] == SESSION
    assert normalized_headers["x-pmt-device"] == DEVICE
    assert normalized_headers["x-pmt-environment"] == ENVIRONMENT
    assert normalized_headers["x-pmt-namespace"] == NAMESPACE
    assert json.loads(raw)["request_id"] == REQUEST
    assert observations[0]["request_id"] == REQUEST
    assert "secret" not in repr(observations[0])


@pytest.mark.parametrize("endpoint", [
    "http://example.test", "https://user@host.test", "https://host.test/?x=1",
    "https://host.test/#fragment", "https://host.test/path\nheader", "https://host.test/a/../b",
    "https://host.test/a/%2e%2e/b", "https://host.test\\@evil.test",
])
def test_rejects_unsafe_endpoint(endpoint):
    with pytest.raises(ValueError):
        HttpStore(endpoint, "PMT_TOKEN", DEVICE, ENVIRONMENT, NAMESPACE,
                  allow_loopback_http=True)


def test_http_loopback_requires_literal_ip_and_explicit_flag():
    with pytest.raises(ValueError):
        HttpStore("http://localhost", "PMT_TOKEN", DEVICE, ENVIRONMENT, NAMESPACE,
                  allow_loopback_http=True)
    with pytest.raises(ValueError):
        HttpStore("http://127.0.0.1", "PMT_TOKEN", DEVICE, ENVIRONMENT, NAMESPACE)


def test_redirect_is_not_followed(monkeypatch):
    monkeypatch.setenv("PMT_FIXTURE_TOKEN", "test-only")
    with Fixture() as fixture:
        fixture.redirect = True
        with pytest.raises(PmtError) as caught:
            fixture.store.execute(request())
    assert caught.value.code == "remote_http_error"
    assert len(fixture.requests) == 1


@pytest.mark.parametrize("body", [b"\xff", b"{broken", b"[]"])
def test_invalid_remote_operation_reply_is_stable_error(monkeypatch, body):
    monkeypatch.setenv("PMT_FIXTURE_TOKEN", "test-only")
    with Fixture() as fixture:
        fixture.body = body
        with pytest.raises(PmtError) as caught:
            fixture.store.execute(request())
    assert caught.value.code in {"remote_response_invalid", "remote_response_too_large"}


@pytest.mark.parametrize("status", [400, 401, 403, 409, 503])
def test_valid_operation_error_body_preserves_core_envelope(monkeypatch, status):
    monkeypatch.setenv("PMT_FIXTURE_TOKEN", "test-only")
    envelope = {"protocol_version": 1, "request_id": REQUEST, "ok": False, "result": None,
                "error": {"code": "revision_conflict", "message": "Conflict", "retryable": False},
                "warnings": []}
    with Fixture() as fixture:
        fixture.status = status
        fixture.body = {"api_version": 1, "envelope": envelope, "exit_code": 3}
        actual, code = fixture.store.execute(request())
    assert actual == envelope
    assert code == 3


def test_get_result_checks_actor_and_session(monkeypatch):
    monkeypatch.setenv("PMT_FIXTURE_TOKEN", "test-only")
    with Fixture() as fixture:
        replay, code = fixture.store.get_request_result(REQUEST, "fixture-user", SESSION)
        assert replay["result"] == {"replayed": True}
        assert code == 0
        with pytest.raises(PmtError, match="invalid or belongs"):
            fixture.store.get_request_result(REQUEST, "another-user", SESSION)
        fixture.notfound = True
        assert fixture.store.get_request_result(REQUEST, "fixture-user", SESSION) is None


def test_get_result_sends_expected_request_fingerprint(monkeypatch):
    monkeypatch.setenv("PMT_FIXTURE_TOKEN", "test-only")
    expected = request()
    with Fixture() as fixture:
        fixture.store.get_request_result(REQUEST, "fixture-user", SESSION, expected_request=expected)
        headers = {key.casefold(): value for key, value in fixture.requests[0][2].items()}
        from pmt.db import semantic_request_fingerprint
        from pmt.service import normalize_request
        assert headers["x-pmt-request-fingerprint"] == semantic_request_fingerprint(normalize_request(expected))
        assert len(headers["x-pmt-request-fingerprint"]) == 64
        changed = {**expected, "payload": {"run_id": str(uuid.uuid4())}}
        fixture.store.get_request_result(REQUEST, "fixture-user", SESSION, expected_request=changed)
        changed_headers = {key.casefold(): value for key, value in fixture.requests[1][2].items()}
        assert changed_headers["x-pmt-request-fingerprint"] == semantic_request_fingerprint(normalize_request(changed))
        assert changed_headers["x-pmt-request-fingerprint"] != headers["x-pmt-request-fingerprint"]


def test_protocol_local_session_strings_and_stable_invalid_identity(monkeypatch):
    monkeypatch.setenv("PMT_FIXTURE_TOKEN", "test-only")
    with Fixture() as fixture:
        req = {**request(), "session_id": "sess-a"}
        envelope, code = fixture.store.execute(req)
        assert envelope["ok"] and code == 0
        sent = {key.casefold(): value for key, value in fixture.requests[0][2].items()}
        assert sent["x-pmt-session"] == "sess-a"
        with pytest.raises(PmtError) as caught:
            fixture.store.execute({**req, "session_id": "bad\nvalue"})
        assert caught.value.code == "invalid_identity"


def test_compatibility_register_session_and_http_status_errors(monkeypatch):
    monkeypatch.setenv("PMT_FIXTURE_TOKEN", "test-only")
    with Fixture() as fixture:
        compatibility = fixture.store.check_compatibility()
        assert compatibility["compatible"] is True
        assert compatibility["storage"] == "http"
        registered = fixture.store.register_session(SESSION)
        assert registered["session_id"] == SESSION
        assert json.loads(fixture.requests[1][3]) == {"session_id": SESSION, "environment_id": ENVIRONMENT}
        fixture.status = 403
        fixture.body = {"error": {"code": "forbidden", "message": "Denied", "retryable": False}}
        with pytest.raises(PmtError) as caught:
            fixture.store.check_compatibility()
        assert caught.value.code == "forbidden"


def test_compatibility_rejects_identity_mismatch(monkeypatch):
    monkeypatch.setenv("PMT_FIXTURE_TOKEN", "test-only")
    with Fixture() as fixture:
        fixture.body = {"api_version": 1, "core_version": "0.3.0", "db_schema": 4,
                        "graph_schema": 1, "protocol_versions": [1], "namespace_id": NAMESPACE,
                        "device_id": str(uuid.uuid4()), "actor": "fixture-user", "scopes": [],
                        "permissions": []}
        with pytest.raises(PmtError) as caught:
            fixture.store.check_compatibility()
    assert caught.value.code == "remote_compatibility_mismatch"


@pytest.mark.parametrize("field,value", [
    ("core_version", "0.4.0"), ("db_schema", 3), ("graph_schema", 2), ("protocol_versions", [2]),
])
def test_compatibility_reports_version_mismatch_without_transport_error(monkeypatch, field, value):
    monkeypatch.setenv("PMT_FIXTURE_TOKEN", "test-only")
    body = {"api_version": 1, "core_version": "0.3.0", "db_schema": 4,
            "graph_schema": 1, "protocol_versions": [1], "namespace_id": NAMESPACE,
            "device_id": DEVICE, "actor": "fixture-user", "scopes": [], "permissions": []}
    body[field] = value
    with Fixture() as fixture:
        fixture.body = body
        compatibility = fixture.store.check_compatibility()
    assert compatibility["compatible"] is False
    assert compatibility["incompatibilities"]


def test_credentials_with_header_controls_are_rejected_and_not_observed(monkeypatch):
    monkeypatch.setenv("PMT_FIXTURE_TOKEN", "good\r\nX-Leak: yes")
    with Fixture() as fixture:
        with pytest.raises(PmtError) as caught:
            fixture.store.execute(request())
    assert caught.value.code == "credential_unavailable"
    assert fixture.requests == []


def test_observer_failure_is_visible_without_changing_success(monkeypatch):
    monkeypatch.setenv("PMT_FIXTURE_TOKEN", "test-only")
    with Fixture() as fixture:
        fixture.store.observer = lambda _: (_ for _ in ()).throw(RuntimeError("sensitive detail"))
        with pytest.warns(RuntimeWarning, match="observer failed"):
            envelope, code = fixture.store.execute(request())
    assert envelope["ok"] is True and code == 0


def test_oversized_response_is_rejected(monkeypatch):
    monkeypatch.setenv("PMT_FIXTURE_TOKEN", "test-only")
    with Fixture() as fixture:
        fixture.body = b"{" + b" " * (1024 * 1024) + b"}"
        with pytest.raises(PmtError) as caught:
            fixture.store.execute(request())
    assert caught.value.code == "remote_response_too_large"


def test_request_size_and_missing_credential(monkeypatch):
    monkeypatch.delenv("PMT_FIXTURE_TOKEN", raising=False)
    with Fixture() as fixture:
        with pytest.raises(PmtError) as caught:
            fixture.store.execute(request())
        assert caught.value.code == "credential_unavailable"
        monkeypatch.setenv("PMT_FIXTURE_TOKEN", "test-only")
        with pytest.raises(PmtError) as caught:
            fixture.store.execute({**request(), "payload": {"data": "x" * (1024 * 1024)}})
        assert caught.value.code == "request_too_large"
