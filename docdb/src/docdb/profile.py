"""프로젝트 사실과 문서 종류 적용 판정(C-19)."""
import json

from docdb import actors, clock, db, history, policy, textnorm
from docdb.actors import Actor
from docdb.db import Connection
from docdb.errors import DocdbError

_ALLOWED_RULE_KEYS = frozenset({"always", "fact", "selected_kind", "all", "any"})


def _tri_str(val: bool | None) -> str:
    """3값 논리 결과를 DB 문자열로 변환한다."""
    if val is True:
        return "true"
    if val is False:
        return "false"
    return "unknown"


def evaluate(rule: dict, facts: dict[str, int | None], selected: set[str]) -> bool | None:
    """문서 종류 적용·필수 규칙을 3값 논리로 평가한다(eval 금지)."""
    if not isinstance(rule, dict) or not rule:
        raise DocdbError("BAD_ARGS", "규칙은 비어있지 않은 객체여야 함")
    unknown = set(rule.keys()) - _ALLOWED_RULE_KEYS
    if unknown:
        raise DocdbError("BAD_ARGS", f"알 수 없는 규칙 키: {sorted(unknown)}")

    if "always" in rule:
        return True

    if "fact" in rule:
        fact_key = rule["fact"]
        if not isinstance(fact_key, str):
            raise DocdbError("BAD_ARGS", "사실 키는 문자열이어야 함")
        val = facts.get(fact_key)
        if val is None:
            return None
        return bool(val)

    if "selected_kind" in rule:
        kind_id = rule["selected_kind"]
        if not isinstance(kind_id, str):
            raise DocdbError("BAD_ARGS", "선택 종류는 문자열이어야 함")
        return kind_id in selected

    if "all" in rule:
        subrules = rule["all"]
        if not isinstance(subrules, list):
            raise DocdbError("BAD_ARGS", "all 규칙 목록은 배열이어야 함")
        sub_results = [evaluate(sub, facts, selected) for sub in subrules]
        if False in sub_results:
            return False
        if all(sub is True for sub in sub_results):
            return True
        return None

    if "any" in rule:
        subrules = rule["any"]
        if not isinstance(subrules, list):
            raise DocdbError("BAD_ARGS", "any 규칙 목록은 배열이어야 함")
        sub_results = [evaluate(sub, facts, selected) for sub in subrules]
        if True in sub_results:
            return True
        if all(sub is False for sub in sub_results):
            return False
        return None

    raise DocdbError("BAD_ARGS", f"알 수 없는 규칙 형태: {rule}")


def unknown_facts(con: Connection, project_id: str) -> list[str]:
    """미확인 상태인 cap.* 사실 키 목록을 정렬해 반환한다."""
    rows = con.execute(
        """SELECT f.fact_key
        FROM cat_fact f
        LEFT JOIN fact_value v ON v.project_id = ? AND v.fact_key = f.fact_key
        WHERE f.fact_type = 'cap' AND (v.value IS NULL)
        ORDER BY f.fact_key""",
        (project_id,),
    ).fetchall()
    return [row[0] for row in rows]


