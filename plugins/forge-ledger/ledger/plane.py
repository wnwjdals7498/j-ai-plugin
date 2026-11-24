from __future__ import annotations

import http.client
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Protocol

from ledger.secrets import find_secrets

_ENV_URL = "FORGE_LEDGER_PLANE_URL"
_ENV_WORKSPACE = "FORGE_LEDGER_PLANE_WORKSPACE"
_ENV_TOKEN = "FORGE_LEDGER_PLANE_TOKEN"  # nosec B105 # 환경 변수 이름이며 비밀값이 아님
_RETRY_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0)
_RATE_WINDOW_SECONDS = 60.0
_RATE_LIMIT_DEFAULT_WAIT = 60.0
_RATE_LIMIT_MAX_WAIT = 65.0
_RATE_LIMIT_RETRIES = 3
_PER_PAGE = 100
_DEFAULT_TIMEOUT = 30.0


class PlaneError(Exception):
    """Plane API 요청 중 발생한 일반 오류."""


class PlaneUnavailable(PlaneError):
    """Plane 연결 실패 또는 재시도 후에도 복구되지 않은 서버/속도 오류."""


class PlaneAuthError(PlaneError):
    """Plane API 인증 또는 권한 오류."""


class PlaneConflict(PlaneError):
    """수정 시점 불일치로 덮어쓰기를 중단했을 때 발생한다."""


