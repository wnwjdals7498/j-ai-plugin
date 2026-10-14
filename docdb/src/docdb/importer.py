"""문서 파일을 구간으로 나누고 규칙에 맞는 필드를 적용한다."""
import difflib
import json
import re
from pathlib import Path

from docdb import backup, clock, db, history, ids, paths, policy, runs, textnorm
from docdb.actors import ACTOR_PATTERNS, Actor
from docdb.db import Connection
from docdb.errors import DocdbError
from docdb.llm import DocDB, parse_handle

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
    con: Connection, actor: Actor, uri: str, digest: str, run_id: str
) -> str | None:
    """새 원본 파일을 등록하고 생성 이력을 기록한다."""
    existing = con.execute(
        "SELECT artifact_id FROM artifact WHERE uri=? AND digest=?",
        (uri, digest),
    ).fetchone()
    if existing is not None:
        return None
    artifact_id = ids.next_id(con, "artifact")
    access_policy_ref = f"r:{run_id}"
    con.execute(
        "INSERT INTO artifact (artifact_id, uri, digest, role, source_mode, access_policy_ref) "
        "VALUES (?, ?, ?, 'primary', 'authored', ?)",
        (artifact_id, uri, digest, access_policy_ref),
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
            "access_policy_ref": access_policy_ref,
        },
        actor.id,
        None,
        None,
    )
    return artifact_id


def _valid_artifact_uri(uri: str) -> bool:
    """artifact 상대 경로가 DB URI 제약을 만족하는지 확인한다."""
    return not (
        uri.startswith("/")
        or ".." in uri
        or "\\" in uri
        or (len(uri) >= 2 and uri[1] == ":")
    )


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
    lines = textnorm.normalize_text(segment["text"]).split("\n")
    if segment["heading"] is not None:
        lines = lines[1:]
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
    segments: list[dict],
    document_id: str | None = None,
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
                "UPDATE import_segment SET status='skipped', why='empty', doc_id=? "
                "WHERE seg_id=?",
                (document_id, segment["seg_id"]),
            )
        return errors

    kind_id = fresh[0]["kind_id"]
    doc_id = document_id
    if doc_id is None:
        owner, export_policy = _artifact_options(con, artifact_id)
        if owner is None:
            raise DocdbError("BAD_ARGS", "가져온 파일의 소유자를 확인할 수 없음")
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
        current = con.execute(
            "SELECT value_text FROM field_value WHERE doc_id=? AND kind_id=? AND field_key=?",
            (doc_id, kind_id, field_key),
        ).fetchone()
        has_current = current is not None and current[0] not in (None, "")
        op = "append" if field_key in previous_fields or has_current else "set"
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
    raw: bytes,
    run_id: str,
) -> list[str]:
    """파일을 등록하고 지원 파일의 구간 분할과 자동 적용을 수행한다."""
    uri = path.relative_to(root).as_posix()
    digest = "sha256:" + textnorm.sha256_hex(raw)
    artifact_id = _record_artifact(con, actor, uri, digest, run_id)
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
            "text_digest, status, kind_id, field_key, rule) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                "field" if field_key is not None else None,
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
    skipped_paths = 0
    try:
        with db.write_tx(con):
            new_files: list[tuple[Path, bytes]] = []
            for path in _walk(target):
                uri = path.relative_to(root).as_posix()
                if not _valid_artifact_uri(uri):
                    skipped_paths += 1
                    continue
                try:
                    raw = path.read_bytes()
                except OSError as exc:
                    raise DocdbError("BAD_ARGS", "import 파일을 읽을 수 없음") from exc
                digest = "sha256:" + textnorm.sha256_hex(raw)
                existing = con.execute(
                    "SELECT 1 FROM artifact WHERE uri=? AND digest=?", (uri, digest)
                ).fetchone()
                if existing is None:
                    new_files.append((path, raw))

            if new_files:
                run_id = runs.ingest(
                    con,
                    actor,
                    {
                        "run_type": "import",
                        "target_ref": target.relative_to(root).as_posix(),
                        "result": "PASS",
                        "runner": "docctl",
                        "trust_source": "runner",
                        "command_argv": [
                            "import",
                            "scan",
                            str(folder),
                            "--owner",
                            owner,
                            "--export-policy",
                            export_policy,
                        ],
                        "env_snapshot": {"owner": owner, "export_policy": export_policy},
                    },
                )
            for path, raw in new_files:
                errors.extend(
                    _scan_file(
                        con,
                        actor,
                        project_id,
                        root,
                        path,
                        raw,
                        run_id,
                    )
                )
    except db.DatabaseError as exc:
        raise DocdbError("DB", str(exc)[:80]) from exc
    output = [f"WARN skipped_paths={skipped_paths}"] if skipped_paths else []
    output.extend(errors)
    return "\n".join(output)


