"""JSON 카탈로그를 읽고 데이터베이스의 cat_* 테이블에 적재한다."""
import hashlib
import json
import re
from pathlib import Path

from docdb import clock, db, history
from docdb.actors import Actor
from docdb.db import Connection
from docdb.errors import DocdbError

VIEW_KINDS = {"A01", "D08", "F06", "A03"}
AUTO_KINDS = {"V05"}
RUN_FILES = ("evidence.json", "results.json", "run.json", "report.json", "findings.json")
PRIMARY_ALIASES = {
    "리소스 가용성 계획": "B03",
    "작업 환경": "G01",
    "제품 성공 기준": "C01",
    "환경별 설정 비교표": "S04",
}
_DEFINED_FIELD = re.compile(r"\(([^()]+)\)")


def _kind_context(kind: object, index: int) -> str:
    """카탈로그 종류를 오류 detail에 안전하게 표시한다."""
    if isinstance(kind, dict):
        kind_id = kind.get("kind_id")
        if isinstance(kind_id, str) and re.fullmatch(r"[A-Z][0-9]{2}", kind_id):
            return f"종류 {kind_id}"
    return f"document_types[{index + 1}]"


def _catalogue_detail(context: str, exc: KeyError | TypeError) -> str:
    """카탈로그 자료 오류를 파일 본문 없이 짧게 설명한다."""
    if isinstance(exc, KeyError):
        key = exc.args[0] if exc.args else None
        if isinstance(key, str) and re.fullmatch(r"[a-z_]+", key):
            return f"{context} 필수 키 누락: {key}"
        return f"{context} 필수 키 누락"
    return f"{context} 자료 형식이 올바르지 않음"


def storage_class(kind: dict) -> str:
    """종류의 파일 구성과 수명주기를 저장 방식으로 분류한다."""
    files = kind["content_files"]
    if kind["kind_id"] in VIEW_KINDS:
        return "view"
    if kind["kind_id"] in AUTO_KINDS:
        return "run"
    if kind["lifecycle"] == "work" and any(name in RUN_FILES for name in files):
        return "run"
    if "detail.md" not in files:
        return "register"
    return "narrative"


def _read_json(path: Path) -> tuple[bytes, object]:
    """UTF-8 JSON 파일의 원본 바이트와 파싱 결과를 돌려준다."""
    try:
        raw = Path(path).read_bytes()
        data = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DocdbError("BAD_ARGS", f"카탈로그 JSON을 읽을 수 없음: {path}") from exc
    return raw, data


def _field_parts(value: object) -> tuple[str, dict | None]:
    """필드 설명 문자열과 관계 추출 규칙을 분리한다."""
    if isinstance(value, str):
        return value, None
    if isinstance(value, dict) and isinstance(value.get("description"), str):
        extract_rel = value.get("extract_rel")
        if extract_rel is not None and not isinstance(extract_rel, dict):
            raise DocdbError("BAD_ARGS", "extract_rel은 JSON 객체여야 함")
        return value["description"], extract_rel
    raise DocdbError("BAD_ARGS", "필드 설명은 문자열 또는 객체여야 함")


def _incompatible(con: Connection, kinds: set[str], fields: set[tuple[str, str]]) -> None:
    """값이 저장된 종류나 필드가 새 카탈로그에서 빠지는지 검사한다."""
    old_kinds = {row[0] for row in con.execute("SELECT kind_id FROM cat_kind")}
    removed_kinds = old_kinds - kinds
    for kind_id in removed_kinds:
        used = con.execute(
            "SELECT 1 FROM document_kind WHERE kind_id = ? LIMIT 1", (kind_id,)
        ).fetchone()
        if used is not None:
            raise DocdbError(
                "CATALOGUE_INCOMPATIBLE", f"사용 중인 종류를 제거할 수 없음: {kind_id}"
            )
        item_used = con.execute(
            "SELECT 1 FROM item AS i JOIN cat_item_type AS t ON t.item_type = i.item_type "
            "WHERE t.defined_by_kind = ? LIMIT 1",
            (kind_id,),
        ).fetchone()
        if item_used is not None:
            raise DocdbError(
                "CATALOGUE_INCOMPATIBLE", f"항목이 있는 종류를 제거할 수 없음: {kind_id}"
            )

    for kind_id, field_key in con.execute(
        "SELECT kind_id, field_key FROM cat_kind_field"
    ):
        if (kind_id, field_key) not in fields:
            used = con.execute(
                "SELECT 1 FROM field_value WHERE kind_id = ? AND field_key = ? LIMIT 1",
                (kind_id, field_key),
            ).fetchone()
            if used is not None:
                raise DocdbError(
                    "CATALOGUE_INCOMPATIBLE",
                    f"값이 있는 필드를 제거할 수 없음: {kind_id}.{field_key}",
                )


