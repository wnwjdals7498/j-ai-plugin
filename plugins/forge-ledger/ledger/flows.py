from __future__ import annotations

import re
import shutil
import subprocess  # nosec B404 # git 저장소 루트 조회용(shell=False)
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Optional

from ledger.check import SourceUnavailable
from ledger.config import LedgerConfig, PlaneProject, load_config, save_config
from ledger.history import HistoryEntry, append_entry, empty_history, new_decision_id, parse_history
from ledger.plane import PlaneAuthError, PlaneLike, PlaneUnavailable
from ledger.secrets import find_secrets
from ledger.sections import append_html_section, replace_html_section, split_html_sections, split_md_sections
from ledger.spec import DOC_BY_CODE, METHOD_VERSION, TEMPLATE_FOR, UNIT_ITEMS

_KEY_RE = re.compile(r"[A-Z0-9]{1,12}")
_MAX_KEY_LEN = 12
_PLAN_LABEL_NAME = "계획서"
_PLAN_LABEL_COLOR = "#6b7280"
_PLAN_MODULE_NAME = "계획서"
_PROTO_CYCLE_NAME = "프로토타입"
# Plane v1.5.0 apps/api/plane/db/models/project.py:145 FORBIDDEN_IDENTIFIER_CHARS_PATTERN
# (검사: apps/api/plane/api/serializers/project.py:111). 이름에 하나라도 있으면 HTTP 400.
_PLANE_FORBIDDEN_CHARS_RE = re.compile(r"[&+,:;$^}{*=?@#|'<>.()%!-]")


class FlowError(Exception):
    """작업 흐름 규칙 위반 또는 상태 오류."""


class SecretFound(FlowError):
    """문서 본문에서 비밀값 패턴이 감지되었을 때 발생한다."""


