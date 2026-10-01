"""Canonical, deterministic data helpers."""
import hashlib
import json
import math
import uuid
from datetime import datetime, timezone

from .errors import PmtError

def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

def new_id():
    return str(uuid.uuid4())

def canonical_json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise PmtError("invalid_json_value", "Value cannot be represented as canonical JSON") from exc

def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def fingerprint(value):
    return sha256_text(canonical_json(value))

def strict_json_loads(text, max_bytes=1024 * 1024):
    if not isinstance(text, (str, bytes)):
        raise PmtError("invalid_json", "JSON input must be text")
    raw = text.encode("utf-8") if isinstance(text, str) else text
    if len(raw) > max_bytes:
        raise PmtError("request_too_large", "JSON input exceeds the size limit")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result
    def constant(_):
        raise ValueError("non-finite number")
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PmtError("invalid_json", "Input is not valid strict JSON") from exc
    def finite(v):
        if isinstance(v, float) and not math.isfinite(v):
            return False
        if isinstance(v, dict):
            return all(finite(k) and finite(x) for k, x in v.items())
        if isinstance(v, list):
            return all(finite(x) for x in v)
        return True
    if not finite(value):
        raise PmtError("invalid_json", "Non-finite numbers are not supported")
    return value
