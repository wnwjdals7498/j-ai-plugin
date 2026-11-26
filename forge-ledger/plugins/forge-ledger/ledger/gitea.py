"""Gitea 읽기 클라이언트(ForgeAdapter 구현)."""

from __future__ import annotations

import http.client
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

from ledger.adapters import IssueInfo, PullInfo, RepoInfo, RunInfo, parse_run_path, to_utc_iso
from ledger.ratelimit import LocalRateLimiter, RateLimiter
from ledger.secrets import find_secrets

_ENV_URL = "FORGE_LEDGER_GITEA_URL"
_ENV_AUTH = "FORGE_LEDGER_GITEA_TOKEN"  # 환경 변수 이름
_RETRY_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0)
_RATE_LIMIT_DEFAULT_WAIT = 60.0
_RATE_LIMIT_MAX_WAIT = 65.0
_RATE_LIMIT_RETRIES = 3
_PER_MINUTE = 120
_PAGE_SIZE = 50
_DEFAULT_TIMEOUT = 30.0
_FORMAT_ERROR = "Gitea 응답 형식 오류"


class GiteaError(Exception):
    """Gitea API 요청 중 발생한 일반 오류."""


class GiteaAuthError(GiteaError):
    """Gitea API 인증 또는 권한 오류."""


class GiteaUnavailable(GiteaError):
    """Gitea 연결 실패 또는 재시도 후에도 복구되지 않은 서버/속도 오류."""


