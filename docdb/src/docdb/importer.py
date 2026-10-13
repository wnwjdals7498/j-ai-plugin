"""문서 파일을 구간으로 나누고 규칙에 맞는 필드를 적용한다."""
import re
from pathlib import Path

from docdb import backup, clock, db, history, ids, paths, policy, textnorm
from docdb.actors import ACTOR_PATTERNS, Actor
from docdb.db import Connection
from docdb.errors import DocdbError
from docdb.llm import DocDB

SUPPORTED_SUFFIXES = (".md", ".markdown", ".txt")
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_OWNER_ID = re.compile(ACTOR_PATTERNS["human"], re.ASCII)
_EXPORT_POLICIES = {"public", "approved_providers", "local_only"}


def _line_content(line: str) -> str:
    """줄 끝의 개행 문자를 제거한다."""
    if line.endswith("\r\n"):
        return line[:-2]
    if line.endswith(("\r", "\n")):
        return line[:-1]
    return line


def _heading_lines(raw: bytes) -> list[tuple[int, int, str]]:
    """코드 펜스 밖 제목의 바이트 위치와 제목을 반환한다."""
    offset = 3 if raw.startswith(b"\xef\xbb\xbf") else 0
    content = raw[offset:]
    found: list[tuple[int, int, str]] = []
    fence: str | None = None
    for line in content.splitlines(keepends=True):
        end = offset + len(line)
        decoded = line.decode("utf-8")
        clean = _line_content(decoded)
        if fence is not None:
            if clean.startswith(fence):
                fence = None
        elif clean.startswith("```"):
            fence = "```"
        elif clean.startswith("~~~"):
            fence = "~~~"
        else:
            match = _HEADING.fullmatch(clean)
            if match is not None:
                found.append((offset, end, match.group(2)))
        offset = end
    return found


def split_segments(raw: bytes) -> list[dict]:
    """UTF-8 원문을 제목 줄 기준 구간으로 나누고 바이트 오프셋을 붙인다."""
    if raw.startswith(b"\xef\xbb\xbf"):
        base = 3
        content = raw[base:]
    else:
        base = 0
        content = raw
    content.decode("utf-8")
    headings = _heading_lines(raw)
    starts = [offset for offset, _end, _heading in headings]
    segments: list[dict] = []

    if starts and content[: starts[0] - base].decode("utf-8").strip():
        end = starts[0]
        segments.append(
            {
                "seq": 0,
                "heading": None,
                "start_off": base,
                "end_off": end,
                "text": raw[base:end].decode("utf-8"),
            }
        )

    for index, (start, _heading_end, heading) in enumerate(headings):
        end = headings[index + 1][0] if index + 1 < len(headings) else len(raw)
        segments.append(
            {
                "seq": len(segments),
                "heading": heading,
                "start_off": start,
                "end_off": end,
                "text": raw[start:end].decode("utf-8"),
            }
        )

    if not headings and content.decode("utf-8").strip():
        segments.append(
            {
                "seq": 0,
                "heading": None,
                "start_off": base,
                "end_off": len(raw),
                "text": raw[base:].decode("utf-8"),
            }
        )
    return segments


def _project_root(con: Connection) -> Path:
    """연결된 프로젝트 DB에서 프로젝트 루트를 찾는다."""
    row = con.execute("PRAGMA database_list").fetchone()
    if row is None or not row[2]:
        raise DocdbError("BAD_ARGS", "프로젝트 DB 경로를 확인할 수 없음")
    return paths.project_root(Path(row[2])).resolve()


def _walk(folder: Path):
    """숨김 폴더와 .docdb를 제외하고 하위 파일을 이름순으로 순회한다."""
    for entry in sorted(folder.iterdir(), key=lambda item: item.name):
        if entry.is_symlink():
            continue
        if entry.is_dir():
            if entry.name.startswith(".") or entry.name == paths.DOT_DIR:
                continue
            yield from _walk(entry)
        elif entry.is_file():
            yield entry


