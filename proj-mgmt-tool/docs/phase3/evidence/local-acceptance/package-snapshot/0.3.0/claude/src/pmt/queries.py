"""Read-only, bounded context queries for P3."""
from __future__ import annotations

import base64
import binascii
import json
import sqlite3
from typing import Any

from .errors import PmtError
from .util import canonical_json, sha256_text

DEFAULT_BUDGET = 4500
MAX_BUDGET = 4500
MAX_LIMIT = 200


def _decode_cursor(value: str | None, binding: dict[str, Any]) -> tuple[str, str] | None:
    if not value:
        return None
    if not isinstance(value, str):
        raise PmtError("invalid_cursor", "cursor must be text")
    try:
        decoded = json.loads(base64.urlsafe_b64decode(value.encode("ascii") + b"=" * (-len(value) % 4)))
        if decoded.get("v") != 1 or decoded.get("binding") != sha256_text(canonical_json(binding)):
            raise ValueError
        return str(decoded["created_at"]), str(decoded["id"])
    except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error, json.JSONDecodeError) as exc:
        raise PmtError("invalid_cursor", "cursor is malformed or belongs to a different query") from exc


def _encode_cursor(created_at: str, record_id: str, binding: dict[str, Any]) -> str:
    raw = canonical_json({"v": 1, "created_at": created_at, "id": record_id,
                          "binding": sha256_text(canonical_json(binding))}).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _json_object(value: str, field: str) -> dict[str, Any]:
    try:
        result = json.loads(value or "{}")
        return result if isinstance(result, dict) else {}
    except (TypeError, json.JSONDecodeError):
        raise PmtError("stored_record_invalid", f"Stored {field} is not valid JSON", 5)


def _text(value: Any, cap: int = 1200) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        value = canonical_json(value)
    value = str(value).strip()
    return value if len(value) <= cap else value[:cap] + "…"


def _record_view(row: sqlite3.Row) -> dict[str, Any]:
    body = _json_object(row["body_json"], "record body")
    return {
        "record_id": row["id"], "kind": row["kind"], "scope_id": row["scope_id"],
        "parent_id": row["parent_id"], "title": row["title"], "state": row["state"],
        "revision": row["revision"], "updated_at": row["updated_at"],
        "body": body,
    }


def _aggregate_work_states(conn, views):
    """Show child progress without silently finishing the parent's own claim."""
    for view in views:
        if view["kind"] != "work":
            continue
        children = conn.execute(
            "WITH RECURSIVE tree(id,kind,state) AS (SELECT id,kind,state FROM records WHERE parent_id=? "
            "UNION ALL SELECT r.id,r.kind,r.state FROM records r JOIN tree t ON r.parent_id=t.id) "
            "SELECT state FROM tree WHERE kind='item'", (view["record_id"],)
        ).fetchall()
        states = [row["state"] for row in children]
        if not states:
            view["aggregate_state"] = view["state"]
        elif all(state == "Canceled" for state in states):
            view["aggregate_state"] = "Canceled"
        elif all(state in {"Done", "Canceled"} for state in states):
            view["aggregate_state"] = "Done"
        else:
            view["aggregate_state"] = next((state for state in ("In Progress", "Blocked", "Paused")
                                           if state in states), "Planned")


def _markdown(scope_id: str, records: list[dict[str, Any]], decisions: list[dict[str, Any]],
              budget: int, next_cursor: str | None) -> tuple[str, bool]:
    rendered = f"# Context {scope_id}"
    truncated = False

    def add_section(title: str, values: list[str]) -> None:
        nonlocal rendered, truncated
        if not values:
            return
        accepted = []
        header = f"## {title}"
        for value in values:
            candidate_section = header + "\n" + "\n".join(accepted + [value])
            candidate = rendered + "\n\n" + candidate_section
            if len(candidate) <= budget:
                accepted.append(value)
            else:
                truncated = True
        if accepted:
            rendered += "\n\n" + header + "\n" + "\n".join(accepted)

    decision_lines = []
    for record in decisions:
        body = record["body"]
        text = f"- **{_text(record['title'], 180)}** ({record['state']}, rev {record['revision']})"
        content = body.get("content") or body.get("delegation_scope") or body.get("option_id")
        if content:
            text += f" — {_text(content, 500)}"
        if body.get("reason"):
            text += f" (Reason: {_text(body['reason'], 300)})"
        if body.get("watch"):
            text += f" — Watch: {_text(body['watch'], 500)}"
        if body.get("next"):
            text += f" — Next: {_text(body['next'], 300)}"
        decision_lines.append(text)
    add_section("Current decisions", decision_lines)

    nexts, watches = [], []
    for record in records:
        body = record["body"]
        if body.get("next"):
            nexts.append(f"- {record['title']}: {_text(body['next'], 300)}")
        if body.get("watch"):
            watches.append(f"- {record['title']}: {_text(body['watch'], 300)}")
    add_section("Next", nexts)
    add_section("Watch", watches)
    detail = []
    for record in records:
        body = record["body"]
        lines = [f"- **{_text(record['title'], 180)}** [{record['kind']}/{record['state']}, rev {record['revision']}] ({record['record_id']})"]
        if "aggregate_state" in record:
            lines.append(f"  - Child progress: {record['aggregate_state']}")
        if body.get("criteria"):
            lines.append(f"  - Criteria: {_text(body['criteria'], 280)}")
        content = body.get("content") or body.get("text") or body.get("result")
        if content:
            lines.append(f"  - Content: {_text(content, 700)}")
        if body.get("next"):
            lines.append(f"  - Next: {_text(body['next'], 300)}")
        if body.get("watch"):
            lines.append(f"  - Watch: {_text(body['watch'], 300)}")
        detail.append("\n".join(lines))
    add_section("Records", detail)
    if next_cursor and (truncated or len(records) > 0):
        marker = f"\n\n상세 페이지 cursor: `{next_cursor}`"
        if len(rendered) + len(marker) <= budget:
            rendered += marker
    return rendered, truncated


