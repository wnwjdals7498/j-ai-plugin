"""로컬 대시보드 서버(설계 6.5). HubStore만 읽고, 접속 토큰은 터미널에 한 번만 출력한다."""

from __future__ import annotations

import hmac
import ipaddress
import json
import sys
import threading
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional, TextIO

from ledger.hubstore import HubStore
from ledger.hubsync import json_logger
from ledger.hubview import health, repo_summary, summary

_COOKIE_NAME = "fl_dash"
_JSON_TYPE = "application/json; charset=utf-8"
_STATIC_ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/style.css": ("style.css", "text/css; charset=utf-8"),
}
_REPO_PREFIX = "/api/v1/repos/"
_SECURITY_HEADERS = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("Content-Security-Policy", "default-src 'self'"),
)
_OPEN_ADDRESS_WARNING = "[!] 사내망에 열린 주소입니다. 접속 토큰을 공유하지 마세요"


@dataclass(frozen=True)
class DashboardApp:
    store: HubStore
    token: str
    interval_s: int
    static_dir: Path
    now: Callable[[], datetime]


@dataclass(frozen=True)
class _Reply:
    status: HTTPStatus
    content_type: str = _JSON_TYPE
    body: bytes = b""
    headers: tuple[tuple[str, str], ...] = ()


def _json_reply(data: Any, status: HTTPStatus = HTTPStatus.OK) -> _Reply:
    return _Reply(status, body=json.dumps(data, ensure_ascii=False).encode("utf-8"))


def _error_reply(status: HTTPStatus) -> _Reply:
    return _json_reply({"error": status.phrase}, status)


def _log(level: str, fields: dict[str, Any]) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    json_logger(sys.stderr)({"ts": stamp, "level": level, "component": "dashboard", **fields})


