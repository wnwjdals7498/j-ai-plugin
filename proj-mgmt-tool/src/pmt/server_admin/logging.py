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


def read_logs(log_dir, tail=200, since=None, *, now=None):
    """Read bounded, redacted log entries; relative time includes rotated days."""
    from collections import deque
    import stat
    from .secrets import _path_reparse

    if type(tail) is not int or not 1 <= tail <= 1000:
        raise ValueError("tail must be between 1 and 1000")
    cutoff = None
    if since is not None:
        match = re.fullmatch(r"([1-9][0-9]{0,5})([smhd])", since) if isinstance(since, str) else None
        if match is None:
            raise ValueError("since must be a positive duration, for example 1h")
        from datetime import timedelta
        seconds = int(match.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]
        if seconds > 365 * 86400:
            raise ValueError("since cannot exceed 365 days")
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(seconds=seconds)
    root = Path(log_dir)
    active = root / "host.log"
    paths = [*sorted(root.glob("host.log.????-??-??")), active] if cutoff is not None else [active]
    entries = deque()
    total = 0
    maximum_line = 16384
    try:
        for path in paths:
            if not path.exists() and path == active and len(paths) > 1:
                continue
            if _path_reparse(path) or not stat.S_ISREG(path.lstat().st_mode):
                raise OSError("unsafe log file")
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                while line := stream.readline(maximum_line + 1):
                    if len(line) > maximum_line:
                        while not line.endswith("\n"):
                            line = stream.readline(maximum_line + 1)
                            if not line:
                                break
                        continue
                    if cutoff is not None:
                        try:
                            stamp = datetime.fromisoformat(json.loads(line)["at_utc"].replace("Z", "+00:00"))
                            if stamp.tzinfo is None or stamp < cutoff:
                                continue
                        except (ValueError, KeyError, TypeError, AttributeError):
                            continue
                    text = redact(line.rstrip("\r\n"))
                    size = len(text.encode("utf-8"))
                    entries.append((text, size))
                    total += size
                    while len(entries) > tail or total > 1024 * 1024:
                        total -= entries.popleft()[1]
    except OSError as exc:
        raise OSError("configured log file is unavailable") from exc
    return [text for text, _size in entries]
