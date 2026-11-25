#!/usr/bin/env python3
"""SessionStart hook: export userConfig options, print ledger summary, and refresh stale hub data."""

from __future__ import annotations

import os
import shlex
import subprocess  # nosec B404 # 백그라운드 대조 실행용(shell=False)
import sys
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

_PLUGIN_ROOT = str(Path(__file__).resolve().parents[1])
if not sys.path or sys.path[0] != _PLUGIN_ROOT:
    sys.path[:] = [_PLUGIN_ROOT, *(p for p in sys.path if p != _PLUGIN_ROOT)]

from ledger.config import load_config  # noqa: E402
from ledger.history import parse_history  # noqa: E402
from ledger.hubstore import DB_NAME, HubStore, load_registry  # noqa: E402
from ledger.hubsync import project_root  # noqa: E402
from ledger.hubview import render_session, repo_summary  # noqa: E402
from ledger.ratelimit import default_state_dir  # noqa: E402

_REFRESH_AFTER = timedelta(seconds=600)
_TOKEN_KEYS = ("FORGE_LEDGER_GITEA_TOKEN", "FORGE_LEDGER_PLANE_TOKEN")

_OPTION_EXPORTS: tuple[tuple[str, str], ...] = (
    ("CLAUDE_PLUGIN_OPTION_PLANE_URL", "FORGE_LEDGER_PLANE_URL"),
    ("CLAUDE_PLUGIN_OPTION_PLANE_WORKSPACE", "FORGE_LEDGER_PLANE_WORKSPACE"),
    ("CLAUDE_PLUGIN_OPTION_PLANE_API_KEY", "FORGE_LEDGER_PLANE_TOKEN"),
    ("CLAUDE_PLUGIN_OPTION_GITEA_URL", "FORGE_LEDGER_GITEA_URL"),
    ("CLAUDE_PLUGIN_OPTION_GITEA_TOKEN", "FORGE_LEDGER_GITEA_TOKEN"),
    ("CLAUDE_PLUGIN_OPTION_DASHBOARD_BIND", "FORGE_LEDGER_DASHBOARD_BIND"),
)


def build_exports(env: Mapping[str, str]) -> list[str]:
    """Build shell export lines from non-empty plugin option environment variables."""
    lines: list[str] = []
    for src_key, dst_key in _OPTION_EXPORTS:
        val = env.get(src_key, "")
        if not val:
            continue
        lines.append(f"export {dst_key}={shlex.quote(val)}")
    return lines


def summary(repo_root: Path) -> str:
    """Return a single-line summary for a repository containing `.ledger/`."""
    ledger_dir = repo_root / ".ledger"
    config_path = ledger_dir / "ledger.yml"
    if not ledger_dir.is_dir() or not config_path.is_file():
        return ""

    cfg = load_config(config_path)
    try:
        default_key = cfg.default_project().key
    except LookupError:
        default_key = cfg.projects[0].key if cfg.projects else "없음"

    history_path = ledger_dir / "history.md"
    entries = parse_history(history_path.read_text(encoding="utf-8")) if history_path.is_file() else []
    decisions = [entry for entry in entries if entry.kind == "decision"]
    if decisions:
        last_id = decisions[-1].ref_id
    elif entries:
        last_id = entries[-1].ref_id
    else:
        last_id = "없음"

    return (
        f"forge-ledger: Plane 프로젝트 {len(cfg.projects)}개(기본 {default_key}), "
        f"결정 {len(decisions)}건, 마지막 {last_id}"
    )


def hub_summary(repo_root: Path, env: Mapping[str, str], now: datetime) -> Optional[str]:
    """Return the hub summary of a synced repository, or None when the hub does not know it."""
    state_dir = default_state_dir(env)
    if not (state_dir / DB_NAME).is_file():
        return None
    store = HubStore(state_dir)
    repo = store.find_repo_by_path(str(repo_root.resolve()))
    if repo is None:
        return None
    return render_session(repo_summary(store, repo, now), now)


def _last_sync_start(state_dir: Path, path: str) -> Optional[datetime]:
    """Return the latest Gitea or Plane sync start of the repository at `path`, if any."""
    if not (state_dir / DB_NAME).is_file():
        return None
    store = HubStore(state_dir)
    repo = store.find_repo_by_path(path)
    if repo is None:
        return None
    runs = (store.last_run(repo.id, source) for source in ("gitea", "plane"))
    stamps = [run.started_at for run in runs if run is not None]
    if not stamps:
        return None
    return datetime.strptime(max(stamps), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def maybe_refresh(
    repo_root: Path,
    env: Mapping[str, str],
    now: datetime,
    spawn: Callable[[list[str], dict[str, str]], None],
) -> bool:
    """Start a background sync for a registered repository whose data is stale. Tokens go through env only."""
    child_env = dict(env)
    for src_key, dst_key in _OPTION_EXPORTS:
        if env.get(src_key, ""):
            child_env[dst_key] = env[src_key]
    if not any(child_env.get(key, "").strip() for key in _TOKEN_KEYS):
        return False
    state_dir = default_state_dir(child_env)
    path = str(repo_root.resolve())
    # 등록 목록은 DB 밖에 있어, DB를 다시 만든 뒤에도 대조가 다시 돈다.
    if path not in load_registry(state_dir):
        return False
    started = _last_sync_start(state_dir, path)
    if started is not None and now - started <= _REFRESH_AFTER:
        return False
    script = str(Path(_PLUGIN_ROOT) / "scripts" / "ledger.py")
    spawn([sys.executable, script, "sync", "--repo", path, "--quiet"], child_env)
    return True


def _spawn_detached(argv: list[str], env: dict[str, str]) -> None:
    subprocess.Popen(  # nosec B603 # 인자 목록 실행, shell 미사용
        argv,
        env=env,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _append_exports(env_file: Path, lines: list[str]) -> None:
    if not lines:
        return
    prefix = ""
    if env_file.is_file():
        existing = env_file.read_text(encoding="utf-8")
        if existing and not existing.endswith("\n"):
            prefix = "\n"
    with env_file.open("a", encoding="utf-8") as handle:
        handle.write(f"{prefix}" + "\n".join(lines) + "\n")


def main() -> int:
    """Run SessionStart hook; fail open on any error."""
    try:
        env = os.environ
        env_file = env.get("CLAUDE_ENV_FILE", "")
        if env_file:
            _append_exports(Path(env_file), build_exports(env))

        project_dir = env.get("CLAUDE_PROJECT_DIR", "")
        if project_dir:
            now = datetime.now(timezone.utc)
            repo_root = project_root(Path(project_dir))
            try:
                text = hub_summary(repo_root, env, now)
            except Exception:  # 허브를 읽지 못해도 기존 요약은 보인다
                text = None
            if text is None:
                text = summary(Path(project_dir))
            if text:
                print(text)
            maybe_refresh(repo_root, env, now, _spawn_detached)  # 이번 출력은 기존 값이다
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
