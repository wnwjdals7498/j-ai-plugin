"""Redacted rotating JSON logging and bounded log display."""
from __future__ import annotations

import json
import logging
import logging.handlers
import re
from datetime import datetime, timezone
from pathlib import Path

_SECRET = re.compile(r'''(?i)\b(authorization|proxy-authorization|cookie|set-cookie|credential|claim[_ -]?key|private[_ -]?key|secret|token)\b(["']?\s*[=:]\s*["']?)(?:"[^"]*"|'[^']*'|[^\s,;}]+)''')
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s,;\"}]+")


def redact(value):
    text = _BEARER.sub("Bearer [REDACTED]", str(value))
    return _SECRET.sub(lambda match: match.group(1) + match.group(2) + "[REDACTED]", text)


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {"at_utc": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                   "level": record.levelname.lower(), "logger": record.name,
                   "message": redact(record.getMessage())}
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def server_log_config(log_dir, retain_days=30, level="info"):
    root = Path(log_dir)
    if not root.is_dir():
        raise OSError("configured log directory is unavailable")
    return {"version": 1, "disable_existing_loggers": False,
            "formatters": {"pmt_json": {"()": "pmt.server_admin.logging.JsonFormatter"}},
            "handlers": {"pmt_file": {"class": "logging.handlers.TimedRotatingFileHandler",
                                        "filename": str(root / "host.log"), "when": "midnight",
                                        "backupCount": retain_days, "encoding": "utf-8",
                                        "delay": True, "formatter": "pmt_json"}},
            "loggers": {"": {"handlers": ["pmt_file"], "level": level.upper()},
                        "uvicorn": {"handlers": ["pmt_file"], "level": level.upper(), "propagate": False},
                        "uvicorn.error": {"handlers": ["pmt_file"], "level": level.upper(), "propagate": False},
                        "uvicorn.access": {"handlers": [], "level": "CRITICAL", "propagate": False}}}


def read_logs(log_dir, tail=200):
    if type(tail) is not int or not 1 <= tail <= 1000:
        raise ValueError("tail must be between 1 and 1000")
    path = Path(log_dir) / "host.log"
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            lines = stream.readlines()[-tail:]
    except OSError as exc:
        raise OSError("configured log file is unavailable") from exc
    return [redact(line.rstrip("\r\n")) for line in lines]