def _artifact_options(con: Connection, artifact_id: str) -> tuple[str | None, str]:
    """artifact가 참조하는 scan run에서 소유자와 전송 정책을 읽는다."""
    artifact = con.execute(
        "SELECT access_policy_ref FROM artifact WHERE artifact_id=?", (artifact_id,)
    ).fetchone()
    if artifact is None or not isinstance(artifact[0], str) or not artifact[0].startswith("r:"):
        return None, "local_only"
    run_id = artifact[0][2:]
    try:
        ids.validate("run", run_id)
    except DocdbError:
        return None, "local_only"
    run = con.execute("SELECT env_snapshot FROM run WHERE run_id=?", (run_id,)).fetchone()
    if run is None:
        return None, "local_only"
    try:
        values = json.loads(run[0])
    except (TypeError, json.JSONDecodeError):
        return None, "local_only"
    if not isinstance(values, dict):
        return None, "local_only"
    owner_value = values.get("owner")
    owner = (
        owner_value
        if isinstance(owner_value, str) and _OWNER_ID.fullmatch(owner_value)
        else None
    )
    policy_value = values.get("export_policy")
    export_policy = (
        policy_value if isinstance(policy_value, str) and policy_value in _EXPORT_POLICIES
        else "local_only"
    )
    return owner, export_policy


def _artifact_path(con: Connection, artifact_id: str) -> tuple[str, Path]:
    """artifact의 프로젝트 상대 경로와 현재 파일 경로를 반환한다."""
    row = con.execute("SELECT uri FROM artifact WHERE artifact_id=?", (artifact_id,)).fetchone()
    if row is None:
        raise DocdbError("BAD_ARGS", f"가져온 파일을 찾을 수 없음: {artifact_id}")
    uri = str(row[0])
    root = _project_root(con)
    return uri, root.joinpath(*Path(uri).parts)


def _segment_is_stale(path: Path, segment: dict) -> bool:
    """현재 원본 구간의 digest가 scan 당시 값과 다른지 확인한다."""
    try:
        raw = path.read_bytes()
        start = int(segment["start_off"])
        end = int(segment["end_off"])
        if start < 0 or end < start or end > len(raw):
            return True
        current = raw[start:end].decode("utf-8")
    except (OSError, UnicodeDecodeError, TypeError, ValueError):
        return True
    return textnorm.digest16(current) != segment["text_digest"]


def _segment_text(path: Path, segment: dict) -> str:
    """현재 원본 바이트에서 구간 텍스트를 읽는다."""
    raw = path.read_bytes()
    return raw[int(segment["start_off"]) : int(segment["end_off"])].decode("utf-8")


def _segment_policy(con: Connection, segment: dict) -> str:
    """연결 문서나 scan run에서 전송 정책을 읽고 모르면 local_only를 반환한다."""
    row = con.execute(
        "SELECT d.export_policy FROM import_segment linked JOIN document d "
        "ON d.doc_id=linked.doc_id WHERE linked.artifact_id=? "
        "AND linked.doc_id IS NOT NULL ORDER BY linked.seq LIMIT 1",
        (segment["artifact_id"],),
    ).fetchone()
    if row is not None:
        return str(row[0])
    return _artifact_options(con, str(segment["artifact_id"]))[1]