class _Handler(BaseHTTPRequestHandler):
    default_request_version = "HTTP/1.0"  # 형식이 틀린 요청에도 상태 줄과 보안 헤더를 보낸다
    path = ""

    def __init__(self, app: DashboardApp, *args: Any) -> None:
        self.app = app
        super().__init__(*args)

    def do_GET(self) -> None:
        try:
            reply = self._route()
        except Exception as exc:  # 문구에 무엇이 섞일지 모르므로 종류만 남긴다
            _log("error", {"action": "request", "result": "failed", "error_kind": type(exc).__name__})
            reply = _error_reply(HTTPStatus.INTERNAL_SERVER_ERROR)
        self._send(reply)

    def send_error(self, code: int, message: Optional[str] = None, explain: Optional[str] = None) -> None:
        """표준 라이브러리가 거절한 요청. 기본 응답은 요청 줄(토큰이 있을 수 있다)을 되풀이하므로 쓰지 않는다."""
        # 501은 `do_<메서드>`가 없는 경우, 곧 GET이 아닌 메서드다.
        status = HTTPStatus.METHOD_NOT_ALLOWED if code == HTTPStatus.NOT_IMPLEMENTED else HTTPStatus(code)
        reply = _error_reply(status)
        if status == HTTPStatus.METHOD_NOT_ALLOWED:
            reply = _Reply(status, body=reply.body, headers=(("Allow", "GET"),))
        self._send(reply)

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        self.log_message("%d", code)

    def log_error(self, format: str, *args: Any) -> None:
        """응답은 모두 `log_request`가 남긴다."""

    def log_message(self, format: str, *args: Any) -> None:
        """`format % args`는 상태 코드다. 경로는 토큰이 실리는 쿼리 문자열을 지우고 남긴다."""
        fields = {"action": "request", "method": self.command or "", "path": self.path.partition("?")[0]}
        _log("info", {**fields, "status": int(format % args)})

    def _send(self, reply: _Reply) -> None:
        self.send_response(reply.status)
        headers = (("Content-Type", reply.content_type), ("Content-Length", str(len(reply.body))))
        for name, value in (*headers, *_SECURITY_HEADERS, *reply.headers):
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(reply.body)

    def _matches(self, given: str) -> bool:
        return hmac.compare_digest(given.encode("utf-8"), self.app.token.encode("utf-8"))

    def _authorized(self) -> bool:
        scheme, _, value = self.headers.get("Authorization", "").partition(" ")
        if scheme == "Bearer" and self._matches(value):
            return True
        # `SimpleCookie`는 같은 호스트의 다른 앱이 남긴 이상한 쿠키 하나에 전체를 버리므로 직접 나눈다.
        for header in self.headers.get_all("Cookie", []):
            for part in header.split(";"):
                name, _, value = part.strip().partition("=")
                if name == _COOKIE_NAME and self._matches(value):
                    return True
        return False

    def _route(self) -> _Reply:
        path, _, query = self.path.partition("?")
        params = urllib.parse.parse_qs(query, keep_blank_values=True)
        if path == "/" and "token" in params:
            if not self._matches(params["token"][0]):
                return _error_reply(HTTPStatus.UNAUTHORIZED)
            cookie = f"{_COOKIE_NAME}={self.app.token}; HttpOnly; SameSite=Strict; Path=/"
            return _Reply(HTTPStatus.FOUND, headers=(("Location", "/"), ("Set-Cookie", cookie)))
        if not self._authorized():
            return _error_reply(HTTPStatus.UNAUTHORIZED)
        if path in _STATIC_ROUTES:  # 허용 목록의 파일만 내준다
            name, content_type = _STATIC_ROUTES[path]
            return _Reply(HTTPStatus.OK, content_type, (self.app.static_dir / name).read_bytes())
        return self._route_api(path)

    def _route_api(self, path: str) -> _Reply:
        store, now, interval_s = self.app.store, self.app.now(), self.app.interval_s
        if path == "/api/v1/summary":
            return _json_reply(summary(store, now, interval_s))
        if path == "/api/v1/health":
            return _json_reply(health(store, now, interval_s))
        if path.startswith(_REPO_PREFIX):
            wanted = [urllib.parse.unquote(part) for part in path[len(_REPO_PREFIX) :].split("/")]
            for repo in store.repos():
                if [repo.owner, repo.name] == wanted:
                    return _json_reply(repo_summary(store, repo, now, interval_s))
        return _error_reply(HTTPStatus.NOT_FOUND)


def make_server(app: DashboardApp, bind: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((bind, port), partial(_Handler, app))


def _is_loopback(bind: str) -> bool:
    if bind == "localhost":
        return True
    try:
        return ipaddress.ip_address(bind).is_loopback
    except ValueError:
        return False


def _sync_loop(sync_once: Callable[[], None], wait_s: float, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            sync_once()
        except Exception as exc:  # 대조가 실패해도 서버와 다음 대조는 계속한다
            _log("error", {"action": "sync", "result": "failed", "error_kind": type(exc).__name__})
        stop.wait(wait_s)


def serve(
    app: DashboardApp,
    bind: str,
    port: int,
    sync_once: Callable[[], None],
    out: TextIO,
    stop: threading.Event,
    *,
    wait_s: Optional[float] = None,
) -> int:
    """`stop`이 켜지거나 Ctrl+C가 올 때까지 서버를 돌린다. `wait_s`는 시험에서만 대조 주기를 줄인다."""
    server = make_server(app, bind, port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        print(f"대시보드: http://{bind}:{server.server_address[1]}/?token=<REDACTED>", file=out)
        if not _is_loopback(bind):
            print(_OPEN_ADDRESS_WARNING, file=out)
        out.flush()  # 파이프로 받는 쪽(백그라운드 실행)도 바로 주소를 본다
        period = app.interval_s if wait_s is None else wait_s
        threading.Thread(target=_sync_loop, args=(sync_once, period, stop), daemon=True).start()
        stop.wait()
    except KeyboardInterrupt:
        stop.set()
    finally:
        server.shutdown()
        server.server_close()
    return 0