def set_fact(
    con: Connection,
    actor: Actor,
    project_id: str,
    fact_key: str,
    value: bool | None,
    reason: str,
    interactive: bool,
) -> None:
    """사람 행위자가 cap.* 사실을 설정하고 프로필 버전을 갱신하며 재계산한다."""
    actors.require_human(actor, interactive)
    if not isinstance(fact_key, str) or not fact_key.startswith("cap."):
        raise DocdbError("BAD_ARGS", "사실 키는 cap.* 형식이어야 함")
    cat_row = con.execute(
        "SELECT 1 FROM cat_fact WHERE fact_key = ? AND fact_type = 'cap'",
        (fact_key,),
    ).fetchone()
    if cat_row is None:
        raise DocdbError("BAD_ARGS", f"카탈로그에 등록되지 않은 cap 사실 키: {fact_key}")
    if not isinstance(reason, str) or not reason.strip():
        raise DocdbError("BAD_ARGS", "사실 사유는 필수 문자열임")

    normalized_reason = textnorm.normalize_text(reason)
    policy.assert_no_secrets(con, f"fact:{fact_key}", [normalized_reason])

    int_val = 1 if value is True else (0 if value is False else None)
    now = clock.now_iso()

    with db.write_tx(con):
        proj_row = con.execute(
            "SELECT profile_revision FROM project WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if proj_row is None:
            raise DocdbError("NOT_FOUND", "프로젝트를 찾을 수 없음")
        prev_rev = proj_row[0]
        next_rev = prev_rev + 1
        con.execute(
            "UPDATE project SET profile_revision = ? WHERE project_id = ?",
            (next_rev, project_id),
        )
        history.append_change(
            con=con,
            entity_type="project",
            entity_id=project_id,
            op="update",
            before={"profile_revision": prev_rev},
            after={"profile_revision": next_rev},
            actor_id=actor.id,
            work_id=None,
            reason=normalized_reason,
        )

        before_row = con.execute(
            "SELECT value, reason, confirmed_by, confirmed_at FROM fact_value WHERE project_id = ? AND fact_key = ?",
            (project_id, fact_key),
        ).fetchone()

        con.execute(
            """INSERT INTO fact_value (project_id, fact_key, value, reason, confirmed_by, confirmed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, fact_key) DO UPDATE SET
            value = excluded.value, reason = excluded.reason, confirmed_by = excluded.confirmed_by, confirmed_at = excluded.confirmed_at""",
            (project_id, fact_key, int_val, normalized_reason, actor.id, now),
        )

        before = (
            None
            if before_row is None
            else {
                "project_id": project_id,
                "fact_key": fact_key,
                "value": before_row[0],
                "reason": before_row[1],
                "confirmed_by": before_row[2],
                "confirmed_at": before_row[3],
            }
        )
        after = {
            "project_id": project_id,
            "fact_key": fact_key,
            "value": int_val,
            "reason": normalized_reason,
            "confirmed_by": actor.id,
            "confirmed_at": now,
        }
        history.append_change(
            con=con,
            entity_type="fact_value",
            entity_id=f"{project_id}|{fact_key}",
            op="insert" if before is None else "update",
            before=before,
            after=after,
            actor_id=actor.id,
            work_id=None,
            reason=normalized_reason,
        )

        recompute(con, project_id)


def recompute(con: Connection, project_id: str) -> dict[str, int]:
    """모든 문서 종류의 applies_when·required_when을 평가해 kind_applicability를 갱신한다."""
    proj_row = con.execute(
        "SELECT profile_revision FROM project WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    if proj_row is None:
        raise DocdbError("NOT_FOUND", "프로젝트를 찾을 수 없음")
    profile_rev = proj_row[0]

    facts = {
        row[0]: row[1]
        for row in con.execute(
            "SELECT fact_key, value FROM fact_value WHERE project_id = ?",
            (project_id,),
        ).fetchall()
    }
    selected = {
        row[0]
        for row in con.execute(
            "SELECT kind_id FROM kind_selection WHERE project_id = ?",
            (project_id,),
        ).fetchall()
    }
    kinds = con.execute(
        "SELECT kind_id, applies_when, required_when FROM cat_kind ORDER BY kind_id"
    ).fetchall()
    now = clock.now_iso()

    with db.write_tx(con):
        for kind_id, applies_when_json, required_when_json in kinds:
            applies_rule = json.loads(applies_when_json)
            required_rule = json.loads(required_when_json)
            applies_bool = evaluate(applies_rule, facts, selected)
            required_bool = evaluate(required_rule, facts, selected)
            applies_str = _tri_str(applies_bool)
            required_str = _tri_str(required_bool)
            reason_str = textnorm.canonical_json(required_rule)
            con.execute(
                """INSERT INTO kind_applicability
                (project_id, kind_id, applies, required, reason, profile_revision, evaluated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, kind_id) DO UPDATE SET
                applies = excluded.applies,
                required = excluded.required,
                reason = excluded.reason,
                profile_revision = excluded.profile_revision,
                evaluated_at = excluded.evaluated_at""",
                (project_id, kind_id, applies_str, required_str, reason_str, profile_rev, now),
            )

    counts = {"true": 0, "false": 0, "unknown": 0}
    for row in con.execute(
        "SELECT required, count(*) FROM kind_applicability WHERE project_id = ? GROUP BY required",
        (project_id,),
    ).fetchall():
        if row[0] in counts:
            counts[row[0]] = row[1]
    return counts
