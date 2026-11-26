"""작업 이어받기: 착수·인계·검증·종료를 작업 항목의 상태와 댓글로 남긴다.

PM 도구는 `adapters.WorkTracker` 규격으로만 다룬다. 댓글 첫 줄이 기록의 형식이고, 점유 여부는 댓글에서 읽는다.
"""

from __future__ import annotations

import html
import re
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from ledger.adapters import WorkTracker
from ledger.flows import SecretFound
from ledger.migrate import parse_comment_time
from ledger.plane import PlaneError
from ledger.secrets import find_exposures
from ledger.statusmap import LABEL_COLOR, STATUS_MAP, pick_state

CLAIM_STALE_MINUTES = 30
NOTE_MAX_CHARS = 400
_STALE_AFTER = timedelta(minutes=CLAIM_STALE_MINUTES)
# 종료 방식 → (원천 상태, 함께 있어야 하는 값). 값 이름은 `[종료]` 줄의 표시 이름이기도 하다.
_END_MODES: dict[str, tuple[str, tuple[str, ...]]] = {
    "done": ("Done", ("result",)),
    "pause": ("Paused", ()),
    "fail": ("Failed", ("cause", "fix")),
    "skip": ("Skipped", ("reason",)),
}
_STATUS_LABELS = frozenset(label for _, label in STATUS_MAP.values() if label)
_RECORD_RE = re.compile(r"\[(착수 취소|착수|인계|검증|종료)\] (.*)")
_BREAK_RE = re.compile(r"<br\s*/?>|</p>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]*>")
_VERIFY_HEAD_RE = re.compile(r"head=([0-9a-f]+) ")
# `[착수 취소]`와 `[종료]` 첫 줄 끝에 붙는, 대상 착수 댓글의 id
_REF_RE = re.compile(r" ref=(\S+)$")


class WorkError(Exception):
    """작업 명령의 규칙 위반."""


@dataclass(frozen=True)
class _Record:
    """작업 댓글 하나. `at`은 서버가 정한 작성 시각이라 컴퓨터마다 시계가 달라도 순서가 같다."""

    kind: str  # 착수 | 착수 취소 | 인계 | 검증 | 종료
    detail: str  # 첫 줄에서 `[종류] ` 뒤의 글
    at: datetime
    comment_id: str
    actor: str  # 작성한 사용자 id

    @property
    def host(self) -> str:
        """`착수` 줄의 `<host> <시각>`에서 host."""
        return self.detail.rsplit(" ", 1)[0]

    @property
    def mode(self) -> str:
        """`종료` 줄의 종료 방식."""
        return self.detail.split(" ", 1)[0]

    @property
    def ref(self) -> str:
        """가리키는 착수 댓글 id. 없으면 빈 문자열이다."""
        found = _REF_RE.search(self.detail)
        return found.group(1) if found else ""


# --- 댓글 읽기·쓰기 ---


def _first_line(comment_html: str) -> str:
    text = html.unescape(_TAG_RE.sub("", _BREAK_RE.sub("\n", comment_html)))
    return next((line.strip() for line in text.split("\n") if line.strip()), "")


def _records(t: WorkTracker, project_id: str, item_id: str) -> list[_Record]:
    """작업 댓글을 작성 시각순으로. 서버가 돌려주는 순서는 믿지 않는다."""
    records = []
    for comment in t.list_comments(project_id, item_id):
        parsed = _RECORD_RE.fullmatch(_first_line(str(comment.get("comment_html", ""))))
        if parsed:
            records.append(
                _Record(
                    kind=parsed.group(1),
                    detail=parsed.group(2),
                    at=parse_comment_time(str(comment.get("created_at", ""))),
                    comment_id=str(comment.get("id", "")),
                    actor=str(comment.get("actor", "")),
                )
            )
    return sorted(records, key=lambda r: (r.at, r.comment_id))


def _cancelled(records: list[_Record]) -> set[str]:
    """`[착수 취소]`가 `ref=`로 가리키는 착수 댓글 id. 작성 시각이나 정렬 순서와 상관없다."""
    return {record.ref for record in records if record.kind == "착수 취소" and record.ref}


def _starts(records: list[_Record]) -> list[_Record]:
    """취소되지 않은 착수. 끝난 것도 포함한다."""
    cancelled = _cancelled(records)
    return [r for r in records if r.kind == "착수" and r.comment_id not in cancelled]


