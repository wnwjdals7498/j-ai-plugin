from __future__ import annotations

import http.client
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Protocol

from ledger.adapters import CycleInfo, ProjectInfo, to_utc_iso
from ledger.ratelimit import LocalRateLimiter, RateLimiter
from ledger.secrets import find_secrets

_ENV_URL = "FORGE_LEDGER_PLANE_URL"
_ENV_WORKSPACE = "FORGE_LEDGER_PLANE_WORKSPACE"
_ENV_AUTH = "FORGE_LEDGER_PLANE_TOKEN"  # 환경 변수 이름
_RETRY_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0)
_RATE_LIMIT_DEFAULT_WAIT = 60.0
_RATE_LIMIT_MAX_WAIT = 65.0
_RATE_LIMIT_RETRIES = 3
_PER_PAGE = 100
_DEFAULT_TIMEOUT = 30.0
_FORMAT_ERROR = "Plane 응답 형식 오류"
_LIST_FORMAT_ERROR = "Plane 목록 응답 형식 오류"
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ITEM_CODE_RE = re.compile(r"(.+)-([0-9]+)")
_UPDATABLE_ITEM_FIELDS = frozenset(
    {"state", "assignees", "labels", "parent", "description_html", "external_source", "external_id"}
)


class PlaneError(Exception):
    """Plane API 요청 중 발생한 일반 오류."""


class PlaneDuplicate(PlaneError):
    def __init__(self, existing_id: str) -> None:
        super().__init__("Plane 원천 표식 중복")
        self.existing_id = existing_id


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
        for key in (_ENV_URL, _ENV_WORKSPACE, _ENV_AUTH):
            raw = env.get(key, "").strip()
            if not raw:
                raise PlaneError(f"설정 없음: {key}")
            values.append(raw)
        url, workspace, token = values
        return cls(base_url=url, workspace=workspace, token=token)


def check_update_fields(fields: Mapping[str, Any]) -> None:
    """`update_item`이 받지 않는 필드가 있으면 `ValueError`다."""
    rejected = sorted(set(fields) - _UPDATABLE_ITEM_FIELDS)
    if rejected:
        raise ValueError(f"수정할 수 없는 필드: {', '.join(rejected)}")


def match_external(items: list[dict[str, Any]], source: str, external_id: str) -> Optional[dict[str, Any]]:
    """원천 표식 두 값이 모두 같은 첫 항목."""
    for item in items:
        if item.get("external_source") == source and item.get("external_id") == external_id:
            return item
    return None


def find_by_code(
    code: str,
    list_project_infos: Callable[[], list[ProjectInfo]],
    list_items: Callable[[str], list[dict[str, Any]]],
) -> tuple[str, dict[str, Any]]:
    """`FL-12` 같은 항목 코드를 (프로젝트 id, 항목)으로 바꾼다."""
    parsed = _ITEM_CODE_RE.fullmatch(code)
    if parsed:
        identifier, number = parsed.group(1), int(parsed.group(2))
        for project in list_project_infos():
            if project.identifier != identifier:
                continue
            for item in list_items(project.id):
                if item.get("sequence_id") == number:
                    return project.id, item
    raise PlaneError(f"항목 없음: {code}")


