"""Optional FastAPI HTTP boundary, with bounded inputs and secret-free errors."""
import uuid
from contextlib import closing

from ..errors import PmtError
from ..service import response
from ..util import canonical_json, strict_json_loads

API_VERSION = 1
MAX_JSON_BYTES = 1024 * 1024


def http_status(error):
    code = error.get("code", "")
    if code in {"unauthenticated", "credential_unavailable"}:
        return 401
    if code in {"scope_forbidden", "host_operation_forbidden"}:
        return 403
    if error.get("retryable") or code in {"host_key_unavailable", "runtime_io_error", "host_internal_error"}:
        return 503
    if any(word in code for word in ("conflict", "stale", "owner", "changed", "unavailable", "active")):
        return 409
    return 400


def create_app(application):
    try:
        from fastapi import FastAPI, Request
        from fastapi.responses import JSONResponse, Response as BinaryResponse
        from pydantic import BaseModel, ConfigDict, ValidationError
        from starlette.concurrency import run_in_threadpool
    except ImportError as exc:
        raise PmtError("host_dependency_missing", "Install proj-mgmt-tool[host] to run the Host", 5) from exc

    class StoreRequest(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        protocol_version: int
        operation: str
        request_id: str
        actor: str
        session_id: str
        payload: dict = {}
        scope_id: str | None = None
        record_id: str | None = None
        expected_revision: int | None = None
        context_refs: list = []
        source: dict = {}
        normalized_event: dict | None = None
        correlation_id: str | None = None
        causation_id: str | None = None

    app = FastAPI(title="PMT storage Host", version="0.3.0", docs_url=None, redoc_url=None)
    app.state.pmt = application
    app.state.acceptance_tier = "not_run"
    from .resources import HostResourceStore
    resource_port = getattr(application, "resources", None) or HostResourceStore(application.db, application.auth)
    application.resources = resource_port
    from .transfer import TransferService
    transfer_port = TransferService(application, application.db.root / "host-transfer-store")

    async def bounded_body(request):
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_JSON_BYTES:
                raise PmtError("request_too_large", "JSON request exceeds 1 MiB")
            chunks.append(chunk)
        return strict_json_loads(b"".join(chunks), max_bytes=MAX_JSON_BYTES)

    def pack(envelope, code):
        value = {"api_version": API_VERSION, "envelope": envelope, "exit_code": code}
        if len(canonical_json(value).encode()) > MAX_JSON_BYTES:
            error = {"code": "remote_response_too_large", "message": "Use a smaller query or resource references",
                     "retryable": False, "details": {"effect": "unknown"}}
            value = {"api_version": API_VERSION, "envelope": response(envelope.get("request_id"), error=error), "exit_code": 2}
        status = 200 if value["envelope"]["ok"] else http_status(value["envelope"]["error"])
        return JSONResponse(value, status_code=status, headers={"X-PMT-Host-Schema": "1"})

    def safe_id(value):
        candidate = value.get("request_id") if isinstance(value, dict) else None
        try:
            return candidate if str(uuid.UUID(candidate)) == candidate else None
        except (ValueError, TypeError, AttributeError):
            return None

    def diagnostic(request_id, operation, error):
        application.db.diagnostics.emit("host.request.rejected", request_id=request_id,
            operation=operation, namespace_id=application.auth.namespace_id,
            api_version=API_VERSION, error_code=error.code, retryable=error.retryable, outcome="error")

    @app.exception_handler(PmtError)
    async def pmt_error(request: Request, error: PmtError):
        diagnostic(None, "host_boundary", error)
        return JSONResponse({"api_version": API_VERSION, "error": error.as_dict()}, status_code=http_status(error.as_dict()))

    @app.exception_handler(Exception)
    async def internal_error(request: Request, caught: Exception):
        application.db.diagnostics.emit("host.internal_error", namespace_id=application.auth.namespace_id,
            outcome="error", reason_code=type(caught).__name__)
        error = PmtError("host_internal_error", "Host storage failed safely", 5)
        return JSONResponse({"api_version": API_VERSION, "error": error.as_dict()}, status_code=503)

    @app.get("/health")
    def health():
        return {"status": "ok", "api_version": API_VERSION}

    @app.get("/api/v1/compatibility")
    async def compatibility(request: Request):
        return await run_in_threadpool(application.compatibility, dict(request.headers))

    @app.post("/api/v1/sessions")
    async def sessions(request: Request):
        return await run_in_threadpool(application.register_session, dict(request.headers), await bounded_body(request))

    @app.get("/api/v1/requests/{request_id}")
    async def request_result(request_id: str, request: Request):
        expected = request.headers.get("x-pmt-request-fingerprint")
        if expected is not None and (len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected)):
            raise PmtError("host_input_invalid", "Expected request fingerprint must be SHA-256")
        return await run_in_threadpool(application.get_request_result, request_id, dict(request.headers), expected)

    @app.post("/api/v1/resources")
    async def upload_resource(request: Request):
        headers = dict(request.headers)
        with closing(application.db.connect()) as conn:
            application.principal(conn, headers)
        metadata = request.headers.get("x-pmt-resource-metadata")
        if not isinstance(metadata, str) or len(metadata) > 4096:
            raise PmtError("host_input_invalid", "A bounded resource metadata header is required")
        value = strict_json_loads(metadata.encode("utf-8"), max_bytes=4096)
        if not isinstance(value, dict) or "content" in value:
            raise PmtError("host_input_invalid", "Resource metadata must be an object without content")
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > resource_port.max_bytes:
                raise PmtError("resource_too_large", "Resource exceeds the Host upload limit")
            chunks.append(chunk)
        receipt = await run_in_threadpool(resource_port.publish, {**value, "content": b"".join(chunks)}, headers)
        return {"api_version": API_VERSION, **receipt}

    @app.get("/api/v1/resources/{resource_id}")
    async def download_resource(resource_id: str, request: Request):
        if set(request.query_params) - {"run_id"}:
            raise PmtError("host_input_invalid", "Unsupported resource query fields")
        value = {"resource_id": resource_id}
        if request.query_params.get("run_id"):
            value["run_id"] = request.query_params["run_id"]
        headers = dict(request.headers)
        with closing(application.db.connect()) as conn:
            application.principal(conn, headers)
        resource = await run_in_threadpool(resource_port.read, value, headers)
        return BinaryResponse(resource["content"], media_type="application/octet-stream", headers={
            "X-PMT-SHA256": resource["sha256"], "X-PMT-Scope": resource["scope_id"],
            "X-PMT-Purpose": resource["purpose"], "X-PMT-Resource": resource["resource_id"]})

    @app.post("/api/v1/transfers/import")
    async def import_transfer(request: Request):
        headers = dict(request.headers)
        with closing(application.db.connect()) as conn:
            principal = application.principal(conn, headers)
            principal.require("admin")
            principal.require("write")
        raw_meta = request.headers.get("x-pmt-transfer-metadata")
        if not isinstance(raw_meta, str) or len(raw_meta) > 4096:
            raise PmtError("host_input_invalid", "Bounded transfer metadata is required")
        metadata = strict_json_loads(raw_meta.encode(), max_bytes=4096)
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 64 * 1024 * 1024:
                raise PmtError("transfer_too_large", "Transfer exceeds 64 MiB")
            chunks.append(chunk)
        receipt = await run_in_threadpool(transfer_port.import_bytes, metadata, b"".join(chunks), headers)
        return {"api_version": API_VERSION, "receipt": receipt}

    @app.post("/api/v1/transfers/backup")
    async def backup_transfer(request: Request):
        result = await run_in_threadpool(transfer_port.create_backup, await bounded_body(request), dict(request.headers))
        return {"api_version": API_VERSION, **result}

    @app.get("/api/v1/transfers/download/{download_ref}")
    async def download_transfer(download_ref: str, request: Request):
        if request.query_params:
            raise PmtError("host_input_invalid", "Transfer download does not accept query fields")
        result = await run_in_threadpool(transfer_port.download_bytes, download_ref, dict(request.headers))
        return BinaryResponse(result["content"], media_type="application/vnd.pmt.migration+zip", headers={
            "X-PMT-Manifest-SHA256": result["manifest_sha256"], "X-PMT-Download-Ref": result["download_ref"],
            "X-PMT-Bundle-SHA256": result["bundle_sha256"]})

    @app.post("/api/v1/operations", openapi_extra={"requestBody": {"required": True, "content": {
        "application/json": {"schema": StoreRequest.model_json_schema()}}}})
    async def operations(request: Request):
        value = None
        try:
            value = await bounded_body(request)
            StoreRequest.model_validate(value)
            envelope, code = await run_in_threadpool(application.execute, value, dict(request.headers))
            return pack(envelope, code)
        except ValidationError:
            error = PmtError("host_input_invalid", "Request fields or types are invalid")
        except PmtError as caught:
            error = caught
        except Exception as caught:
            application.db.diagnostics.emit("host.internal_error", request_id=safe_id(value),
                namespace_id=application.auth.namespace_id, outcome="error", reason_code=type(caught).__name__)
            error = PmtError("host_internal_error", "Host storage failed safely", 5)
        operation = value.get("operation") if isinstance(value, dict) else "host_boundary"
        diagnostic(safe_id(value), operation, error)
        return pack(response(safe_id(value), error=error.as_dict()), error.exit_code)

    return app
