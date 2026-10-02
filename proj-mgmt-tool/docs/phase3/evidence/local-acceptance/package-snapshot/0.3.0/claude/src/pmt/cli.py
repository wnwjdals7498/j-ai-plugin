"""One UTF-8 JSON request and one JSON response; diagnostics go to stderr."""
from __future__ import annotations

import argparse
import os
import sys
import uuid

from . import __version__
from .db import Database
from .errors import PmtError
from .paths import resolve_roots
from .service import execute, normalize_request, response
from .util import canonical_json, new_id, strict_json_loads


def main(argv=None):
    parser = argparse.ArgumentParser(prog="pmt", description="Local SQLite project memory")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--data-root", default=os.environ.get("PMT_DATA_ROOT"))
    parser.add_argument("--config-root", default=os.environ.get("PMT_CONFIG_ROOT"))
    parser.add_argument("--busy-timeout-ms", type=int, default=500)
    commands = parser.add_subparsers(dest="command")
    storage = commands.add_parser("storage", help="Configure or inspect the selected storage profile")
    storage_commands = storage.add_subparsers(dest="storage_action", required=True)
    for action in ("configure", "probe", "status"):
        subcommand = storage_commands.add_parser(action)
        subcommand.set_defaults(command="storage", storage_action=action)
    pending = commands.add_parser("pending", help="Preserve or reconcile already-generated hosted results")
    pending_commands = pending.add_subparsers(dest="pending_action", required=True)
    for action in ("status", "capture", "reconcile"):
        subcommand = pending_commands.add_parser(action)
        subcommand.set_defaults(command="pending", pending_action=action)
    args = parser.parse_args(argv)
    request_id = None
    exit_code = 5
    try:
        data_root, config_root = resolve_roots(args.data_root, args.config_root)
        if args.command == "pending":
            from .storage_config import execute_pending_command
            raw = sys.stdin.buffer.read(1024 * 1024 + 1)
            body = strict_json_loads(raw)
            if not isinstance(body, dict):
                raise PmtError("pending_input_invalid", "Pending command input must be one JSON object", 2)
            request_id = body.setdefault("request_id", new_id())
            result = execute_pending_command(data_root, config_root, args.pending_action, body)
            if isinstance(result, tuple):
                output, code = result
            else:
                output, code = response(request_id, result=result), 0
            sys.stdout.buffer.write((canonical_json(output) + "\n").encode("utf-8"))
            sys.stdout.buffer.flush()
            return code
        if args.command == "storage":
            from . import storage_config
            raw = sys.stdin.buffer.read(storage_config.MAX_CONFIG_BYTES + 1)
            if len(raw) > storage_config.MAX_CONFIG_BYTES:
                raise PmtError("storage_config_too_large", "Storage command input exceeds 64 KiB", 2)
            body = strict_json_loads(raw, max_bytes=storage_config.MAX_CONFIG_BYTES) if raw.strip() else {}
            if not isinstance(body, dict):
                raise PmtError("storage_config_invalid", "Storage command input must be one JSON object", 2)
            request_id = body.get("request_id") or new_id()
            try:
                if not isinstance(request_id, str) or str(uuid.UUID(request_id)) != request_id:
                    raise ValueError
            except (ValueError, TypeError, AttributeError) as exc:
                raise PmtError("invalid_request_id", "Storage request_id must be a canonical UUID", 2) from exc
            if args.storage_action == "configure":
                body = {key: value for key, value in body.items() if key != "request_id"}
                result = storage_config.configure_storage(config_root, body)
            elif args.storage_action == "probe":
                if set(body) - {"request_id"}:
                    raise PmtError("storage_config_invalid", "Storage probe accepts only request_id", 2)
                result = storage_config.probe_storage(config_root)
            else:
                if set(body) - {"request_id"}:
                    raise PmtError("storage_config_invalid", "Storage status accepts only request_id", 2)
                result = storage_config.storage_status(config_root)
            output = response(request_id, result=result)
            sys.stdout.buffer.write((canonical_json(output) + "\n").encode("utf-8"))
            sys.stdout.buffer.flush()
            return 0
        raw = sys.stdin.buffer.read(1024 * 1024 + 1)
        request = strict_json_loads(raw)
        if isinstance(request, dict):
            request_id = request.get("request_id")
        request = normalize_request(request)
        from .storage_config import select_store
        from .store import LocalStore
        store = select_store(data_root, config_root, request, environ=os.environ,
            local_store_factory=lambda data, config: LocalStore(
                Database(data, config, busy_timeout_ms=args.busy_timeout_ms)))
        result, exit_code = store.execute(request)
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