@dataclass
class PlaneSettings:
    """Plane CE API 접속 설정."""

    base_url: str
    workspace: str
    token: str = field(repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> PlaneSettings:
        values: list[str] = []
        for key in (_ENV_URL, _ENV_WORKSPACE, _ENV_TOKEN):
            raw = env.get(key, "").strip()
            if not raw:
                raise PlaneError(f"설정 없음: {key}")
            values.append(raw)
        return cls(base_url=values[0], workspace=values[1], token=<REDACTED>


class PlaneLike(Protocol):
    """Plane 클라이언트 인터페이스(PlaneClient와 테스트 FakePlane이 따른다)."""

    def list_projects(self) -> list[dict[str, Any]]: ...

    def create_project(self, name: str, identifier: str) -> dict[str, Any]: ...

    def delete_project(self, project_id: str) -> None: ...

    def create_label(self, project_id: str, name: str, color: str) -> dict[str, Any]: ...

    def create_module(self, project_id: str, name: str) -> dict[str, Any]: ...

    def add_to_module(self, project_id: str, module_id: str, item_ids: list[str]) -> None: ...

    def create_cycle(self, project_id: str, name: str) -> dict[str, Any]: ...

    def create_item(
        self,
        project_id: str,
        name: str,
        description_html: str,
        label_ids: list[str],
    ) -> dict[str, Any]: ...

    def list_items(self, project_id: str) -> list[dict[str, Any]]: ...

    def get_item(self, project_id: str, item_id: str) -> dict[str, Any]: ...

    def update_item_description(
        self,
        project_id: str,
        item_id: str,
        html: str,
        expected_updated_at: str,
    ) -> dict[str, Any]: ...


def _seg(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _notify_stderr(message: str) -> None:
    print(message, file=sys.stderr)


def _retry_after_seconds(raw: Optional[str]) -> float:
    """429 응답의 Retry-After(정수 초)를 읽는다. 없거나 잘못되면 60초, 최대 65초."""
    wait = _RATE_LIMIT_DEFAULT_WAIT
    if raw is not None:
        try:
            wait = float(int(raw.strip()))
        except ValueError:
            wait = _RATE_LIMIT_DEFAULT_WAIT
    return max(0.0, min(wait, _RATE_LIMIT_MAX_WAIT))


class PlaneClient:
    """표준 라이브러리 urllib 기반 Plane CE API 클라이언트."""

    def __init__(
        self,
        settings: PlaneSettings,
        per_minute: int = 50,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        notify: Optional[Callable[[str], None]] = None,
    ) -> None:
        if per_minute < 1:
            raise ValueError("per_minute는 1 이상이어야 한다")
        self._settings = settings
        self._base_url = settings.base_url.rstrip("/")
        parsed_url = urllib.parse.urlsplit(self._base_url)
        if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
            raise PlaneError(f"설정 오류: {_ENV_URL}")
        self._workspace = _seg(settings.workspace)
        self._per_minute = per_minute
        self._clock = clock
        self._sleep = sleep
        self._notify = notify if notify is not None else _notify_stderr
        self._timestamps: deque[float] = deque()
        if parsed_url.hostname in ("127.0.0.1", "localhost", "::1"):
            self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        else:
            self._opener = urllib.request.build_opener()

    def _projects_path(self, *parts: str) -> str:
        base = f"/api/v1/workspaces/{self._workspace}/projects/"
        if not parts:
            return base
        return base + "/".join(_seg(p) for p in parts) + "/"

    def _sanitize(self, text: str) -> str:
        token = self._settings.token
        cleaned = text.replace(token, "[REDACTED]") if token else text
        if find_secrets(cleaned):
            return ""
        return cleaned.strip()

    def _wait_rate_limit(self) -> None:
        now = self._clock()
        while self._timestamps and now - self._timestamps[0] >= _RATE_WINDOW_SECONDS:
            self._timestamps.popleft()
        if len(self._timestamps) >= self._per_minute:
            wait = _RATE_WINDOW_SECONDS - (now - self._timestamps[0])
            if wait > 0:
                self._sleep(wait)
            now = self._clock()
            while self._timestamps and now - self._timestamps[0] >= _RATE_WINDOW_SECONDS:
                self._timestamps.popleft()
            if len(self._timestamps) >= self._per_minute:
                self._timestamps.popleft()
        self._timestamps.append(now)

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[dict[str, Any]] = None,
        query: Optional[dict[str, str]] = None,
    ) -> Any:
        url = f"{self._base_url}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"

        data: Optional[bytes] = None
        headers: dict[str, str] = {
            "X-API-Key": self._settings.token,
            "Accept": "application/json",
        }
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        attempt = 0
        rate_limited = 0
        while True:
            self._wait_rate_limit()
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with self._opener.open(req, timeout=_DEFAULT_TIMEOUT) as resp:
                    raw = resp.read()
                if not raw:
                    return None
                try:
                    return json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise PlaneError("Plane 응답 JSON 파싱 실패") from None
            except urllib.error.HTTPError as exc:
                status = exc.code
                retry_after = exc.headers.get("Retry-After") if exc.headers is not None else None
                exc.close()
                if status in (401, 403):
                    raise PlaneAuthError("Plane 토큰 확인 필요") from None
                if status == 429:
                    if rate_limited < _RATE_LIMIT_RETRIES:
                        rate_limited += 1
                        wait = _retry_after_seconds(retry_after)
                        self._notify(f"Plane 호출 한도에 걸려 {int(wait)}초 기다립니다")
                        self._sleep(wait)
                        continue
                    raise PlaneUnavailable("Plane 요청 실패: HTTP 429") from None
                if 500 <= status <= 599:
                    if attempt < len(_RETRY_DELAYS):
                        self._sleep(_RETRY_DELAYS[attempt])
                        attempt += 1
                        continue
                    raise PlaneUnavailable(f"Plane 요청 실패: HTTP {status}") from None
                raise PlaneError(f"Plane 요청 오류: HTTP {status}") from None
            except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
                if attempt < len(_RETRY_DELAYS):
                    self._sleep(_RETRY_DELAYS[attempt])
                    attempt += 1
                    continue
                raw_reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
                detail = self._sanitize(str(raw_reason))
                message = f"Plane 연결 실패: {detail}" if detail else "Plane 연결 실패"
                raise PlaneUnavailable(message) from None

    def _request_dict(
        self,
        method: str,
        path: str,
        payload: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        result = self._request(method, path, payload=payload)
        if not isinstance(result, dict):
            raise PlaneError("Plane 응답 형식 오류")
        return result

    def _list_paginated(self, path: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor: Optional[str] = None
        while True:
            query: dict[str, str] = {"per_page": str(_PER_PAGE)}
            if cursor is not None:
                query["cursor"] = cursor
            data = self._request("GET", path, query=query)
            if isinstance(data, list):
                items.extend(entry for entry in data if isinstance(entry, dict))
                break
            if not isinstance(data, dict):
                raise PlaneError("Plane 목록 응답 형식 오류")
            results = data.get("results", [])
            if not isinstance(results, list):
                raise PlaneError("Plane 목록 응답 형식 오류")
            items.extend(entry for entry in results if isinstance(entry, dict))
            next_cursor = data.get("next_cursor")
            if not data.get("next_page_results") or not isinstance(next_cursor, str) or not next_cursor:
                break
            cursor = next_cursor
        return items

    def list_projects(self) -> list[dict[str, Any]]:
        return self._list_paginated(self._projects_path())

    def create_project(self, name: str, identifier: str) -> dict[str, Any]:
        return self._request_dict(
            "POST",
            self._projects_path(),
            payload={
                "name": name,
                "identifier": identifier,
                "cycle_view": True,
                "module_view": True,
            },
        )

    def delete_project(self, project_id: str) -> None:
        self._request("DELETE", self._projects_path(project_id))

    def create_label(self, project_id: str, name: str, color: str) -> dict[str, Any]:
        return self._request_dict(
            "POST",
            self._projects_path(project_id, "labels"),
            payload={"name": name, "color": color},
        )

    def create_module(self, project_id: str, name: str) -> dict[str, Any]:
        return self._request_dict(
            "POST",
            self._projects_path(project_id, "modules"),
            payload={"name": name},
        )

    def add_to_module(self, project_id: str, module_id: str, item_ids: list[str]) -> None:
        self._request(
            "POST",
            self._projects_path(project_id, "modules", module_id, "module-issues"),
            payload={"issues": list(item_ids)},
        )

    def create_cycle(self, project_id: str, name: str) -> dict[str, Any]:
        return self._request_dict(
            "POST",
            self._projects_path(project_id, "cycles"),
            payload={"name": name},
        )

    def create_item(
        self,
        project_id: str,
        name: str,
        description_html: str,
        label_ids: list[str],
    ) -> dict[str, Any]:
        return self._request_dict(
            "POST",
            self._projects_path(project_id, "work-items"),
            payload={
                "name": name,
                "description_html": description_html,
                "labels": list(label_ids),
            },
        )

    def list_items(self, project_id: str) -> list[dict[str, Any]]:
        return self._list_paginated(self._projects_path(project_id, "work-items"))

    def get_item(self, project_id: str, item_id: str) -> dict[str, Any]:
        return self._request_dict(
            "GET",
            self._projects_path(project_id, "work-items", item_id),
        )

    def update_item_description(
        self,
        project_id: str,
        item_id: str,
        html: str,
        expected_updated_at: str,
    ) -> dict[str, Any]:
        current = self.get_item(project_id, item_id)
        if current.get("updated_at") != expected_updated_at:
            raise PlaneConflict(f"작업 항목 수정 충돌: {item_id}")
        return self._request_dict(
            "PATCH",
            self._projects_path(project_id, "work-items", item_id),
            payload={"description_html": html},
        )