def _candidate_fields(con: Connection, kind_id: str, heading: str | None) -> list[str]:
    """제목과 가까운 필드 후보를 최대 다섯 개까지 순서대로 반환한다."""
    key = textnorm.norm_key(heading or "")
    rows = con.execute(
        "SELECT field_key, description, sort_order FROM cat_kind_field "
        "WHERE kind_id=? ORDER BY sort_order, field_key",
        (kind_id,),
    ).fetchall()
    ranked: list[tuple[float, int, str]] = []
    for field_key, description, sort_order in rows:
        first_phrase = re.split(r"[(:,—]", description, maxsplit=1)[0]
        score = max(
            difflib.SequenceMatcher(None, key, textnorm.norm_key(field_key)).ratio(),
            difflib.SequenceMatcher(None, key, textnorm.norm_key(first_phrase)).ratio(),
        )
        ranked.append((score, int(sort_order), str(field_key)))
    ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
    return [field_key for _score, _order, field_key in ranked[:5]]


def next_batch(con: Connection, budget: int = 1500) -> str:
    """미분류 파일과 대기 구간을 예산 안에서 다음 처리 묶음으로 반환한다."""
    if not isinstance(budget, int) or isinstance(budget, bool) or budget < 1:
        raise DocdbError("BAD_ARGS", "budget는 양의 정수여야 함")

    documents: list[str] = []
    for artifact_id, uri in con.execute(
        "SELECT a.artifact_id, a.uri FROM artifact a WHERE EXISTS ("
        "SELECT 1 FROM import_segment s WHERE s.artifact_id=a.artifact_id) "
        "AND NOT EXISTS (SELECT 1 FROM import_segment s WHERE s.artifact_id=a.artifact_id "
        "AND s.kind_id IS NOT NULL) ORDER BY a.uri, a.artifact_id"
    ):
        headings = [
            str(row[0])
            for row in con.execute(
                "SELECT heading FROM import_segment WHERE artifact_id=? "
                "ORDER BY seq",
                (artifact_id,),
            )
            if row[0] is not None
        ]
        documents.append(
            f"d?:{artifact_id} {textnorm.q(uri)} heads={'|'.join(headings)}"
        )

    pending_rows = con.execute(
        "SELECT s.seg_id, s.heading, s.start_off, s.end_off, s.text_digest, s.kind_id, "
        "s.artifact_id, s.rule, a.uri, a.digest FROM import_segment s "
        "JOIN artifact a USING (artifact_id) "
        "WHERE s.status='pending' ORDER BY s.seg_id"
    ).fetchall()
    pending_lines: list[str] = []
    for row in pending_rows:
        segment = {
            "seg_id": row[0],
            "heading": row[1],
            "start_off": row[2],
            "end_off": row[3],
            "text_digest": row[4],
            "kind_id": row[5],
            "artifact_id": row[6],
            "rule": row[7],
        }
        stale = True
        excerpt = ""
        try:
            _uri, path = _artifact_path(con, str(row[6]))
            raw = path.read_bytes()
            if "sha256:" + textnorm.sha256_hex(raw) == row[9]:
                start = int(segment["start_off"])
                end = int(segment["end_off"])
                if 0 <= start <= end <= len(raw):
                    excerpt = raw[start:end].decode("utf-8")
                    stale = False
        except (DocdbError, OSError, UnicodeDecodeError, TypeError, ValueError):
            stale = True
            excerpt = ""
        token_count = textnorm.tok_est(excerpt)
        line = f"s:{row[0]} {textnorm.q(row[1] or '')} tok={token_count}"
        if _segment_policy(con, segment) == "local_only":
            line += " | policy=local_only"
        elif not stale:
            snippet = textnorm.normalize_text(excerpt or "") or ""
            snippet = snippet[:200].replace("\n", " ⏎ ")
            line += f" | {snippet}"
            fields = (
                _candidate_fields(con, str(row[5]), row[1]) if row[5] is not None else []
            )
            if fields:
                line += f" | cand={','.join(f'{row[5]}/{field}' for field in fields)}"
        if stale:
            line += " stale"
        pending_lines.append(line)

    pending_count = len(pending_rows)
    entries = documents + pending_lines
    selected: list[str] = []
    used = 0
    omitted = 0
    for line in entries:
        cost = textnorm.tok_est(line) + 1
        if used + cost > budget:
            omitted += 1
            continue
        selected.append(line)
        used += cost
    header = f"#import-next pending={pending_count} tok={used}/{budget} omitted={omitted}"
    return "\n".join([header, *selected])


