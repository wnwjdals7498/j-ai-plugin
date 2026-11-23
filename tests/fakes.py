from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from ledger.check import SourceUnavailable
from ledger.plane import PlaneConflict, PlaneError
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


_FORBIDDEN_NAME_RE = re.compile(r"[&+,:;$^}{*=?@#|'<>.()%!-]")
_FORBIDDEN_NAME_BODY = {"non_field_errors": ["Project name cannot contain special characters."]}


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
        self.projects: dict[str, dict[str, Any]] = {}
        self.items: dict[str, dict[str, dict[str, Any]]] = {}
        self._item_seq: dict[str, int] = {}
        self._tick = 0
        self.touch_next_get_item: bool = False
        self._closed = False

        owner = self

        def _next_timestamp() -> str:
            owner._tick += 1
            ts = datetime(2026, 11, 23, 0, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=owner._tick)
            return ts.strftime("%Y-%m-%dT%H:%M:%SZ")

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
                parts = clean_path.strip("/").split("/")
                if method == "GET":
                    if clean_path.endswith("/projects/"):
                        if owner.pages:
                            page = owner.pages.pop(0)
                        else:
                            page = {
                                "results": [dict(p) for p in owner.projects.values()],
                                "next_cursor": None,
                                "next_page_results": False,
                            }
                        self._send_json(200, page)
                        return
                    if clean_path.endswith("/work-items/"):
                        if owner.pages:
                            page = owner.pages.pop(0)
                        else:
                            pid = parts[-2] if len(parts) >= 2 else ""
                            page = {
                                "results": [dict(it) for it in owner.items.get(pid, {}).values()],
                                "next_cursor": None,
                                "next_page_results": False,
                            }
                        self._send_json(200, page)
                        return
                    if "/work-items/" in clean_path:
                        if owner.item is not None:
                            item_payload = dict(owner.item)
                        else:
                            pid = parts[-3] if len(parts) >= 3 else ""
                            item_id = parts[-1]
                            stored = owner.items.get(pid, {}).get(item_id)
                            if stored is not None:
                                item_payload = dict(stored)
                                if owner.touch_next_get_item:
                                    owner.touch_next_get_item = False
                                    stored["updated_at"] = _next_timestamp()
                            else:
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
                    if clean_path.endswith("/projects/"):
                        if isinstance(parsed_json, dict) and (
                            _FORBIDDEN_NAME_RE.search(str(parsed_json.get("name", "")))
                            or _FORBIDDEN_NAME_RE.search(str(parsed_json.get("identifier", "")))
                        ):
                            self._send_json(400, _FORBIDDEN_NAME_BODY)
                            return
                        proj_id = f"id-{seq}"
                        payload: dict[str, Any] = {
                            "id": proj_id,
                            "sequence_id": seq,
                            "updated_at": _next_timestamp(),
                        }
                        if isinstance(parsed_json, dict):
                            payload.update(parsed_json)
                        owner.projects[proj_id] = dict(payload)
                        self._send_json(201, payload)
                        return
                    if clean_path.endswith("/work-items/"):
                        pid = parts[-2] if len(parts) >= 2 else ""
                        item_seq = owner._item_seq.get(pid, 0) + 1
                        owner._item_seq[pid] = item_seq
                        item_id = f"id-{seq}"
                        payload = {
                            "id": item_id,
                            "sequence_id": item_seq,
                            "updated_at": _next_timestamp(),
                        }
                        if isinstance(parsed_json, dict):
                            payload.update(parsed_json)
                        owner.items.setdefault(pid, {})[item_id] = dict(payload)
                        self._send_json(201, payload)
                        return
                    payload = {
                        "id": f"id-{seq}",
                        "sequence_id": seq,
                        "updated_at": "2026-11-23T00:00:00Z",
                    }
                    if isinstance(parsed_json, dict):
                        payload.update(parsed_json)
                    self._send_json(201, payload)
                    return

                if method == "PATCH":
                    pid = parts[-3] if len(parts) >= 3 else ""
                    item_id = parts[-1]
                    if owner.item is not None:
                        payload = dict(owner.item)
                        if isinstance(parsed_json, dict):
                            payload.update(parsed_json)
                        payload["updated_at"] = "2026-11-23T12:00:00Z"
                    elif item_id in owner.items.get(pid, {}):
                        stored = owner.items[pid][item_id]
                        if isinstance(parsed_json, dict):
                            stored.update(parsed_json)
                        stored["updated_at"] = _next_timestamp()
                        payload = dict(stored)
                    else:
                        payload = {
                            "id": item_id,
                            "sequence_id": 1,
                            "name": "",
                            "description_html": "",
                        }
                        if isinstance(parsed_json, dict):
                            payload.update(parsed_json)
                        payload["updated_at"] = "2026-11-23T12:00:00Z"
                    self._send_json(200, payload)
                    return

                if method == "DELETE":
                    if len(parts) >= 2 and parts[-2] == "projects":
                        pid = parts[-1]
                        owner.projects.pop(pid, None)
                        owner.items.pop(pid, None)
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


