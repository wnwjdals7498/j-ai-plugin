"""PMT 이전 매니페스트의 git 쪽(project·결정·사실·resources)을 `.ledger/`에 쓴다.

원칙: 같은 매니페스트를 다시 적용해도 중복되지 않고, 적용 뒤 바뀐 내용은 덮어쓰지 않고 충돌로 알린다.
다만 지금 내용이 쓰려는 내용과 같으면 `same`으로 보고 적용 기록을 복구한다(쓴 뒤 기록 저장 전에 멈춘 경우).
결정은 Plane '[계획서] 결정'에도 절이 생긴다. 모든 확인을 끝낸 뒤에 쓰기를 시작한다.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import subprocess  # nosec B404 # 현재 브랜치 조회용(shell=False)
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ledger.facts import Fact, format_row, normalize_fact, parse_facts, upsert_fact
from ledger.flows import FlowError, Repo, SecretFound, append_decision, decision_item, plane_ref
from ledger.history import HistoryEntry, append_entry, format_entry, new_decision_id, parse_history
from ledger.migrate import Manifest, check_manifest, load_resource, resolve_source_dir
from ledger.plane import PlaneLike
from ledger.secrets import find_secrets
from ledger.sections import split_html_headings, split_html_sections

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


@dataclass(frozen=True)
class AppliedEntry:
    """적용 기록 한 건. `digest`는 쓴 직후의 내용, `remote_updated_at`은 Plane 레코드에만 쓴다.

    결정의 `digest`는 이력 줄, 매니페스트 내용, Plane이 저장한 절의 글(태그 제거)을 함께 담는다.

    `digest`와 `applied_at`이 비어 있으면 결정의 새 ID(`target`)만 예약하고 아직 쓰지 않은 것이다.
    """

    target: str
    applied_at: str
    digest: str
    remote_updated_at: str = ""


@dataclass(frozen=True)
class Outcome:
    key: str
    action: str  # created | updated | same | conflict
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


def _remember(state: AppliedState, key: str, target: str, digest: str) -> None:
    applied_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state.put(key, AppliedEntry(target=target, applied_at=applied_at, digest=digest))
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


def _apply_file(repo: Repo, state: AppliedState, key: str, relative: str, content: bytes) -> Outcome:
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


def _apply_fact(repo: Repo, state: AppliedState, key: str, wanted: Fact) -> Outcome:
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
    decision: _Decision, texts: Mapping[str, str], repo: Repo, client: PlaneLike, state: AppliedState
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

    if section is not None:  # Plane에는 썼고 이력을 남기기 전에 멈췄다.
        if section != decision.expected_section():
            return conflict
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
    source_dir = resolve_source_dir(m, os.environ if env is None else env)
    problems = check_manifest(m, source_dir)
    if problems:  # 공개 저장소에 쓰는 마지막 관문이다. 하나라도 걸리면 아무것도 쓰지 않는다.
        raise FlowError(f"매니페스트 검사 실패: {len(problems)}건 (ledger migrate check로 확인)")
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
        _apply_file(repo, state, r["key"], _PROJECT_FILE, r["body_md"].encode("utf-8")) for r in by_kind["project"]
    ]
    outcomes += [_apply_decision(decision, section_texts, repo, client, state) for decision in decisions]
    outcomes += [_apply_fact(repo, state, key, fact) for key, fact in facts]
    outcomes += [_apply_file(repo, state, key, relative, data) for key, relative, data in files]
    return outcomes
