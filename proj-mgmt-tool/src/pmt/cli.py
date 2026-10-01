"""One UTF-8 JSON request and one JSON response; diagnostics go to stderr."""
from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .db import Database
from .errors import PmtError
from .service import execute, normalize_request, response
from .util import canonical_json, strict_json_loads


def main(argv=None):
    parser = argparse.ArgumentParser(prog="pmt", description="Local SQLite project memory")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--data-root", default=os.environ.get("PMT_DATA_ROOT"))
    parser.add_argument("--config-root", default=os.environ.get("PMT_CONFIG_ROOT"))
    parser.add_argument("--busy-timeout-ms", type=int, default=500)
    args = parser.parse_args(argv)
    request_id = None
    exit_code = 5
    try:
        raw = sys.stdin.buffer.read(1024 * 1024 + 1)
        request = strict_json_loads(raw)
        if isinstance(request, dict):
            request_id = request.get("request_id")
        request = normalize_request(request)
        db = Database(args.data_root, args.config_root, busy_timeout_ms=args.busy_timeout_ms)
        result, exit_code = execute(db, request)
    except PmtError as error:
        result, exit_code = response(request_id, error=error.as_dict()), error.exit_code
    except (OSError, ValueError, UnicodeError):
        result, exit_code = response(request_id, error={"code": "runtime_io_error", "message": "Runtime I/O failed",
                                                       "retryable": True}), 4
    except Exception as error:
        sys.stderr.write(canonical_json({"component": "pmt", "event_name": "internal_error",
                                        "error_type": type(error).__name__}) + "\n")
        result = response(request_id, error={"code": "internal_error", "message": "PMT internal error",
                                             "retryable": False})
    output = (canonical_json(result) + "\n").encode("utf-8")
    sys.stdout.buffer.write(output)
    sys.stdout.buffer.flush()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