def _kind_for(con: Connection, path: Path, raw: bytes) -> str | None:
    """파일 이름이나 첫 H1과 일치하는 단일 문서 종류를 찾는다."""
    try:
        raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None

    first_title = None
    for start, _end, heading in _heading_lines(raw):
        line = raw[start:].splitlines(keepends=True)[0].decode("utf-8")
        match = _HEADING.fullmatch(_line_content(line))
        if match is not None and len(match.group(1)) == 1:
            first_title = heading
            break
    candidates = {textnorm.norm_key(path.stem)}
    if first_title:
        candidates.add(textnorm.norm_key(first_title))

    matches: set[str] = set()
    for kind_id, title in con.execute("SELECT kind_id, title_ko FROM cat_kind"):
        if textnorm.norm_key(title) in candidates:
            matches.add(kind_id)
    for kind_id, alias in con.execute(
        "SELECT kind_id, name FROM cat_kind_alias WHERE is_primary=1"
    ):
        if textnorm.norm_key(alias) in candidates:
            matches.add(kind_id)
    return next(iter(matches)) if len(matches) == 1 else None


def _field_for(con: Connection, kind_id: str, heading: str | None) -> str | None:
    """제목이 종류 필드 키나 설명 첫 마디와 일치하는지 찾는다."""
    if heading is None:
        return None
    key = textnorm.norm_key(heading)
    matches: set[str] = set()
    for field_key, description in con.execute(
        "SELECT field_key, description FROM cat_kind_field WHERE kind_id=?",
        (kind_id,),
    ):
        first_phrase = re.split(r"[(:,—]", description, maxsplit=1)[0]
        if key in {textnorm.norm_key(field_key), textnorm.norm_key(first_phrase)}:
            matches.add(field_key)
    return next(iter(matches)) if len(matches) == 1 else None


def _record_artifact(
    con: Connection, actor: Actor, uri: str, digest: str
) -> str | None:
    """새 원본 파일을 등록하고 생성 이력을 기록한다."""
    existing = con.execute(
        "SELECT artifact_id FROM artifact WHERE uri=? AND digest=?",
        (uri, digest),
    ).fetchone()
    if existing is not None:
        return None
    artifact_id = ids.next_id(con, "artifact")
    con.execute(
        "INSERT INTO artifact (artifact_id, uri, digest, role, source_mode) "
        "VALUES (?, ?, ?, 'primary', 'authored')",
        (artifact_id, uri, digest),
    )
    history.append_change(
        con,
        "artifact",
        artifact_id,
        "insert",
        None,
        {
            "artifact_id": artifact_id,
            "uri": uri,
            "digest": digest,
            "role": "primary",
            "source_mode": "authored",
        },
        actor.id,
        None,
        None,
    )
    return artifact_id


def _new_doc_id(con: Connection, kind_id: str, path: Path, artifact_id: str) -> str:
    """파일명 또는 artifact 순번으로 충돌 없는 문서 ID를 만든다."""
    stem = re.sub(r"[^A-Za-z0-9]", "", path.stem).upper()[:32]
    if not stem:
        stem = f"IMP-{int(artifact_id.rsplit('-', 1)[1]):04d}"
    base = f"DOC-{kind_id}-{stem}"
    candidate = base
    suffix = 2
    while True:
        ids.validate("doc", candidate)
        try:
            ids.ensure_not_reused(con, "document", candidate)
        except DocdbError as exc:
            if exc.code != "ID_FORMAT":
                raise
        else:
            if con.execute("SELECT 1 FROM document WHERE doc_id=?", (candidate,)).fetchone() is None:
                return candidate
        candidate = f"{base}-{suffix}"
        suffix += 1


def _document_title(raw: bytes, path: Path) -> str:
    """첫 H1 또는 원본 파일 이름을 문서 제목으로 반환한다."""
    try:
        for start, _end, heading in _heading_lines(raw):
            line = raw[start:].splitlines(keepends=True)[0].decode("utf-8")
            match = _HEADING.fullmatch(_line_content(line))
            if match is not None and len(match.group(1)) == 1:
                return textnorm.normalize_text(heading) or path.name
    except UnicodeDecodeError:
        pass
    return textnorm.normalize_text(path.name) or path.name