def _label_error(target: str, error: DocdbError) -> str:
    """라벨 오류를 계약의 한 줄 형식으로 바꾼다."""
    detail = f" {error.detail}" if error.detail else ""
    return f"ERR {target} {error.code}{detail}"


def _kind_exists(con: Connection, kind_id: object) -> bool:
    """카탈로그에 문서 종류가 있는지 확인한다."""
    return isinstance(kind_id, str) and con.execute(
        "SELECT 1 FROM cat_kind WHERE kind_id=?", (kind_id,)
    ).fetchone() is not None


def _segment_row(con: Connection, seg_id: int) -> dict | None:
    """구간 적용에 필요한 값을 한 행 사전으로 읽는다."""
    row = con.execute(
        "SELECT seg_id, artifact_id, seq, heading, start_off, end_off, text_digest, status, "
        "kind_id, field_key, doc_id, why, rule FROM import_segment WHERE seg_id=?",
        (seg_id,),
    ).fetchone()
    if row is None:
        return None
    names = (
        "seg_id",
        "artifact_id",
        "seq",
        "heading",
        "start_off",
        "end_off",
        "text_digest",
        "status",
        "kind_id",
        "field_key",
        "doc_id",
        "why",
        "rule",
    )
    return dict(zip(names, row))


def _segment_document(con: Connection, segment: dict) -> str | None:
    """구간 또는 같은 파일의 형제 구간에서 문서 ID를 찾는다."""
    if segment["doc_id"] is not None:
        return str(segment["doc_id"])
    row = con.execute(
        "SELECT doc_id FROM import_segment WHERE artifact_id=? AND doc_id IS NOT NULL "
        "ORDER BY seq LIMIT 1",
        (segment["artifact_id"],),
    ).fetchone()
    return str(row[0]) if row is not None else None


def _document_has_kind(con: Connection, doc_id: str, kind_id: str) -> bool:
    """문서에 지정한 종류가 연결되어 있는지 확인한다."""
    return con.execute(
        "SELECT 1 FROM document_kind WHERE doc_id=? AND kind_id=?", (doc_id, kind_id)
    ).fetchone() is not None


def _label_segment(
    con: Connection, actor: Actor, segment: dict, kind_id: str, field_key: str
) -> str:
    """지정한 구간 본문을 문서 필드에 적용한다."""
    artifact_id = str(segment["artifact_id"])
    uri, path = _artifact_path(con, artifact_id)
    _ = uri
    raw = path.read_bytes()
    segment["text"] = _segment_text(path, segment)
    body = _segment_body(segment)
    if not body:
        con.execute(
            "UPDATE import_segment SET kind_id=?, field_key=?, status='skipped', "
            "why='empty', rule='agent' "
            "WHERE seg_id=?",
            (kind_id, field_key, segment["seg_id"]),
        )
        return f"OK s:{segment['seg_id']} skipped"

    doc_id = _segment_document(con, segment)
    if doc_id is not None and not _document_has_kind(con, doc_id, kind_id):
        raise DocdbError("BAD_ARGS", "문서 종류와 구간 라벨이 일치하지 않음")
    project_id = str(con.execute("SELECT project_id FROM project LIMIT 1").fetchone()[0])
    if doc_id is None:
        owner, export_policy = _artifact_options(con, artifact_id)
        if owner is None or export_policy is None:
            raise DocdbError("BAD_ARGS", "가져온 파일의 소유자와 전송 정책을 확인할 수 없음")
        doc_id = _create_document(
            con,
            actor,
            project_id,
            kind_id,
            path,
            raw,
            artifact_id,
            owner,
            export_policy,
        )

    current_row = con.execute(
        "SELECT value_text FROM field_value WHERE doc_id=? AND kind_id=? AND field_key=?",
        (doc_id, kind_id, field_key),
    ).fetchone()
    current_text = current_row[0] if current_row is not None else None
    if current_text:
        operation = "append"
        patch_text = "\n" + body
    else:
        operation = "set"
        patch_text = body
    result = DocDB(con, actor).put(
        [{"h": f"f:{doc_id}/{kind_id}/{field_key}", "op": operation, "text": patch_text}]
    )
    if "NOTHING_APPLIED" in result:
        error_line = next((line for line in result.splitlines() if line.startswith("ERR ")), "")
        parts = error_line.split(" ", 3)
        if len(parts) >= 3:
            raise DocdbError(parts[2], parts[3] if len(parts) == 4 else "")
        raise DocdbError("DB", "import 필드 적용에 실패함")
    con.execute(
        "UPDATE import_segment SET kind_id=?, field_key=?, status='applied', doc_id=?, "
        "why=NULL, rule='agent' "
        "WHERE seg_id=?",
        (kind_id, field_key, doc_id, segment["seg_id"]),
    )
    return f"OK s:{segment['seg_id']} applied"


