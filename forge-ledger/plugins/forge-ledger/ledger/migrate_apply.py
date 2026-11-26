"""PMT 이전 매니페스트를 적용한다. git 쪽(project·결정·사실·resources)은 `.ledger/`에,
Plane 쪽(work·item·comment·backlog)은 작업 항목과 댓글로 쓴다. 커밋은 하지 않는다.

원칙: 같은 매니페스트를 다시 적용해도 중복되지 않고, 적용 뒤 바뀐 내용은 덮어쓰지 않고 충돌로 알린다.
다만 지금 내용이 쓰려는 내용과 같으면 `same`으로 보고 적용 기록을 복구한다(쓴 뒤 기록 저장 전에 멈춘 경우).
결정은 Plane '[계획서] 결정'에도 절이 생긴다. 모든 확인을 끝낸 뒤에 쓰기를 시작한다.
미리 보기(`plan_apply`)는 같은 판정을 `dry_run`으로 거쳐 아무것도 쓰지 않는다.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import subprocess  # nosec B404 # 현재 브랜치 조회용(shell=False)
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ledger.facts import Fact, format_row, normalize_fact, parse_facts, upsert_fact
from ledger.flows import FlowError, Repo, SecretFound, append_decision, decision_item, default_project_id, plane_ref
from ledger.history import HistoryEntry, append_entry, format_entry, new_decision_id, parse_history
from ledger.migrate import (
    ApplyPlan,
    Manifest,
    check_manifest,
    load_manifest,
    load_resource,
    manifest_path,
    parse_comment_time,
    render_preview,
    resolve_source_dir,
)
from ledger.plane import PlaneLike, match_external
from ledger.secrets import find_secrets
from ledger.sections import split_html_headings, split_html_sections
from ledger.statusmap import LABEL_COLOR, STATUS_MAP, pick_state

_PROJECT_FILE = ".ledger/project.md"
_FACTS_FILE = ".ledger/facts.md"
_HISTORY_FILE = ".ledger/history.md"
_RESOURCE_DIR = ".ledger/resources/pmt"
_STATE_VERSION = 1
_DECISION_PROJECTS = "*"  # 저장소 전체 결정. Plane에서는 기본 프로젝트의 항목에 들어간다.
# 첫머리 표지 바로 뒤의 `상태: … · 대체: <새 ID>`만 읽는다(`.match`).
_REPLACEMENT_PATTERN = r" 상태: (?:승인|대체|폐기) · 대체: (D-[0-9]{6}-[0-9a-f]{4})"
_TAG_RE = re.compile(r"<[^>]+>")
_PLANE_REF_PATTERN = r" · plane:[^\s·]+@[^\s·]+"  # `plane_ref`가 만드는 모양 하나만
_SOURCE = "forge-ledger-pmt"  # Plane 원천 표식의 `external_source`. `external_id`는 원천 키다.
_BLOCKED_LABEL = "blocked"
_NEW_ID = "new:"  # 미리 보기에서 아직 없는 항목의 자리 id
_MARKER_ATTRIBUTE_RE = re.compile(r"""data-fl-key\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_VISIBLE_MARKER = "fl:"


@dataclass(frozen=True)
class AppliedEntry:
    """적용 기록 한 건. `digest`는 쓴 직후의 내용, `remote_updated_at`은 Plane 레코드에만 쓴다.

    결정의 `digest`는 이력 줄, 매니페스트 내용, Plane이 저장한 절의 글(태그 제거)을 함께 담는다.
    작업 항목의 `digest`는 `<item|link>:<매니페스트 내용>:<Plane이 저장한 내용>`이고, `remote_updated_at`은
    그 항목에 마지막으로 쓴 뒤(댓글 포함) 다시 읽은 시각이다. 비어 있으면 쓰는 도중이다.

    `digest`와 `applied_at`이 비어 있으면 아직 적용이 끝나지 않은 것이다. 결정은 새 ID(`target`)만
    예약했고, 댓글은 만들었지만(`target`은 댓글 id) 표식을 아직 확인하지 못했다.
    """

    target: str
    applied_at: str
    digest: str
    remote_updated_at: str = ""


@dataclass(frozen=True)
class Outcome:
    key: str
    action: str  # created | updated | same | same-unmarked(표식 없는 같은 글 댓글) | conflict
    target: str


class AppliedState:
    """원천 키별 적용 기록(`<slug>.applied.json`)."""

    def __init__(self, entries: Optional[dict[str, AppliedEntry]] = None, path: Optional[Path] = None) -> None:
        self._entries = dict(entries or {})
        self._path = path

    @classmethod
    def load(cls, path: Path) -> AppliedState:
        """파일이 없으면 빈 기록이다. 읽을 수 없으면 `FlowError`다(빈 기록으로 보면 다시 쓰게 된다)."""
        if not path.exists():
            return cls(path=path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data["version"] != _STATE_VERSION:
                raise ValueError("version")
            entries = {key: AppliedEntry(**value) for key, value in data["entries"].items()}
            if not all(isinstance(text, str) for entry in entries.values() for text in asdict(entry).values()):
                raise ValueError("type")
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise FlowError(f"적용 기록 파일 형식 오류: {path.name} (지우지 말고 내용을 확인하세요)") from None
        return cls(entries, path)

    def save(self, path: Path) -> None:
        data = {"version": _STATE_VERSION, "entries": {key: asdict(entry) for key, entry in self._entries.items()}}
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)  # 쓰다가 멈춰도 이전 기록이 남는다.

    def checkpoint(self) -> None:
        """`load`한 자리에 저장한다. 레코드 하나가 끝날 때마다 부른다."""
        if self._path is not None:
            self.save(self._path)

    def detached(self) -> AppliedState:
        """저장 자리가 없는 복사본. 미리 보기가 적용 기록을 바꾸지 않게 한다."""
        return AppliedState(self._entries)

    def get(self, key: str) -> Optional[AppliedEntry]:
        return self._entries.get(key)

    def put(self, key: str, entry: AppliedEntry) -> None:
        self._entries[key] = entry


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _repair(state: AppliedState, key: str, target: str, digest: str) -> Outcome:
    """지금 내용이 맞으므로 `same`이다. 적용 기록이 없거나 옛 값이면 지금 값으로 고친다."""
    entry = state.get(key)
    if entry is None or (entry.target, entry.digest) != (target, digest):
        _remember(state, key, target, digest)
    return Outcome(key, "same", target)