def _create_document(
    con: Connection,
    actor: Actor,
    project_id: str,
    kind_id: str,
    path: Path,
    raw: bytes,
    artifact_id: str,
    owner: str,
    export_policy: str,
) -> str:
    """종류 기본값을 사용해 문서를 만들고 이력을 남긴다."""
    kind = con.execute(
        "SELECT lifecycle, group_code FROM cat_kind WHERE kind_id=?", (kind_id,)
    ).fetchone()
    if kind is None:
        raise DocdbError("NOT_FOUND", f"문서 종류를 찾을 수 없음: {kind_id}")
    doc_id = _new_doc_id(con, kind_id, path, artifact_id)
    title = _document_title(raw, path)
    policy.assert_no_secrets(con, f"d:{doc_id}", [title])
    now = clock.now_iso()
    con.execute(
        "INSERT INTO document (doc_id, project_id, title, lifecycle, category, authority, "
        "baseline_mode, owner_id, export_policy, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 'normative', 'desired', ?, ?, ?, ?)",
        (doc_id, project_id, title, kind[0], kind[1], owner, export_policy, now, now),
    )
    con.execute(
        "INSERT INTO document_kind (doc_id, kind_id) VALUES (?, ?)", (doc_id, kind_id)
    )
    history.append_change(
        con,
        "document_kind",
        f"{doc_id}|{kind_id}",
        "insert",
        None,
        {"doc_id": doc_id, "kind_id": kind_id},
        actor.id,
        None,
        None,
    )
    history.append_change(
        con,
        "document",
        doc_id,
        "insert",
        None,
        {
            "doc_id": doc_id,
            "project_id": project_id,
            "title": title,
            "lifecycle": kind[0],
            "category": kind[1],
            "authority": "normative",
            "baseline_mode": "desired",
            "owner_id": owner,
            "export_policy": export_policy,
        },
        actor.id,
        None,
        None,
    )
    return doc_id


def _is_stale(path: Path, segment: dict) -> bool:
    """현재 원본의 해당 바이트 구간이 등록 당시 digest와 같은지 확인한다."""
    try:
        raw = path.read_bytes()
        current = raw[segment["start_off"] : segment["end_off"]].decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return True
    return textnorm.digest16(current) != textnorm.digest16(segment["text"])


def _segment_body(segment: dict) -> str:
    """제목 줄을 빼고 앞뒤 빈 줄만 제거한 본문을 반환한다."""
    lines = textnorm.normalize_text(segment["text"]).split("\n")[1:]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def _apply_segments(
    con: Connection,
    actor: Actor,
    project_id: str,
    path: Path,
    raw: bytes,
    artifact_id: str,
    owner: str,
    export_policy: str,
    segments: list[dict],
) -> list[str]:
    """규칙이 맞는 구간을 적용하고 구간 상태를 갱신한다."""
    ruled = [segment for segment in segments if segment["field_key"] is not None]
    if not ruled:
        return []

    errors: list[str] = []
    fresh: list[dict] = []
    for segment in ruled:
        if _is_stale(path, segment):
            con.execute(
                "UPDATE import_segment SET status='stale' WHERE seg_id=?",
                (segment["seg_id"],),
            )
            errors.append(f"ERR s:{segment['seg_id']} IMPORT_STALE")
        else:
            fresh.append(segment)
    if not fresh:
        return errors

    fresh.sort(key=lambda item: item["seq"])
    bodies = [_segment_body(segment) for segment in fresh]
    if not any(bodies):
        for segment in fresh:
            con.execute(
                "UPDATE import_segment SET status='skipped', why='empty' WHERE seg_id=?",
                (segment["seg_id"],),
            )
        return errors

    kind_id = fresh[0]["kind_id"]
    doc_id = _create_document(
        con, actor, project_id, kind_id, path, raw, artifact_id, owner, export_policy
    )
    patches: list[dict] = []
    applied: list[dict] = []
    previous_fields: set[str] = set()
    for segment, body in zip(fresh, bodies):
        if not body:
            con.execute(
                "UPDATE import_segment SET status='skipped', why='empty', doc_id=? "
                "WHERE seg_id=?",
                (doc_id, segment["seg_id"]),
            )
            continue
        field_key = segment["field_key"]
        op = "append" if field_key in previous_fields else "set"
        patch_text = "\n" + body if op == "append" else body
        patches.append(
            {
                "h": f"f:{doc_id}/{kind_id}/{field_key}",
                "op": op,
                "text": patch_text,
            }
        )
        applied.append(segment)
        previous_fields.add(field_key)

    if patches:
        result = DocDB(con, actor).put(patches)
        if "NOTHING_APPLIED" in result:
            error_line = next(
                (line for line in result.splitlines() if line.startswith("ERR ")), ""
            )
            parts = error_line.split(" ", 3)
            if len(parts) >= 3:
                code = parts[2]
                detail = parts[3] if len(parts) == 4 else ""
                raise DocdbError(code, detail)
            raise DocdbError("DB", "import 필드 적용에 실패함")
        for segment in applied:
            con.execute(
                "UPDATE import_segment SET status='applied', doc_id=? WHERE seg_id=?",
                (doc_id, segment["seg_id"]),
            )
    return errors


