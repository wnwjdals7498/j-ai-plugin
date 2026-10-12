"""Host administration and optional ASGI server entry point."""
from __future__ import annotations

import argparse
import base64
import ipaddress
import os
import re
import secrets
import sys

from ..db import Database
from ..errors import PmtError
from ..util import canonical_json
from .auth import AuthRegistry, PERMISSIONS

_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def _key_reference(value):
    if not isinstance(value, str) or not _ENV.fullmatch(value):
        raise PmtError("host_key_unavailable", "Claim key must be referenced by an environment variable name", 5)
    text = os.environ.get(value)
    try:
        key = base64.b64decode(text, validate=True)
    except (ValueError, TypeError):
        raise PmtError("host_key_unavailable", "Configured claim key is unavailable or invalid", 5)
    if len(key) < 32:
        raise PmtError("host_key_unavailable", "Configured claim key is too short", 5)
    return key


def build_parser():
    parser = argparse.ArgumentParser(prog="pmt-host", description="PMT storage Host")
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--config-root", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    issue = commands.add_parser("issue-device", help="Issue a credential once through local administration")
    issue.add_argument("--actor", required=True)
    issue.add_argument("--scope", action="append", required=True)
    issue.add_argument("--permission", action="append", choices=sorted(PERMISSIONS), required=True)
    for command in ("rotate-device", "revoke-device"):
        action = commands.add_parser(command)
        action.add_argument("--device-id", required=True)
        action.add_argument("--expected-revision", type=int, required=True)
    grants = commands.add_parser("update-grants")
    grants.add_argument("--device-id", required=True)
    grants.add_argument("--expected-revision", type=int, required=True)
    grants.add_argument("--scope", action="append", required=True)
    grants.add_argument("--permission", action="append", choices=sorted(PERMISSIONS), required=True)
    commands.add_parser("generate-claim-key", help="Return a new base64 key once; store it outside PMT data")
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--claim-key-env", default="PMT_HOST_CLAIM_KEY")
    serve.add_argument("--claim-key-id", default="primary")
    serve.add_argument("--retained-key", action="append", default=[], metavar="KEY_ID=ENV_NAME")
    serve.add_argument("--ssl-certfile")
    serve.add_argument("--ssl-keyfile")
    serve.add_argument("--allow-loopback-http", action="store_true")
    serve.add_argument("--behind-proxy", action="store_true")
    serve.add_argument("--trusted-proxy", action="append", default=[])
    return parser


def _serve(db, args):
    return serve_host(db, args)


def serve_host(db, args, *, claim_keys=None, log_config=None):
    try:
        address = ipaddress.ip_address(args.host)
    except ValueError:
        raise PmtError("host_bind_invalid", "Host bind address must be a literal IP")
    if not 0 <= args.port <= 65535:
        raise PmtError("host_bind_invalid", "Host port is invalid")
    tls = bool(args.ssl_certfile and args.ssl_keyfile)
    if bool(args.ssl_certfile) != bool(args.ssl_keyfile):
        raise PmtError("host_tls_invalid", "TLS requires both certificate and key files")
    if not tls:
        if not address.is_loopback or not (args.allow_loopback_http or args.behind_proxy):
            raise PmtError("host_tls_required", "HTTPS or an explicit loopback test/proxy listener is required")
    trusted = []
    if args.behind_proxy:
        if not address.is_loopback or not args.trusted_proxy:
            raise PmtError("host_proxy_invalid", "Proxy mode requires a loopback listener and explicit trusted proxy IPs")
        for item in args.trusted_proxy:
            try:
                trusted.append(str(ipaddress.ip_address(item)))
            except ValueError:
                raise PmtError("host_proxy_invalid", "Trusted proxies must be literal IPs")
    elif args.trusted_proxy:
        raise PmtError("host_proxy_invalid", "Trusted proxy settings require proxy mode")
    keys = dict(claim_keys) if claim_keys is not None else {args.claim_key_id: _key_reference(args.claim_key_env)}
    for mapping in ([] if claim_keys is not None else args.retained_key):
        if "=" not in mapping:
            raise PmtError("host_key_unavailable", "Retained key mapping must be KEY_ID=ENV_NAME", 5)
        key_id, env_name = mapping.split("=", 1)
        if not key_id or key_id in keys:
            raise PmtError("host_key_unavailable", "Claim key IDs must be distinct", 5)
        keys[key_id] = _key_reference(env_name)
    try:
        import uvicorn
    except ImportError as exc:
        raise PmtError("host_dependency_missing", "Install proj-mgmt-tool[host]", 5) from exc
    from .application import HostApplication
    from .server import create_app
    application = HostApplication(db, keys, args.claim_key_id)
    # The data extension is installed by the completed source/runtime boundary.
    # This entry point must not select the unrestricted local dispatcher.
    try:
        from .data import HostDataExtension
    except ImportError as exc:
        raise PmtError("host_data_unavailable", "Host data boundary is unavailable", 5) from exc
    from .resources import HostResourceStore
    resource_port = HostResourceStore(db, application.auth)
    application.extension = HostDataExtension(db, application.auth, resource_port, authorizer=application.authorize)
    application.resources = resource_port
    logging_options = {} if log_config is None else {"log_config": log_config}
    uvicorn.run(create_app(application), host=args.host, port=args.port, workers=1,
                ssl_certfile=args.ssl_certfile, ssl_keyfile=args.ssl_keyfile,
                proxy_headers=args.behind_proxy, forwarded_allow_ips=trusted,
                access_log=False, log_level="info", timeout_graceful_shutdown=15, **logging_options)
    return 0


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command == "generate-claim-key":
            # One-time administration output is not persisted as a PMT event/log.
            print(canonical_json({"key": base64.b64encode(secrets.token_bytes(48)).decode(), "encoding": "base64"}))
            return 0
        db = Database(args.data_root, args.config_root)
        if args.command == "serve":
            return _serve(db, args)
        auth = AuthRegistry(db)
        if args.command == "issue-device":
            result = auth.issue_device(args.actor, args.scope, args.permission)
        elif args.command == "rotate-device":
            result = auth.rotate_device(args.device_id, args.expected_revision)
        elif args.command == "revoke-device":
            result = auth.revoke_device(args.device_id, args.expected_revision)
        else:
            result = auth.update_grants(args.device_id, args.expected_revision, args.scope, args.permission)
        print(canonical_json(result))
        return 0
    except PmtError as error:
        print(canonical_json({"ok": False, "error": error.as_dict()}))
        return error.exit_code
    except Exception as error:
        sys.stderr.write(canonical_json({"component": "pmt-host", "event_name": "internal_error",
                                         "reason_code": type(error).__name__}) + "\n")
        print(canonical_json({"ok": False, "error": {"code": "host_internal_error", "message": "Host failed safely", "retryable": False}}))
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
