"""Execution identities cross the wire; client process configuration does not."""
from ..errors import PmtError

ROUTE_FIELDS = frozenset({"agent", "provider", "model", "mode", "adapter_kind",
    "authorization_state", "max_concurrency", "selection_reason", "selection_reason_code",
    "capability_ref", "actual_support", "waiting", "blocked", "price_status", "auth_state"})
_PRIVATE = frozenset({"command", "argv", "env", "environment", "environment_variables",
    "pid", "supervisor_pid", "process_id", "spool", "spool_root", "path", "cwd",
    "working_directory", "credential", "credentials", "token", "api_key", "secret"})


def _private_fields(value):
    if isinstance(value, dict):
        return any(str(key).lower() in _PRIVATE or _private_fields(item)
                   for key, item in value.items())
    if isinstance(value, list):
        return any(_private_fields(item) for item in value)
    return False


def validate_execution_metadata(request):
    if not isinstance(request, dict):
        return
    payload = request.get("payload", {})
    if not isinstance(payload, dict):
        return
    routes = [payload.get("route")]
    result = payload.get("result")
    if isinstance(result, dict):
        routes.append(result.get("actual_route"))
    for route in routes:
        if route is None:
            continue
        if not isinstance(route, dict) or set(route) - ROUTE_FIELDS or _private_fields(route):
            raise PmtError("host_execution_metadata_invalid",
                "Only execution identity and verified selection metadata may be sent to Host", 2)
        for key, value in route.items():
            if key in {"waiting", "blocked"}:
                valid = type(value) is bool
            elif key == "max_concurrency":
                valid = value is None or type(value) is int and 1 <= value <= 100000
            else:
                valid = value is None or isinstance(value, str) and len(value) <= 2048
            if not valid:
                raise PmtError("host_execution_metadata_invalid",
                    "Execution selection metadata must contain bounded scalar values", 2)
    handle = payload.get("handle")
    if handle is not None and _private_fields(handle):
        raise PmtError("host_execution_metadata_invalid",
            "Host handles must be opaque references without client process configuration", 2)
