"""행위자(Actor) 모델, 등록, 식별 및 권한 검사 모듈."""
import os
import re
from typing import NamedTuple

from docdb import db, history
from docdb.db import Connection
from docdb.errors import DocdbError
from docdb.textnorm import normalize_text


class Actor(NamedTuple):
    """행위자 식별자 및 종류를 나타내는 불변 튜플."""

    id: str
    type: str  # "human" | "ai" | "team"


ACTOR_PATTERNS: dict[str, str] = {
    "human": r"^u-[a-z0-9][a-z0-9._-]{0,62}$",
    "ai": r"^ai-[a-z0-9][a-z0-9-]{0,62}$",
    "team": r"^team-[a-z0-9][a-z0-9-]{0,62}$",
}


def add_actor(
    con: Connection,
    actor_id: str,
    actor_type: str,
    name: str,
    by: Actor | None,
) -> None:
    """새로운 행위자를 등록하고 이력에 생성 기록을 남긴다.

    ID가 행위자 종류의 규격에 맞지 않으면 ID_FORMAT 오류를 발생시킨다.
    """
    pattern = ACTOR_PATTERNS.get(actor_type)
    if pattern is None or not re.fullmatch(pattern, actor_id, re.ASCII):
        raise DocdbError("ID_FORMAT", f"행위자 ID 형식 위반: {actor_id} ({actor_type})")

    normalized_name = normalize_text(name) or name
    with db.write_tx(con):
        try:
            con.execute(
                "INSERT INTO actor (actor_id, actor_type, name, active) VALUES (?, ?, ?, 1)",
                (actor_id, actor_type, normalized_name),
            )
        except db.DatabaseError as exc:
            raise DocdbError("DB", str(exc)[:80]) from exc

        recorder_id = by.id if by is not None else actor_id
        history.append_change(
            con=con,
            entity_type="actor",
            entity_id=actor_id,
            op="insert",
            before=None,
            after={
                "actor_id": actor_id,
                "actor_type": actor_type,
                "name": normalized_name,
                "active": 1,
            },
            actor_id=recorder_id,
            work_id=None,
            reason=None,
        )


def resolve_actor(con: Connection, explicit: str | None) -> Actor:
    """명시적 지정 또는 환경 변수로부터 활성 행위자를 결정한다.

    둘 다 없으면 BAD_ARGS, 존재하지 않거나 비활성이면 NOT_FOUND 오류를 발생시킨다.
    """
    target_id: str | None = None
    if explicit is not None and explicit != "":
        target_id = explicit
    else:
        env_actor = os.environ.get("DOCDB_ACTOR")
        if env_actor:
            target_id = env_actor

    if not target_id:
        raise DocdbError("BAD_ARGS", "행위자가 지정되지 않음 (explicit 또는 DOCDB_ACTOR 필요)")

    try:
        row = con.execute(
            "SELECT actor_id, actor_type, active FROM actor WHERE actor_id = ?",
            (target_id,),
        ).fetchone()
    except db.DatabaseError as exc:
        raise DocdbError("DB", str(exc)[:80]) from exc

    if row is None or row[2] == 0:
        raise DocdbError("NOT_FOUND", f"행위자를 찾을 수 없거나 비활성 상태임: {target_id}")

    return Actor(id=row[0], type=row[1])


def require_human(actor: Actor, interactive: bool) -> None:
    """행위자가 사람이 아니거나 대화형 터미널(TTY)이 아니면 NEEDS_HUMAN 오류를 발생시킨다."""
    if actor.type != "human" or not interactive:
        raise DocdbError("NEEDS_HUMAN", "사람 및 대화형 터미널(TTY)이 필요함")


def require_ai(actor: Actor) -> None:
    """행위자가 AI가 아니면 BAD_ARGS 오류를 발생시킨다."""
    if actor.type != "ai":
        raise DocdbError("BAD_ARGS", "MCP 행위자는 ai여야 함")