@dataclass(frozen=True)
class GiteaSettings:
    """Gitea API 접속 설정."""

    base_url: str
    token: str = field(repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> GiteaSettings:
        values: list[str] = []
        for key in (_ENV_URL, _ENV_AUTH):
            raw = env.get(key, "").strip()
            if not raw:
                raise GiteaError(f"설정 없음: {key}")
            values.append(raw)
        url, token = values
        return cls(base_url=url, token=token)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """리다이렉트를 거부한다. urllib은 Authorization 헤더를 다른 호스트로도 보낸다."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


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


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GiteaError(_FORMAT_ERROR)
    return value


def _objects(value: Any) -> list[dict[str, Any]]:
    """객체 목록. Gitea가 빈 목록을 null로 주는 경우는 빈 목록으로 본다."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise GiteaError(_FORMAT_ERROR)
    return [_object(entry) for entry in value]


def _text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    return value if isinstance(value, str) else ""


def _time(data: dict[str, Any], key: str) -> str:
    try:
        return to_utc_iso(_text(data, key))
    except ValueError:
        raise GiteaError(_FORMAT_ERROR) from None


def _number(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise GiteaError(_FORMAT_ERROR)
    return value


def _issue_info(data: dict[str, Any]) -> IssueInfo:
    return IssueInfo(
        number=_number(data, "number"),
        title=_text(data, "title"),
        labels=tuple(_text(label, "name") for label in _objects(data.get("labels"))),
        created_at=_time(data, "created_at"),
    )


def _pull_info(data: dict[str, Any]) -> PullInfo:
    return PullInfo(
        number=_number(data, "number"),
        title=_text(data, "title"),
        draft=bool(data.get("draft")),
        created_at=_time(data, "created_at"),
    )


def _run_info(data: dict[str, Any]) -> RunInfo:
    workflow, ref = parse_run_path(_text(data, "path"))
    return RunInfo(
        run_id=_number(data, "id"),
        run_number=_number(data, "run_number"),
        event=_text(data, "event"),
        status=_text(data, "status"),
        conclusion=_text(data, "conclusion"),
        workflow=workflow,
        ref=ref,
        head_sha=_text(data, "head_sha"),
        pull_numbers=tuple(_number(pull, "number") for pull in _objects(data.get("pull_requests"))),
        started_at=_time(data, "started_at"),
    )


class GiteaClient:
    """표준 라이브러리 urllib 기반 Gitea API 읽기 클라이언트."""

    def __init__(
        self,
        settings: GiteaSettings,
        *,
        limiter: Optional[RateLimiter] = None,
        sleep: Callable[[float], None] = time.sleep,
        notify: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.calls = 0
        self._settings = settings
        self._base_url = settings.base_url.rstrip("/")
        parsed_url = urllib.parse.urlsplit(self._base_url)
        if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
            raise GiteaError(f"설정 오류: {_ENV_URL}")
        self._sleep = sleep
        self._notify = notify if notify is not None else _notify_stderr
        self._limiter: RateLimiter = (
            limiter if limiter is not None else LocalRateLimiter(per_minute=_PER_MINUTE, sleep=sleep)
        )
        if parsed_url.hostname in ("127.0.0.1", "localhost", "::1"):
            self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
        else:
            self._opener = urllib.request.build_opener(_NoRedirect)

    def _sanitize(self, text: str) -> str:
        token = self._settings.token
        cleaned = text.replace(token, "[REDACTED]") if token else text
        if find_secrets(cleaned):
            return ""
        return cleaned.strip()

    def _get(self, path: str, query: Optional[dict[str, str]] = None) -> tuple[Any, Optional[str]]:
        """GET 한 번의 (JSON 본문, X-Total-Count 헤더)를 돌려준다. 429·5xx·연결 오류는 다시 시도한다."""
        url = f"{self._base_url}/api/v1{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        headers = {"Authorization": f"token {self._settings.token}", "Accept": "application/json"}

        attempt = 0
        rate_limited = 0
        while True:
            self._limiter.acquire()
            req = urllib.request.Request(url, headers=headers, method="GET")
            self.calls += 1
            try:
                with self._opener.open(req, timeout=_DEFAULT_TIMEOUT) as resp:
                    raw = resp.read()
                    total = resp.headers.get("X-Total-Count")
                try:
                    return json.loads(raw.decode("utf-8")), total
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise GiteaError("Gitea 응답 JSON 파싱 실패") from None
            except urllib.error.HTTPError as exc:
                status = exc.code
                retry_after = exc.headers.get("Retry-After") if exc.headers is not None else None
                exc.close()
                if status in (401, 403):
                    raise GiteaAuthError("Gitea 토큰 확인 필요") from None
                if status == 429:
                    if rate_limited < _RATE_LIMIT_RETRIES:
                        rate_limited += 1
                        wait = _retry_after_seconds(retry_after)
                        self._notify(f"Gitea 호출 한도에 걸려 {int(wait)}초 기다립니다")
                        self._sleep(wait)
                        continue
                    raise GiteaUnavailable("Gitea 요청 실패: HTTP 429") from None
                if 500 <= status <= 599:
                    if attempt < len(_RETRY_DELAYS):
                        self._sleep(_RETRY_DELAYS[attempt])
                        attempt += 1
                        continue
                    raise GiteaUnavailable(f"Gitea 요청 실패: HTTP {status}") from None
                raise GiteaError(f"Gitea 요청 오류: HTTP {status}") from None
            except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
                if attempt < len(_RETRY_DELAYS):
                    self._sleep(_RETRY_DELAYS[attempt])
                    attempt += 1
                    continue
                raw_reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
                detail = self._sanitize(str(raw_reason))
                message = f"Gitea 연결 실패: {detail}" if detail else "Gitea 연결 실패"
                raise GiteaUnavailable(message) from None
            except ValueError:
                # http.client의 잘못된 헤더 오류 문구에는 Authorization 값이 그대로 들어간다.
                raise GiteaError("Gitea 요청 구성 오류: 주소와 토큰 형식 확인 필요") from None

    def _list_paginated(self, path: str, query: dict[str, str]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            data, total = self._get(path, {**query, "page": str(page), "limit": str(_PAGE_SIZE)})
            batch = _objects(data)
            items.extend(batch)
            if len(batch) < _PAGE_SIZE:
                break
            if total is not None and total.isdecimal() and len(items) >= int(total):
                break
            page += 1
        return items

    def _repo_path(self, owner: str, name: str) -> str:
        return f"/repos/{_seg(owner)}/{_seg(name)}"

    def whoami(self) -> str:
        data, _ = self._get("/user")
        return _text(_object(data), "login")

    def get_repo(self, owner: str, name: str) -> RepoInfo:
        data, _ = self._get(self._repo_path(owner, name))
        repo = _object(data)
        return RepoInfo(
            full_name=_text(repo, "full_name"),
            default_branch=_text(repo, "default_branch"),
            html_url=_text(repo, "html_url"),
        )

    def list_open_issues(self, owner: str, name: str) -> list[IssueInfo]:
        path = f"{self._repo_path(owner, name)}/issues"
        return [_issue_info(d) for d in self._list_paginated(path, {"state": "open", "type": "issues"})]

    def list_open_pulls(self, owner: str, name: str) -> list[PullInfo]:
        path = f"{self._repo_path(owner, name)}/pulls"
        return [_pull_info(d) for d in self._list_paginated(path, {"state": "open"})]

    def list_runs(self, owner: str, name: str, limit: int = 50) -> list[RunInfo]:
        data, _ = self._get(f"{self._repo_path(owner, name)}/actions/runs", {"limit": str(limit)})
        return [_run_info(d) for d in _objects(_object(data).get("workflow_runs"))]