def handle(db, conn, request) -> dict[str, Any]:
    """Handle read_context using the caller's read connection."""
    scope_id = request.get("scope_id")
    if not isinstance(scope_id, str) or not scope_id:
        raise PmtError("scope_required", "scope_id is required")
    scope = conn.execute("SELECT id,kind,slug,parent_id FROM scopes WHERE id=?", (scope_id,)).fetchone()
    if scope is None:
        raise PmtError("scope_not_found", "scope_id does not exist")
    payload = request.get("payload") or {}
    query = payload.get("query", "")
    if not isinstance(query, str):
        raise PmtError("invalid_query", "query must be text")
    limit = payload.get("limit", 50)
    budget = payload.get("budget", DEFAULT_BUDGET)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise PmtError("invalid_limit", f"limit must be between 1 and {MAX_LIMIT}")
    if isinstance(budget, bool) or not isinstance(budget, int) or not 128 <= budget <= MAX_BUDGET:
        raise PmtError("invalid_budget", f"budget must be between 128 and {MAX_BUDGET}")
    record_id = request.get("record_id")
    binding = {"scope_id": scope_id, "record_id": record_id, "query": query, "limit": limit}
    cursor = _decode_cursor(payload.get("cursor"), binding)

    if record_id is not None:
        selected = conn.execute(
            "WITH RECURSIVE scope_tree(id) AS (SELECT ? UNION ALL SELECT s.id FROM scopes s JOIN scope_tree t ON s.parent_id=t.id) "
            "SELECT r.id,r.scope_id FROM records r WHERE r.id=? AND r.scope_id IN scope_tree", (scope_id, record_id)
        ).fetchone()
        if selected is None:
            raise PmtError("record_not_in_scope", "record_id is missing or outside scope_id")
        rows = conn.execute(
            "WITH RECURSIVE scope_tree(id) AS (SELECT ? UNION ALL SELECT s.id FROM scopes s JOIN scope_tree t ON s.parent_id=t.id), "
            "tree(id) AS (SELECT ? UNION ALL SELECT r.id FROM records r JOIN tree t ON r.parent_id=t.id WHERE r.scope_id IN scope_tree) "
            "SELECT r.* FROM records r JOIN tree t ON t.id=r.id ORDER BY r.created_at,r.id", (scope_id, record_id)
        ).fetchall()
    else:
        rows = conn.execute(
            "WITH RECURSIVE scope_tree(id) AS (SELECT ? UNION ALL SELECT s.id FROM scopes s JOIN scope_tree t ON s.parent_id=t.id) "
            "SELECT r.* FROM records r WHERE r.scope_id IN scope_tree ORDER BY r.created_at,r.id", (scope_id,)
        ).fetchall()
    if query:
        needle = query.casefold()
        rows = [r for r in rows if needle in (str(r["title"]) + " " + str(r["body_json"])).casefold()]
    if cursor:
        rows = [r for r in rows if (r["created_at"], r["id"]) > cursor]
    page = rows[:limit]
    has_more = len(rows) > limit
    next_cursor = _encode_cursor(page[-1]["created_at"], page[-1]["id"], binding) if has_more and page else None
    views = [_record_view(row) for row in page]
    _aggregate_work_states(conn, views)
    decision_rows = conn.execute(
        "WITH RECURSIVE scope_tree(id) AS (SELECT ? UNION ALL SELECT s.id FROM scopes s JOIN scope_tree t ON s.parent_id=t.id) "
        "SELECT * FROM records WHERE scope_id IN scope_tree AND kind='decision' ORDER BY updated_at DESC,id DESC LIMIT 20",
        (scope_id,)
    ).fetchall()
    decisions = [_record_view(r) for r in decision_rows
                 if r["state"].casefold() not in {"superseded", "canceled", "cancelled", "void"}]
    decisions.sort(key=lambda r: (r["updated_at"], r["record_id"]), reverse=True)
    context_md, text_truncated = _markdown(scope_id, views, decisions[:10], budget, next_cursor)
    return {
        "scope_id": scope_id, "scope_kind": scope["kind"], "scope_slug": scope["slug"],
        "record_id": record_id, "records": views, "current_decisions": decisions[:10],
        "next_actions": [{"record_id": r["record_id"], "value": r["body"].get("next")} for r in views if r["body"].get("next")],
        "watch": [{"record_id": r["record_id"], "value": r["body"].get("watch")} for r in views if r["body"].get("watch")],
        "context_markdown": context_md, "limit": limit, "budget": budget,
        "truncated": bool(has_more or text_truncated), "next_cursor": next_cursor,
    }