def _scan_file(
    con: Connection,
    actor: Actor,
    project_id: str,
    root: Path,
    path: Path,
    owner: str,
    export_policy: str,
) -> list[str]:
    """파일을 등록하고 지원 파일의 구간 분할과 자동 적용을 수행한다."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise DocdbError("BAD_ARGS", "import 파일을 읽을 수 없음") from exc
    uri = path.relative_to(root).as_posix()
    digest = "sha256:" + textnorm.sha256_hex(raw)
    artifact_id = _record_artifact(con, actor, uri, digest)
    if artifact_id is None:
        return []

    try:
        if path.suffix not in SUPPORTED_SUFFIXES:
            return []
        raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return []

    kind_id = _kind_for(con, path, raw)
    segments = split_segments(raw)
    stored: list[dict] = []
    for segment in segments:
        segment["heading"] = textnorm.normalize_text(segment["heading"])
        policy.assert_no_secrets(con, f"a:{artifact_id}", [segment["heading"]])
        field_key = _field_for(con, kind_id, segment["heading"]) if kind_id else None
        status = "ruled" if field_key else "pending"
        cursor = con.execute(
            "INSERT INTO import_segment (artifact_id, seq, heading, start_off, end_off, "
            "text_digest, status, kind_id, field_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                artifact_id,
                segment["seq"],
                segment["heading"],
                segment["start_off"],
                segment["end_off"],
                textnorm.digest16(segment["text"]),
                status,
                kind_id,
                field_key,
            ),
        )
        segment.update(
            {
                "seg_id": int(cursor.lastrowid),
                "kind_id": kind_id,
                "field_key": field_key,
            }
        )
        stored.append(segment)
    return _apply_segments(
        con,
        actor,
        project_id,
        path,
        raw,
        artifact_id,
        owner,
        export_policy,
        stored,
    )


def scan(con: Connection, actor: Actor, folder: Path, owner: str, export_policy: str) -> str:
    """프로젝트 폴더를 백업한 뒤 원본을 등록하고 규칙에 맞는 구간을 적용한다."""
    if not isinstance(owner, str) or not _OWNER_ID.fullmatch(owner):
        raise DocdbError("BAD_ARGS", "소유자는 사람 행위자 ID여야 함")
    if not isinstance(export_policy, str) or export_policy not in _EXPORT_POLICIES:
        raise DocdbError("BAD_ARGS", "export-policy 값이 올바르지 않음")
    root = _project_root(con)
    target = Path(folder).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise DocdbError("BAD_ARGS", "import 폴더는 프로젝트 루트 안이어야 함") from exc
    docdb_dir = (root / paths.DOT_DIR).resolve()
    if target == docdb_dir or docdb_dir in target.parents:
        raise DocdbError("BAD_ARGS", "import 폴더는 .docdb 안에 있을 수 없음")
    if not target.is_dir():
        raise DocdbError("BAD_ARGS", "import 폴더가 없거나 디렉터리가 아님")

    project = con.execute("SELECT project_id FROM project LIMIT 1").fetchone()
    if project is None:
        raise DocdbError("NOT_FOUND", "프로젝트를 찾을 수 없음")
    project_id = str(project[0])
    backup.backup(con, project_id, "pre-import-scan")

    errors: list[str] = []
    try:
        with db.write_tx(con):
            for path in _walk(target):
                errors.extend(
                    _scan_file(
                        con,
                        actor,
                        project_id,
                        root,
                        path,
                        owner,
                        export_policy,
                    )
                )
    except db.DatabaseError as exc:
        raise DocdbError("DB", str(exc)[:80]) from exc
    return "\n".join(errors)
