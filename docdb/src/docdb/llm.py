"""LLM 에이전트가 DB를 압축해 읽고 필드 값을 쓰는 경로."""
import difflib
import json
import re

from docdb import clock, db, history, ids, policy, relations, textnorm
from docdb.actors import Actor
from docdb.db import Connection
from docdb.errors import DocdbError

SNIP = 80
SEV = {"critical": 0, "error": 1, "warning": 2, "info": 3}

_HANDLE_PATTERNS: dict[str, str] = {
    "d": r"d:(DOC-[A-Z0-9]+(?:-[A-Z0-9]+)*)",
    "f": r"f:(DOC-[A-Z0-9]+(?:-[A-Z0-9]+)*)/([A-Z][0-9]{2})/([a-z][a-z0-9_]*)",
    "i": r"i:([A-Z0-9]+-[A-Z0-9]+(?:-[A-Z0-9]+)*)",
    "w": r"w:(WORK-[0-9]{4}-[0-9]{4,}|new)",
    "r": r"r:(RUN-[0-9]{4}-[0-9]{4,})",
    "a": r"a:(ART-[0-9]{4}-[0-9]{4,})",
    "k": r"k:([A-Z][0-9]{2})",
    "s": r"s:([0-9]+)",
    "d?": r"d\?:(ART-[0-9]{4}-[0-9]{4,})",
}
_ENTITY = {"d": "document", "i": "item", "w": "work", "r": "run", "a": "artifact"}
_PREFIX = {value: key for key, value in _ENTITY.items()}


def parse_handle(h: str) -> tuple[str, tuple[str, ...]]:
    """C-12 핸들을 형식 검사하고 종류와 키 조각을 반환한다."""
    if not isinstance(h, str):
        raise DocdbError("BAD_HANDLE")
    prefix, separator, _rest = h.partition(":")
    pattern = _HANDLE_PATTERNS.get(prefix)
    if not separator or pattern is None:
        raise DocdbError("BAD_HANDLE")
    match = re.fullmatch(pattern, h, re.ASCII)
    if match is None:
        raise DocdbError("BAD_HANDLE")
    return prefix, tuple(match.groups())


class Out:
    """예산을 넘는 줄을 세고 강제 줄은 항상 포함한다."""

    def __init__(self, budget: int | None) -> None:
        self.budget = budget
        self.lines: list[str] = []
        self.used = 0
        self.omitted = 0

    def add(self, line: str, force: bool = False) -> bool:
        """예산을 확인해 줄을 추가하고 포함 여부를 반환한다."""
        cost = textnorm.tok_est(line) + 1
        if not force and self.budget and self.used + cost > self.budget:
            self.omitted += 1
            return False
        self.lines.append(line)
        self.used += cost
        return True


