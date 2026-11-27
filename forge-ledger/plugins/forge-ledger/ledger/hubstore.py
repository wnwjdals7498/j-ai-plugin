"""허브 SQLite 저장소와 등록 목록. SQLite는 다시 만들 수 있는 사본이고, 등록 목록은 DB 밖에 둔다."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

from .filelock import locked

WORKSPACE_REPO_ID = 0
SCHEMA_VERSION = 1
DB_NAME = "hub.sqlite3"
REGISTRY_NAME = "hub-repos.json"
LOCK_NAME = "hub-sync.lock"
DB_LOCK_NAME = "hub-db.lock"  # 스키마 확인·재생성 전용. 대조 잠금과 따로 둔다

_REGISTRY_VERSION = 1
_BUSY_TIMEOUT_SECONDS = 10

_SCHEMA = (
    """CREATE TABLE source (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,
        base_url_digest TEXT NOT NULL,
        UNIQUE (kind, base_url_digest)
    )""",
    """CREATE TABLE repo (
        id INTEGER PRIMARY KEY,
        owner TEXT NOT NULL,
        name TEXT NOT NULL,
        default_branch TEXT NOT NULL DEFAULT '',
        html_url TEXT NOT NULL DEFAULT '',
        local_paths TEXT NOT NULL,
        plane_workspace TEXT NOT NULL,
        registered_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        UNIQUE (owner, name)
    )""",
    # 이슈 번호와 Plane 프로젝트 키는 저장소끼리 겹치므로 repo_id까지 묶어 구분한다.
    """CREATE TABLE entity (
        id INTEGER PRIMARY KEY,
        source_id INTEGER NOT NULL,
        repo_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        ext_key TEXT NOT NULL,
        state TEXT NOT NULL,
        parent_id INTEGER,
        attrs TEXT NOT NULL,
        first_seen_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (source_id, repo_id, kind, ext_key)
    )""",
    """CREATE TABLE sync_run (
        id INTEGER PRIMARY KEY,
        source_id INTEGER NOT NULL,
        repo_id INTEGER NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        result TEXT NOT NULL DEFAULT '',
        api_calls INTEGER NOT NULL DEFAULT 0,
        error_kind TEXT NOT NULL DEFAULT ''
    )""",
)


@dataclass(frozen=True)
class EntityRow:
    kind: str
    ext_key: str
    state: str
    attrs: dict[str, Any]
    parent: Optional[tuple[str, str]] = None  # (부모 kind, 부모 ext_key)


@dataclass(frozen=True)
class EntityView:
    ext_key: str
    state: str
    attrs: dict[str, Any]
    parent_ext_key: Optional[str]


@dataclass(frozen=True)
class RepoRow:
    id: int
    owner: str
    name: str
    default_branch: str
    html_url: str
    local_paths: tuple[str, ...]
    plane_workspace: str


@dataclass(frozen=True)
class RunRow:
    source_kind: str
    repo_id: int
    started_at: str
    finished_at: Optional[str]
    result: str
    api_calls: int
    error_kind: str


def url_digest(base_url: str) -> str:
    """주소 원문을 저장하지 않기 위한 소스 구분자(sha256 앞 16자)."""
    return hashlib.sha256(base_url.rstrip("/").encode("utf-8")).hexdigest()[:16]


def _make_state_dir(state_dir: Path) -> None:
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    """여러 프로세스(와 스레드)를 한 번에 하나씩 들여보낸다."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with locked(fd):
            yield
    finally:
        os.close(fd)  # 닫으면 잠금도 풀린다


def _create_private_file(path: Path) -> None:
    """파일이 없을 때만 0600으로 만든다.

    있는 DB 파일을 열었다 닫으면 같은 프로세스의 SQLite 연결이 잡은 POSIX 잠금이 모두 풀린다.
    """
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return
    os.close(fd)