def _active(records: list[_Record]) -> list[_Record]:
    """아직 유효한 착수·인계·검증. 취소됐거나 끝난 착수는 빼고, `[종료]`는 그 앞의 기록을 모두 끝낸다.

    `ref`로 가리킨 착수는 정렬 위치와 상관없이 빠진다. `ref`가 있는 `[종료]`와 작성 시각이 같은 다른 착수는
    앞뒤를 알 수 없으므로 남긴다. 다음 회차의 점유를 지우면 두 곳이 함께 착수할 수 있다.
    """
    finished = _cancelled(records) | {r.ref for r in records if r.kind == "종료" and r.ref}
    active: list[_Record] = []
    for record in records:
        if record.kind == "종료":
            active = [r for r in active if record.ref and r.kind == "착수" and r.at == record.at]
        elif record.kind != "착수 취소" and record.comment_id not in finished:
            active.append(record)
    return active


def require_clean(*texts: str) -> None:
    """노출 패턴이 있으면 `SecretFound`다. 메시지에는 패턴 이름만 담는다."""
    found = dict.fromkeys(name for text in texts for name in find_exposures(text))
    if found:
        raise SecretFound(f"비밀값 감지: {', '.join(found)}")


def _post(t: WorkTracker, project_id: str, item_id: str, *lines: str) -> str:
    """줄마다 문단 하나인 댓글을 남기고 id를 돌려준다. 쓰기 실패는 그대로 올린다(다시 보내면 중복될 수 있다)."""
    require_clean("\n".join(lines))
    body = "".join(f"<p>{html.escape(line, quote=False)}</p>" for line in lines)
    return str(t.create_comment(project_id, item_id, body)["id"])


def short_host() -> str:
    """이 컴퓨터의 짧은 이름. `.local` 같은 전체 이름은 내부 호스트 이름이라 기록에 남기지 않는다."""
    return socket.gethostname().split(".")[0]


def _line(name: str, value: Optional[str]) -> str:
    """기록 한 줄에 넣을 값. 줄바꿈은 공백이 된다."""
    text = " ".join((value or "").splitlines()).strip()
    if not text:
        raise WorkError(f"값이 필요함: {name}")
    return text


# --- 상태 ---


def _state_id(t: WorkTracker, project_id: str, status: str) -> str:
    """`status`의 Plane 상태 id. 없으면 기록을 남기기 전에 실패하도록 댓글보다 먼저 부른다."""
    return pick_state(t.list_states(project_id), STATUS_MAP[status][0])


def _status_fields(
    t: WorkTracker, project_id: str, item: Mapping[str, Any], status: str, state_id: str
) -> dict[str, Any]:
    """상태와 상태 라벨을 `status`에 맞추는 수정 내용. 상태 라벨이 아닌 라벨은 그대로 둔다.

    없는 라벨은 만들므로 거부될 수 있는 확인을 모두 마친 뒤에 부른다.
    """
    wanted = STATUS_MAP[status][1]
    fields: dict[str, Any] = {"state": state_id}
    known = {str(label["name"]): str(label["id"]) for label in t.list_labels(project_id)}
    stale = {known[name] for name in _STATUS_LABELS if name in known}
    current = [str(label) for label in item.get("labels") or []]
    labels = [label for label in current if label not in stale]
    if wanted:
        if wanted not in known:
            known[wanted] = str(t.create_label(project_id, wanted, LABEL_COLOR)["id"])
        labels.append(known[wanted])
    if set(labels) != set(current):
        fields["labels"] = labels
    return fields


# --- 착수 ---