def _remove_stale_rows(
    con: Connection,
    kinds: set[str],
    fields: set[tuple[str, str]],
    links: set[tuple[str, str]],
    aliases: set[tuple[str, str]],
    item_types: set[str],
) -> None:
    """새 카탈로그에 없는 참조 행을 종속 순서대로 지운다."""
    for kind_id, target_kind_id in con.execute(
        "SELECT kind_id, target_kind_id FROM cat_kind_link"
    ).fetchall():
        if (kind_id, target_kind_id) not in links:
            con.execute(
                "DELETE FROM cat_kind_link WHERE kind_id = ? AND target_kind_id = ?",
                (kind_id, target_kind_id),
            )
    for kind_id, field_key in con.execute(
        "SELECT kind_id, field_key FROM cat_kind_field"
    ).fetchall():
        if (kind_id, field_key) not in fields:
            con.execute(
                "DELETE FROM cat_kind_field WHERE kind_id = ? AND field_key = ?",
                (kind_id, field_key),
            )
    for kind_id, name in con.execute(
        "SELECT kind_id, name FROM cat_kind_alias"
    ).fetchall():
        if (kind_id, name) not in aliases:
            con.execute(
                "DELETE FROM cat_kind_alias WHERE kind_id = ? AND name = ?",
                (kind_id, name),
            )
    for (item_type,) in con.execute("SELECT item_type FROM cat_item_type").fetchall():
        if item_type not in item_types:
            con.execute("DELETE FROM cat_item_type WHERE item_type = ?", (item_type,))
    for (kind_id,) in con.execute("SELECT kind_id FROM cat_kind").fetchall():
        if kind_id not in kinds:
            con.execute("DELETE FROM cat_kind WHERE kind_id = ?", (kind_id,))