@dataclass
class _FakeProject:
    id: str
    name: str
    identifier: str
    labels: list[dict[str, Any]] = field(default_factory=list)
    modules: list[dict[str, Any]] = field(default_factory=list)
    cycles: list[dict[str, Any]] = field(default_factory=list)
    items: list[dict[str, Any]] = field(default_factory=list)
    next_seq: int = 1


class FakePlane:
    """메모리 기반 PlaneLike 테스트 대역."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.fail_on: dict[str, Exception] = {}
        self._projects: dict[str, _FakeProject] = {}
        self._id_seq = 0
        self._tick = 0
        self._touch_next_get: set[tuple[str, str]] = set()

    def _next_id(self, prefix: str) -> str:
        self._id_seq += 1
        return f"{prefix}-{self._id_seq}"

    def _next_timestamp(self) -> str:
        self._tick += 1
        ts = datetime(2026, 11, 23, 0, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=self._tick)
        return ts.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _record_and_check(self, method: str, *args: Any) -> None:
        self.calls.append((method, *args))
        exc = self.fail_on.get(method)
        if exc is not None:
            raise exc

    def _project_by_id(self, project_id: str) -> _FakeProject:
        proj = self._projects.get(project_id)
        if proj is None:
            raise PlaneError(f"project not found: {project_id}")
        return proj

    def _project_by_key(self, project_key: str) -> _FakeProject:
        for proj in self._projects.values():
            if proj.identifier == project_key:
                return proj
        raise KeyError(project_key)

    @staticmethod
    def _copy_item(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": item["id"],
            "sequence_id": item["sequence_id"],
            "updated_at": item["updated_at"],
            "name": item["name"],
            "description_html": item["description_html"],
            "labels": list(item["labels"]),
        }

    # --- PlaneLike 구현 ---

    def list_projects(self) -> list[dict[str, Any]]:
        self._record_and_check("list_projects")
        return [
            {"id": p.id, "name": p.name, "identifier": p.identifier}
            for p in self._projects.values()
        ]

    def create_project(self, name: str, identifier: str) -> dict[str, Any]:
        self._record_and_check("create_project", name, identifier)
        if _FORBIDDEN_NAME_RE.search(name) or _FORBIDDEN_NAME_RE.search(identifier):
            raise PlaneError("Plane 요청 오류: HTTP 400")
        project_id = self._next_id("proj")
        proj = _FakeProject(id=project_id, name=name, identifier=identifier)
        self._projects[project_id] = proj
        return {"id": proj.id, "name": proj.name, "identifier": proj.identifier}

    def delete_project(self, project_id: str) -> None:
        self._record_and_check("delete_project", project_id)
        if project_id not in self._projects:
            raise PlaneError(f"project not found: {project_id}")
        del self._projects[project_id]

    def create_label(self, project_id: str, name: str, color: str) -> dict[str, Any]:
        self._record_and_check("create_label", project_id, name, color)
        proj = self._project_by_id(project_id)
        label = {"id": self._next_id("lbl"), "name": name, "color": color}
        proj.labels.append(label)
        return dict(label)

    def create_module(self, project_id: str, name: str) -> dict[str, Any]:
        self._record_and_check("create_module", project_id, name)
        proj = self._project_by_id(project_id)
        module: dict[str, Any] = {"id": self._next_id("mod"), "name": name, "item_ids": []}
        proj.modules.append(module)
        return {"id": module["id"], "name": module["name"]}

    def add_to_module(self, project_id: str, module_id: str, item_ids: list[str]) -> None:
        self._record_and_check("add_to_module", project_id, module_id, list(item_ids))
        proj = self._project_by_id(project_id)
        for mod in proj.modules:
            if mod["id"] == module_id:
                members: list[str] = mod["item_ids"]
                for item_id in item_ids:
                    if item_id not in members:
                        members.append(item_id)
                return
        raise PlaneError(f"module not found: {module_id}")

    def create_cycle(self, project_id: str, name: str) -> dict[str, Any]:
        self._record_and_check("create_cycle", project_id, name)
        proj = self._project_by_id(project_id)
        cycle = {"id": self._next_id("cyc"), "name": name}
        proj.cycles.append(cycle)
        return dict(cycle)

    def create_item(
        self,
        project_id: str,
        name: str,
        description_html: str,
        label_ids: list[str],
    ) -> dict[str, Any]:
        self._record_and_check("create_item", project_id, name)
        proj = self._project_by_id(project_id)
        seq = proj.next_seq
        proj.next_seq += 1
        item: dict[str, Any] = {
            "id": self._next_id("item"),
            "sequence_id": seq,
            "updated_at": self._next_timestamp(),
            "name": name,
            "description_html": description_html,
            "labels": list(label_ids),
        }
        proj.items.append(item)
        return self._copy_item(item)

    def list_items(self, project_id: str) -> list[dict[str, Any]]:
        self._record_and_check("list_items", project_id)
        proj = self._project_by_id(project_id)
        return [self._copy_item(item) for item in proj.items]

    def get_item(self, project_id: str, item_id: str) -> dict[str, Any]:
        self._record_and_check("get_item", project_id, item_id)
        proj = self._project_by_id(project_id)
        for item in proj.items:
            if item["id"] == item_id:
                snapshot = self._copy_item(item)
                touch_key = (proj.identifier, str(item["name"]))
                if touch_key in self._touch_next_get:
                    self._touch_next_get.remove(touch_key)
                    item["updated_at"] = self._next_timestamp()
                return snapshot
        raise PlaneError(f"item not found: {item_id}")

    def update_item_description(
        self,
        project_id: str,
        item_id: str,
        html: str,
        expected_updated_at: str,
    ) -> dict[str, Any]:
        self._record_and_check("update_item_description", project_id, item_id)
        proj = self._project_by_id(project_id)
        for item in proj.items:
            if item["id"] == item_id:
                if item["updated_at"] != expected_updated_at:
                    raise PlaneConflict(f"작업 항목 수정 충돌: {item_id}")
                item["description_html"] = html
                item["updated_at"] = self._next_timestamp()
                return self._copy_item(item)
        raise PlaneError(f"item not found: {item_id}")

    # --- 시험 도우미 ---

    def item_names(self, project_key: str) -> list[str]:
        proj = self._project_by_key(project_key)
        return [str(item["name"]) for item in proj.items]

    def labels(self, project_key: str) -> list[str]:
        proj = self._project_by_key(project_key)
        return [str(lbl["name"]) for lbl in proj.labels]

    def module_members(self, project_key: str, module_name: str) -> list[str]:
        proj = self._project_by_key(project_key)
        for mod in proj.modules:
            if mod["name"] == module_name:
                return list(mod["item_ids"])
        raise KeyError(module_name)

    def item_ids(self, project_key: str) -> list[str]:
        proj = self._project_by_key(project_key)
        return [str(item["id"]) for item in proj.items]

    def cycles(self, project_key: str) -> list[str]:
        proj = self._project_by_key(project_key)
        return [str(cyc["name"]) for cyc in proj.cycles]

    def item_html(self, project_key: str, item_name: str) -> str:
        proj = self._project_by_key(project_key)
        for item in proj.items:
            if item["name"] == item_name:
                return str(item["description_html"])
        raise KeyError(item_name)

    def touch_on_next_get(self, project_key: str, item_name: str) -> None:
        self._touch_next_get.add((project_key, item_name))
