"""Structured, redacted diagnostics with observable failure warnings."""
import json
import logging
import sys
import re
from .util import utc_now

FIELDS = ("request_id", "operation", "session_id", "source", "scope_id", "record_id", "event_id",
          "outcome", "error_code", "retryable", "duration_ms", "exit_code", "old_revision",
          "new_revision", "transaction_outcome", "ownership_result", "correlation_id",
          "work_id", "item_id", "step_id", "job_id", "run_id", "resource_id", "model", "route",
          "directive_version", "last_seen", "last_changed", "wait_reason",
          "source_hash", "graph_hash", "graph_revision", "rule_version", "template_version",
          "context_id", "batch_id", "measurement_id", "manifest_hash", "count",
          "input_bytes", "output_bytes", "incomplete", "reason_code",
          "device_id", "environment_id", "namespace_id", "api_version", "host_schema_version")
SENSITIVE_KEYS = {"token", "claim_token", "authorization", "secret", "transcript", "payload", "body"}

def _safe(value, depth=0):
    if depth > 5:
        return "<truncated>"
    if isinstance(value, dict):
        return {str(k): ("<redacted>" if str(k).lower() in SENSITIVE_KEYS else _safe(v, depth + 1))
                for k, v in value.items() if str(k).lower() not in {"path", "data_root", "config_root"}}
    if isinstance(value, (list, tuple)):
        return [_safe(v, depth + 1) for v in value[:50]]
    if isinstance(value, str):
        value = re.sub(r"(?i)bearer\s+[^\s,;]+", "Bearer <redacted>", value)
        value = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "<redacted>", value)
        return value[:500]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(type(value).__name__)

class JsonFormatter(logging.Formatter):
    def format(self, record):
        fields = {key: _safe(getattr(record, key, None)) for key in FIELDS}
        fields.update(at_utc=utc_now(), level=record.levelname, component="pmt", event_name=record.getMessage())
        return json.dumps(fields, ensure_ascii=False, separators=(",", ":"))

class ObservableStreamHandler(logging.StreamHandler):
    """Let the caller detect I/O failures that logging normally swallows."""
    def handleError(self, record):
        error = sys.exc_info()[1]
        if error is not None:
            raise error
        raise OSError("Diagnostic stream unavailable")

class DiagnosticLogger:
    def __init__(self, logger=None):
        self.logger = logger or logging.Logger("pmt")
        self.sink_unavailable = False
        if not self.logger.handlers:
            handler = ObservableStreamHandler(sys.stderr)
            handler.setFormatter(JsonFormatter())
            self.logger.addHandler(handler)
            self.logger.setLevel(logging.INFO)
            self.logger.propagate = False

    def emit(self, event_name, level=logging.INFO, **fields):
        extra = {key: fields.get(key) for key in FIELDS}
        try:
            self.logger.log(level, event_name, extra=extra)
            return not self.sink_unavailable
        except Exception:
            try:
                sys.stderr.write(json.dumps({"at_utc": utc_now(), "level": "WARNING", "component": "pmt",
                                            "event_name": "diagnostic_log_unavailable"}) + "\n")
            except Exception:
                pass
            return False