def _remember(state: AppliedState, key: str, target: str, digest: str, remote_updated_at: str = "") -> None:
    applied_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state.put(key, AppliedEntry(target, applied_at, digest, remote_updated_at))
    state.checkpoint()


def _require_branch(repo: Repo, slug: str) -> None:
    expected = f"migrate/{slug}"
    try:
        proc = subprocess.run(  # nosec B603 B607 # 고정 git symbolic-ref 명령(shell=False)
            ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
            cwd=repo.root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        raise FlowError(f"브랜치 {expected}에서 실행하세요") from None
    if proc.returncode != 0 or proc.stdout.strip() != expected:
        raise FlowError(f"브랜치 {expected}에서 실행하세요")


def _require_plain_path(repo: Repo, relative: str) -> None:
    """심볼릭 링크를 거치면 `.ledger/` 밖(저장소 밖이나 `.git/`)에 쓸 수 있으므로 거부한다."""
    try:
        plain = (repo.root / relative).resolve() == repo.root.resolve() / relative
    except (OSError, RuntimeError):
        plain = False
    if not plain:
        raise FlowError(f"심볼릭 링크를 거치는 경로라 쓰지 않음: {relative}")


# --- 파일(project·resource)과 사실 ---


def _apply_file(repo: Repo, state: AppliedState, key: str, relative: str, content: bytes, dry_run: bool) -> Outcome:
    path = repo.root / relative
    conflict = Outcome(key, "conflict", relative)
    if path.exists() and not path.is_file():
        return conflict
    current = path.read_bytes() if path.is_file() else None
    if current == content:
        return _repair(state, key, relative, _sha(content))
    recorded = state.get(key)
    if recorded is not None:
        if recorded.target != relative or current is None or _sha(current) != recorded.digest:
            return conflict
        action = "updated"
    elif current is None:
        action = "created"
    else:
        return conflict
    if dry_run:
        return Outcome(key, action, relative)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    except (FileExistsError, NotADirectoryError):
        return conflict  # 상위 경로에 같은 이름의 파일이 있다.
    _remember(state, key, relative, _sha(content))
    return Outcome(key, action, relative)


def _read_facts(repo: Repo) -> dict[str, Fact]:
    path = repo.root / _FACTS_FILE
    if not path.is_file():
        return {}
    try:
        facts = parse_facts(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise FlowError(f"{_FACTS_FILE}: {exc}") from None
    by_id = {fact.fact_id: fact for fact in facts}
    if len(by_id) != len(facts):
        raise FlowError(f"{_FACTS_FILE}: 중복된 ID가 있음")
    return by_id


def _apply_fact(repo: Repo, state: AppliedState, key: str, wanted: Fact, dry_run: bool) -> Outcome:
    """충돌은 파일 전체가 아니라 그 사실의 행으로 판단한다. 다른 행을 더해도 이 행은 그대로다."""
    target = f"{_FACTS_FILE}#{wanted.fact_id}"
    current = _read_facts(repo).get(wanted.fact_id)
    digest = _sha(format_row(wanted).encode("utf-8"))
    if current == wanted:
        return _repair(state, key, target, digest)
    recorded = state.get(key)
    if recorded is not None:
        if recorded.target != target or current is None:
            return Outcome(key, "conflict", target)
        if _sha(format_row(current).encode("utf-8")) != recorded.digest:
            return Outcome(key, "conflict", target)
    elif current is not None:
        return Outcome(key, "conflict", target)
    if dry_run:
        return Outcome(key, "created" if current is None else "updated", target)
    action = upsert_fact(repo.root / _FACTS_FILE, wanted)
    _remember(state, key, target, digest)
    return Outcome(key, action, target)


# --- 결정 ---


@dataclass(frozen=True)
class _Decision:
    key: str
    marker: str  # 상세 칸과 Plane 절이 시작하는 `원래 ID: <slug> <ID> · `
    wanted: HistoryEntry  # 상세 칸에는 아직 Plane 참조가 없다.
    body_html: str
    replaced_by: str  # 대체한 결정의 원천 키. 없으면 빈 문자열

    def digest(self, line: HistoryEntry, section: str) -> str:
        """이력 한 줄, 매니페스트 내용, Plane 절의 글을 함께 담는다. 어느 것이 바뀌어도 값이 달라진다."""
        source = [self.wanted.date, self.wanted.title, self.wanted.detail, self.body_html, self.replaced_by]
        return _sha(f"{format_entry(line)}\n{json.dumps([*source, section], ensure_ascii=False)}".encode())

    def expected_section(self) -> str:
        """보낸 그대로 저장됐을 때의 절 글(`_section_texts`의 모양)."""
        return _normalized(f"{self.wanted.ref_id} {self.wanted.title} {_plain_text(self.body_html)}")

    def matches(self, line: HistoryEntry, section: str) -> bool:
        """이력 줄과 Plane 절이 지금 매니페스트로 쓰려는 것과 같은가."""
        wanted = self.wanted
        return (
            (line.date, line.projects, line.title) == (wanted.date, wanted.projects, wanted.title)
            and re.fullmatch(re.escape(wanted.detail) + _PLANE_REF_PATTERN, line.detail) is not None
            and section == self.expected_section()
        )


def _one_line(text: str) -> str:
    """이력 한 칸에 쓸 수 있게 `|`를 바꾸고 줄 바꿈과 겹친 공백을 하나로 만든다."""
    return " ".join(text.replace("|", "/").split())


def _plain_text(fragment: str) -> str:
    return " ".join(html.unescape(_TAG_RE.sub(" ", fragment)).split())


def _section_texts(item_html: str) -> dict[str, str]:
    """결정 ID마다 절의 제목과 본문 글(태그 제거, 공백 정리)."""
    headings = split_html_headings(item_html)
    bodies = split_html_sections(item_html)
    return {ref_id: _normalized(f"{headings[ref_id]} {_plain_text(body)}") for ref_id, body in bodies.items()}


def _decision_body(meta: str, detail: str) -> str:
    paragraphs = [meta, *(block for block in detail.replace("\r\n", "\n").split("\n\n") if block.strip())]
    return "".join(f"<p>{html.escape(p, quote=False).replace(chr(10), '<br>')}</p>" for p in paragraphs)


def _normalized(text: str) -> str:
    return " ".join(text.split())


def _id_has_date(ref_id: str, day: str) -> bool:
    return ref_id.startswith(f"D-{date.fromisoformat(day).strftime('%y%m%d')}-")


def _assign_ids(
    records: list[dict[str, Any]],
    markers: Mapping[str, str],
    state: AppliedState,
    lines: Mapping[str, HistoryEntry],
    sections: Mapping[str, str],
) -> dict[str, str]:
    """결정마다 새 ID를 정한다. 적용 기록, 이력·Plane 절의 표지, 예약 순으로 이미 정한 ID를 다시 쓴다."""
    ids: dict[str, str] = {}
    leads: dict[str, str] = {}  # 이미 기록된 결정의 첫머리 글(상세 칸, 또는 Plane 절의 첫 문단)
    for record in records:
        key = record["key"]
        marker = markers[key]
        entry = state.get(key)
        line = next((line for line in lines.values() if line.detail.startswith(marker)), None)
        section = next((i for i, body in sections.items() if _plain_text(body).startswith(_normalized(marker))), None)
        if entry is not None and entry.digest:
            ids[key] = entry.target
        elif line is not None:
            ids[key] = line.ref_id
        elif section is not None:
            ids[key] = section
        elif entry is not None and _id_has_date(entry.target, record["date"]):
            ids[key] = entry.target  # 예약만 하고 쓰기 전에 멈췄다.
        else:
            continue
        if ids[key] in lines:
            leads[key] = _normalized(lines[ids[key]].detail)
        elif ids[key] in sections:
            leads[key] = _plain_text(sections[ids[key]].split("</p>")[0])

    # 적용 기록까지 잃었으면, 아직 못 쓴 대상의 ID는 대체당한 결정의 첫머리에만 남아 있다.
    by_key = {record["key"]: record for record in records}
    for record in records:
        target = record.get("replaced_by")
        if not target or target in ids or record["key"] not in leads:
            continue
        pattern = re.escape(_normalized(markers[record["key"]])) + _REPLACEMENT_PATTERN
        found = re.match(pattern, leads[record["key"]])
        if found and found.group(1) not in ids.values() and _id_has_date(found.group(1), by_key[target]["date"]):
            ids[target] = found.group(1)

    used = {*lines, *sections, *ids.values()}
    for record in records:
        if record["key"] not in ids:
            ids[record["key"]] = new_decision_id(date.fromisoformat(record["date"]), used)
            used.add(ids[record["key"]])
    return ids


def _read_history(repo: Repo) -> dict[str, HistoryEntry]:
    path = repo.root / _HISTORY_FILE
    if not path.is_file():
        return {}
    return {entry.ref_id: entry for entry in parse_history(path.read_text(encoding="utf-8"))}


def _decision_html(repo: Repo, client: PlaneLike) -> str:
    item = decision_item(repo, client, _DECISION_PROJECTS)[3]
    return str(item.get("description_html", ""))


def _plan_decisions(
    m: Manifest, records: list[dict[str, Any]], repo: Repo, client: PlaneLike, state: AppliedState
) -> tuple[list[_Decision], dict[str, str]]:
    """두 단계의 첫째: 모든 결정의 새 ID를 정한 뒤 대체 관계를 새 ID로 잇는다. 아무것도 쓰지 않는다.

    '[계획서] 결정'은 여기서 한 번만 읽는다. 둘째 값은 결정 ID별 절의 글이다.
    """
    if not records:
        return [], {}
    prefix = f"pmt:{m.slug}/"
    originals = {r["key"]: _one_line(r["key"][len(prefix) :]) for r in records}
    markers = {key: f"원래 ID: {m.slug} {original} · " for key, original in originals.items()}
    item_html = _decision_html(repo, client)
    sections = split_html_sections(item_html)
    ids = _assign_ids(records, markers, state, _read_history(repo), sections)

    decisions = []
    for record in records:
        key = record["key"]
        meta = f"{markers[key]}상태: {record['status']}"
        if record.get("replaced_by"):
            meta += f" · 대체: {ids[record['replaced_by']]}"
        title = _one_line(record["title"])
        body = _decision_body(meta, record["detail"])
        found = find_secrets(f"{title}\n{meta}\n{body}")
        if found:
            raise SecretFound(f"비밀값 감지: {', '.join(found)}")
        wanted = HistoryEntry(record["date"], "decision", ids[key], _DECISION_PROJECTS, title, meta)
        decisions.append(_Decision(key, markers[key], wanted, body, record.get("replaced_by") or ""))
    return decisions, _section_texts(item_html)


def _reserve_ids(decisions: list[_Decision], state: AppliedState) -> None:
    """두 단계의 둘째 앞: 아직 안 쓴 결정의 ID를 첫 쓰기 전에 모두 적용 기록에 남긴다.

    이렇게 해야 중간에 멈춰도 이미 쓴 결정이 가리키는 ID로 나머지를 만든다.
    """
    changed = False
    for decision in decisions:
        entry = state.get(decision.key)
        if entry is None or (not entry.digest and entry.target != decision.wanted.ref_id):
            state.put(decision.key, AppliedEntry(target=decision.wanted.ref_id, applied_at="", digest=""))
            changed = True
    if changed:
        state.checkpoint()


def _apply_decision(
    decision: _Decision,
    texts: Mapping[str, str],
    repo: Repo,
    client: PlaneLike,
    state: AppliedState,
    dry_run: bool,
) -> Outcome:
    """결정은 한 번 쓰면 고치지 않는다. 매니페스트·이력·Plane 절이 달라졌으면 충돌로 알린다."""
    key, wanted = decision.key, decision.wanted
    ref_id = wanted.ref_id
    conflict = Outcome(key, "conflict", ref_id)
    line = _read_history(repo).get(ref_id)
    section = texts.get(ref_id)
    recorded = state.get(key)
    applied = recorded is not None and bool(recorded.digest)

    if line is not None and section is not None:
        digest = decision.digest(line, section)
        if recorded is not None and recorded.digest == digest:
            return Outcome(key, "same", ref_id)
        # 기록이 없거나 옛 값이어도, 지금 내용이 쓰려는 것과 같으면 이어받는다.
        return _repair(state, key, ref_id, digest) if decision.matches(line, section) else conflict
    if applied or line is not None:  # 적용한 뒤 이력 줄이나 Plane 절이 사라졌다.
        return conflict

    if section is not None and section != decision.expected_section():
        return conflict
    if dry_run:
        return Outcome(key, "created", ref_id)

    if section is not None:  # Plane에는 썼고 이력을 남기기 전에 멈췄다.
        target_key, _, _, item = decision_item(repo, client, wanted.projects)
        written = HistoryEntry(
            wanted.date,
            wanted.kind,
            ref_id,
            wanted.projects,
            wanted.title,
            f"{wanted.detail} · {plane_ref(target_key, item)}",
        )
        append_entry(repo.root / _HISTORY_FILE, written)
    else:
        written, stored_html = append_decision(repo, client, wanted, decision.body_html)
        # 저장 응답의 글을 기준으로 남긴다. 서버가 글을 다듬어도 다음 실행이 충돌로 보지 않는다.
        section = _section_texts(stored_html).get(ref_id, decision.expected_section())
    _remember(state, key, ref_id, decision.digest(written, section))
    return Outcome(key, "created", ref_id)


# --- 전체 ---


def apply_git(
    m: Manifest,
    repo: Repo,
    client: PlaneLike,
    state: AppliedState,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> list[Outcome]:
    """project → 결정 → 사실 → resources 순서로 쓴다. Plane 쪽 kind는 건드리지 않는다."""
    return _run_git(m, repo, client, state, env, dry_run=False)


def _run_git(
    m: Manifest,
    repo: Repo,
    client: PlaneLike,
    state: AppliedState,
    env: Optional[Mapping[str, str]],
    *,
    dry_run: bool,
) -> list[Outcome]:
    source_dir = resolve_source_dir(m, os.environ if env is None else env)
    problems = check_manifest(m, source_dir)
    if problems:  # 공개 저장소에 쓰는 마지막 관문이다. 하나라도 걸리면 아무것도 쓰지 않는다.
        raise FlowError(f"매니페스트 검사 실패: {len(problems)}건 (ledger migrate check로 확인)")
    if not dry_run:
        _require_branch(repo, m.slug)

    by_kind: dict[str, list[dict[str, Any]]] = {kind: [] for kind in ("project", "decision", "fact", "resource")}
    for record in m.records:
        if record["kind"] in by_kind:
            by_kind[record["kind"]].append(record)

    files: list[tuple[str, str, bytes]] = []
    for record in by_kind["resource"]:
        data, problem = load_resource(record["path"], source_dir / "resources")
        if problem is not None:  # 검사 뒤에 파일이 바뀌었다. 여기서 읽은 바이트를 그대로 쓴다.
            raise SecretFound(f"{record['key']}: {problem}")
        files.append((record["key"], f"{_RESOURCE_DIR}/{record['path']}", data))
    for relative in (_PROJECT_FILE, _FACTS_FILE, _HISTORY_FILE, *(relative for _, relative, _ in files)):
        _require_plain_path(repo, relative)
    facts = [
        (r["key"], normalize_fact(Fact(r["key"][len(f"pmt:{m.slug}/") :], r["status"], r["date"], r["text"], r["ref"])))
        for r in by_kind["fact"]
    ]
    if facts:
        _read_facts(repo)
    decisions, section_texts = _plan_decisions(m, by_kind["decision"], repo, client, state)

    # 여기까지는 읽기만 했다. 아래부터 쓴다.
    _reserve_ids(decisions, state)
    outcomes = [
        _apply_file(repo, state, r["key"], _PROJECT_FILE, r["body_md"].encode("utf-8"), dry_run)
        for r in by_kind["project"]
    ]
    outcomes += [_apply_decision(d, section_texts, repo, client, state, dry_run) for d in decisions]
    outcomes += [_apply_fact(repo, state, key, fact, dry_run) for key, fact in facts]
    outcomes += [_apply_file(repo, state, key, relative, data, dry_run) for key, relative, data in files]
    return outcomes


# --- Plane: 작업 항목 ---


@dataclass(frozen=True)
class _Desired:
    """작업 항목에 쓰려는 내용. 부모와 라벨은 아직 Plane id가 아니라 원천 키와 이름이다."""

    name: str
    description_html: str
    state_id: str
    labels: tuple[str, ...]
    parent_key: str  # 부모가 없으면 빈 문자열


def _desired(record: Mapping[str, Any], states: list[dict[str, Any]]) -> _Desired:
    group, status_label = STATUS_MAP[record["status"]]
    description = record["description_html"]
    labels = list(record["labels"])
    blocked_by = record.get("blocked_by")
    if blocked_by:
        description = f"<p>차단: {html.escape(', '.join(blocked_by), quote=False)}</p>{description}"
        labels.append(_BLOCKED_LABEL)
    if status_label:
        labels.append(status_label)
    return _Desired(
        name=record["name"],
        description_html=description,
        state_id=pick_state(states, group),
        labels=tuple(dict.fromkeys(label for label in labels if label.strip())),
        parent_key=record.get("parent", ""),
    )


def _json_sha(value: Any) -> str:
    return _sha(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _manifest_digest(record: Mapping[str, Any], desired: _Desired) -> str:
    """매니페스트 쪽 내용. `link_to` 레코드는 표식만 쓰므로 대상 id만 담는다."""
    if record.get("link_to"):
        return _json_sha(record["link_to"])
    content = [desired.name, desired.description_html, record["status"], sorted(desired.labels), desired.parent_key]
    return _json_sha(content)


def _item_digest(item: Mapping[str, Any]) -> str:
    """Plane이 저장한 항목에서 이전이 쓰는 필드만 담는다."""
    labels = sorted(str(label) for label in item.get("labels") or [])
    description = item.get("description_html") or ""
    return _json_sha([item.get("name"), description, item.get("state"), labels, item.get("parent") or None])


# --- Plane: 댓글 표식 ---


def _marker_attribute(key: str) -> str:
    return f'<p data-fl-key="{html.escape(key)}"></p>'


def _visible_marker(key: str) -> str:
    return f"<p>{_VISIBLE_MARKER}{html.escape(key, quote=False)}</p>"


def _same_text(comment: Mapping[str, Any], body: str) -> bool:
    return _plain_text(str(comment.get("comment_html", ""))) == _plain_text(body)


def _marker_keys(comment_html: str) -> set[str]:
    """댓글의 원천 키. 속성 표식과, 마지막 글 줄에 보이는 표식을 읽는다."""
    keys = {html.unescape(a or b) for a, b in _MARKER_ATTRIBUTE_RE.findall(comment_html)}
    lines = [text.strip() for text in _TAG_RE.split(comment_html) if text.strip()]
    last = html.unescape(lines[-1]) if lines else ""
    if last.startswith(_VISIBLE_MARKER):
        keys.add(last[len(_VISIBLE_MARKER) :])
    return keys


class _PlaneRun:
    """Plane 쪽 적용 한 번. 항목·상태·라벨 목록은 처음에 한 번만 읽고 다시 쓴다(호출 한도).

    `dry_run`이면 판정만 한다. 이때 `state`는 저장 자리가 없는 복사본이어야 한다.
    """

    def __init__(
        self, tracker: PlaneLike, project_id: str, state: AppliedState, comment_keys: list[str], dry_run: bool
    ) -> None:
        self._tracker = tracker
        self._project_id = project_id
        self._state = state
        self._comment_keys = comment_keys
        self._dry_run = dry_run
        self._items = {str(item["id"]): item for item in tracker.list_items(project_id)}
        self.states = tracker.list_states(project_id)
        self._label_ids = {str(label["name"]): str(label["id"]) for label in tracker.list_labels(project_id)}
        self._ids: dict[str, str] = {}  # 원천 키 → 항목 id. 자식을 붙이고 댓글을 읽을 수 있는 것만
        self._desired: dict[str, _Desired] = {}  # 원천 키 → 쓰려는 내용
        self._clean: set[str] = set()  # 충돌이 아닌 항목. 이 항목에만 댓글을 쓴다.
        self._pending: set[str] = set()  # 댓글을 쓰는 중이라고 표시한 항목
        self.disturbed: set[str] = set()  # 댓글을 쓰는 사이에 다른 곳에서 고친 항목
        self._stand_ins: set[str] = set()  # 이번 실행에서 어떤 레코드의 것으로 본 표식 없는 댓글 id
        self._comments: dict[str, list[dict[str, Any]]] = {}  # 항목 id별
        self._visible_only = False  # 서버가 표식 속성을 지우는 것을 확인했다.

    # 항목

    def item(self, record: Mapping[str, Any], desired: _Desired) -> Outcome:
        self._desired[record["key"]] = desired
        outcome = self._judge_item(record, desired)
        if outcome.action != "conflict":
            self._clean.add(outcome.key)
        return outcome

    def _judge_item(self, record: Mapping[str, Any], desired: _Desired) -> Outcome:
        key = record["key"]
        link_to = record.get("link_to")
        wanted = _manifest_digest(record, desired)
        mode = "link" if link_to else "item"
        recorded = self._state.get(key)
        applied = recorded if recorded is not None and recorded.digest else None
        found = match_external(list(self._items.values()), _SOURCE, key)
        if found is None:
            if applied is not None:  # 지웠거나 원천 표식을 뗐다. 다시 만들지 않는다.
                return Outcome(key, "conflict", applied.target)
            if link_to:
                return self._link(key, link_to, wanted)
            return self._create(key, desired, wanted)

        item_id = str(found["id"])
        if (applied is not None and applied.target != item_id) or (link_to and item_id != link_to):
            return Outcome(key, "conflict", item_id)
        self._ids[key] = item_id
        return self._existing(key, found, desired, mode, wanted, applied)

    def _existing(
        self,
        key: str,
        found: dict[str, Any],
        desired: _Desired,
        mode: str,
        wanted: str,
        applied: Optional[AppliedEntry],
    ) -> Outcome:
        item_id = str(found["id"])
        updated_at = str(found.get("updated_at", ""))
        parts = applied.digest.split(":") if applied is not None else []
        recorded_mode, recorded_wanted, stored = parts if len(parts) == 3 else ("", "", "")
        conflict = Outcome(key, "conflict", item_id)
        if applied is not None and recorded_mode != mode:  # `link_to`를 붙이거나 뗐다.
            return conflict
        fresh = applied is not None and recorded_wanted == wanted
        unchanged = applied is not None and applied.remote_updated_at == updated_at
        # 저장 내용 그대로인가. 서버가 저장하며 글을 다듬었어도 알아본다.
        as_stored = applied is not None and _item_digest(found) == stored
        # 관리 필드를 먼저 비교한다. 매니페스트와 같으면 수정 시각과 상관없이 같다. 쓰지 않고 기록만 맞춘다.
        # `link_to` 항목에서 관리하는 것은 원천 표식뿐이고, 여기까지 왔으면 표식은 맞다.
        if mode == "link" or self._matches(found, desired) or (fresh and as_stored):
            digest = f"{mode}:{wanted}:{_item_digest(found)}"
            if applied is None or (applied.digest, applied.remote_updated_at) != (digest, updated_at):
                _remember(self._state, key, item_id, digest, updated_at)
            return Outcome(key, "same", item_id)
        # 다르다. 마지막으로 기록한 뒤 손대지 않았을 때만 갱신한다.
        # 댓글을 쓰다가 멈춘 기록은 시각이 없으므로 저장 내용이 그대로인지로 본다.
        if unchanged or (applied is not None and not applied.remote_updated_at and as_stored):
            return self._update(key, found, desired, wanted)
        return conflict

    def _matches(self, found: Mapping[str, Any], desired: _Desired) -> bool:
        parent = self._ids.get(desired.parent_key) if desired.parent_key else None
        if (desired.parent_key and parent is None) or any(name not in self._label_ids for name in desired.labels):
            return False
        return (
            found.get("name") == desired.name
            and _plain_text(found.get("description_html") or "") == _plain_text(desired.description_html)
            and found.get("state") == desired.state_id
            and {str(i) for i in found.get("labels") or []} == {self._label_ids[n] for n in desired.labels}
            and (found.get("parent") or None) == parent
        )

    def _labels(self, names: tuple[str, ...]) -> list[str]:
        """없는 라벨은 만든다."""
        for name in names:
            if name not in self._label_ids:
                label = self._tracker.create_label(self._project_id, name, LABEL_COLOR)
                self._label_ids[name] = str(label["id"])
        return [self._label_ids[name] for name in names]

    def _create(self, key: str, desired: _Desired, wanted: str) -> Outcome:
        if desired.parent_key and desired.parent_key not in self._ids:
            return Outcome(key, "conflict", "")
        if self._dry_run:
            self._ids[key] = f"{_NEW_ID}{key}"
            return Outcome(key, "created", "")
        labels = self._labels(desired.labels)
        created = self._tracker.create_item(
            self._project_id,
            desired.name,
            desired.description_html,
            labels,
            parent=self._ids.get(desired.parent_key),
            state=desired.state_id,
            external_source=_SOURCE,
            external_id=key,
        )
        return self._written(key, "created", created, "item", wanted)

    def _link(self, key: str, link_to: str, wanted: str) -> Outcome:
        """기존 항목에는 원천 표식만 단다. 다른 표식이 이미 있으면 덮어쓰지 않는다."""
        target = self._items.get(link_to)
        if target is None or target.get("external_source") or target.get("external_id"):
            return Outcome(key, "conflict", link_to)
        marker = {"external_source": _SOURCE, "external_id": key}
        self._ids[key] = link_to
        if self._dry_run:
            self._items[link_to] = {**target, **marker}  # 같은 대상을 고른 다음 레코드가 충돌이 되게 한다.
            return Outcome(key, "updated", link_to)
        response = self._tracker.update_item(self._project_id, link_to, dict(marker))
        return self._written(key, "updated", response, "link", wanted)

    def _update(self, key: str, found: Mapping[str, Any], desired: _Desired, wanted: str) -> Outcome:
        """적용한 뒤 Plane에서 손대지 않은 항목에 바뀐 매니페스트를 쓴다."""
        item_id = str(found["id"])
        conflict = Outcome(key, "conflict", item_id)
        # 이름은 `update_item`이 받지 않으므로 바뀌었으면 알리기만 한다.
        if found.get("name") != desired.name or (desired.parent_key and desired.parent_key not in self._ids):
            return conflict
        if self._dry_run:
            return Outcome(key, "updated", item_id)
        # 목록은 실행을 시작할 때 읽었다. 그사이에 고쳤을 수 있으므로 쓰기 직전에 다시 확인한다.
        if self._tracker.get_item(self._project_id, item_id).get("updated_at") != found.get("updated_at"):
            return conflict
        fields = {
            "state": desired.state_id,
            "labels": self._labels(desired.labels),
            "parent": self._ids.get(desired.parent_key),
            "description_html": desired.description_html,
        }
        response = self._tracker.update_item(self._project_id, item_id, fields)
        return self._written(key, "updated", response, "item", wanted)

    def _written(self, key: str, action: str, response: dict[str, Any], mode: str, wanted: str) -> Outcome:
        """쓴 항목을 먼저 '쓰는 도중'으로 남기고, 다시 읽어 우리가 쓴 그대로면 수정 시각을 기록한다.

        다르면 그사이에 다른 곳에서 고친 것이다. 시각을 기록하지 않고 충돌로 알린다.
        """
        item_id = str(response["id"])
        saved = _item_digest(response)
        _remember(self._state, key, item_id, f"{mode}:{wanted}:{saved}", "")
        self._ids[key] = item_id
        current = self._tracker.get_item(self._project_id, item_id)
        self._items[item_id] = current
        if not self._as_written(key, current, mode, saved):
            return Outcome(key, "conflict", item_id)
        _remember(self._state, key, item_id, f"{mode}:{wanted}:{_item_digest(current)}", str(current["updated_at"]))
        return Outcome(key, action, item_id)

    def _as_written(self, key: str, current: dict[str, Any], mode: str, saved: str) -> bool:
        """다시 읽은 항목의 관리 필드가 우리가 보낸 내용이나 저장된 내용(`saved`) 그대로인가."""
        if match_external([current], _SOURCE, key) is None:
            return False
        return mode == "link" or _item_digest(current) == saved or self._matches(current, self._desired[key])

    # 댓글

    def comments(self, records: list[dict[str, Any]]) -> list[Outcome]:
        """대상 항목별로 시각순으로 쓰고, 항목 하나가 끝날 때마다 그 항목의 수정 시각을 다시 기록한다."""
        by_item: dict[str, list[dict[str, Any]]] = {}
        for record in sorted(records, key=lambda r: parse_comment_time(r["at"])):
            by_item.setdefault(record["item"], []).append(record)
        outcomes = []
        for item_key, group in by_item.items():
            outcomes += [self._comment(record, item_key) for record in group]
            self._settle(item_key)
        return outcomes

    def _comment(self, record: Mapping[str, Any], item_key: str) -> Outcome:
        key = record["key"]
        recorded = self._state.get(key)
        if recorded is not None and recorded.digest:  # 적용한 댓글은 Plane에서 고치거나 지워도 그대로 둔다.
            return Outcome(key, "same", recorded.target)
        item_id = self._ids.get(item_key)
        if item_id is None:
            return Outcome(key, "conflict", "")
        if item_id.startswith(_NEW_ID):
            return Outcome(key, "created", "")

        body = f"<p>{record['at']}</p>{record['body_html']}"
        digest = _sha(body.encode("utf-8"))
        existing = self._comment_list(item_id)
        for comment in existing:
            if key in _marker_keys(str(comment.get("comment_html", ""))):
                _remember(self._state, key, str(comment["id"]), digest)
                return Outcome(key, "same", str(comment["id"]))

        reserved = None
        if recorded is not None:  # 만들고 표식을 확인하기 전에 멈춘 댓글
            reserved = next((c for c in existing if str(c["id"]) == recorded.target), None)
        if reserved is not None and not _same_text(reserved, body):
            return Outcome(key, "conflict", str(reserved["id"]))
        unmarked = reserved or self._unclaimed_copy(body, existing)
        if unmarked is not None:  # 이미 있는 표식 없는 댓글은 누가 만든 것이든 고치지 않고, 또 만들지도 않는다.
            self._stand_ins.add(str(unmarked["id"]))
            return Outcome(key, "same-unmarked", str(unmarked["id"]))
        if item_key not in self._clean:
            return Outcome(key, "conflict", "")

        if self._dry_run:
            return Outcome(key, "created", "")
        self._before_comment_write(item_key)
        marker = _visible_marker(key) if self._visible_only else _marker_attribute(key)
        created = self._tracker.create_comment(self._project_id, item_id, body + marker)
        comment_id = str(created["id"])
        existing.append(created)
        self._reserve_comment(key, comment_id)
        if key not in _marker_keys(str(created.get("comment_html", ""))):
            existing[-1] = self._save_visible_marker(key, item_id, comment_id, body)
        _remember(self._state, key, comment_id, digest)
        return Outcome(key, "created", comment_id)

    def _comment_list(self, item_id: str) -> list[dict[str, Any]]:
        if item_id not in self._comments:
            self._comments[item_id] = self._tracker.list_comments(self._project_id, item_id)
        return self._comments[item_id]

    def _unclaimed_copy(self, body: str, existing: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
        """표식이 없고 다른 레코드의 것도 아닌 같은 글의 댓글. 댓글 하나는 레코드 하나만 대신한다."""
        entries = (self._state.get(key) for key in self._comment_keys)
        claimed = self._stand_ins | {entry.target for entry in entries if entry is not None}
        for comment in existing:
            marked = _marker_keys(str(comment.get("comment_html", "")))
            if str(comment["id"]) not in claimed and not marked and _same_text(comment, body):
                return comment
        return None

    def _reserve_comment(self, key: str, comment_id: str) -> None:
        self._state.put(key, AppliedEntry(target=comment_id, applied_at="", digest=""))
        self._state.checkpoint()

    def _save_visible_marker(self, key: str, item_id: str, comment_id: str, body: str) -> dict[str, Any]:
        updated = self._tracker.update_comment(self._project_id, item_id, comment_id, body + _visible_marker(key))
        if key not in _marker_keys(str(updated.get("comment_html", ""))):
            raise FlowError(f"댓글 표식이 저장되지 않음: {key} (다시 실행하기 전에 Plane 댓글을 확인하세요)")
        self._visible_only = True
        return updated

    def _before_comment_write(self, item_key: str) -> None:
        """댓글은 항목의 수정 시각을 바꾼다. 멈춰도 내 변경으로 알아보게 '쓰는 도중'으로 남긴다."""
        entry = self._state.get(item_key)
        if item_key not in self._pending and entry is not None:
            self._state.put(item_key, replace(entry, remote_updated_at=""))
            self._state.checkpoint()
            self._pending.add(item_key)

    def _settle(self, item_key: str) -> None:
        """댓글까지 쓴 항목을 다시 읽어, 관리 필드가 우리가 쓴 그대로일 때만 새 수정 시각을 기록한다.

        다르면 댓글을 쓰는 사이에 다른 곳에서 고친 것이다. 기록은 시각 없이 남아 다음 실행도 충돌이다.
        """
        entry = self._state.get(item_key)
        if item_key not in self._pending or entry is None:
            return
        self._pending.discard(item_key)
        current = self._tracker.get_item(self._project_id, entry.target)
        mode, _, saved = entry.digest.split(":")
        if not self._as_written(item_key, current, mode, saved):
            self._clean.discard(item_key)
            self.disturbed.add(item_key)
            return
        self._state.put(item_key, replace(entry, remote_updated_at=str(current["updated_at"])))
        self._state.checkpoint()


def apply_plane(m: Manifest, tracker: PlaneLike, project_id: str, state: AppliedState) -> list[Outcome]:
    """Work → Item(부모 먼저) → 댓글(시각순) → 백로그 순서로 쓴다. 백로그의 댓글은 백로그 뒤에 쓴다."""
    return _run_plane(m, tracker, project_id, state, dry_run=False)


def _run_plane(
    m: Manifest, tracker: PlaneLike, project_id: str, state: AppliedState, *, dry_run: bool
) -> list[Outcome]:
    # Plane에 쓰기 전의 마지막 관문이다. resource 파일은 `apply_git`이 읽으면서 검사한다.
    plane_side = replace(m, records=tuple(r for r in m.records if r.get("kind") != "resource"))
    problems = check_manifest(plane_side, Path())
    if problems:
        raise FlowError(f"매니페스트 검사 실패: {len(problems)}건 (ledger migrate check로 확인)")

    by_key = {r["key"]: r for r in m.records}

    def depth(record: Mapping[str, Any]) -> int:
        return 1 + depth(by_key[record["parent"]]) if record["kind"] == "item" else 0

    def of_kind(kind: str) -> list[dict[str, Any]]:
        return [r for r in m.records if r["kind"] == kind]

    comments = of_kind("comment")
    if not (comments or any(of_kind(kind) for kind in ("work", "item", "backlog"))):
        return []
    run = _PlaneRun(tracker, project_id, state, [r["key"] for r in comments], dry_run)
    tree = [*of_kind("work"), *sorted(of_kind("item"), key=depth)]
    backlog = of_kind("backlog")
    # 쓰려는 내용을 모두 만든 뒤에 쓰기를 시작한다(상태 그룹이 없으면 여기서 멈춘다).
    desired = {r["key"]: _desired(r, run.states) for r in (*tree, *backlog)}
    backlog_keys = {r["key"] for r in backlog}

    outcomes = [run.item(r, desired[r["key"]]) for r in tree]
    outcomes += run.comments([r for r in comments if r["item"] not in backlog_keys])
    outcomes += [run.item(r, desired[r["key"]]) for r in backlog]
    outcomes += run.comments([r for r in comments if r["item"] in backlog_keys])
    # 댓글을 쓴 뒤에야 알게 된 충돌은 앞에서 낸 항목 결과를 바꾼다.
    return [replace(o, action="conflict") if o.key in run.disturbed else o for o in outcomes]


# --- 미리 보기와 `migrate apply` ---


def plan_apply(
    m: Manifest,
    repo: Repo,
    tracker: PlaneLike,
    project_id: str,
    state: AppliedState,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> ApplyPlan:
    """`apply`가 할 일을 같은 판정으로 계산한다. Plane·git·적용 기록을 읽기만 한다."""
    preview = state.detached()
    outcomes = _run_git(m, repo, tracker, preview, env, dry_run=True)
    outcomes += _run_plane(m, tracker, project_id, preview, dry_run=True)
    plan = ApplyPlan()
    lists = {
        "created": plan.create,
        "updated": plan.update,
        "same": plan.skip,
        "same-unmarked": plan.skip,
        "conflict": plan.conflict,
    }
    for outcome in outcomes:
        lists[outcome.action].append(outcome.key)
    return plan


def load_checked(path: Path, env: Mapping[str, str]) -> Optional[Manifest]:
    """매니페스트를 읽어 검사한다. 문제가 있으면 목록을 출력하고 `None`이다."""
    m = load_manifest(path)
    problems = check_manifest(m, resolve_source_dir(m, env))
    if not problems:
        return m
    for problem in problems:
        print(f"{problem.key}: {problem.message}")
    print(f"문제 {len(problems)}건")
    return None


def _load_applied(m: Manifest, env: Mapping[str, str]) -> AppliedState:
    """매니페스트 파일이 어디에 있든 적용 기록은 정해진 자리의 것을 쓴다."""
    return AppliedState.load(manifest_path(m.slug, env).with_name(f"{m.slug}.applied.json"))


def _render_result(m: Manifest, outcomes: list[Outcome]) -> str:
    kinds = {r["key"]: r["kind"] for r in m.records}
    lines = ["종류 | 새로 만듦 | 바꿈 | 그대로 | 충돌", "--- | --- | --- | --- | ---"]
    for kind in dict.fromkeys(kinds[o.key] for o in outcomes):
        actions = [o.action.replace("same-unmarked", "same") for o in outcomes if kinds[o.key] == kind]
        counts = [str(actions.count(action)) for action in ("created", "updated", "same", "conflict")]
        lines.append(" | ".join([kind, *counts]))
    conflicts = [o for o in outcomes if o.action == "conflict"]
    if conflicts:
        lines += ["", "충돌(덮어쓰지 않음)"]
        lines += [f"- {o.key} → {o.target}" if o.target else f"- {o.key}" for o in conflicts]
    unmarked = [o for o in outcomes if o.action == "same-unmarked"]
    if unmarked:
        lines += ["", "표식 없는 같은 글 댓글이 있어 건너뜀(그대로에 포함, 고치지 않음)"]
        lines += [f"- {o.key} → {o.target} (same-unmarked)" for o in unmarked]
    return "\n".join(lines)


def run_preview(path: Path, repo: Repo, tracker: PlaneLike, env: Mapping[str, str]) -> int:
    """`ledger migrate preview`. 아무것도 쓰지 않는다. 검사에 걸리면 1이다."""
    m = load_checked(path, env)
    if m is None:
        return 1
    plan = plan_apply(m, repo, tracker, default_project_id(repo, tracker), _load_applied(m, env), env=env)
    print(render_preview(m, plan), end="")
    return 0


def run_apply(path: Path, repo: Repo, tracker: PlaneLike, env: Mapping[str, str]) -> int:
    """`ledger migrate apply`. 검사를 통과해야 쓴다. 검사 실패는 1, 충돌이 있으면 3이다."""
    m = load_checked(path, env)
    if m is None:
        return 1
    state = _load_applied(m, env)
    project_id = default_project_id(repo, tracker)
    outcomes = apply_git(m, repo, tracker, state, env=env)
    outcomes += apply_plane(m, tracker, project_id, state)
    print(_render_result(m, outcomes))
    return 3 if any(o.action == "conflict" for o in outcomes) else 0
