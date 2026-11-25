"""허브 시험용 가짜 서버."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit


class FakeGiteaServer:
    """127.0.0.1 임의 포트에서 도는 테스트용 Gitea HTTP 서버(저장소 하나, 읽기 전용)."""

    def __init__(self) -> None:
        self.repo: dict[str, Any] = {"full_name": "o/n", "default_branch": "main", "html_url": ""}
        self.issues: list[dict[str, Any]] = []
        self.pulls: list[dict[str, Any]] = []
        self.runs: list[dict[str, Any]] = []
        self.status_queue: list[int] = []
        self.redirect_location = ""
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self._closed = False

        owner = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def _send_json(self, status: int, body: Any, total: Optional[int] = None) -> None:
                raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                if total is not None:
                    self.send_header("X-Total-Count", str(total))
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _send_page(self, entries: list[dict[str, Any]], query: dict[str, list[str]]) -> None:
                page = int(query.get("page", ["1"])[0])
                limit = int(query.get("limit", ["50"])[0])
                start = (page - 1) * limit
                self._send_json(200, entries[start : start + limit], total=len(entries))

            def do_GET(self) -> None:
                owner.requests.append(("GET", self.path, dict(self.headers.items())))
                if owner.status_queue:
                    status = owner.status_queue.pop(0)
                    if 300 <= status < 400:
                        self.send_response(status)
                        self.send_header("Location", owner.redirect_location)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    if status >= 400:
                        self._send_json(status, {"message": f"forced status {status}"})
                        return

                url = urlsplit(self.path)
                query = parse_qs(url.query)
                parts = url.path.strip("/").split("/")
                if parts == ["api", "v1", "user"]:
                    self._send_json(200, {"login": "tester"})
                    return
                if parts[:3] == ["api", "v1", "repos"] and len(parts) >= 5:
                    rest = parts[5:]
                    if not rest:
                        self._send_json(200, owner.repo)
                        return
                    if rest == ["issues"]:
                        self._send_page(owner.issues, query)
                        return
                    if rest == ["pulls"]:
                        self._send_page(owner.pulls, query)
                        return
                    if rest == ["actions", "runs"]:
                        limit = int(query.get("limit", ["50"])[0])
                        total = len(owner.runs)
                        body = {"total_count": total, "workflow_runs": owner.runs[:limit]}
                        self._send_json(200, body, total=total)
                        return
                self._send_json(404, {"message": "not found"})

        self._server = HTTPServer(("127.0.0.1", 0), _Handler)
        self._port = int(self._server.server_address[1])
        self.repo["html_url"] = f"{self.base_url}/o/n"
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._server.shutdown()
        self._thread.join(timeout=5.0)
        self._server.server_close()