def _plan_label(con: Connection, raw_label: object) -> tuple[dict | None, str, DocdbError | None]:
    """라벨 입력을 검증하고 적용 계획 또는 출력 오류를 반환한다."""
    if not isinstance(raw_label, dict):
        return None, "?", DocdbError("BAD_OP", "라벨은 객체여야 함")
    target = "?"
    keys = set(raw_label)
    if "doc" in keys:
        if keys != {"doc", "kind"}:
            return None, target, DocdbError("BAD_OP", "문서 라벨 키가 올바르지 않음")
        prefix, handle_keys = parse_handle(str(raw_label["doc"]))
        if isinstance(raw_label["doc"], str):
            target = raw_label["doc"]
        if prefix != "d?" or not _kind_exists(con, raw_label["kind"]):
            return None, target, DocdbError("BAD_ARGS", "문서 또는 종류 형식이 올바르지 않음")
        artifact_id = handle_keys[0]
        rows = con.execute(
            "SELECT seg_id, kind_id FROM import_segment WHERE artifact_id=? ORDER BY seq",
            (artifact_id,),
        ).fetchall()
        if not rows or any(row[1] not in (None, raw_label["kind"]) for row in rows):
            return None, target, DocdbError("BAD_ARGS", "문서 종류와 라벨이 일치하지 않음")
        return {"op": "doc", "artifact_id": artifact_id, "kind_id": raw_label["kind"]}, target, None

    if "seg" not in keys or not isinstance(raw_label["seg"], str):
        return None, target, DocdbError("BAD_OP", "구간 라벨에 seg가 필요함")
    prefix, handle_keys = parse_handle(raw_label["seg"])
    target = raw_label["seg"]
    if prefix != "s":
        return None, target, DocdbError("BAD_ARGS", "구간 ID 형식이 올바르지 않음")
    segment = _segment_row(con, int(handle_keys[0]))
    if segment is None:
        return None, target, DocdbError("BAD_ARGS", "구간을 찾을 수 없음")
    if segment["status"] in {"applied", "skipped"}:
        return None, target, DocdbError("BAD_ARGS", f"already {segment['status']}")
    if keys == {"seg", "skip", "why"}:
        if raw_label["skip"] is not True or not isinstance(raw_label["why"], str):
            return None, target, DocdbError("BAD_ARGS", "skip 라벨은 true와 문자열 why가 필요함")
        why = textnorm.normalize_text(raw_label["why"])
        policy.assert_no_secrets(con, target, [why])
        return {"op": "skip", "segment": segment, "why": why}, target, None
    if keys != {"seg", "kind", "field"}:
        return None, target, DocdbError("BAD_OP", "구간 라벨 키가 올바르지 않음")
    kind_id = raw_label["kind"]
    field_key = raw_label["field"]
    if not _kind_exists(con, kind_id) or not isinstance(field_key, str):
        return None, target, DocdbError("BAD_ARGS", "문서 종류 또는 필드 형식이 올바르지 않음")
    if segment["kind_id"] not in (None, kind_id):
        return None, target, DocdbError("BAD_ARGS", "구간에 지정된 문서 종류와 라벨이 다름")
    if con.execute(
        "SELECT 1 FROM cat_kind_field WHERE kind_id=? AND field_key=?",
        (kind_id, field_key),
    ).fetchone() is None:
        return None, target, DocdbError("BAD_ARGS", "문서 종류에 필드가 없음")
    doc_id = _segment_document(con, segment)
    if doc_id is not None and not _document_has_kind(con, doc_id, kind_id):
        return None, target, DocdbError("BAD_ARGS", "문서 종류와 구간 라벨이 일치하지 않음")
    return {
        "op": "segment",
        "segment": segment,
        "kind_id": kind_id,
        "field_key": field_key,
    }, target, None