@dataclass
class Repo:
    """로컬 작업 저장소 정보."""

    root: Path
    name: str

    @classmethod
    def discover(cls, start: Path) -> Repo:
        """git 최상위 폴더를 찾고, 실패하면 start를 저장소 루트로 쓴다."""
        try:
            proc = subprocess.run(  # nosec B603 B607 # 고정 git rev-parse 명령 실행(shell=False)
                ["git", "rev-parse", "--show-toplevel"],
                cwd=start,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            proc = None
        if proc is not None and proc.returncode == 0 and proc.stdout.strip():
            root = Path(proc.stdout.strip())
        else:
            root = start.resolve()
        return cls(root=root, name=root.name)


@dataclass
class PlanNewResult:
    """plan_new 실행 결과."""

    project_id: str
    item_ids: dict[str, str]
    decision_id: str


def plane_project_name(text: str) -> str:
    """Plane 프로젝트 이름에 쓸 수 없는 문자를 공백으로 바꾸고 공백을 정리한다."""
    replaced = _PLANE_FORBIDDEN_CHARS_RE.sub(" ", text)
    return re.sub(r"\s+", " ", replaced).strip()


def maintenance_key(repo_name: str, taken: set[str]) -> str:
    """저장소 이름과 사용 중인 키 집합으로 유지보수 프로젝트 키를 정한다."""
    taken_upper = {k.upper() for k in taken}
    cleaned = re.sub(r"[^A-Z0-9]", "", repo_name.upper())
    prefix = cleaned[:4] or "REPO"
    base = f"{prefix}M"
    if base not in taken_upper:
        return base
    n = 2
    while True:
        suffix = str(n)
        if len(base) + len(suffix) <= _MAX_KEY_LEN:
            candidate = f"{base}{suffix}"
        else:
            keep = _MAX_KEY_LEN - len(suffix)
            candidate = f"{prefix[: keep - 1]}M{suffix}" if keep >= 1 else suffix[:_MAX_KEY_LEN]
        if candidate not in taken_upper:
            return candidate
        n += 1


def _ledger_dir(repo: Repo) -> Path:
    return repo.root / ".ledger"


def _load_repo_config(repo: Repo) -> LedgerConfig:
    path = _ledger_dir(repo) / "ledger.yml"
    if not path.is_file():
        raise FlowError("초기화되지 않은 저장소: .ledger/ledger.yml 없음")
    return load_config(path)


def _require_registered_key(cfg: LedgerConfig, key: str) -> None:
    if not any(p.key == key for p in cfg.projects):
        raise FlowError(f"등록되지 않은 프로젝트 키: {key}")


def _existing_history_ids(history_path: Path) -> set[str]:
    if not history_path.is_file():
        return set()
    return {entry.ref_id for entry in parse_history(history_path.read_text(encoding="utf-8"))}


def _find_project_id(client: PlaneLike, key: str) -> str:
    for proj in client.list_projects():
        if proj.get("identifier") == key:
            return str(proj["id"])
    raise FlowError(f"Plane 프로젝트 없음: {key}")


def _find_container_item(client: PlaneLike, project_id: str, container: str) -> dict[str, Any]:
    expected_name = UNIT_ITEMS[container]
    for item in client.list_items(project_id):
        if item.get("name") == expected_name:
            return item
    raise FlowError(f"계획서 항목 없음: {expected_name}")


def _copy_ledger_templates(templates: Path, ledger_dir: Path) -> None:
    src_root = templates / "ledger"
    for src in sorted(src_root.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(src_root)
        dst = ledger_dir / rel
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)


def _check_secrets(*texts: str) -> None:
    found: set[str] = set()
    for text in texts:
        found.update(find_secrets(text))
    if found:
        raise SecretFound(f"비밀값 감지: {', '.join(sorted(found))}")


def _append_error_note(exc: Exception, note: str) -> Exception:
    message = f"{exc}{note}"
    try:
        return type(exc)(message)
    except Exception:
        exc.args = (message,)
        return exc


def _restore_ledger_yml(cfg_path: Path, previous_bytes: bytes, key: str, exc: Exception) -> Exception:
    try:
        cfg_path.write_bytes(previous_bytes)
    except Exception:
        return _append_error_note(exc, f" (ledger.yml 복원 실패: {key} 항목 수동 삭제 필요)")
    return exc


def _rollback_project(client: PlaneLike, project_id: str, key: str, exc: Exception) -> Exception:
    try:
        client.delete_project(project_id)
    except Exception:
        return _append_error_note(exc, f" (정리 실패: Plane 프로젝트 {key} 수동 삭제 필요)")
    return exc


def init_repo(
    repo: Repo,
    client: PlaneLike,
    workspace: str,
    templates: Path,
    today: date,
) -> LedgerConfig:
    """Plane 유지보수 프로젝트를 먼저 만든 뒤 로컬 .ledger/ 뼈대와 설정을 쓴다."""
    _ = today
    ledger_dir = _ledger_dir(repo)
    cfg_path = ledger_dir / "ledger.yml"
    if cfg_path.exists():
        raise FlowError("이미 초기화된 저장소입니다: .ledger/ledger.yml")

    title = plane_project_name(f"{repo.name} 유지보수")
    _check_secrets(repo.name, title, workspace)

    decision_html = (templates / "plane" / TEMPLATE_FOR["DECISION"]).read_text(encoding="utf-8")

    existing_projects = client.list_projects()
    taken = {str(p["identifier"]).upper() for p in existing_projects if p.get("identifier")}
    key = maintenance_key(repo.name, taken)

    project = client.create_project(title, key)
    project_id = str(project["id"])
    try:
        label = client.create_label(project_id, _PLAN_LABEL_NAME, _PLAN_LABEL_COLOR)
        label_id = str(label["id"])
        module = client.create_module(project_id, _PLAN_MODULE_NAME)
        module_id = str(module["id"])
        decision_item = client.create_item(project_id, UNIT_ITEMS["DECISION"], decision_html, [label_id])
        client.add_to_module(project_id, module_id, [str(decision_item["id"])])

        _copy_ledger_templates(templates, ledger_dir)
        history_path = ledger_dir / "history.md"
        if not history_path.exists():
            history_path.parent.mkdir(parents=True, exist_ok=True)
            history_path.write_text(empty_history(), encoding="utf-8")

        cfg = LedgerConfig(
            method_version=METHOD_VERSION,
            workspace=workspace,
            projects=[PlaneProject(key=key, title=title, default=True)],
        )
        save_config(cfg, cfg_path)
    except Exception as exc:
        rolled = _rollback_project(client, project_id, key, exc)
        if rolled is exc:
            raise
        raise rolled from exc
    return cfg


def plan_new(
    repo: Repo,
    client: PlaneLike,
    title: str,
    key: str,
    templates: Path,
    today: date,
) -> PlanNewResult:
    """단위 프로젝트를 Plane에 만들고 ledger.yml과 history.md에 기록한다."""
    if not _KEY_RE.fullmatch(key):
        raise FlowError(f"프로젝트 키 규칙 위반: {key}")
    raw_title = title
    title = plane_project_name(raw_title)
    _check_secrets(raw_title, title)
    if not title:
        raise FlowError("제목에 Plane 프로젝트 이름으로 쓸 수 있는 글자가 없습니다")
    cfg = _load_repo_config(repo)
    _check_secrets(cfg.workspace)
    if any(p.key == key for p in cfg.projects):
        raise FlowError(f"이미 붙은 키: {key}")

    html_by_container = {
        container: (templates / "plane" / filename).read_text(encoding="utf-8")
        for container, filename in TEMPLATE_FOR.items()
    }

    project = client.create_project(title, key)
    project_id = str(project["id"])
    ledger_dir = _ledger_dir(repo)
    cfg_path = ledger_dir / "ledger.yml"
    previous_cfg_bytes: Optional[bytes] = None
    try:
        label = client.create_label(project_id, _PLAN_LABEL_NAME, _PLAN_LABEL_COLOR)
        label_id = str(label["id"])
        module = client.create_module(project_id, _PLAN_MODULE_NAME)
        module_id = str(module["id"])

        item_ids: dict[str, str] = {}
        prd_item: Optional[dict[str, Any]] = None
        for container, html in html_by_container.items():
            item = client.create_item(project_id, UNIT_ITEMS[container], html, [label_id])
            item_ids[container] = str(item["id"])
            if container == "PRD":
                prd_item = item

        client.add_to_module(project_id, module_id, list(item_ids.values()))
        client.create_cycle(project_id, _PROTO_CYCLE_NAME)

        if prd_item is None:
            raise FlowError("PRD 항목 생성 실패")

        previous_cfg_bytes = cfg_path.read_bytes()
        cfg.projects.append(PlaneProject(key=key, title=title, default=False))
        save_config(cfg, cfg_path)

        history_path = ledger_dir / "history.md"
        decision_id = new_decision_id(today, _existing_history_ids(history_path))
        detail = f"plane:{key}-{prd_item['sequence_id']}@{prd_item['updated_at']}"
        entry = HistoryEntry(
            date=today.isoformat(),
            kind="decision",
            ref_id=decision_id,
            projects=key,
            title=f"단위 프로젝트 {key} 시작",
            detail=detail,
        )
        append_entry(history_path, entry)
    except Exception as exc:
        rolled = exc
        if previous_cfg_bytes is not None:
            rolled = _restore_ledger_yml(cfg_path, previous_cfg_bytes, key, rolled)
        rolled = _rollback_project(client, project_id, key, rolled)
        if rolled is exc:
            raise
        raise rolled from exc
    return PlanNewResult(project_id=project_id, item_ids=item_ids, decision_id=decision_id)


def plan_get(repo: Repo, client: PlaneLike, key: str, code: str) -> str:
    """문서 코드에 해당하는 절 본문을 읽는다."""
    doc = DOC_BY_CODE.get(code)
    if doc is None:
        raise FlowError(f"알 수 없는 문서 코드: {code}")

    if doc.scope == "repo":
        path = repo.root / doc.container
        if not path.is_file():
            raise FlowError(f"파일 없음: {doc.container}")
        md_sections = split_md_sections(path.read_text(encoding="utf-8"))
        if code not in md_sections:
            raise FlowError(f"절 없음: {code}")
        return md_sections[code]

    cfg = _load_repo_config(repo)
    _require_registered_key(cfg, key)
    project_id = _find_project_id(client, key)
    listed = _find_container_item(client, project_id, doc.container)
    item = client.get_item(project_id, str(listed["id"]))
    html_sections = split_html_sections(str(item.get("description_html", "")))
    if code not in html_sections:
        raise FlowError(f"절 없음: {code}")
    return html_sections[code]


def plan_put(repo: Repo, client: PlaneLike, key: str, code: str, body_html: str) -> str:
    """단위 문서의 지정 절을 갱신하고 새 updated_at을 돌려준다."""
    doc = DOC_BY_CODE.get(code)
    if doc is None:
        raise FlowError(f"알 수 없는 문서 코드: {code}")
    if doc.scope == "repo":
        raise FlowError(f"저장소 문서는 .ledger/ 파일을 직접 고친다: {doc.container}")

    found = find_secrets(body_html)
    if found:
        raise SecretFound(f"비밀값 감지: {', '.join(found)}")

    cfg = _load_repo_config(repo)
    _require_registered_key(cfg, key)
    project_id = _find_project_id(client, key)
    listed = _find_container_item(client, project_id, doc.container)
    item_id = str(listed["id"])
    item = client.get_item(project_id, item_id)
    expected_updated_at = str(item["updated_at"])

    try:
        new_html = replace_html_section(str(item.get("description_html", "")), code, body_html)
    except KeyError:
        raise FlowError(f"절 없음: {code}") from None

    updated = client.update_item_description(
        project_id,
        item_id,
        new_html,
        expected_updated_at=expected_updated_at,
    )
    return str(updated["updated_at"])


def decide(
    repo: Repo,
    client: PlaneLike,
    title: str,
    project: str,
    body_html: str,
    today: date,
) -> HistoryEntry:
    """결정 항목을 Plane '[계획서] 결정'에 추가하고 history.md에 한 줄을 남긴다."""
    found = find_secrets(f"{title}\n{body_html}")
    if found:
        raise SecretFound(f"비밀값 감지: {', '.join(found)}")
    if any(ch in title for ch in ("|", "\n", "\r")):
        raise FlowError("제목에 사용할 수 없는 문자가 있음")

    cfg = _load_repo_config(repo)
    if project == "*":
        try:
            target_key = cfg.default_project().key
        except LookupError as exc:
            raise FlowError("기본 프로젝트가 설정되지 않음") from exc
    else:
        _require_registered_key(cfg, project)
        target_key = project

    history_path = _ledger_dir(repo) / "history.md"
    ref_id = new_decision_id(today, _existing_history_ids(history_path))

    project_id = _find_project_id(client, target_key)
    listed = _find_container_item(client, project_id, "DECISION")
    item_id = str(listed["id"])
    item = client.get_item(project_id, item_id)
    expected_updated_at = str(item["updated_at"])

    new_html = append_html_section(str(item.get("description_html", "")), f"{ref_id} {title}", body_html)
    updated = client.update_item_description(
        project_id,
        item_id,
        new_html,
        expected_updated_at=expected_updated_at,
    )

    detail = f"plane:{target_key}-{updated['sequence_id']}@{updated['updated_at']}"
    entry = HistoryEntry(
        date=today.isoformat(),
        kind="decision",
        ref_id=ref_id,
        projects=project,
        title=title,
        detail=detail,
    )
    append_entry(history_path, entry)
    return entry


class PlaneDocSource:
    """Plane 작업 항목에서 단위 계획서 컨테이너 HTML을 읽는 UnitDocSource 구현."""

    def __init__(self, client: PlaneLike, cfg: LedgerConfig) -> None:
        self._client = client
        self._cfg = cfg
        self._project_ids: Optional[dict[str, str]] = None
        self._item_ids: dict[str, dict[str, str]] = {}

    def _resolve_project_id(self, project_key: str) -> Optional[str]:
        if self._project_ids is None:
            projects = self._client.list_projects()
            self._project_ids = {
                str(p["identifier"]): str(p["id"]) for p in projects if p.get("identifier") and p.get("id")
            }
        return self._project_ids.get(project_key)

    def _resolve_item_id(self, project_id: str, item_name: str) -> Optional[str]:
        if project_id not in self._item_ids:
            items = self._client.list_items(project_id)
            self._item_ids[project_id] = {
                str(it["name"]): str(it["id"]) for it in items if it.get("name") and it.get("id")
            }
        return self._item_ids[project_id].get(item_name)

    def get_container_html(self, project_key: str, container: str) -> Optional[str]:
        item_name = UNIT_ITEMS.get(container)
        if item_name is None:
            return None
        try:
            project_id = self._resolve_project_id(project_key)
            if project_id is None:
                return None
            item_id = self._resolve_item_id(project_id, item_name)
            if item_id is None:
                return None
            item = self._client.get_item(project_id, item_id)
        except PlaneAuthError as exc:
            raise SourceUnavailable(str(exc), kind="auth") from exc
        except PlaneUnavailable as exc:
            raise SourceUnavailable(str(exc), kind="unavailable") from exc
        html = item.get("description_html")
        return str(html) if isinstance(html, str) else ""