class DocDB:
    """이미 연 DB 연결과 행위자 정보를 사용해 에이전트 조회·쓰기를 수행한다."""

    def __init__(
        self,
        con: Connection,
        actor: Actor,
        work: str | None = None,
        session: str | None = None,
        model: str | None = None,
    ) -> None:
        """연결, 행위자, 작업·세션·모델 정보를 보관한다."""
        self.con = con
        self.actor = actor
        self.work = work
        self.session = session
        self.model = model
        self._seen: set[tuple[str, str]] = set()

    def x(self, sql: str, args: tuple | list = ()):
        """SQL을 실행한다."""
        try:
            return self.con.execute(sql, args)
        except db.DatabaseError as exc:
            raise DocdbError("DB", str(exc)[:80]) from exc

    def cursor(self) -> int:
        """변경 이력의 현재 마지막 번호를 반환한다."""
        row = self.x("SELECT coalesce(max(change_id), 0) FROM change_log").fetchone()
        return int(row[0])

    def _session_ref(self) -> str | None:
        """세션과 모델을 C-15 형식으로 연결한다."""
        if self.session is None and self.model is None:
            return None
        return f"{self.session or ''}/{self.model or ''}"

    def _finish(self, tool: str, args: dict, out: Out, header: str) -> str:
        """응답 머리줄과 호출 기록을 만들고 읽기 잠금을 초기화한다."""
        budget = out.budget or "-"
        head = f"#{tool} {header} as_of=c:{self.cursor()} tok={out.used}/{budget}"
        if out.omitted:
            head += f" omitted={out.omitted}"
        result = "\n".join([head] + out.lines)
        session_ref = self._session_ref()
        try:
            with db.write_tx(self.con):
                self.x(
                    "INSERT INTO call_log "
                    "(tool, args_json, actor_id, work_id, session_ref, out_chars, out_tok_est, "
                    "budget, omitted, called_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        tool,
                        json.dumps(args, ensure_ascii=False),
                        self.actor.id,
                        self.work,
                        session_ref,
                        len(result),
                        textnorm.tok_est(result),
                        out.budget,
                        out.omitted,
                        clock.now_iso(),
                    ),
                )
        except db.DatabaseError as exc:
            raise DocdbError("DB", str(exc)[:80]) from exc
        self._seen.clear()
        return result

    def _read(self, target_type: str, target_id: str, digest: str | None = None) -> None:
        """실제로 응답에 담은 대상을 작업 컨텍스트 잠금으로 기록한다."""
        if not self.work or (target_type, target_id) in self._seen:
            return
        self._seen.add((target_type, target_id))
        try:
            with db.write_tx(self.con):
                self.x(
                    "INSERT INTO context_read "
                    "(work_id, actor_id, session_ref, target_type, target_id, digest, "
                    "last_change_id, reason, read_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'auto', ?)",
                    (
                        self.work,
                        self.actor.id,
                        self._session_ref(),
                        target_type,
                        target_id,
                        digest,
                        self.cursor() or None,
                        clock.now_iso(),
                    ),
                )
        except db.DatabaseError as exc:
            raise DocdbError("DB", str(exc)[:80]) from exc

    def _is_local_only(self, doc_id: str) -> bool:
        """문서가 외부 반출 금지인지 확인한다."""
        return not policy.can_export(self.con, doc_id)

    def _field_row(self, doc_id: str, kind_id: str, field_key: str):
        """필드의 상태, 값, digest와 토큰 추정치를 조회한다."""
        return self.x(
            "SELECT fill_state, format, value_text, value_json, ref_locator, reason, digest, tok_est "
            "FROM field_value WHERE doc_id=? AND kind_id=? AND field_key=?",
            (doc_id, kind_id, field_key),
        ).fetchone()

    def _field_line(
        self,
        doc_id: str,
        kind_id: str,
        field_key: str,
        level: int,
        row=None,
        head: bool = True,
    ) -> str:
        """필드 상태나 L0·L1·L2 표현을 만든다."""
        row = row if row is not None else self._field_row(doc_id, kind_id, field_key)
        handle = f"f:{doc_id}/{kind_id}/{field_key}"
        if row is None:
            return f"{handle} MISSING"
        state, fmt, value_text, value_json, locator, reason, digest, token_est = row
        local_only = self._is_local_only(doc_id)
        policy_suffix = " policy=local_only" if local_only else ""
        if state != "filled":
            return f"{handle} {state} reason={textnorm.q(reason)}{policy_suffix}"
        body = value_text if fmt == "md" else (value_json if fmt == "json" else f"ref {locator}")
        body = body or ""
        meta = f"dg={digest} tok={token_est}"
        if local_only:
            return f"{handle} {meta}{policy_suffix}"
        if level == 0:
            return f"{handle} {meta}"
        if level == 1:
            summary = self.x(
                "SELECT summary, source_digest FROM entity_summary WHERE handle=?", (handle,)
            ).fetchone()
            if summary and summary[1] == digest:
                return f"{handle} {meta} sum={textnorm.q(summary[0])}"
            if not head:
                return f"{handle} {meta} {'sum=stale' if summary else 'sum=none'}"
            excerpt = body[:SNIP]
            remaining = len(body) - len(excerpt)
            return (
                f"{handle} {meta} {'sum=stale' if summary else 'sum=none'} "
                f"head={textnorm.q(excerpt)}"
                + (f" +{remaining}ch" if remaining > 0 else "")
            )
        return f"{handle} {meta}\n{body}"

    def _doc_line(self, doc_id: str) -> str:
        """문서의 L0 줄에 필수 필드 누락과 반출 정책을 덧붙인다."""
        row = self.x("SELECT line FROM v_l0_document WHERE doc_id=?", (doc_id,)).fetchone()
        missing = [
            entry[0]
            for entry in self.x(
                "SELECT field_key FROM v_missing_required_fields WHERE doc_id=?", (doc_id,)
            )
        ]
        line = row[0] if row else f"D {doc_id} NOT_FOUND"
        if missing:
            line += f" missing={','.join(missing)}"
        if row is not None and self._is_local_only(doc_id):
            line += " policy=local_only"
        return line

    def _doc_fields(self, doc_id: str) -> list[tuple[str, str]]:
        """문서에 저장된 필드를 카탈로그 순서로 반환한다."""
        return self.x(
            "SELECT dk.kind_id, f.field_key FROM document_kind dk "
            "JOIN field_value v ON v.doc_id=dk.doc_id AND v.kind_id=dk.kind_id "
            "JOIN cat_kind_field f ON f.kind_id=v.kind_id AND f.field_key=v.field_key "
            "WHERE dk.doc_id=? ORDER BY dk.kind_id, f.sort_order",
            (doc_id,),
        ).fetchall()

    def _item_line(self, item_id: str, level: int) -> str:
        """항목의 짧은 표현을 반환한다."""
        row = self.x(
            "SELECT item_type, status, title, attrs FROM item WHERE item_id=?", (item_id,)
        ).fetchone()
        if row is None:
            return f"i:{item_id} NOT_FOUND"
        line = f"i:{item_id} {row[0]} {row[1] or '-'} {textnorm.q(row[2])}"
        if level >= 1 and row[3] not in (None, "{}"):
            line += f" attrs={row[3]}"
        return line

    def get(
        self,
        handles: list[str],
        level: int = 1,
        known: dict[str, str] | None = None,
        budget: int = 2000,
    ) -> str:
        """핸들의 L0·L1·L2 내용을 조회하고 알려진 digest를 건너뛴다."""
        out = Out(budget)
        known_digests = known or {}
        for handle in handles:
            try:
                kind, key = parse_handle(handle)
            except DocdbError:
                out.add(f"ERR {handle} BAD_HANDLE")
                continue
            if kind == "f":
                doc_id, kind_id, field_key = key
                row = self._field_row(doc_id, kind_id, field_key)
                if row is not None and known_digests.get(handle) == row[6]:
                    out.add(f"{handle} UNCHANGED")
                    continue
                line = self._field_line(doc_id, kind_id, field_key, level, row)
                if out.add(line) and level >= 1:
                    self._read("document", doc_id, row[6] if row is not None else None)
            elif kind == "d":
                doc_id = key[0]
                out.add(self._doc_line(doc_id))
                if level >= 1:
                    for field_kind, field_key in self._doc_fields(doc_id):
                        out.add(self._field_line(doc_id, field_kind, field_key, level - 1))
                    self._read("document", doc_id)
            elif kind == "i":
                item_id = key[0]
                line = self._item_line(item_id, level)
                dg_row = self.x("SELECT digest FROM item WHERE item_id=?", (item_id,)).fetchone()
                if dg_row and dg_row[0]:
                    line += f" dg={dg_row[0]}"
                if out.add(line) and level >= 1:
                    self._read("item", item_id)
            elif kind == "k":
                for field_key, required, description in self.x(
                    "SELECT field_key, is_required, description FROM cat_kind_field "
                    "WHERE kind_id=? ORDER BY sort_order",
                    (key[0],),
                ):
                    line = ("*" if required else "-") + field_key
                    if level >= 1:
                        line += f" {textnorm.q(description)}"
                    out.add(line)
            else:
                out.add(f"{handle} UNSUPPORTED")
        return self._finish("get", {"h": handles, "level": level}, out, f"level={level}")

    def find(self, query: str, limit: int = 10, budget: int = 800) -> str:
        """FTS trigram 또는 짧은 검색어의 LIKE로 필드와 항목을 찾는다."""
        terms = query.split()
        out = Out(budget)
        try:
            if all(len(term) >= 3 for term in terms):
                mode = "fts"
                match_query = " AND ".join(f'"{term}"' for term in terms)
                rows = self.x(
                    "SELECT rowid, snippet(fts_text, 0, '[', ']', '…', 8) FROM fts_text "
                    "WHERE fts_text MATCH ? ORDER BY bm25(fts_text) LIMIT ?",
                    (match_query, limit),
                ).fetchall()
            else:
                mode = "like"
                condition = " AND ".join(["body LIKE ?"] * len(terms))
                rows = self.x(
                    "SELECT unit_id, substr(body, max(1, instr(body, ?) - 20), 50) "
                    "FROM v_text_unit WHERE "
                    + condition
                    + " LIMIT ?",
                    (terms[0], *[f"%{term}%" for term in terms], limit),
                ).fetchall()
        except db.DatabaseError as exc:
            raise DocdbError("DB", str(exc)[:80]) from exc
        for unit_id, snippet in rows:
            unit = self.x(
                "SELECT src, doc_id, kind_id, field_key, item_id FROM text_unit WHERE unit_id=?",
                (unit_id,),
            ).fetchone()
            if unit[0] == "f":
                handle = f"f:{unit[1]}/{unit[2]}/{unit[3]}"
                if self._is_local_only(unit[1]):
                    out.add(f"{handle} policy=local_only")
                    continue
            else:
                handle = f"i:{unit[4]}"
            out.add(f"{handle} {textnorm.q(snippet)}")
        header = f"q={textnorm.q(query)} mode={mode} hits={len(rows)}"
        return self._finish("find", {"q": query, "limit": limit}, out, header)

    def trace(
        self,
        handle: str,
        rel_types: list[str] | None = None,
        depth: int = 1,
        budget: int = 800,
    ) -> str:
        """관계의 ID 연결만 지정 깊이만큼 반환한다."""
        kind, key = parse_handle(handle)
        frontier = [(_ENTITY.get(kind, kind), key[0])]
        seen: set[tuple[str, str]] = set()
        out = Out(budget)
        for _level in range(depth):
            next_frontier = []
            for entity_type, entity_id in frontier:
                if (entity_type, entity_id) in seen:
                    continue
                seen.add((entity_type, entity_id))
                sql = (
                    "SELECT from_type, from_id, rel_type, to_type, to_id FROM relation "
                    "WHERE ((from_type=? AND from_id=?) OR (to_type=? AND to_id=?))"
                )
                args: list = [entity_type, entity_id, entity_type, entity_id]
                if rel_types:
                    sql += " AND rel_type IN (" + ",".join("?" for _ in rel_types) + ")"
                    args.extend(rel_types)
                try:
                    rows = self.x(sql + " ORDER BY rel_id", args).fetchall()
                except db.DatabaseError as exc:
                    raise DocdbError("DB", str(exc)[:80]) from exc
                for from_type, from_id, rel_type, to_type, to_id in rows:
                    out.add(
                        f"{_PREFIX.get(from_type, from_type)}:{from_id} -{rel_type}-> "
                        f"{_PREFIX.get(to_type, to_type)}:{to_id}"
                    )
                    next_frontier.append(
                        (to_type, to_id)
                        if (from_type, from_id) == (entity_type, entity_id)
                        else (from_type, from_id)
                    )
            frontier = next_frontier
        return self._finish(
            "trace",
            {"h": handle, "rel": rel_types, "depth": depth},
            out,
            f"{handle} depth={depth}",
        )

    def since(
        self,
        cursor: int,
        work: str | None = None,
        budget: int = 1200,
        diff_lines: int = 6,
    ) -> str:
        """변경 커서 뒤의 엔터티별 변경과 짧은 본문 diff를 반환한다."""
        out = Out(budget)
        # 규칙 관계는 원인인 문서·항목 변경으로 이미 보고하므로 별도 집계하지 않는다.
        sql = (
            "SELECT entity_type, entity_id, max(change_id), count(*), "
            "group_concat(DISTINCT actor_id) FROM change_log WHERE change_id > ? "
            "AND NOT (entity_type='relation' AND substr(CASE "
            "WHEN op='delete' THEN json_extract(before_json, '$.source') "
            "WHEN op='insert' THEN json_extract(after_json, '$.source') "
            "ELSE coalesce(json_extract(after_json, '$.source'), "
            "(SELECT json_extract(c2.after_json, '$.source') FROM change_log c2 "
            "WHERE c2.entity_type='relation' AND c2.entity_id=change_log.entity_id "
            "AND c2.op='insert' AND c2.change_id<=change_log.change_id "
            "ORDER BY c2.change_id DESC LIMIT 1), "
            "(SELECT source FROM relation r "
            "WHERE CAST(r.rel_id AS TEXT)=change_log.entity_id)) END, 1, 5)='rule:')"
        )
        args: list = [cursor]
        if work:
            sql += " AND work_id = ?"
            args.append(work)
        rows = self.x(
            sql + " GROUP BY entity_type, entity_id ORDER BY max(change_id)", args
        ).fetchall()
        for entity_type, entity_id, last_id, count, actors in rows:
            if entity_type == "field_value":
                parts = entity_id.split("|")
                if len(parts) != 3:
                    raise DocdbError("DB", "field_value 이력 ID 형식이 올바르지 않음")
                handle = f"f:{parts[0]}/{parts[1]}/{parts[2]}"
            else:
                handle = f"{_PREFIX.get(entity_type, entity_type)}:{entity_id}"
            after_row = self.x(
                "SELECT after_json FROM change_log WHERE change_id=?", (last_id,)
            ).fetchone()
            before_row = self.x(
                "SELECT after_json FROM change_log WHERE entity_type=? AND entity_id=? "
                "AND change_id<=? ORDER BY change_id DESC LIMIT 1",
                (entity_type, entity_id, cursor),
            ).fetchone()
            after = json.loads(after_row[0] or "null")
            before = json.loads(before_row[0] or "null") if before_row else None
            line = f"{handle} x{count} by={actors} c:{last_id}"
            if isinstance(after, dict) and "value_text" in after:
                old_text = (before or {}).get("value_text") or ""
                new_text = after.get("value_text") or ""
                line += f" {len(old_text)}→{len(new_text)}ch"
                if self._is_local_only(handle.split(":", 1)[1].split("/", 1)[0]):
                    out.add(line + " policy=local_only")
                    continue
                if out.add(line):
                    differences = [
                        entry
                        for entry in difflib.unified_diff(
                            old_text.splitlines(), new_text.splitlines(), lineterm="", n=0
                        )
                        if entry[:1] in "+-" and entry[:3] not in ("+++", "---")
                    ]
                    for entry in differences[:diff_lines]:
                        out.add("  " + entry[:120])
                    if len(differences) > diff_lines:
                        out.add(
                            f"  … +{len(differences) - diff_lines} lines (get {handle} level=2)"
                        )
            else:
                out.add(line + f" new={textnorm.q(json.dumps(after, ensure_ascii=False)[:120])}")
        return self._finish(
            "since",
            {"cursor": cursor, "work": work},
            out,
            f"from=c:{cursor} entities={len(rows)}",
        )

    def ctx(self, work_id: str, budget: int = 1500) -> str:
        """작업 카드, 차단 요인, 관련 문서·항목을 예산 안에 묶는다."""
        self.work = work_id
        out = Out(budget)
        card = self.x(
            "SELECT title, status, owner_id, last_handoff_id, last_handoff_ack, next_actions, "
            "open_findings, unresolved_runs, stale_context FROM v_work_resume WHERE work_id=?",
            (work_id,),
        ).fetchone()
        if card is None:
            out.add(f"w:{work_id} NOT_FOUND_OR_CLOSED", force=True)
            return self._finish("ctx", {"w": work_id}, out, f"w:{work_id}")
        out.add(
            f"W {work_id} {card[1]} owner={card[2]} {textnorm.q(card[0])} "
            f"handoff={card[3] or '-'}:{card[4] or '-'}",
            force=True,
        )
        for action in json.loads(card[5] or "[]"):
            out.add(f"> {action}", force=True)
        for run_id, run_type, result in self.x(
            "SELECT run_id, run_type, result FROM run WHERE work_id=? "
            "AND result IN ('FAIL','NOT_RUN','ERROR','UNKNOWN') ORDER BY recorded_at",
            (work_id,),
        ):
            out.add(f"! r:{run_id} {run_type} {result}", force=True)
        findings = self.x(
            "SELECT f.finding_id, f.severity, f.code, f.target_type, f.target_id, f.message "
            "FROM finding f JOIN run r ON r.run_id=f.run_id "
            "WHERE r.work_id=? AND f.disposition='open'",
            (work_id,),
        ).fetchall()
        for finding_id, severity, code, target_type, target_id, message in sorted(
            findings, key=lambda row: (SEV[row[1]], row[0])
        ):
            out.add(
                f"! F{finding_id} {severity} {code} {target_type}:{target_id} "
                f"{textnorm.q(message[:80])}"
            )
        for target_type, target_id, read_id, latest_id in self.x(
            "SELECT target_type, target_id, read_change_id, latest_change_id "
            "FROM v_context_stale WHERE work_id=?",
            (work_id,),
        ):
            out.add(
                f"! stale {_PREFIX.get(target_type, target_type)}:{target_id} "
                f"read=c:{read_id} now=c:{latest_id}"
            )
        docs = [
            row[0]
            for row in self.x(
                "SELECT doc_id FROM document WHERE work_id=?1 OR origin_work_id=?1 "
                "UNION SELECT to_id FROM relation WHERE from_type='work' AND from_id=?1 "
                "AND to_type='document' "
                "UNION SELECT target_id FROM context_read WHERE work_id=?1 "
                "AND target_type='document' ORDER BY 1",
                (work_id,),
            )
        ]
        for doc_id in docs:
            out.add(self._doc_line(doc_id))
        if docs:
            placeholders = ",".join("?" for _ in docs)
            args = docs + docs
            items = self.x(
                "SELECT item_type, item_id FROM item WHERE defined_in IN (" + placeholders + ") "
                "UNION SELECT i.item_type, i.item_id FROM doc_scope s "
                "JOIN item i ON i.item_id=s.item_id WHERE s.doc_id IN (" + placeholders + ") "
                "ORDER BY 1, 2",
                args,
            ).fetchall()
        else:
            items = []
        by_type: dict[str, list[str]] = {}
        for item_type, item_id in items:
            by_type.setdefault(item_type, []).append(item_id)
        for item_type, item_ids in by_type.items():
            line = (
                f"I {item_type}×{len(item_ids)} {item_ids[0]}…{item_ids[-1]}"
                if len(item_ids) > 1
                else f"I {item_type} {item_ids[0]}"
            )
            out.add(line)
        for doc_id in docs:
            for kind_id, field_key in self._doc_fields(doc_id):
                row = self._field_row(doc_id, kind_id, field_key)
                line = self._field_line(doc_id, kind_id, field_key, 1, row, head=False)
                if out.add(line):
                    self._read("document", doc_id, row[6] if row else None)
        for _item_type, item_id in items:
            out.add(self._item_line(item_id, 0))
        if out.omitted:
            out.add("… omitted → get <handle> / since <cursor>", force=True)
        return self._finish(
            "ctx",
            {"w": work_id, "budget": budget},
            out,
            f"w:{work_id} docs={len(docs)} items={len(items)}",
        )

    def put(self, patches: list[dict], reason: str | None = None) -> str:
        """필드 패치를 원자적으로 저장하고 본문 없이 결과를 반환한다."""
        if not isinstance(patches, list):
            raise DocdbError("BAD_OP", "put 입력은 JSON 배열이어야 함")
        results: list[str] = []
        errors = 0
        successful = 0
        rollback_error: DocdbError | None = None
        try:
            with db.write_tx(self.con):
                for patch in patches:
                    if not isinstance(patch, dict):
                        handle = "?"
                        error = DocdbError("BAD_OP", "패치가 객체가 아님")
                    else:
                        handle = str(patch.get("h", "?"))
                        try:
                            patch_op = patch.get("op")
                            if patch_op == "link":
                                handle = str(patch.get("from", "?"))
                                for key in ("from", "rel", "to", "why"):
                                    if key not in patch:
                                        raise DocdbError("BAD_OP", f"link 필수 키 누락: {key}")
                                result = relations._propose(
                                    self.con,
                                    self.actor,
                                    patch["from"],
                                    patch["rel"],
                                    patch["to"],
                                    patch["why"],
                                    self.work,
                                )
                                results.append(result)
                                successful += 1
                                continue
                            if patch_op == "unlink":
                                rel_id = patch.get("rel_id")
                                handle = f"rel={rel_id}" if rel_id is not None else "rel=?"
                                for key in ("rel_id", "why"):
                                    if key not in patch:
                                        raise DocdbError("BAD_OP", f"unlink 필수 키 누락: {key}")
                                result = relations.withdraw(
                                    self.con, self.actor, patch["rel_id"], patch["why"]
                                )
                                results.append(result)
                                successful += 1
                                continue
                            kind, key = parse_handle(handle)
                            if kind == "f":
                                doc_id, kind_id, field_key = key
                                if self.x(
                                    "SELECT 1 FROM document d JOIN document_kind dk "
                                    "ON dk.doc_id=d.doc_id WHERE d.doc_id=? AND dk.kind_id=? "
                                    "AND EXISTS (SELECT 1 FROM cat_kind_field f WHERE f.kind_id=? "
                                    "AND f.field_key=?)",
                                    (doc_id, kind_id, kind_id, field_key),
                                ).fetchone() is None:
                                    raise DocdbError("NOT_FOUND", "문서 또는 필드를 찾을 수 없음")
                                row = self._field_row(doc_id, kind_id, field_key)
                                current_text = row[2] if row is not None else None
                                current_digest = row[6] if row is not None else None
                                if "expect" in patch and patch["expect"] != current_digest:
                                    raise DocdbError(
                                        "CONFLICT",
                                        f"now={current_digest} (since 로 변경분 확인 후 재시도)",
                                    )
                                op = patch.get("op", "set")
                                state = patch.get("state", "filled")
                                if state not in ("filled", "tbd", "unknown", "not_applicable"):
                                    raise DocdbError("BAD_OP", "필드 상태가 올바르지 않음")
                                if state != "filled":
                                    new_value = None
                                elif op == "set":
                                    if "text" not in patch:
                                        raise DocdbError("BAD_OP", "필수 키 누락: text")
                                    new_value = patch["text"]
                                elif op == "append":
                                    if "text" not in patch:
                                        raise DocdbError("BAD_OP", "필수 키 누락: text")
                                    new_value = (current_text + "\n" if current_text else "") + patch["text"]
                                elif op == "replace_section":
                                    if "anchor" not in patch or "text" not in patch:
                                        raise DocdbError("BAD_OP", "replace_section에 anchor와 text 필요")
                                    new_value = replace_section(
                                        current_text or "", patch["anchor"], patch["text"]
                                    )
                                    if new_value is None:
                                        raise DocdbError(
                                            "ANCHOR_NOT_FOUND", textnorm.q(patch["anchor"])
                                        )
                                else:
                                    raise DocdbError("BAD_OP", str(op))
                                new_value = textnorm.normalize_text(new_value)
                                field_reason = textnorm.normalize_text(patch.get("why"))
                                normalized_reason = textnorm.normalize_text(reason)
                                policy.assert_no_secrets(
                                    self.con, handle, [new_value, field_reason, normalized_reason]
                                )
                                digest = textnorm.digest16(new_value)
                                token_est = textnorm.tok_est(new_value)
                                changed_at = clock.now_iso()
                                self.x(
                                    "INSERT INTO field_value (doc_id, kind_id, field_key, fill_state, "
                                    "value_text, reason, "
                                    "author_id, updated_at, digest, tok_est) "
                                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                                    "ON CONFLICT(doc_id, kind_id, field_key) DO UPDATE SET "
                                    "fill_state=excluded.fill_state, value_text=excluded.value_text, "
                                    "reason=excluded.reason, "
                                    "author_id=excluded.author_id, updated_at=excluded.updated_at, "
                                    "digest=excluded.digest, tok_est=excluded.tok_est",
                                    (
                                        doc_id,
                                        kind_id,
                                        field_key,
                                        state,
                                        new_value,
                                        field_reason,
                                        self.actor.id,
                                        changed_at,
                                        digest,
                                        token_est,
                                    ),
                                )
                                history.append_change(
                                    self.con,
                                    "field_value",
                                    f"{doc_id}|{kind_id}|{field_key}",
                                    "insert" if row is None else "update",
                                    {"value_text": current_text} if row is not None else None,
                                    {"value_text": new_value, "fill_state": state},
                                    self.actor.id,
                                    self.work,
                                    normalized_reason or field_reason,
                                )
                                notes = relations.after_field_write(
                                    self.con,
                                    self.actor,
                                    doc_id,
                                    kind_id,
                                    field_key,
                                    new_value,
                                    self.work,
                                )
                                results.append(f"OK {handle} dg={digest} tok={token_est}")
                                results.extend(notes)
                                successful += 1
                                continue
                            elif kind == "i":
                                item_id = key[0]
                                op = patch.get("op", "upsert")
                                if op != "upsert":
                                    raise DocdbError("BAD_OP", f"지원하지 않는 항목 연산: {op}")
    
                                if "attrs" in patch and "attrs_patch" in patch:
                                    raise DocdbError("BAD_OP", "attrs와 attrs_patch를 함께 쓸 수 없음")
    
                                if "attrs" in patch and not isinstance(patch["attrs"], dict):
                                    raise DocdbError("BAD_OP", "attrs는 객체여야 함")
                                if "attrs_patch" in patch and not isinstance(patch["attrs_patch"], dict):
                                    raise DocdbError("BAD_OP", "attrs_patch는 객체여야 함")

                                if "title" in patch and not isinstance(patch["title"], str):
                                    raise DocdbError("BAD_OP", "title은 문자열이어야 함")
                                if "status" in patch and not isinstance(patch["status"], str):
                                    raise DocdbError("BAD_OP", "status는 문자열이어야 함")
    
                                row = self.x(
                                    "SELECT project_id, item_type, title, status, defined_in, attrs, digest "
                                    "FROM item WHERE item_id=?",
                                    (item_id,),
                                ).fetchone()
    
                                if row is None:
                                    ids.validate("item", item_id)
                                    ids.ensure_not_reused(self.con, "item", item_id)
    
                                    prefix_rows = self.x(
                                        "SELECT item_type, id_prefix FROM cat_item_type"
                                    ).fetchall()
                                    matching = [
                                        (it, pfx) for it, pfx in prefix_rows if item_id.startswith(pfx)
                                    ]
                                    if not matching:
                                        raise DocdbError("ID_FORMAT", f"항목 ID에 일치하는 접두어 없음: {item_id}")
                                    matching.sort(key=lambda pair: len(pair[1]), reverse=True)
                                    item_type = matching[0][0]
    
                                    if "title" not in patch or patch["title"] is None:
                                        raise DocdbError("BAD_OP", "새 항목에는 title 필수")
    
                                    cur_title = None
                                    cur_status = None
                                    cur_defined_in = None
                                    cur_attrs_obj = {}
                                    cur_digest = None
                                else:
                                    item_type = row[1]
                                    cur_title = row[2]
                                    cur_status = row[3]
                                    cur_defined_in = row[4]
                                    cur_attrs_obj = json.loads(row[5] or "{}")
                                    cur_digest = row[6]
    
                                if "expect" in patch and patch["expect"] != cur_digest:
                                    raise DocdbError("CONFLICT", f"now={cur_digest}")
    
                                new_title = patch.get("title", cur_title)
                                if new_title is not None:
                                    new_title = textnorm.normalize_text(new_title)
    
                                new_status = patch.get("status", cur_status)
                                if new_status is not None:
                                    new_status = textnorm.normalize_text(new_status)
    
                                if "defined_in" in patch:
                                    def_in_raw = patch["defined_in"]
                                    if def_in_raw is None:
                                        new_defined_in = None
                                    elif isinstance(def_in_raw, str) and def_in_raw.startswith("d:"):
                                        _, d_key = parse_handle(def_in_raw)
                                        new_defined_in = d_key[0]
                                    else:
                                        new_defined_in = def_in_raw
                                else:
                                    new_defined_in = cur_defined_in
    
                                if "attrs" in patch:
                                    new_attrs_obj = patch["attrs"]
                                elif "attrs_patch" in patch:
                                    merged = dict(cur_attrs_obj)
                                    for k, v in patch["attrs_patch"].items():
                                        if v is None:
                                            merged.pop(k, None)
                                        else:
                                            merged[k] = v
                                    new_attrs_obj = merged
                                else:
                                    new_attrs_obj = cur_attrs_obj
    
                                field_reason = textnorm.normalize_text(patch.get("why"))
                                normalized_reason = textnorm.normalize_text(reason)
                                attrs_str = json.dumps(new_attrs_obj, ensure_ascii=False)
                                policy.assert_no_secrets(
                                    self.con,
                                    handle,
                                    [new_title, attrs_str, field_reason, normalized_reason],
                                )
    
                                def_in_handle = f"d:{new_defined_in}" if new_defined_in else None
                                digest_payload = {
                                    "title": new_title,
                                    "status": new_status,
                                    "attrs": new_attrs_obj,
                                    "defined_in": def_in_handle,
                                }
                                canon = textnorm.canonical_json(digest_payload)
                                digest = textnorm.digest16(canon)
                                token_est = textnorm.tok_est(canon)
    
                                proj_row = self.x("SELECT project_id FROM project LIMIT 1").fetchone()
                                if not proj_row:
                                    raise DocdbError("NOT_FOUND", "project")
                                project_id = proj_row[0]
    
                                now_ts = clock.now_iso()
                                new_attrs_json = textnorm.canonical_json(new_attrs_obj)
    
                                if row is None:
                                    self.x(
                                        "INSERT INTO item (item_id, project_id, item_type, title, status, "
                                        "defined_in, attrs, created_at, updated_at, digest, tok_est) "
                                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                        (
                                            item_id,
                                            project_id,
                                            item_type,
                                            new_title,
                                            new_status,
                                            new_defined_in,
                                            new_attrs_json,
                                            now_ts,
                                            now_ts,
                                            digest,
                                            token_est,
                                        ),
                                    )
                                    history.append_change(
                                        self.con,
                                        "item",
                                        item_id,
                                        "insert",
                                        None,
                                        {
                                            "title": new_title,
                                            "status": new_status,
                                            "attrs": new_attrs_obj,
                                            "defined_in": new_defined_in,
                                            "item_type": item_type,
                                        },
                                        self.actor.id,
                                        self.work,
                                        normalized_reason or field_reason,
                                    )
                                else:
                                    self.x(
                                        "UPDATE item SET title=?, status=?, defined_in=?, attrs=?, "
                                        "updated_at=?, digest=?, tok_est=? WHERE item_id=?",
                                        (
                                            new_title,
                                            new_status,
                                            new_defined_in,
                                            new_attrs_json,
                                            now_ts,
                                            digest,
                                            token_est,
                                            item_id,
                                        ),
                                    )
                                    history.append_change(
                                        self.con,
                                        "item",
                                        item_id,
                                        "update",
                                        {
                                            "title": cur_title,
                                            "status": cur_status,
                                            "attrs": cur_attrs_obj,
                                            "defined_in": cur_defined_in,
                                        },
                                        {
                                            "title": new_title,
                                            "status": new_status,
                                            "attrs": new_attrs_obj,
                                            "defined_in": new_defined_in,
                                        },
                                        self.actor.id,
                                        self.work,
                                        normalized_reason or field_reason,
                                    )

                                relations.after_item_write(
                                    self.con, self.actor, item_id, self.work
                                )
    
                                results.append(f"OK {handle} dg={digest} tok={token_est}")
                                successful += 1
                                continue
                            else:
                                raise DocdbError("UNSUPPORTED_HANDLE")
                        except DocdbError as exc:
                            error = exc
                        except db.DatabaseError as exc:
                            error = DocdbError("DB", str(exc)[:80])
                    detail = textnorm.q(error.detail) if error.code == "DB" else error.detail
                    suffix = f" {detail}" if detail else ""
                    results.append(f"ERR {handle} {error.code}{suffix}")
                    errors += 1
                    if rollback_error is None:
                        rollback_error = DocdbError("BAD_OP", "put 전체를 취소함")
                if rollback_error is not None:
                    raise rollback_error
        except DocdbError as exc:
            if exc is not rollback_error:
                raise
        out = Out(None)
        out.add(
            f"{'APPLIED' if errors == 0 else 'NOTHING_APPLIED'} ok={successful} err={errors}"
        )
        for result in results:
            out.add(result)
        return self._finish("put", {"n": len(patches)}, out, f"n={len(patches)}")


def replace_section(text: str, anchor: str, new_body: str) -> str | None:
    """Markdown 제목의 같은 수준 이상 다음 제목 전까지 내용을 교체한다."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != anchor.strip():
            continue
        level = len(line) - len(line.lstrip("#"))
        end = index + 1
        while end < len(lines):
            candidate = lines[end]
            candidate_level = len(candidate) - len(candidate.lstrip("#"))
            if (
                candidate_level
                and candidate_level <= level
                and candidate[candidate_level : candidate_level + 1] == " "
            ):
                break
            end += 1
        return "\n".join(lines[: index + 1] + new_body.splitlines() + lines[end:])
    return None