def _label_document(con: Connection, actor: Actor, action: dict) -> str:
    """문서 종류를 기록하고 재적용 가능한 구간에 필드 규칙을 적용한다."""
    artifact_id = action["artifact_id"]
    kind_id = action["kind_id"]
    _uri, path = _artifact_path(con, artifact_id)
    rows = con.execute(
        "SELECT seg_id, seq, heading, start_off, end_off, text_digest, status, doc_id "
        "FROM import_segment WHERE artifact_id=? ORDER BY seq",
        (artifact_id,),
    ).fetchall()
    doc_ids = {str(row[7]) for row in rows if row[7] is not None}
    if len(doc_ids) > 1:
        raise DocdbError("BAD_ARGS", "가져온 파일의 문서가 일치하지 않음")
    doc_id = next(iter(doc_ids)) if doc_ids else None
    if doc_id is not None and not _document_has_kind(con, doc_id, kind_id):
        raise DocdbError("BAD_ARGS", "문서 종류와 라벨이 일치하지 않음")

    segments: list[dict] = []
    ruled = 0
    for row in rows:
        heading = row[2]
        field_key = _field_for(con, kind_id, heading)
        ruled += field_key is not None
        if row[6] not in {"pending", "ruled", "stale"}:
            continue
        status = "ruled" if field_key is not None else "pending"
        segment = {
            "seg_id": row[0],
            "seq": row[1],
            "heading": heading,
            "start_off": row[3],
            "end_off": row[4],
            "text_digest": row[5],
            "kind_id": kind_id,
            "field_key": field_key,
            "rule": "field" if field_key is not None else None,
        }
        con.execute(
            "UPDATE import_segment SET kind_id=?, field_key=?, status=?, rule=?, why=NULL "
            "WHERE seg_id=?",
            (kind_id, field_key, status, segment["rule"], row[0]),
        )
        if field_key is not None:
            segment["text"] = _segment_text(path, segment)
            segments.append(segment)

    if ruled:
        project_id = str(con.execute("SELECT project_id FROM project LIMIT 1").fetchone()[0])
        stale_or_apply = _apply_segments(
            con,
            actor,
            project_id,
            path,
            path.read_bytes(),
            artifact_id,
            segments,
            doc_id,
        )
        if stale_or_apply:
            raise DocdbError("IMPORT_STALE", f"구간이 변경됨: {stale_or_apply[0]}")
    return f"OK d?:{artifact_id} kind={kind_id} ruled={ruled}"