@contextmanager
def _open(path: Path) -> Iterator[sqlite3.Connection]:
    """작업 하나에 쓰는 연결. 열린 트랜잭션은 COMMIT하지 않으면 닫을 때 버려진다."""
    conn = sqlite3.connect(path, timeout=_BUSY_TIMEOUT_SECONDS, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_SECONDS * 1000}")
        conn.execute("PRAGMA journal_mode=WAL")
        yield conn
    finally:
        conn.close()


class HubStore:
    def __init__(self, state_dir: Path) -> None:
        _make_state_dir(state_dir)
        self.path = state_dir / DB_NAME
        with _exclusive_lock(state_dir / DB_LOCK_NAME):
            self._ensure_schema()

    def _ensure_schema(self) -> None:
        try:
            if self._init_schema():
                return
        except sqlite3.OperationalError:
            raise  # 잠김·권한·디스크 오류는 파일이 깨진 것이 아니므로 지우지 않는다
        except sqlite3.DatabaseError:
            pass
        for suffix in ("", "-wal", "-shm"):
            Path(f"{self.path}{suffix}").unlink(missing_ok=True)
        self._init_schema()

    def _init_schema(self) -> bool:
        """스키마를 맞춘다. 다른 버전의 파일이면 False를 돌려준다."""
        _create_private_file(self.path)
        with _open(self.path) as conn:
            # 이미 맞는 파일이면 쓰기 잠금을 잡지 않는다(대조 중에도 읽기가 바로 열린다).
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version == SCHEMA_VERSION:
                return True
            if version != 0 or conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone() is not None:
                return False
            conn.execute("BEGIN IMMEDIATE")
            for statement in _SCHEMA:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("COMMIT")
            return True

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with _open(self.path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")

    def source_id(self, kind: str, base_url: str) -> int:
        digest = url_digest(base_url)
        with self._write() as conn:
            conn.execute("INSERT OR IGNORE INTO source (kind, base_url_digest) VALUES (?, ?)", (kind, digest))
            row = conn.execute(
                "SELECT id FROM source WHERE kind = ? AND base_url_digest = ?", (kind, digest)
            ).fetchone()
            return int(row["id"])

    def upsert_repo(self, owner: str, name: str, local_path: str, plane_workspace: str) -> int:
        with self._write() as conn:
            row = conn.execute(
                "SELECT id, local_paths FROM repo WHERE owner = ? AND name = ?", (owner, name)
            ).fetchone()
            paths = set(json.loads(row["local_paths"])) if row is not None else set()
            paths.add(local_path)
            encoded = json.dumps(sorted(paths), ensure_ascii=False)
            if row is None:
                cursor = conn.execute(
                    "INSERT INTO repo (owner, name, local_paths, plane_workspace) VALUES (?, ?, ?, ?)",
                    (owner, name, encoded, plane_workspace),
                )
                if cursor.lastrowid is None:
                    raise RuntimeError("허브 저장소 쓰기 실패")
                return cursor.lastrowid
            conn.execute(
                "UPDATE repo SET local_paths = ?, plane_workspace = ? WHERE id = ?",
                (encoded, plane_workspace, row["id"]),
            )
            return int(row["id"])

    def set_repo_meta(self, repo_id: int, default_branch: str, html_url: str) -> None:
        with self._write() as conn:
            conn.execute(
                "UPDATE repo SET default_branch = ?, html_url = ? WHERE id = ?", (default_branch, html_url, repo_id)
            )

    def repos(self) -> list[RepoRow]:
        with _open(self.path) as conn:
            rows = conn.execute("SELECT * FROM repo ORDER BY owner, name").fetchall()
        return [
            RepoRow(
                id=row["id"],
                owner=row["owner"],
                name=row["name"],
                default_branch=row["default_branch"],
                html_url=row["html_url"],
                local_paths=tuple(json.loads(row["local_paths"])),
                plane_workspace=row["plane_workspace"],
            )
            for row in rows
        ]

    def find_repo_by_path(self, local_path: str) -> Optional[RepoRow]:
        return next((repo for repo in self.repos() if local_path in repo.local_paths), None)

    def start_run(self, source_id: int, repo_id: int, now: str) -> int:
        with self._write() as conn:
            cursor = conn.execute(
                "INSERT INTO sync_run (source_id, repo_id, started_at) VALUES (?, ?, ?)", (source_id, repo_id, now)
            )
            if cursor.lastrowid is None:
                raise RuntimeError("허브 저장소 쓰기 실패")
            return cursor.lastrowid

    def finish_run(self, run_id: int, result: str, api_calls: int, error_kind: str, now: str) -> None:
        with self._write() as conn:
            conn.execute(
                "UPDATE sync_run SET result = ?, api_calls = ?, error_kind = ?, finished_at = ? WHERE id = ?",
                (result, api_calls, error_kind, now, run_id),
            )

    def apply_kind(
        self,
        source_id: int,
        repo_id: int,
        kind: str,
        rows: Sequence[EntityRow],
        now: str,
        close_missing: bool = True,
    ) -> None:
        """한 종류의 조회 결과를 한 트랜잭션으로 반영한다. 부모는 먼저 반영돼 있어야 한다."""
        with self._write() as conn:
            for row in rows:
                if row.kind != kind:
                    raise ValueError(f"{kind} 반영에 {row.kind} 행이 섞였다")
                parent_id = None
                if row.parent is not None:
                    found = conn.execute(
                        "SELECT id FROM entity WHERE source_id = ? AND repo_id = ? AND kind = ? AND ext_key = ?",
                        (source_id, repo_id, *row.parent),
                    ).fetchone()
                    if found is None:
                        raise ValueError(f"부모 엔터티가 없다: {row.parent[0]} {row.parent[1]}")
                    parent_id = found["id"]
                conn.execute(
                    """INSERT INTO entity
                        (source_id, repo_id, kind, ext_key, state, parent_id, attrs, first_seen_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (source_id, repo_id, kind, ext_key) DO UPDATE SET
                        state = excluded.state,
                        parent_id = excluded.parent_id,
                        attrs = excluded.attrs,
                        updated_at = excluded.updated_at""",
                    (
                        source_id,
                        repo_id,
                        kind,
                        row.ext_key,
                        row.state,
                        parent_id,
                        json.dumps(row.attrs, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
            if close_missing:
                seen = {row.ext_key for row in rows}
                open_rows = conn.execute(
                    "SELECT id, ext_key FROM entity "
                    "WHERE source_id = ? AND repo_id = ? AND kind = ? AND state = 'open'",
                    (source_id, repo_id, kind),
                ).fetchall()
                conn.executemany(
                    "UPDATE entity SET state = 'closed', updated_at = ? WHERE id = ?",
                    [(now, found["id"]) for found in open_rows if found["ext_key"] not in seen],
                )

    def retire_worklog(self, source_id: int, seen: set[str], months: set[str], now: str) -> int:
        """조회한 달에서 사라진 기록만 삭제 상태로 바꾼다."""
        with self._write() as conn:
            rows = conn.execute(
                "SELECT id, ext_key, json_extract(attrs, '$.month') AS month FROM entity "
                "WHERE source_id = ? AND repo_id = ? AND kind = 'worklog' AND state = 'open'",
                (source_id, WORKSPACE_REPO_ID),
            ).fetchall()
            deleted = [(now, row["id"]) for row in rows if row["month"] in months and row["ext_key"] not in seen]
            conn.executemany("UPDATE entity SET state = 'deleted', updated_at = ? WHERE id = ?", deleted)
            return len(deleted)

    def retire_other_worklog_sources(self, source_id: int, now: str) -> None:
        """전체 수집이 성공한 뒤 이전 소스의 열린 일지를 삭제 상태로 바꾼다."""
        with self._write() as conn:
            conn.execute(
                "UPDATE entity SET state = 'deleted', updated_at = ? "
                "WHERE repo_id = ? AND kind = 'worklog' AND state = 'open' AND source_id != ?",
                (now, WORKSPACE_REPO_ID, source_id),
            )

    def close_other_sources(self, source_id: int, repo_id: int, kind: str, now: str) -> None:
        """서버 주소가 바뀌기 전의 같은 종류 소스에 남은, 이 저장소·kind의 열린 행을 닫는다."""
        with self._write() as conn:
            conn.execute(
                """UPDATE entity SET state = 'closed', updated_at = ?
                WHERE repo_id = ? AND kind = ? AND state = 'open' AND source_id IN (
                    SELECT old.id FROM source AS old JOIN source AS cur ON cur.kind = old.kind
                    WHERE cur.id = ? AND old.id != cur.id
                )""",
                (now, repo_id, kind, source_id),
            )

    def entities(self, repo_id: int, kind: str, states: Optional[Sequence[str]] = ("open",)) -> list[EntityView]:
        """`states`가 None이면 모든 상태를 돌려준다."""
        with _open(self.path) as conn:
            rows = conn.execute(
                """SELECT e.ext_key, e.state, e.attrs, p.ext_key AS parent_ext_key
                FROM entity AS e LEFT JOIN entity AS p ON p.id = e.parent_id
                WHERE e.repo_id = ? AND e.kind = ? ORDER BY e.id""",
                (repo_id, kind),
            ).fetchall()
        return [
            EntityView(row["ext_key"], row["state"], json.loads(row["attrs"]), row["parent_ext_key"])
            for row in rows
            if states is None or row["state"] in states
        ]

    def last_run(self, repo_id: int, source_kind: str) -> Optional[RunRow]:
        """끝나지 않은 실행도 포함한 마지막 실행."""
        return self._latest_run(repo_id, source_kind, good_only=False)

    def last_good_run(self, repo_id: int, source_kind: str) -> Optional[RunRow]:
        return self._latest_run(repo_id, source_kind, good_only=True)

    def _latest_run(self, repo_id: int, source_kind: str, good_only: bool) -> Optional[RunRow]:
        with _open(self.path) as conn:
            row = conn.execute(
                """SELECT r.* FROM sync_run AS r JOIN source AS s ON s.id = r.source_id
                WHERE r.repo_id = ? AND s.kind = ? AND (? = 0 OR r.result IN ('ok', 'partial'))
                ORDER BY r.id DESC LIMIT 1""",
                (repo_id, source_kind, int(good_only)),
            ).fetchone()
        if row is None:
            return None
        return RunRow(
            source_kind=source_kind,
            repo_id=row["repo_id"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            result=row["result"],
            api_calls=row["api_calls"],
            error_kind=row["error_kind"],
        )


def load_registry(state_dir: Path) -> list[str]:
    """등록된 로컬 경로. 파일이 없거나 형식이 다르면 빈 목록이다."""
    try:
        data = json.loads((state_dir / REGISTRY_NAME).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return []
    if not isinstance(data, dict) or data.get("version") != _REGISTRY_VERSION:
        return []
    paths = data.get("paths")
    if not isinstance(paths, list) or not all(isinstance(item, str) for item in paths):
        return []
    return list(paths)


def save_registry(state_dir: Path, paths: Sequence[str]) -> None:
    """임시 파일을 0600으로 만들어 쓴 뒤 바꿔치기한다(읽는 쪽이 쓰다 만 파일을 보지 않게)."""
    _make_state_dir(state_dir)
    tmp = state_dir / f".{REGISTRY_NAME}.{uuid.uuid4().hex}.tmp"
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"version": _REGISTRY_VERSION, "paths": sorted(set(paths))}, fh, ensure_ascii=False)
        os.replace(tmp, state_dir / REGISTRY_NAME)
    finally:
        tmp.unlink(missing_ok=True)