def load(
    con: Connection,
    catalogue_path: Path,
    item_types_path: Path,
    actor: Actor | None,
) -> dict:
    """JSON 카탈로그와 항목 종류를 검사해 cat_* 테이블에 적재한다."""
    catalogue_raw, catalogue = _read_json(catalogue_path)
    _item_types_raw, item_types_data = _read_json(item_types_path)
    if not isinstance(catalogue, dict) or not isinstance(item_types_data, list):
        raise DocdbError("BAD_ARGS", "카탈로그 최상위 형식이 올바르지 않음")

    try:
        version = catalogue["standard_version"]
        schema_version = catalogue["schema_version"]
        groups = catalogue["groups"]
        facts = catalogue["facts"]
        kinds_data = catalogue["document_types"]
        if not isinstance(version, str) or not isinstance(schema_version, str):
            raise DocdbError("BAD_ARGS", "카탈로그 버전은 문자열이어야 함")
        if not isinstance(groups, dict) or not isinstance(facts, dict) or not isinstance(kinds_data, list):
            raise DocdbError("BAD_ARGS", "카탈로그 자료 구조가 올바르지 않음")
    except KeyError as exc:
        raise DocdbError("BAD_ARGS", f"카탈로그 필수 키가 없음: {exc.args[0]}") from exc

    source_digest = "sha256:" + hashlib.sha256(catalogue_raw).hexdigest()
    kinds: set[str] = set()
    fields: set[tuple[str, str]] = set()
    links: set[tuple[str, str]] = set()
    aliases: set[tuple[str, str]] = set()
    entry_context = "카탈로그 종류"
    selected_items = []
    retained_item_types: set[str] = set()
    try:
        for index, kind in enumerate(kinds_data):
            entry_context = _kind_context(kind, index)
            kind_id = kind["kind_id"]
            kinds.add(kind_id)
            entry_context = f"{_kind_context(kind, index)} 필드"
            for field_key in kind["required_fields"]:
                fields.add((kind_id, field_key))
            for field_key in kind["optional_fields"]:
                fields.add((kind_id, field_key))
            entry_context = f"{_kind_context(kind, index)} 링크"
            links.update((kind_id, target) for target in kind["required_link_kinds"])
            links.update((kind_id, target) for target in kind["optional_link_kinds"])
            entry_context = f"{_kind_context(kind, index)} 별칭"
            aliases.update((kind_id, name) for name in kind["covered_names"])

        for index, item in enumerate(item_types_data):
            entry_context = f"항목 종류 #{index + 1}"
            if item["defined_by_kind"] in kinds:
                selected_items.append(item)
                retained_item_types.add(item["item_type"])
    except (KeyError, TypeError) as exc:
        raise DocdbError("BAD_ARGS", _catalogue_detail(entry_context, exc)) from exc

    counts = {
        "catalogue_version": version,
        "kinds": len(kinds_data),
        "fields": len(fields),
        "links": len(links),
        "aliases": len(aliases),
        "item_types": len(selected_items),
        "item_types_skipped": len(item_types_data) - len(selected_items),
    }

    entry_context = "카탈로그"
    try:
        with db.write_tx(con):
            _incompatible(con, kinds, fields)
            existing_version = con.execute(
                "SELECT schema_version, source_digest, loaded_at FROM cat_version "
                "WHERE catalogue_version = ?",
                (version,),
            ).fetchone()
            changed = existing_version is None or existing_version[:2] != (
                schema_version,
                source_digest,
            )
            loaded_at = clock.now_iso() if changed else existing_version[2]
            con.execute(
                "INSERT INTO cat_version (catalogue_version, schema_version, source_digest, loaded_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(catalogue_version) DO UPDATE SET "
                "schema_version = excluded.schema_version, source_digest = excluded.source_digest, "
                "loaded_at = excluded.loaded_at",
                (version, schema_version, source_digest, loaded_at),
            )

            for code, group in groups.items():
                entry_context = "그룹 항목"
                con.execute(
                    "INSERT INTO cat_group (group_code, directory, title_ko, title_en, "
                    "default_owner_role, review_days) VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(group_code) DO UPDATE SET directory = excluded.directory, "
                    "title_ko = excluded.title_ko, title_en = excluded.title_en, "
                    "default_owner_role = excluded.default_owner_role, review_days = excluded.review_days",
                    (
                        code,
                        group["directory"],
                        group["title_ko"],
                        group["title_en"],
                        group["default_owner_role"],
                        group["review_days"],
                    ),
                )

            for fact_type in ("cap", "evt"):
                for key, description in facts.get(fact_type, {}).items():
                    entry_context = "사실 항목"
                    con.execute(
                        "INSERT INTO cat_fact (fact_key, fact_type, description, derived_rule) "
                        "VALUES (?, ?, ?, NULL) ON CONFLICT(fact_key) DO UPDATE SET "
                        "fact_type = excluded.fact_type, description = excluded.description, "
                        "derived_rule = excluded.derived_rule",
                        (f"{fact_type}.{key}", fact_type, description),
                    )

            for index, kind in enumerate(kinds_data):
                entry_context = _kind_context(kind, index)
                con.execute(
                    "INSERT INTO cat_kind (kind_id, abbreviation, title_ko, title_en, group_code, "
                    "lifecycle, mode, storage_class, owner_role, review_days, trigger_text, "
                    "required_when, applies_when, content_files) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(kind_id) DO UPDATE SET abbreviation = excluded.abbreviation, "
                    "title_ko = excluded.title_ko, title_en = excluded.title_en, "
                    "group_code = excluded.group_code, lifecycle = excluded.lifecycle, "
                    "mode = excluded.mode, storage_class = excluded.storage_class, "
                    "owner_role = excluded.owner_role, review_days = excluded.review_days, "
                    "trigger_text = excluded.trigger_text, required_when = excluded.required_when, "
                    "applies_when = excluded.applies_when, content_files = excluded.content_files",
                    (
                        kind["kind_id"],
                        kind["abbreviation"],
                        kind["title_ko"],
                        kind["title_en"],
                        kind["category"],
                        kind["lifecycle"],
                        kind["mode"],
                        storage_class(kind),
                        kind["owner_role"],
                        kind["review_days"],
                        kind["trigger"],
                        json.dumps(kind["required_when"], ensure_ascii=False),
                        json.dumps(kind["applies_when"], ensure_ascii=False),
                        json.dumps(kind["content_files"], ensure_ascii=False),
                    ),
                )

            for index, kind in enumerate(kinds_data):
                entry_context = _kind_context(kind, index)
                kind_id = kind["kind_id"]
                preserve = set(kind.get("brief_must_preserve_keys", []))
                sort_order = 0
                for is_required, field_map in (
                    (1, kind["required_fields"]),
                    (0, kind["optional_fields"]),
                ):
                    entry_context = f"{_kind_context(kind, index)} 필드"
                    for field_key, field_value in field_map.items():
                        sort_order += 1
                        description, extract_rel = _field_parts(field_value)
                        con.execute(
                            "INSERT INTO cat_kind_field (kind_id, field_key, is_required, description, "
                            "brief_preserve, sort_order, extract_rel) VALUES (?, ?, ?, ?, ?, ?, ?) "
                            "ON CONFLICT(kind_id, field_key) DO UPDATE SET "
                            "is_required = excluded.is_required, description = excluded.description, "
                            "brief_preserve = excluded.brief_preserve, sort_order = excluded.sort_order, "
                            "extract_rel = excluded.extract_rel",
                            (
                                kind_id,
                                field_key,
                                is_required,
                                description,
                                int(field_key in preserve),
                                sort_order,
                                json.dumps(extract_rel, ensure_ascii=False)
                                if extract_rel is not None
                                else None,
                            ),
                        )
                for required, targets in (
                    (1, kind["required_link_kinds"]),
                    (0, kind["optional_link_kinds"]),
                ):
                    entry_context = f"{_kind_context(kind, index)} 링크"
                    for target in targets:
                        con.execute(
                            "INSERT INTO cat_kind_link (kind_id, target_kind_id, is_required) "
                            "VALUES (?, ?, ?) ON CONFLICT(kind_id, target_kind_id) "
                            "DO UPDATE SET is_required = excluded.is_required",
                            (kind_id, target, required),
                        )
                entry_context = f"{_kind_context(kind, index)} 별칭"
                for name in kind["covered_names"]:
                    is_primary = int(
                        name not in PRIMARY_ALIASES
                        or PRIMARY_ALIASES[name] == kind_id
                    )
                    con.execute(
                        "INSERT INTO cat_kind_alias (kind_id, name, is_primary) VALUES (?, ?, ?) "
                        "ON CONFLICT(kind_id, name) DO UPDATE SET is_primary = excluded.is_primary",
                        (kind_id, name, is_primary),
                    )

            for index, item in enumerate(selected_items):
                entry_context = f"항목 종류 #{index + 1}"
                con.execute(
                    "INSERT INTO cat_item_type (item_type, id_prefix, defined_by_kind, description, "
                    "defined_by_field) VALUES (?, ?, ?, ?, ?) ON CONFLICT(item_type) DO UPDATE SET "
                    "id_prefix = excluded.id_prefix, defined_by_kind = excluded.defined_by_kind, "
                    "description = excluded.description, defined_by_field = excluded.defined_by_field",
                    (
                        item["item_type"],
                        item["id_prefix"],
                        item["defined_by_kind"],
                        item["description"],
                        item["defined_by_field"],
                    ),
                )

            _remove_stale_rows(
                con, kinds, fields, links, aliases, retained_item_types
            )
            if changed and actor is not None:
                history.append_change(
                    con=con,
                    entity_type="catalogue",
                    entity_id=version,
                    op="insert" if existing_version is None else "update",
                    before=(
                        {
                            "schema_version": existing_version[0],
                            "source_digest": existing_version[1],
                            "loaded_at": existing_version[2],
                        }
                        if existing_version is not None
                        else None
                    ),
                    after={
                        "schema_version": schema_version,
                        "source_digest": source_digest,
                        "loaded_at": loaded_at,
                    },
                    actor_id=actor.id,
                    work_id=None,
                    reason=None,
                )
    except DocdbError:
        raise
    except (KeyError, TypeError) as exc:
        raise DocdbError("BAD_ARGS", _catalogue_detail(entry_context, exc)) from exc
    except db.DatabaseError as exc:
        raise DocdbError("DB", str(exc)[:80]) from exc

    return counts