def _minutes(since: datetime, now: datetime) -> int:
    return max(0, int((now - since).total_seconds() // 60))


def _require_free(item: Mapping[str, Any], records: list[_Record], me: str, now: datetime, take: bool) -> None:
    """다른 사람이 점유 중이면 거부한다. 마지막 기록이 오래됐으면 `take`일 때만 넘겨받는다."""
    assignees = [str(user) for user in item.get("assignees") or []]
    if not assignees or me in assignees:
        return
    owner = ", ".join(assignees)
    active = _active(records)
    if not active:
        if any(record.kind == "종료" for record in records) or take:
            return
        raise WorkError(f"점유 중: {owner} 작업 기록 없음 (--take로 넘겨받을 수 있음)")
    last = active[-1].at
    message = f"점유 중: {owner} {_minutes(last, now)}분 전"
    if now - last <= _STALE_AFTER:
        raise WorkError(message)
    if not take:
        raise WorkError(f"{message} (--take로 넘겨받을 수 있음)")


def _earlier_start(records: list[_Record], mine_id: str, host: str) -> Optional[_Record]:
    """내 착수와 같거나 이른 시각이고 30분 안인, 아직 유효한 다른 host의 착수.

    같은 시각이면 어느 쪽이 먼저인지 알 수 없다(댓글 id 순서는 작성 순서가 아니다). 그때는 내가 진다.
    상대의 댓글이 없을 때 확인을 마친 쪽만 이기고, 둘 다 서로를 보면 둘 다 진다.
    """
    mine = next((record for record in records if record.comment_id == mine_id), None)
    if mine is None:
        raise WorkError("방금 남긴 착수 댓글을 다시 읽지 못함. 댓글을 확인한 뒤 다시 실행하세요")
    for record in _active([r for r in records if r is not mine]):
        if record.kind == "착수" and record.host != host and timedelta(0) <= mine.at - record.at <= _STALE_AFTER:
            return record
    return None


def start(t: WorkTracker, code: str, host: str, now: datetime, take: bool = False) -> str:
    """착수를 기록하고 담당자를 나로, 상태를 started로 바꾼다.

    착수 댓글을 먼저 남기고 다시 읽는다. 먼저 착수한 곳이 있으면 `[착수 취소]`를 남기고 실패한다.
    담당자·상태는 이 확인을 통과한 쪽만 바꾼다. 진 쪽이 옛 값으로 되돌리면 먼저 착수한 쪽의 담당까지 지울 수 있다.
    """
    host = _line("host", host)
    stamp = now.isoformat(timespec="seconds")
    project_id, item = t.resolve_code(code)
    item_id = str(item["id"])
    me = t.whoami_id()
    _require_free(item, _records(t, project_id, item_id), me, now, take)
    state_id = _state_id(t, project_id, "In Progress")

    mine_id = _post(t, project_id, item_id, f"[착수] {host} {stamp}")
    rival = _earlier_start(_records(t, project_id, item_id), mine_id, host)
    if rival is not None:
        _post(t, project_id, item_id, f"[착수 취소] {host} {stamp} ref={mine_id}")
        raise WorkError(f"착수 취소: {rival.host}의 착수({_minutes(rival.at, now)}분 전)가 먼저이거나 같은 시각임")
    fields = _status_fields(t, project_id, item, "In Progress", state_id)
    t.update_item(project_id, item_id, {**fields, "assignees": [me]})
    return f"착수: {code} ({host})"


# --- 인계·검증·종료 ---


def note(t: WorkTracker, code: str, did: str, next_: str, now: datetime) -> str:
    """인계 메모를 남긴다. 기록 시각은 댓글의 작성 시각이라 `now`는 본문에 넣지 않는다."""
    values = {"did": _line("did", did), "next": _line("next", next_)}
    for name, value in values.items():
        if len(value) > NOTE_MAX_CHARS:
            raise WorkError(f"{name}은(는) {NOTE_MAX_CHARS}자 이하여야 함: {len(value)}자")
    project_id, item = t.resolve_code(code)
    _post(t, project_id, str(item["id"]), f"[인계] did: {values['did']} / next: {values['next']}")
    return f"인계 기록: {code}"


def verify(
    t: WorkTracker,
    code: str,
    cmd: str,
    exit_code: int,
    head: str,
    now: datetime,
    limits: Optional[str] = None,
) -> str:
    """검증 결과를 남긴다. `limits`(검증이 확인하지 못한 범위)는 둘째 줄이 된다."""
    short = _line("head", head)[:7]
    lines = [f"[검증] head={short} exit={exit_code} cmd={_line('cmd', cmd)}"]
    if limits:
        lines.append(f"한계: {_line('limits', limits)}")
    project_id, item = t.resolve_code(code)
    _post(t, project_id, str(item["id"]), *lines)
    return f"검증 기록: {code} head={short} exit={exit_code}"


def _is_verified(records: list[_Record], head: str) -> bool:
    heads = (_VERIFY_HEAD_RE.match(record.detail) for record in records if record.kind == "검증")
    return any(found is not None and found.group(1) == head[:7] for found in heads)


def _current_start(records: list[_Record], me: str, host: str) -> Optional[_Record]:
    """내 최신 착수는 이전 착수를 대체하며, 종료됐어도 재실행 판정에 쓴다."""
    return _latest_start([r for r in _starts(records) if (r.host, r.actor) == (host, me)])


def _latest_start(starts: list[_Record]) -> Optional[_Record]:
    """최신 착수가 같은 시각에 여럿이면 댓글 id로 순서를 추측하지 않는다."""
    if not starts:
        return None
    latest = max(r.at for r in starts)
    candidates = [r for r in starts if r.at == latest]
    if len(candidates) != 1:
        raise WorkError("같은 시각의 착수를 구분할 수 없습니다. 작업 댓글을 확인하세요")
    return candidates[0]


def _require_not_superseded(records: list[_Record], me: str, host: str, mine: Optional[_Record], detail: str) -> None:
    """내 착수 뒤에 다른 컴퓨터의 착수가 있으면 `WorkError`다. 그 착수의 상태와 점유를 덮으면 안 된다.

    착수 없이 끝낸 항목은 같은 내용(`detail`)의 내 `[종료]`를 기준으로 삼는다.
    """
    anchor = mine
    if anchor is None:
        same_ends = (
            r
            for r in reversed(records)
            if r.kind == "종료" and r.actor == me and detail in (r.detail, _REF_RE.sub("", r.detail))
        )
        anchor = next(same_ends, None)
    if anchor is None:
        return
    for other in _starts(records):
        if other.at >= anchor.at and (other.host, other.actor) != (host, me):
            raise WorkError(f"end 거부: 그 뒤에 {other.host}에서 착수함. 상태와 댓글을 바꾸지 않았습니다")


def _recorded_end(records: list[_Record], start_record: _Record, mode: str) -> Optional[_Record]:
    """`start_record`를 이미 `mode`로 끝낸 `[종료]`. 아직 끝나지 않았으면 `None`이다.

    다른 방식으로 끝났으면 `WorkError`다. `ref`로 짝을 찾으므로 작성 시각이 같은 이전 회차의 `[종료]`와 섞이지 않는다.
    """
    ends = [r for r in records if r.kind == "종료" and r.ref == start_record.comment_id]
    same = next((r for r in ends if r.mode == mode), None)
    if ends and same is None:
        raise WorkError(
            f"end 거부: 이 착수는 이미 {ends[-1].mode}(으)로 끝남. 다른 방식으로 끝내려면 start부터 다시 실행하세요"
        )
    return same


def end(
    t: WorkTracker,
    code: str,
    mode: str,
    now: datetime,
    head: str,
    result: Optional[str] = None,
    unverified: Optional[str] = None,
    cause: Optional[str] = None,
    fix: Optional[str] = None,
    reason: Optional[str] = None,
    *,
    host: Optional[str] = None,
) -> str:
    """작업을 끝낸다. `done`은 현재 HEAD의 검증 기록이 없으면 `unverified`(사유)가 필요하다.

    `head`는 `done`에서만 쓴다. 상태·라벨을 먼저 바꾸고 `[종료]`를 나중에 남긴다. `[종료]`는 점유를 풀기 때문에
    상태를 바꾸지 못했으면 남기지 않는다.

    `[종료]`는 끝내는 착수를 `ref=`로 가리킨다. 내 착수가 없으면 아직 끝나지 않은 마지막 착수이고, 그것도 없으면
    붙이지 않는다. 내 착수를 같은 방식으로 이미 끝냈으면 아무것도 바꾸지 않고 성공하며, 방식이 다르면 거부한다.
    그 밖에 이미 목표 상태면 댓글만 쓴다. 내 착수 뒤에 다른 컴퓨터가 착수했으면 거부한다.
    `host`를 주지 않으면 이 컴퓨터의 짧은 이름이다.
    """
    if mode not in _END_MODES:
        raise WorkError(f"알 수 없는 종료 방식: {mode}")
    status, required = _END_MODES[mode]
    host = _line("host", host if host is not None else short_host())
    given = {"result": result, "cause": cause, "fix": fix, "reason": reason}
    details = [f"{name}: {_line(name, given[name])}" for name in required]
    if mode == "done":
        _line("head", head)
        if unverified:
            details.append(f"unverified: {_line('unverified', unverified)}")

    detail = " ".join(part for part in (mode, " / ".join(details)) if part)
    require_clean(f"[종료] {detail}")  # 기록하지 못할 내용이면 상태도 바꾸지 않는다.

    project_id, item = t.resolve_code(code)
    item_id = str(item["id"])
    records = _records(t, project_id, item_id)
    me = t.whoami_id()
    mine = _current_start(records, me, host)
    _require_not_superseded(records, me, host, mine, detail)
    if mine is not None:
        recorded = _recorded_end(records, mine, mode)
        if recorded is not None:
            return f"종료 기록이 이미 있어 아무것도 바꾸지 않음: {code} ([종료] {recorded.detail})"
    target = mine or _latest_start([r for r in _active(records) if r.kind == "착수"])
    line = f"[종료] {detail}" if target is None else f"[종료] {detail} ref={target.comment_id}"
    require_clean(line)
    if mode == "done" and not unverified and not _is_verified(records, head):
        raise WorkError(
            f"현재 HEAD({head[:7]})의 검증 기록이 없음. work verify를 실행하거나 unverified 사유를 적으세요"
        )
    fields = _status_fields(t, project_id, item, status, _state_id(t, project_id, status))
    if "labels" in fields or str(item.get("state")) != fields["state"]:
        t.update_item(project_id, item_id, fields)
    try:
        _post(t, project_id, item_id, line)
    except PlaneError as exc:
        raise WorkError(f"상태는 바꿨지만 [종료] 댓글을 남기지 못함. end를 다시 실행하세요: {exc}") from exc
    return f"종료: {code} ({mode})"