class PlaneLike(Protocol):
    """Plane 클라이언트 인터페이스(PlaneClient와 테스트 FakePlane이 따른다)."""

    def whoami_profile(self) -> dict[str, str]: ...

    def list_projects(self) -> list[dict[str, Any]]: ...

    def create_project(self, name: str, identifier: str) -> dict[str, Any]: ...

    def delete_project(self, project_id: str) -> None: ...

    def create_label(self, project_id: str, name: str, color: str) -> dict[str, Any]: ...

    def create_module(self, project_id: str, name: str) -> dict[str, Any]: ...

    def add_to_module(self, project_id: str, module_id: str, item_ids: list[str]) -> None: ...

    def create_cycle(self, project_id: str, name: str) -> dict[str, Any]: ...

    def list_states(self, project_id: str) -> list[dict[str, Any]]: ...

    def list_labels(self, project_id: str) -> list[dict[str, Any]]: ...

    def create_item(
        self,
        project_id: str,
        name: str,
        description_html: str,
        label_ids: list[str],
        *,
        parent: Optional[str] = None,
        state: Optional[str] = None,
        external_source: Optional[str] = None,
        external_id: Optional[str] = None,
    ) -> dict[str, Any]: ...

    def update_item(self, project_id: str, item_id: str, fields: dict[str, Any]) -> dict[str, Any]: ...

    def find_item_by_external(self, project_id: str, source: str, external_id: str) -> Optional[dict[str, Any]]: ...

    def list_comments(self, project_id: str, item_id: str) -> list[dict[str, Any]]: ...

    def create_comment(
        self,
        project_id: str,
        item_id: str,
        html: str,
        *,
        external_source: Optional[str] = None,
        external_id: Optional[str] = None,
    ) -> dict[str, Any]: ...

    def resolve_code(self, code: str) -> tuple[str, dict[str, Any]]: ...

    def list_items(self, project_id: str) -> list[dict[str, Any]]: ...

    def get_item(self, project_id: str, item_id: str) -> dict[str, Any]: ...

    def update_item_description(
        self,
        project_id: str,
        item_id: str,
        html: str,
        expected_updated_at: str,
    ) -> dict[str, Any]: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """리다이렉트를 거부한다. urllib은 인증 헤더를 다른 호스트로도 보낸다."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _seg(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    return value if isinstance(value, str) else ""


def _required_text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise PlaneError(_FORMAT_ERROR)
    return value


def _int_or_zero(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if value is None:
        return 0
    if not isinstance(value, int) or isinstance(value, bool):
        raise PlaneError(_FORMAT_ERROR)
    return value


def _cycle_time(data: dict[str, Any], key: str) -> str:
    """cycle 시작/종료 일시를 UTC `YYYY-MM-DDTHH:MM:SSZ`로 바꾼다. 날짜만 있으면 UTC 자정으로 본다."""
    value = data.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise PlaneError(_FORMAT_ERROR)
    if not value:
        return ""
    raw = f"{value}T00:00:00Z" if _DATE_ONLY_RE.fullmatch(value) else value
    try:
        return to_utc_iso(raw)
    except ValueError:
        raise PlaneError(_FORMAT_ERROR) from None


def _first_cycle_entry(data: Any) -> Optional[dict[str, Any]]:
    if isinstance(data, dict):
        if "results" not in data:
            raise PlaneError(_FORMAT_ERROR)
        entries = data["results"]
    else:
        entries = data
    if not isinstance(entries, list):
        raise PlaneError(_FORMAT_ERROR)
    if not entries:
        return None
    if not isinstance(entries[0], dict):
        raise PlaneError(_FORMAT_ERROR)
    return entries[0]


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
        *,
        limiter: Optional[RateLimiter] = None,
    ) -> None:
        if per_minute < 1:
            raise ValueError("per_minute는 1 이상이어야 한다")
        self.calls = 0
        self._settings = settings
        self._base_url = settings.base_url.rstrip("/")
        parsed_url = urllib.parse.urlsplit(self._base_url)
        if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
            raise PlaneError(f"설정 오류: {_ENV_URL}")
        self._workspace = _seg(settings.workspace)
        self._sleep = sleep
        self._notify = notify if notify is not None else _notify_stderr
        self._limiter: RateLimiter = (
            limiter if limiter is not None else LocalRateLimiter(per_minute=per_minute, clock=clock, sleep=sleep)
        )
        if parsed_url.hostname in ("127.0.0.1", "localhost", "::1"):
            self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
        else:
            self._opener = urllib.request.build_opener(_NoRedirect)

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

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[dict[str, Any]] = None,
        query: Optional[dict[str, str]] = None,
        *,
        retry_transient: bool = True,
    ) -> Any:
        """429는 항상 다시 보낸다. 5xx·연결 오류는 `retry_transient`일 때만 다시 보낸다.

        생성 POST는 `retry_transient=False`로 부른다. 서버가 이미 만들었을 수 있어 다시 보내면 중복된다.
        """
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
            self._limiter.acquire()
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            self.calls += 1
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
                if status == 409:
                    try:
                        body = json.loads(exc.read().decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        body = None
                    if isinstance(body, dict) and isinstance(body.get("id"), str):
                        exc.close()
                        raise PlaneDuplicate(body["id"]) from None
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
                    if retry_transient and attempt < len(_RETRY_DELAYS):
                        self._sleep(_RETRY_DELAYS[attempt])
                        attempt += 1
                        continue
                    raise PlaneUnavailable(f"Plane 요청 실패: HTTP {status}") from None
                raise PlaneError(f"Plane 요청 오류: HTTP {status}") from None
            except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
                if retry_transient and attempt < len(_RETRY_DELAYS):
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
        *,
        retry_transient: bool = True,
    ) -> dict[str, Any]:
        result = self._request(method, path, payload=payload, retry_transient=retry_transient)
        if not isinstance(result, dict):
            raise PlaneError(_FORMAT_ERROR)
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
                raise PlaneError(_LIST_FORMAT_ERROR)
            results = data.get("results", [])
            if not isinstance(results, list):
                raise PlaneError(_LIST_FORMAT_ERROR)
            items.extend(entry for entry in results if isinstance(entry, dict))
            next_cursor = data.get("next_cursor")
            if not data.get("next_page_results") or not isinstance(next_cursor, str) or not next_cursor:
                break
            cursor = next_cursor
        return items

    def whoami(self) -> str:
        """현재 API 키 사용자의 이메일을 돌려준다."""
        data = self._request_dict("GET", "/api/v1/users/me/")
        return _required_text(data, "email")

    def whoami_id(self) -> str:
        """현재 API 키 사용자의 id를 돌려준다. 담당자 지정과 댓글 작성자 비교에 쓴다."""
        data = self._request_dict("GET", "/api/v1/users/me/")
        return _required_text(data, "id")

    def whoami_profile(self) -> dict[str, str]:
        data = self._request_dict("GET", "/api/v1/users/me/")
        return {key: _required_text(data, key) for key in ("id", "display_name")}

    def list_projects(self) -> list[dict[str, Any]]:
        return self._list_paginated(self._projects_path())

    def list_project_infos(self) -> list[ProjectInfo]:
        """프로젝트 목록을 공통 `ProjectInfo` 목록으로 변환한다."""
        return [
            ProjectInfo(
                id=_required_text(project, "id"),
                identifier=_required_text(project, "identifier"),
                name=_text(project, "name"),
            )
            for project in self.list_projects()
        ]

    def current_cycle(self, project_id: str) -> Optional[CycleInfo]:
        """프로젝트의 현재 cycle을 돌려준다. 없으면 `None`이다."""
        data = self._request("GET", self._projects_path(project_id, "cycles"), query={"cycle_view": "current"})
        first = _first_cycle_entry(data)
        if first is None:
            return None
        return CycleInfo(
            id=_text(first, "id"),
            name=_text(first, "name"),
            start_date=_cycle_time(first, "start_date"),
            end_date=_cycle_time(first, "end_date"),
            total_issues=_int_or_zero(first, "total_issues"),
            completed_issues=_int_or_zero(first, "completed_issues"),
        )

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

    def list_states(self, project_id: str) -> list[dict[str, Any]]:
        return self._list_paginated(self._projects_path(project_id, "states"))

    def list_labels(self, project_id: str) -> list[dict[str, Any]]:
        return self._list_paginated(self._projects_path(project_id, "labels"))

    def create_item(
        self,
        project_id: str,
        name: str,
        description_html: str,
        label_ids: list[str],
        *,
        parent: Optional[str] = None,
        state: Optional[str] = None,
        external_source: Optional[str] = None,
        external_id: Optional[str] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": name,
            "description_html": description_html,
            "labels": list(label_ids),
        }
        optional = {
            "parent": parent,
            "state": state,
            "external_source": external_source,
            "external_id": external_id,
        }
        payload.update({key: value for key, value in optional.items() if value is not None})
        path = self._projects_path(project_id, "work-items")
        return self._request_dict("POST", path, payload=payload, retry_transient=False)

    def update_item(self, project_id: str, item_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        """허용한 필드만 PATCH한다. `None` 값은 그 필드를 비운다."""
        check_update_fields(fields)
        return self._request_dict(
            "PATCH",
            self._projects_path(project_id, "work-items", item_id),
            payload=dict(fields),
        )

    def find_item_by_external(self, project_id: str, source: str, external_id: str) -> Optional[dict[str, Any]]:
        # 목록 전체를 받아 거른다. 필터 질의는 실서버에서 확인하기 전에는 쓰지 않는다.
        return match_external(self.list_items(project_id), source, external_id)

    def list_comments(self, project_id: str, item_id: str) -> list[dict[str, Any]]:
        return self._list_paginated(self._projects_path(project_id, "work-items", item_id, "comments"))

    def create_comment(
        self,
        project_id: str,
        item_id: str,
        html: str,
        *,
        external_source: Optional[str] = None,
        external_id: Optional[str] = None,
    ) -> dict[str, Any]:
        payload = {"comment_html": html}
        if external_source is not None:
            payload["external_source"] = external_source
        if external_id is not None:
            payload["external_id"] = external_id
        return self._request_dict(
            "POST",
            self._projects_path(project_id, "work-items", item_id, "comments"),
            payload=payload,
            retry_transient=False,
        )

    def resolve_code(self, code: str) -> tuple[str, dict[str, Any]]:
        return find_by_code(code, self.list_project_infos, self.list_items)

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