def label(con: Connection, actor: Actor, labels: list[dict]) -> str:
    """라벨 묶음을 검증하고 한 트랜잭션으로 적용한다."""
    if not isinstance(labels, list):
        raise DocdbError("BAD_OP", "label 입력은 JSON 배열이어야 함")
    project = con.execute("SELECT project_id FROM project LIMIT 1").fetchone()
    if project is None:
        raise DocdbError("NOT_FOUND", "프로젝트를 찾을 수 없음")
    project_id = str(project[0])
    backup.backup(con, project_id, "pre-import-label")

    actions: list[tuple[dict, str]] = []
    errors: list[str] = []
    stale_ids: set[int] = set()
    seen_segment_ids: set[int] = set()
    current_target = "?"
    try:
        with db.write_tx(con):
            for raw_label in labels:
                target = "?"
                try:
                    if isinstance(raw_label, dict) and isinstance(raw_label.get("seg"), str):
                        prefix, handle_keys = parse_handle(raw_label["seg"])
                        target = raw_label["seg"]
                        if prefix == "s":
                            seg_id = int(handle_keys[0])
                            if seg_id in seen_segment_ids:
                                errors.append(
                                    _label_error(
                                        f"s:{seg_id}",
                                        DocdbError("BAD_ARGS", "duplicate segment label"),
                                    )
                                )
                                continue
                            seen_segment_ids.add(seg_id)
                    action, planned_target, error = _plan_label(con, raw_label)
                    target = planned_target
                except DocdbError as exc:
                    errors.append(_label_error(target, exc))
                    continue
                if error is not None:
                    errors.append(_label_error(target, error))
                    continue
                assert action is not None
                if action["op"] == "doc":
                    uri, path = _artifact_path(con, action["artifact_id"])
                    _ = uri
                    rows = con.execute(
                        "SELECT seg_id, start_off, end_off, text_digest, status "
                        "FROM import_segment "
                        "WHERE artifact_id=? ORDER BY seq",
                        (action["artifact_id"],),
                    ).fetchall()
                    for row in rows:
                        if row[4] not in {"pending", "ruled", "stale"}:
                            continue
                        if _segment_is_stale(path, dict(zip(
                            ("seg_id", "start_off", "end_off", "text_digest"), row[:4]
                        ))):
                            stale_ids.add(int(row[0]))
                            errors.append(
                                _label_error(f"s:{row[0]}", DocdbError("IMPORT_STALE"))
                            )
                elif action["op"] == "segment":
                    segment = action["segment"]
                    _uri, path = _artifact_path(con, str(segment["artifact_id"]))
                    if _segment_is_stale(path, segment):
                        stale_ids.add(int(segment["seg_id"]))
                        errors.append(_label_error(target, DocdbError("IMPORT_STALE")))
                        continue
                actions.append((action, target))

            if errors:
                for seg_id in stale_ids:
                    con.execute(
                        "UPDATE import_segment SET status='stale' WHERE seg_id=?", (seg_id,)
                    )
                return_text = "\n".join(errors)
            else:
                results: list[str] = []
                for action, target in actions:
                    current_target = target
                    if action["op"] == "doc":
                        results.append(_label_document(con, actor, action))
                    elif action["op"] == "skip":
                        segment = action["segment"]
                        con.execute(
                            "UPDATE import_segment SET status='skipped', why=?, rule='agent' "
                            "WHERE seg_id=?",
                            (action["why"], segment["seg_id"]),
                        )
                        results.append(f"OK s:{segment['seg_id']} skipped")
                    else:
                        segment = action["segment"]
                        results.append(
                            _label_segment(
                                con,
                                actor,
                                segment,
                                action["kind_id"],
                                action["field_key"],
                            )
                        )
                return_text = "\n".join(results)
    except DocdbError as exc:
        return _label_error(current_target, exc)
    except db.DatabaseError as exc:
        raise DocdbError("DB", str(exc)[:80]) from exc
    return return_text


def report(con: Connection) -> str:
    """가져온 파일 수와 구간 상태별 개수를 반환한다."""
    artifacts = con.execute(
        "SELECT artifact_id, uri FROM artifact WHERE role='primary' AND source_mode='authored' "
        "ORDER BY artifact_id"
    ).fetchall()
    supported_ids = {
        str(row[0])
        for row in con.execute("SELECT DISTINCT artifact_id FROM import_segment")
    }
    files = len(artifacts)
    supported = len(supported_ids)
    root = _project_root(con)
    for artifact_id, uri in artifacts:
        if str(artifact_id) in supported_ids or Path(str(uri)).suffix not in SUPPORTED_SUFFIXES:
            continue
        try:
            root.joinpath(*Path(str(uri)).parts).read_bytes().decode("utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue
        supported += 1
    segments = int(con.execute("SELECT count(*) FROM import_segment").fetchone()[0])
    counts = {str(row[0]): int(row[1]) for row in con.execute(
        "SELECT status, count(*) FROM import_segment GROUP BY status"
    )}
    status_line = "status " + " ".join(
        f"{status}={counts.get(status, 0)}"
        for status in ("pending", "ruled", "labeled", "skipped", "applied", "stale")
    )
    return (
        f"#import-report files={files} supported={supported} "
        f"unsupported={files - supported} segments={segments}\n{status_line}"
    )
