"""오프라인 작업 일지와 전송 상태를 관리한다."""

from __future__ import annotations

import fcntl
import json
import os
import socket
import sys
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterator, Mapping, Optional, cast

from .secrets import find_exposures

MAX_TEXT = 400


@dataclass(frozen=True)
class Entry:
    id: str
    at: str
    host: str
    source: str
    text: str
    workspace: str
    repo: str = ""
    item: str = ""
    session: str = ""

    def local_date(self) -> date:
        return datetime.fromisoformat(self.at).date()

    def month(self) -> str:
        return self.local_date().strftime("%Y-%m")


@dataclass(frozen=True)
class RemoteEntry:
    entry: Entry
    author: str
    comment_id: str


class WorklogError(Exception):
    pass


def validate(text: str, host: str, repo: str, item: str) -> None:
    if not text.strip():
        raise WorklogError("본문이 비어 있습니다")
    if len(text) > MAX_TEXT:
        raise WorklogError(f"본문은 {MAX_TEXT}자 이하여야 합니다")
    patterns = {name for value in (text, host, repo, item) for name in find_exposures(value)}
    if patterns:
        raise WorklogError(", ".join(sorted(patterns)))


def host_label(env: Mapping[str, str]) -> str:
    return env.get("FORGE_LEDGER_WORKLOG_HOST", "") or socket.gethostname().split(".")[0]


def new_entry(
    text: str,
    *,
    source: str,
    env: Mapping[str, str],
    now: datetime,
    repo: str = "",
    item: str = "",
    session: str = "",
) -> Entry:
    host = host_label(env)
    validate(text, host, repo, item)
    return Entry(
        id=uuid.uuid4().hex,
        at=now.astimezone().isoformat(timespec="seconds"),
        host=host,
        source=source,
        text=text,
        workspace=env.get("FORGE_LEDGER_PLANE_WORKSPACE", ""),
        repo=repo,
        item=item,
        session=session,
    )


class LocalLog:
    def __init__(self, state_dir: Path) -> None:
        self._dir = state_dir / "worklog"
        self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock_path = self._dir / "worklog.lock"
        self._sent_path = self._dir / "sent.json"

    @contextmanager
    def _lock(self) -> Iterator[None]:
        fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def append(self, entry: Entry) -> None:
        validate(entry.text, entry.host, entry.repo, entry.item)
        with self._lock():
            fd = os.open(self._dir / f"{entry.month()}.jsonl", os.O_CREAT | os.O_RDWR | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a+b") as stream:
                stream.seek(0, os.SEEK_END)
                if stream.tell():
                    stream.seek(-1, os.SEEK_END)
                    if stream.read(1) != b"\n":
                        stream.write(b"\n")
                stream.write((json.dumps(asdict(entry), ensure_ascii=False) + "\n").encode("utf-8"))

    def _entries(self) -> list[Entry]:
        result: list[Entry] = []
        for path in sorted(self._dir.glob("????-??.jsonl")):
            with path.open("rb") as stream:
                for number, line in enumerate(stream, 1):
                    try:
                        entry = Entry(**json.loads(line.decode("utf-8")))
                        datetime.fromisoformat(entry.at)
                    except (UnicodeDecodeError, ValueError, TypeError):
                        print(f"손상된 일지 건너뜀: {path.name}:{number}", file=sys.stderr)
                        continue
                    result.append(entry)
        return sorted(result, key=lambda entry: datetime.fromisoformat(entry.at))

    def _sent(self) -> dict[str, str]:
        try:
            with self._sent_path.open(encoding="utf-8") as stream:
                return cast(dict[str, str], json.load(stream))
        except FileNotFoundError:
            return {}

    def entries(self, from_: date, to: date) -> list[Entry]:
        with self._lock():
            return [entry for entry in self._entries() if from_ <= entry.local_date() <= to]

    def pending(self, workspace: str) -> list[Entry]:
        with self._lock():
            sent = self._sent()
            return [entry for entry in self._entries() if entry.id not in sent and entry.workspace in (workspace, "")]

    def pending_other(self, workspace: str) -> int:
        with self._lock():
            sent = self._sent()
            return sum(entry.id not in sent and entry.workspace not in (workspace, "") for entry in self._entries())

    def mark_sent(self, entry_id: str, comment_id: str) -> None:
        with self._lock():
            sent = self._sent()
            sent[entry_id] = comment_id
            temporary = self._dir / f".sent.{uuid.uuid4().hex}.tmp"
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(sent, stream, ensure_ascii=False)
                os.replace(temporary, self._sent_path)
            finally:
                temporary.unlink(missing_ok=True)

    def last_for_session(self, session: str) -> Optional[Entry]:
        with self._lock():
            return next((entry for entry in reversed(self._entries()) if entry.session == session), None)
