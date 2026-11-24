from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ledger.miniyaml import MiniYamlError, loads

_TOP_KEYS = {"method_version", "plane", "screens", "hub"}
_PLANE_KEYS = {"workspace", "projects"}
_PROJECT_KEYS = {"key", "title", "default"}
_SCREENS_KEYS = {"tool", "team", "files"}


@dataclass
class PlaneProject:
    """Single Plane project attached to a repository."""

    key: str
    title: str
    default: bool = False


@dataclass
class LedgerConfig:
    """Parsed contents of `.ledger/ledger.yml`."""

    method_version: int
    workspace: str
    projects: list[PlaneProject]
    screens_tool: str = "penpot"
    screens_team: str = ""
    screens_files: list[str] = field(default_factory=list)
    hub: str = ""

    def default_project(self) -> PlaneProject:
        """Return the project marked `default: true`, or raise LookupError."""
        for project in self.projects:
            if project.default:
                return project
        raise LookupError("no default project configured")


def _check_allowed_keys(mapping: dict[str, Any], allowed: set[str], prefix: str) -> None:
    for key in mapping:
        if key not in allowed:
            dotted = f"{prefix}.{key}" if prefix else key
            raise MiniYamlError(f"unknown key: {dotted}")


def _require_str(mapping: dict[str, Any], key: str, dotted: str) -> str:
    if key not in mapping:
        raise MiniYamlError(f"missing required key: {dotted}")
    val = mapping[key]
    if not isinstance(val, str):
        raise MiniYamlError(f"expected string for {dotted}")
    return val


def _optional_str(mapping: dict[str, Any], key: str, default: str, dotted: str) -> str:
    if key not in mapping:
        return default
    val = mapping[key]
    if not isinstance(val, str):
        raise MiniYamlError(f"expected string for {dotted}")
    return val


def load_config_text(text: str) -> LedgerConfig:
    """Parse and validate a `ledger.yml` string."""
    raw = loads(text)
    _check_allowed_keys(raw, _TOP_KEYS, "")

    if "method_version" not in raw:
        raise MiniYamlError("missing required key: method_version")
    mv = raw["method_version"]
    if isinstance(mv, bool) or not isinstance(mv, int):
        raise MiniYamlError("expected integer for method_version")

    raw_plane = raw.get("plane")
    if not isinstance(raw_plane, dict):
        raise MiniYamlError("missing or invalid mapping for plane")
    _check_allowed_keys(raw_plane, _PLANE_KEYS, "plane")

    workspace = _require_str(raw_plane, "workspace", "plane.workspace")

    raw_projects = raw_plane.get("projects", [])
    if raw_projects == {}:
        raw_projects = []
    if not isinstance(raw_projects, list):
        raise MiniYamlError("expected list for plane.projects")

    projects: list[PlaneProject] = []
    for item in raw_projects:
        if not isinstance(item, dict):
            raise MiniYamlError("expected mapping in plane.projects[]")
        _check_allowed_keys(item, _PROJECT_KEYS, "plane.projects[]")
        p_key = _require_str(item, "key", "plane.projects[].key")
        p_title = _require_str(item, "title", "plane.projects[].title")
        p_default = item.get("default", False)
        if not isinstance(p_default, bool):
            raise MiniYamlError("expected boolean for plane.projects[].default")
        projects.append(PlaneProject(key=p_key, title=p_title, default=p_default))

    raw_screens = raw.get("screens", {})
    if not isinstance(raw_screens, dict):
        raise MiniYamlError("expected mapping for screens")
    _check_allowed_keys(raw_screens, _SCREENS_KEYS, "screens")

    screens_tool = _optional_str(raw_screens, "tool", "penpot", "screens.tool")
    screens_team = _optional_str(raw_screens, "team", "", "screens.team")
    raw_files = raw_screens.get("files", [])
    if raw_files == {}:
        raw_files = []
    if not isinstance(raw_files, list) or not all(isinstance(f, str) for f in raw_files):
        raise MiniYamlError("expected list of strings for screens.files")

    hub = _optional_str(raw, "hub", "", "hub")

    return LedgerConfig(
        method_version=mv,
        workspace=workspace,
        projects=projects,
        screens_tool=screens_tool,
        screens_team=screens_team,
        screens_files=list(raw_files),
        hub=hub,
    )


def load_config(path: Path) -> LedgerConfig:
    """Read and validate `ledger.yml` from disk."""
    return load_config_text(path.read_text(encoding="utf-8"))


def _quote_double(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
    return f'"{escaped}"'


def _yaml_scalar(value: str) -> str:
    if (
        value == ""
        or value in ("true", "false")
        or (value.lstrip("-").isdigit() and value != "-")
        or any(ch in value for ch in ("#", ":", '"', "'", "[", "]", "\n", "\t"))
        or value[0].isspace()
        or value[-1].isspace()
    ):
        return _quote_double(value)
    return value


def _with_comment(line: str, comment: str) -> str:
    pad = max(1, 31 - len(line))
    return f"{line}{' ' * pad}# {comment}"


def render_config(cfg: LedgerConfig) -> str:
    """Render a `LedgerConfig` in the canonical `.ledger/ledger.yml` format."""
    lines: list[str] = [
        "# 비밀값과 접속 주소는 넣지 않는다",
        _with_comment(f"method_version: {cfg.method_version}", "작성방식 규격 버전"),
        "plane:",
        _with_comment(f"  workspace: {_yaml_scalar(cfg.workspace)}", "워크스페이스 이름"),
        _with_comment("  projects:", "이 저장소에 붙은 Plane 프로젝트(D5)"),
    ]
    for idx, project in enumerate(cfg.projects):
        key_line = f"    - key: {_yaml_scalar(project.key)}"
        if idx == 0:
            key_line = _with_comment(key_line, "작업 항목 식별자 접두어(키 규칙은 P0에서)")
        lines.append(key_line)
        lines.append(f"      title: {_yaml_scalar(project.title)}")
        if project.default:
            lines.append(
                _with_comment("      default: true", "plan fix, 넘어간 기능, 저장소 전체 결정의 대상(D15·D16)")
            )

    files_rendered = ", ".join(_quote_double(f) for f in cfg.screens_files)
    lines.extend(
        [
            "screens:",
            _with_comment(f"  tool: {_yaml_scalar(cfg.screens_tool)}", "D14. figma는 자리만"),
            _with_comment(f"  team: {_yaml_scalar(cfg.screens_team)}", "Penpot 팀 이름"),
            f"  files: [{files_rendered}]",
            _with_comment(f"hub: {_yaml_scalar(cfg.hub)}", "허브 이름. 주소는 PMT 환경 문서와 userConfig"),
        ]
    )
    return "\n".join(lines) + "\n"


def save_config(cfg: LedgerConfig, path: Path) -> None:
    """Render and write `ledger.yml` to disk with UTF-8 encoding."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_config(cfg), encoding="utf-8")
