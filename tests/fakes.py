from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from ledger.check import SourceUnavailable
from ledger.spec import TEMPLATE_FOR

_PLANE_TEMPLATES = (
    Path(__file__).resolve().parent.parent
    / "plugins"
    / "forge-ledger"
    / "skills"
    / "ledger-plan"
    / "templates"
    / "plane"
)


class DictSource:
    """메모리 딕셔너리 기반 단위 문서 원천."""

    def __init__(self, docs: dict[tuple[str, str], str]) -> None:
        self._docs = dict(docs)

    def get_container_html(self, project_key: str, container: str) -> Optional[str]:
        return self._docs.get((project_key, container))

    @classmethod
    def from_templates(cls, project_key: str) -> DictSource:
        docs = {
            (project_key, container): (_PLANE_TEMPLATES / filename).read_text(encoding="utf-8")
            for container, filename in TEMPLATE_FOR.items()
        }
        return cls(docs)


class RaisingSource:
    """항상 SourceUnavailable을 던지는 테스트용 원천."""

    def get_container_html(self, project_key: str, container: str) -> Optional[str]:
        raise SourceUnavailable(f"unavailable: {project_key}/{container}")


class _HeaderMap:
    """대소문자 구분 없는 HTTP 요청 헤더 맵."""

    def __init__(self, items: list[tuple[str, str]]) -> None:
        self._store = {k.lower(): v for k, v in items}

    def __getitem__(self, key: str) -> str:
        return self._store[key.lower()]

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and key.lower() in self._store

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        return self._store.get(key.lower(), default)


@dataclass
class RecordedRequest:
    """FakePlaneServer가 기록한 단일 HTTP 요청."""

    method: str
    path: str
    headers: _HeaderMap
    json: Any


class FakePlaneServer:
    """127.0.0.1 임의 포트에서 도는 테스트용 Plane HTTP 서버."""

    def __init__(self) -> None:
        self.requests: list[RecordedRequest] = []
        self.pages: list[dict[str, Any]] = []
        self.item: Optional[dict[str, Any]] = None
        self.force_status: Optional[int] = None
        self.status_queue: list[int] = []
        self.error_body: str = ""
        self._closed = False

        owner = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def _send_json(self, status: int, body: Any) -> None:
                raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _handle(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or "0")
                raw_body = self.rfile.read(length) if length > 0 else b""
                parsed_json: Any = json.loads(raw_body.decode("utf-8")) if raw_body else None
                owner.requests.append(
                    RecordedRequest(
                        method=method,
                        path=self.path,
                        headers=_HeaderMap(list(self.headers.items())),
                        json=parsed_json,
                    )
                )

                status_override = owner.status_queue.pop(0) if owner.status_queue else owner.force_status
                if status_override is not None and status_override >= 400:
                    detail = owner.error_body or f"forced status {status_override}"
                    self._send_json(status_override, {"detail": detail})
                    return

                clean_path = urlsplit(self.path).path
                if method == "GET":
                    if clean_path.endswith("/projects/") or clean_path.endswith("/work-items/"):
                        if owner.pages:
                            page = owner.pages.pop(0)
                        else:
                            page = {"results": [], "next_cursor": None, "next_page_results": False}
                        self._send_json(200, page)
                        return
                    if "/work-items/" in clean_path:
                        if owner.item is not None:
                            item_payload = dict(owner.item)
                        else:
                            item_id = clean_path.rstrip("/").rsplit("/", 1)[-1]
                            item_payload = {
                                "id": item_id,
                                "sequence_id": 1,
                                "updated_at": "2026-11-23T00:00:00Z",
                                "name": "",
                                "description_html": "",
                            }
                        self._send_json(200, item_payload)
                        return
                    self._send_json(200, {})
                    return

                if method == "POST":
                    seq = len(owner.requests)
                    payload: dict[str, Any] = {
                        "id": f"id-{seq}",
                        "sequence_id": seq,
                        "updated_at": "2026-11-23T00:00:00Z",
                    }
                    if isinstance(parsed_json, dict):
                        payload.update(parsed_json)
                    self._send_json(201, payload)
                    return

                if method == "PATCH":
                    item_id = clean_path.rstrip("/").rsplit("/", 1)[-1]
                    payload = (
                        dict(owner.item)
                        if owner.item is not None
                        else {
                            "id": item_id,
                            "sequence_id": 1,
                            "name": "",
                            "description_html": "",
                        }
                    )
                    if isinstance(parsed_json, dict):
                        payload.update(parsed_json)
                    payload["updated_at"] = "2026-11-23T12:00:00Z"
                    self._send_json(200, payload)
                    return

                if method == "DELETE":
                    self.send_response(204)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

                self._send_json(405, {"detail": "method not allowed"})

            def do_GET(self) -> None:
                self._handle("GET")

            def do_POST(self) -> None:
                self._handle("POST")

            def do_PATCH(self) -> None:
                self._handle("PATCH")

            def do_DELETE(self) -> None:
                self._handle("DELETE")

        self._server = HTTPServer(("127.0.0.1", 0), _Handler)
        self._port = int(self._server.server_address[1])
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

    def __enter__(self) -> FakePlaneServer:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
