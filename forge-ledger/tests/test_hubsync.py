from __future__ import annotations

import io
import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ledger.adapters import CycleInfo, IssueInfo, ProjectInfo, PullInfo, RepoInfo, RunInfo
from ledger.config import LedgerConfig, PlaneProject, save_config
from ledger.gitea import GiteaAuthError, GiteaError, GiteaUnavailable
from ledger.hubstore import HubStore, save_registry
from ledger.hubsync import (
    RepoRef,
    RepoResolveError,
    SyncOutcome,
    json_logger,
    parse_remote,
    resolve_repo,
    sync_lock,
    sync_paths,
    sync_repo,
)
from ledger.hubview import health, render_session, repo_summary
from ledger.plane import PlaneAuthError, PlaneUnavailable

FORGE_URL = "http://forge.invalid"
PM_URL = "http://pm.invalid"
LOG_KEYS = {
    "ts",
    "level",
    "component",
    "source",
    "action",
    "result",
    "duration_ms",
    "correlation_id",
    "api_calls",
}


def _now() -> datetime:
    return datetime(2026, 11, 25, 1, 2, 3, tzinfo=timezone.utc)


class _Fake:
    """`errors`에 이름이 있는 메서드는 그 예외를 던진다. 호출마다 `calls`가 1 늘어난다."""

    def __init__(self) -> None:
        self.calls = 0
        self.errors: dict[str, Exception] = {}

    def _call(self, name: str) -> None:
        self.calls += 1
        if name in self.errors:
            raise self.errors[name]


class FakeForge(_Fake):
    def __init__(self) -> None:
        super().__init__()
        self.issues = [IssueInfo(n, f"이슈 {n}", ("bug",), "2026-11-20T00:00:00Z") for n in (1, 2)]
        self.pulls = [PullInfo(3, "PR 3", False, "2026-11-21T00:00:00Z")]
        self.runs = [
            RunInfo(n, n, "push", "completed", "success", "ci.yml", "refs/heads/main", "a" * 40, (), "")
            for n in (11, 12, 13)
        ]

    def whoami(self) -> str:
        return "tester"

    def get_repo(self, owner: str, name: str) -> RepoInfo:
        self._call("get_repo")
        return RepoInfo(f"{owner}/{name}", "main", f"{FORGE_URL}/{owner}/{name}")

    def list_open_issues(self, owner: str, name: str) -> list[IssueInfo]:
        self._call("list_open_issues")
        return list(self.issues)

    def list_open_pulls(self, owner: str, name: str) -> list[PullInfo]:
        self._call("list_open_pulls")
        return list(self.pulls)

    def list_runs(self, owner: str, name: str, limit: int = 50) -> list[RunInfo]:
        self._call("list_runs")
        return list(self.runs)


class FakePm(_Fake):
    """`HUB`에는 현재 cycle이 있고 `OPS`에는 없다."""

    def __init__(self) -> None:
        super().__init__()
        self.projects = [ProjectInfo("p1", "HUB", "허브"), ProjectInfo("p2", "OPS", "운영")]
        self.cycles = {"p1": CycleInfo("c1", "1주차", "2026-11-23T00:00:00Z", "2026-11-30T00:00:00Z", 5, 2)}

    def whoami(self) -> str:
        return "tester"

    def list_project_infos(self) -> list[ProjectInfo]:
        self._call("list_project_infos")
        return list(self.projects)

    def current_cycle(self, project_id: str) -> Optional[CycleInfo]:
        self._call("current_cycle")
        return self.cycles.get(project_id)


def _write_ledger(root: Path, keys: tuple[str, ...]) -> None:
    projects = [PlaneProject(key, f"{key} 제목", index == 0) for index, key in enumerate(keys)]
    save_config(LedgerConfig(method_version=1, workspace="ws", projects=projects), root / ".ledger" / "ledger.yml")


def _git_repo(root: Path, remote: str) -> Path:
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", remote], cwd=root, check=True, capture_output=True)
    return root


class _SyncCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.state_dir = self.tmp / "state"
        self.store = HubStore(self.state_dir)
        self.forge = FakeForge()
        self.pm = FakePm()
        self.events: list[dict[str, Any]] = []

    def _ref(self, keys: tuple[str, ...] = ("HUB", "OPS")) -> RepoRef:
        root = self.tmp / "work"
        if keys:
            _write_ledger(root, keys)
        return RepoRef(str(root), "o", "n", "ws" if keys else "", keys)

    def _sync(
        self,
        ref: RepoRef,
        forge_url: str = FORGE_URL,
        pm_url: str = PM_URL,
    ) -> dict[str, SyncOutcome]:
        outcomes = sync_repo(self.store, ref, self.forge, forge_url, self.pm, pm_url, _now, self.events.append)
        return {outcome.source: outcome for outcome in outcomes}

    def _repo_id(self) -> int:
        [repo] = self.store.repos()
        return repo.id

    def _keys(self, kind: str) -> list[str]:
        return [entity.ext_key for entity in self.store.entities(self._repo_id(), kind)]

    def _run_count(self) -> int:
        conn = sqlite3.connect(self.store.path)
        try:
            return int(conn.execute("SELECT COUNT(*) FROM sync_run").fetchone()[0])
        finally:
            conn.close()


class ParseRemoteTest(unittest.TestCase):
    def test_parse_remote_forms(self) -> None:
        for url in (
            "https://host/o/n",
            "https://host/o/n.git",
            "https://host/o/n.git/",
            "http://user:pw@host:3000/o/n.git",
            "ssh://git@host:2222/o/n.git",
            "git@host:o/n.git",
        ):
            self.assertEqual(parse_remote(url), ("o", "n"), url)
        with self.assertRaises(RepoResolveError):
            parse_remote("https://host/only")

    def test_rejects_other_forms_without_echoing_them(self) -> None:
        secret = "s3" + "cret"
        for url in (
            "",
            "/srv/git/o/n.git",
            "file:///srv/n.git",
            "https://host/g/o/n.git",
            f"http://user:{secret}@host/only",
            f"http://user:{secret}/x@host/o/n.git",
            f"user:{secret}@host:o/n.git",
            f"http://user:{secret}@[host/o/n.git",
        ):
            with self.assertRaises(RepoResolveError, msg=url) as caught:
                parse_remote(url)
            self.assertNotIn(secret, str(caught.exception))
            self.assertIsNone(caught.exception.__cause__)


class ResolveRepoTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_without_ledger_yml(self) -> None:
        ref = resolve_repo(self.root, lambda _: "git@host:o/n.git")
        self.assertEqual(ref, RepoRef(str(self.root), "o", "n", "", ()))

    def test_reads_workspace_and_keys(self) -> None:
        _write_ledger(self.root, ("HUB", "OPS"))
        ref = resolve_repo(self.root, lambda _: "git@host:o/n.git")
        self.assertEqual((ref.plane_workspace, ref.plane_keys), ("ws", ("HUB", "OPS")))

    def test_broken_ledger_yml(self) -> None:
        (self.root / ".ledger").mkdir()
        (self.root / ".ledger" / "ledger.yml").write_text("method_version: x\n", encoding="utf-8")
        with self.assertRaises(RepoResolveError):
            resolve_repo(self.root, lambda _: "git@host:o/n.git")

    def test_default_git_remote(self) -> None:
        repo = _git_repo(self.root / "clone", "ssh://git@host:2222/o/n.git")
        ref = resolve_repo(repo)
        self.assertEqual((ref.owner, ref.name), ("o", "n"))
        subprocess.run(["git", "remote", "remove", "origin"], cwd=repo, check=True, capture_output=True)
        with self.assertRaises(RepoResolveError):
            resolve_repo(repo)

    def test_resolve_repo_uses_configured_remote(self) -> None:
        repo = _git_repo(self.root / "clone", "git@host:mirror/other.git")

        def git(*args: str) -> None:
            subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

        git("remote", "add", "gitea", "ssh://git@host:2222/o/n.git")
        git("config", "forge-ledger.remote", "gitea")
        ref = resolve_repo(repo)
        self.assertEqual((ref.owner, ref.name), ("o", "n"))
        # 설정한 원격이 없으면 origin으로 넘어가지 않는다(다른 서버의 저장소를 대조하지 않게).
        git("config", "forge-ledger.remote", "nowhere")
        with self.assertRaises(RepoResolveError):
            resolve_repo(repo)
        git("config", "--unset", "forge-ledger.remote")
        ref = resolve_repo(repo)
        self.assertEqual((ref.owner, ref.name), ("mirror", "other"))


class SyncRepoTest(_SyncCase):
    def test_sync_writes_entities_and_runs(self) -> None:
        outcomes = self._sync(self._ref())
        self.assertEqual(
            outcomes,
            {
                "gitea": SyncOutcome("o/n", "gitea", "ok", 4, ""),
                "plane": SyncOutcome("o/n", "plane", "ok", 3, ""),
            },
        )
        self.assertEqual(self._keys("repo"), ["o/n"])
        self.assertEqual(self._keys("issue"), ["1", "2"])
        self.assertEqual(self._keys("pull"), ["3"])
        self.assertEqual(self._keys("ci_run"), ["11", "12", "13"])
        self.assertEqual(self._keys("plane_project"), ["HUB", "OPS"])
        self.assertEqual(self._run_count(), 2)
        repo_id = self._repo_id()
        [repo] = self.store.repos()
        self.assertEqual((repo.default_branch, repo.html_url), ("main", f"{FORGE_URL}/o/n"))
        self.assertEqual(repo.local_paths, (str(self.tmp / "work"),))
        issue = self.store.entities(repo_id, "issue")[0]
        self.assertEqual(
            issue.attrs, {"number": 1, "title": "이슈 1", "labels": ["bug"], "created_at": "2026-11-20T00:00:00Z"}
        )
        self.assertEqual(
            [e.attrs for e in self.store.entities(repo_id, "plane_project")],
            [
                {"key": "HUB", "title": "HUB 제목", "default": True, "id": "p1", "found": True},
                {"key": "OPS", "title": "OPS 제목", "default": False, "id": "p2", "found": True},
            ],
        )
        [cycle] = self.store.entities(repo_id, "cycle")
        self.assertEqual((cycle.ext_key, cycle.parent_ext_key, cycle.attrs["completed_issues"]), ("c1", "HUB", 2))
        run = self.store.last_run(repo_id, "gitea")
        assert run is not None
        self.assertEqual((run.started_at, run.finished_at), ("2026-11-25T01:02:03Z", "2026-11-25T01:02:03Z"))

    def test_second_sync_closes_gone_rows_but_keeps_old_ci_runs(self) -> None:
        ref = self._ref()
        self._sync(ref)
        self.forge.issues = self.forge.issues[:1]
        self.forge.pulls = []
        self.forge.runs = self.forge.runs[2:]
        self.pm.cycles = {}
        outcomes = self._sync(ref)
        # 어댑터를 다시 써도 호출 수는 이번 실행분만 센다.
        self.assertEqual((outcomes["gitea"].api_calls, outcomes["plane"].api_calls), (4, 3))
        self.assertEqual(self._keys("issue"), ["1"])
        self.assertEqual(self._keys("pull"), [])
        self.assertEqual(self._keys("ci_run"), ["11", "12", "13"])
        self.assertEqual(self._keys("cycle"), [])

    def test_partial_when_runs_fail(self) -> None:
        ref = self._ref()
        self._sync(ref)
        self.forge.issues = self.forge.issues[:1]
        self.forge.runs = []
        self.forge.errors["list_runs"] = GiteaUnavailable("연결 실패")
        outcome = self._sync(ref)["gitea"]
        self.assertEqual((outcome.result, outcome.error_kind), ("partial", "unavailable"))
        self.assertEqual(self._keys("issue"), ["1"])
        self.assertEqual(self._keys("pull"), ["3"])
        self.assertEqual(self._keys("ci_run"), ["11", "12", "13"])

    def test_early_failure_does_not_block_later_kinds(self) -> None:
        self.forge.errors["get_repo"] = GiteaError("형식 오류")
        outcome = self._sync(self._ref())["gitea"]
        self.assertEqual((outcome.result, outcome.error_kind, outcome.api_calls), ("partial", "error", 4))
        self.assertEqual(self._keys("repo"), [])
        self.assertEqual(self._keys("issue"), ["1", "2"])
        self.assertEqual(self._keys("ci_run"), ["11", "12", "13"])

    def test_all_gitea_kinds_failing_is_failed(self) -> None:
        for name in ("get_repo", "list_open_issues", "list_open_pulls", "list_runs"):
            self.forge.errors[name] = GiteaAuthError("토큰 확인")
        outcomes = self._sync(self._ref())
        self.assertEqual((outcomes["gitea"].result, outcomes["gitea"].error_kind), ("failed", "auth"))
        self.assertEqual(outcomes["plane"].result, "ok")
        self.assertIsNone(self.store.last_good_run(self._repo_id(), "gitea"))

    def test_plane_project_list_failure_keeps_previous_rows(self) -> None:
        ref = self._ref()
        self._sync(ref)
        for error, error_kind in (
            (PlaneAuthError("토큰 확인"), "auth"),
            (PlaneUnavailable("연결 실패"), "unavailable"),
            (RuntimeError("뜻밖의 오류"), "error"),
        ):
            self.pm.errors["list_project_infos"] = error
            outcome = self._sync(ref)["plane"]
            self.assertEqual((outcome.result, outcome.error_kind), ("failed", error_kind))
            self.assertEqual(self._keys("plane_project"), ["HUB", "OPS"])
            self.assertEqual(self._keys("cycle"), ["c1"])

    def test_cycle_failure_is_partial_and_keeps_previous_cycle(self) -> None:
        ref = self._ref()
        self._sync(ref)
        self.pm.errors["current_cycle"] = PlaneUnavailable("연결 실패")
        outcome = self._sync(ref)["plane"]
        self.assertEqual((outcome.result, outcome.error_kind), ("partial", "unavailable"))
        self.assertEqual(self._keys("cycle"), ["c1"])

    def test_missing_plane_project_is_partial(self) -> None:
        outcome = self._sync(self._ref(("GONE", "HUB")))["plane"]
        self.assertEqual((outcome.result, outcome.error_kind), ("partial", "not_found"))
        projects = {e.ext_key: e.attrs for e in self.store.entities(self._repo_id(), "plane_project")}
        self.assertEqual(
            projects["GONE"], {"key": "GONE", "title": "GONE 제목", "default": True, "id": "", "found": False}
        )
        self.assertEqual((projects["HUB"]["id"], projects["HUB"]["found"]), ("p1", True))
        self.assertEqual([e.parent_ext_key for e in self.store.entities(self._repo_id(), "cycle")], ["HUB"])

    def test_removed_ledger_closes_plane_rows_without_calls(self) -> None:
        self._sync(self._ref())
        runs_before, calls_before = self._run_count(), self.pm.calls
        outcomes = self._sync(self._ref(()), pm_url="")
        self.assertEqual(list(outcomes), ["gitea"])
        self.assertEqual((self._run_count() - runs_before, self.pm.calls - calls_before), (1, 0))
        self.assertEqual(self._keys("plane_project"), [])
        self.assertEqual(self._keys("cycle"), [])

    def test_repo_without_ledger_hides_plane(self) -> None:
        outcomes = self._sync(self._ref(()))
        self.assertEqual(list(outcomes), ["gitea"])
        self.assertEqual((self._run_count(), self.pm.calls, len(self.events)), (1, 0, 1))
        [repo] = self.store.repos()
        self.assertIsNone(self.store.last_run(repo.id, "plane"))
        repo_sum = repo_summary(self.store, repo, _now())
        self.assertEqual(list(repo_sum["sources"]), ["gitea"])
        self.assertNotIn("Plane", render_session(repo_sum, _now()))
        self.assertEqual([item["source"] for item in health(self.store, _now())["sources"]], ["gitea"])

    def test_removed_ledger_hides_earlier_plane_runs(self) -> None:
        self._sync(self._ref())
        shutil.rmtree(self.tmp / "work" / ".ledger")
        self._sync(self._ref(()))
        [repo] = self.store.repos()
        self.assertNotIn("Plane", render_session(repo_summary(self.store, repo, _now()), _now()))

    def test_no_token_is_skipped(self) -> None:
        outcomes = sync_repo(self.store, self._ref(), None, "", self.pm, PM_URL, _now, self.events.append)
        self.assertEqual(
            outcomes,
            [SyncOutcome("o/n", "gitea", "skipped", 0, "no_token"), SyncOutcome("o/n", "plane", "ok", 3, "")],
        )
        self.assertEqual(self._keys("issue"), [])
        self.assertEqual(self._keys("plane_project"), ["HUB", "OPS"])
        self.assertEqual(self._run_count(), 2)

    def test_no_pm_is_skipped(self) -> None:
        outcomes = sync_repo(self.store, self._ref(), self.forge, FORGE_URL, None, "", _now, self.events.append)
        self.assertEqual(outcomes[1], SyncOutcome("o/n", "plane", "skipped", 0, "no_token"))
        self.assertEqual(self._keys("issue"), ["1", "2"])

    def test_source_url_change_closes_old_rows(self) -> None:
        ref = self._ref()
        self._sync(ref)
        self.forge.errors["list_runs"] = GiteaUnavailable("연결 실패")
        self.forge.issues = self.forge.issues[:1]
        self._sync(ref, forge_url="http://moved.invalid", pm_url="http://moved-pm.invalid")
        # 새 주소에서 반영에 성공한 종류만 옛 주소의 열린 행을 닫는다.
        for kind, expected in (
            ("repo", ["o/n"]),
            ("issue", ["1"]),
            ("pull", ["3"]),
            ("ci_run", ["11", "12", "13"]),
            ("plane_project", ["HUB", "OPS"]),
            ("cycle", ["c1"]),
        ):
            self.assertEqual(self._keys(kind), expected, kind)
        closed = self.store.entities(self._repo_id(), "ci_run", states=("closed",))
        self.assertEqual(closed, [])
        del self.forge.errors["list_runs"]
        self._sync(ref, forge_url="http://moved.invalid", pm_url="http://moved-pm.invalid")
        self.assertEqual(self._keys("ci_run"), ["11", "12", "13"])
        self.assertEqual(len(self.store.entities(self._repo_id(), "ci_run", states=("closed",))), 3)

    def test_source_url_change_leaves_other_repo_and_skipped_source(self) -> None:
        ref = self._ref()
        self._sync(ref)
        other = RepoRef(str(self.tmp / "other"), "o", "other", "", ())
        sync_repo(self.store, other, self.forge, FORGE_URL, self.pm, PM_URL, _now, self.events.append)
        sync_repo(self.store, ref, None, "", None, "", _now, self.events.append)
        ids = {repo.name: repo.id for repo in self.store.repos()}
        self.assertEqual(len(self.store.entities(ids["n"], "issue")), 2)
        self._sync(ref, forge_url="http://moved.invalid")
        self.assertEqual(len(self.store.entities(ids["other"], "issue")), 2)
        self.assertEqual(len(self.store.entities(ids["n"], "issue")), 2)


class SyncPathsTest(_SyncCase):
    def _sync_paths(self, paths: list[str]) -> list[SyncOutcome]:
        self.forge_made = 0
        self.workspaces: list[str] = []

        def make_forge() -> tuple[FakeForge, str]:
            self.forge_made += 1
            return self.forge, FORGE_URL

        def make_pm(workspace: str) -> tuple[FakePm, str]:
            self.workspaces.append(workspace)
            return self.pm, PM_URL

        return sync_paths(self.store, paths, make_forge, make_pm, _now, self.events.append)

    def _synced_then_removed(self, name: str, keys: tuple[str, ...]) -> str:
        """한 번 대조한 저장소의 폴더를 지우고 그 경로를 돌려준다."""
        root = _git_repo(self.tmp / name, f"git@host:o/{name}.git")
        if keys:
            _write_ledger(root, keys)
        self._sync_paths([str(root)])
        shutil.rmtree(root)
        self.events.clear()
        return str(root)

    def _ids(self) -> dict[str, int]:
        return {repo.name: repo.id for repo in self.store.repos()}

    def test_path_missing_skipped(self) -> None:
        gone = self._synced_then_removed("gone", ("HUB",))
        good = _git_repo(self.tmp / "good", "git@host:o/n.git")
        outcomes = self._sync_paths([gone, str(good)])
        self.assertEqual(
            outcomes[:2], [SyncOutcome("o/gone", s, "skipped", 0, "path_missing") for s in ("gitea", "plane")]
        )
        self.assertEqual([(o.repo, o.source, o.result) for o in outcomes[2:]], [("o/n", "gitea", "ok")])
        repo_id = self._ids()["gone"]
        for source, line in zip(("gitea", "plane"), self.events):
            run = self.store.last_run(repo_id, source)
            assert run is not None
            self.assertEqual(
                (run.result, run.error_kind, run.api_calls, run.finished_at),
                ("skipped", "path_missing", 0, "2026-11-25T01:02:03Z"),
            )
            good_run = self.store.last_good_run(repo_id, source)
            assert good_run is not None
            self.assertEqual(good_run.result, "ok")
            self.assertEqual(set(line), LOG_KEYS | {"repo", "error_kind"})
            self.assertEqual(
                (line["source"], line["repo"], line["result"], line["error_kind"], line["level"]),
                (source, "o/gone", "skipped", "path_missing", "warning"),
            )
        # 첫 대조가 run-1·2를 썼으므로 건너뛴 실행은 run-3·4다.
        self.assertEqual([line["correlation_id"] for line in self.events[:2]], ["run-3", "run-4"])
        self.assertNotIn(gone, json.dumps(self.events))
        # 건너뛴 저장소의 이전 행은 닫지 않는다.
        self.assertEqual(len(self.store.entities(repo_id, "issue")), 2)
        self.assertEqual([e.ext_key for e in self.store.entities(repo_id, "plane_project")], ["HUB"])

    def test_path_missing_uses_current_source_and_stored_form_of_path(self) -> None:
        gone = self._synced_then_removed("gone", ("HUB",))
        outcomes = self._sync_paths([gone + "/"])
        self.assertEqual([o.error_kind for o in outcomes], ["path_missing", "path_missing"])
        self.assertEqual((self.forge_made, self.workspaces), (1, ["ws"]))
        conn = sqlite3.connect(self.store.path)
        try:
            [(sources,)] = conn.execute("SELECT COUNT(*) FROM source").fetchall()
        finally:
            conn.close()
        self.assertEqual(sources, 2)  # 첫 대조와 같은 주소의 source를 쓴다

    def test_path_missing_without_plane_workspace_records_gitea_only(self) -> None:
        gone = self._synced_then_removed("gone", ())
        outcomes = self._sync_paths([gone])
        self.assertEqual(outcomes, [SyncOutcome("o/gone", "gitea", "skipped", 0, "path_missing")])
        self.assertEqual(self.workspaces, [])
        self.assertIsNone(self.store.last_run(self._ids()["gone"], "plane"))

    def test_path_missing_without_any_source_still_names_repo_in_log(self) -> None:
        gone = self._synced_then_removed("gone", ())
        runs_before = self._run_count()
        outcomes = sync_paths(self.store, [gone], lambda: (None, ""), lambda _: (None, ""), _now, self.events.append)
        self.assertEqual(outcomes, [SyncOutcome("o/gone", "", "skipped", 0, "path_missing")])
        self.assertEqual(self._run_count(), runs_before)
        [line] = self.events
        self.assertEqual(
            (line["repo"], line["source"], line["correlation_id"], line["error_kind"]),
            ("o/gone", "", "", "path_missing"),
        )

    def test_gone_clone_does_not_hide_live_clone_result(self) -> None:
        clones = [_git_repo(self.tmp / name, "git@host:o/n.git") for name in ("a", "b")]
        for clone in clones:
            _write_ledger(clone, ("HUB",))
        live, gone = (str(clone) for clone in clones)
        self._sync_paths([live, gone])
        shutil.rmtree(gone)
        for name in ("get_repo", "list_open_issues", "list_open_pulls", "list_runs"):
            self.forge.errors[name] = GiteaError("형식 오류")
        # 살아 있는 경로를 함께 넘기지 않아도(일부 경로만 대조) 저장소의 다른 경로를 확인한다.
        for paths in ([live, gone], [gone]):
            self._sync_paths([live])
            runs_before = self._run_count()
            self.events.clear()
            outcomes = self._sync_paths(paths)
            self.assertEqual(outcomes[-1], SyncOutcome("o/n", "", "skipped", 0, "path_missing"), paths)
            self.assertEqual(self._run_count() - runs_before, 2 * (len(paths) - 1), paths)
            gitea = self.store.last_run(self._repo_id(), "gitea")
            plane = self.store.last_run(self._repo_id(), "plane")
            assert gitea is not None and plane is not None
            self.assertEqual((gitea.result, gitea.error_kind, plane.result), ("failed", "error", "ok"), paths)
            line = self.events[-1]
            self.assertEqual(
                (line["repo"], line["source"], line["correlation_id"], line["error_kind"]),
                ("o/n", "", "", "path_missing"),
            )
        shutil.rmtree(live)
        outcomes = self._sync_paths([gone])
        self.assertEqual(outcomes, [SyncOutcome("o/n", s, "skipped", 0, "path_missing") for s in ("gitea", "plane")])
        for source in ("gitea", "plane"):
            run = self.store.last_run(self._repo_id(), source)
            assert run is not None
            self.assertEqual((run.result, run.error_kind), ("skipped", "path_missing"), source)

    def test_never_synced_missing_path_has_no_run(self) -> None:
        gone = str(self.tmp / "never")
        outcomes = self._sync_paths([gone])
        self.assertEqual(outcomes, [SyncOutcome(gone, "", "skipped", 0, "path_missing")])
        self.assertEqual(self._run_count(), 0)
        [line] = self.events
        self.assertEqual(set(line), LOG_KEYS | {"error_kind"})
        self.assertNotIn(gone, json.dumps(line))

    def test_unresolvable_path_does_not_stop_others(self) -> None:
        plain = self.tmp / "plain"
        _git_repo(plain, "https://host/only")
        good = _git_repo(self.tmp / "good", "git@host:o/n.git")
        outcomes = self._sync_paths([str(plain), str(good)])
        self.assertEqual(outcomes[0], SyncOutcome(str(plain), "", "failed", 0, "error"))
        self.assertEqual([o.result for o in outcomes[1:]], ["ok"])

    def test_adapters_are_made_once_per_workspace(self) -> None:
        paths = [str(_git_repo(self.tmp / name, f"git@host:o/{name}.git")) for name in ("a", "b", "c")]
        for path in paths[:2]:
            _write_ledger(Path(path), ("HUB",))
        self._sync_paths(paths)
        self.assertEqual(self.forge_made, 1)
        self.assertEqual(self.workspaces, ["ws", ""])

    def test_remote_credentials_never_stored(self) -> None:
        secret = "s3" + "cret"
        repo = _git_repo(self.tmp / "clone", f"http://user:{secret}@host/o/n.git")
        broken = _git_repo(self.tmp / "broken", f"http://user:{secret}@host/only")
        paths = [str(repo), str(broken)]
        save_registry(self.state_dir, paths)
        self.forge.errors["list_runs"] = GiteaError(f"http://user:{secret}@host 실패")
        stream = io.StringIO()
        outcomes = sync_paths(
            self.store,
            paths,
            lambda: (self.forge, FORGE_URL),
            lambda _: (self.pm, PM_URL),
            _now,
            json_logger(stream),
        )
        self.assertEqual(len(outcomes), 2)
        self.assertTrue(stream.getvalue())
        stored = {path.name: path.read_bytes() for path in self.state_dir.iterdir()}
        stored["log"] = stream.getvalue().encode("utf-8")
        stored["outcomes"] = repr(outcomes).encode("utf-8")
        for name, content in stored.items():
            self.assertNotIn(secret.encode(), content, name)
            self.assertNotIn(b"user:", content, name)


class MonorepoPathsTest(_SyncCase):
    """같은 `owner/name`의 두 경로. `.ledger`는 하위 폴더에만 있다."""

    def setUp(self) -> None:
        super().setUp()
        self.upper = _git_repo(self.tmp / "mono", "git@host:o/n.git")
        self.lower = self.upper / "tool"
        _write_ledger(self.lower, ("HUB",))

    def _sync_paths(self, *roots: Path) -> list[SyncOutcome]:
        self.events.clear()
        return sync_paths(
            self.store,
            [str(root) for root in roots],
            lambda: (self.forge, FORGE_URL),
            lambda _: (self.pm, PM_URL),
            _now,
            self.events.append,
        )

    def _assert_plane_kept(self, label: object, result: str = "ok") -> None:
        [repo] = self.store.repos()
        self.assertEqual(repo.plane_workspace, "ws", label)
        self.assertEqual((self._keys("plane_project"), self._keys("cycle")), (["HUB"], ["c1"]), label)
        run = self.store.last_run(repo.id, "plane")
        assert run is not None
        self.assertEqual(run.result, result, label)
        self.assertNotIn("no_ledger", [line.get("error_kind") for line in self.events], label)

    def test_path_without_ledger_keeps_plane_rows_in_any_order(self) -> None:
        self._sync_paths(self.lower)
        for roots in ((self.lower, self.upper), (self.upper, self.lower), (self.upper,)):
            outcomes = self._sync_paths(*roots)
            self.assertEqual([o.source for o in outcomes].count("plane"), len(roots) - 1, roots)
            self._assert_plane_kept(roots)

    def test_plane_outage_keeps_previous_rows(self) -> None:
        self._sync_paths(self.upper, self.lower)
        self.pm.errors["list_project_infos"] = PlaneUnavailable("연결 실패")
        for roots in ((self.lower, self.upper), (self.upper, self.lower)):
            self._sync_paths(*roots)
            self._assert_plane_kept(roots, "failed")

    def test_unreadable_ledger_in_other_path_keeps_plane_rows(self) -> None:
        self._sync_paths(self.upper, self.lower)
        (self.lower / ".ledger" / "ledger.yml").write_text("method_version: x\n", encoding="utf-8")
        self._sync_paths(self.upper)
        self._assert_plane_kept("broken")

    def test_plane_rows_close_with_log_when_no_path_has_ledger(self) -> None:
        for name, remove in (("ledger", self.lower / ".ledger"), ("folder", self.lower)):
            _write_ledger(self.lower, ("HUB",))
            self._sync_paths(self.upper, self.lower)
            shutil.rmtree(remove)
            runs_before, calls_before = self._run_count(), self.pm.calls
            self._sync_paths(self.upper)
            [repo] = self.store.repos()
            self.assertEqual(repo.plane_workspace, "", name)
            self.assertEqual((self._keys("plane_project"), self._keys("cycle")), ([], []), name)
            # gitea 실행 하나만 늘고 Plane은 부르지 않는다.
            self.assertEqual((self._run_count() - runs_before, self.pm.calls - calls_before), (1, 0), name)
            [line] = [event for event in self.events if event["source"] == "plane"]
            self.assertEqual(set(line), LOG_KEYS | {"repo", "error_kind"}, name)
            self.assertEqual(
                (line["component"], line["result"], line["error_kind"], line["level"], line["repo"]),
                ("sync", "skipped", "no_ledger", "warning", "o/n"),
                name,
            )
            self.assertEqual((line["correlation_id"], line["api_calls"]), ("", 0), name)
            # 닫을 행이 없으면 다시 기록하지 않는다.
            self._sync_paths(self.upper)
            self.assertEqual([event["source"] for event in self.events], ["gitea"], name)


class LockTest(unittest.TestCase):
    def test_lock_busy_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            with sync_lock(state_dir) as first:
                with sync_lock(state_dir) as second:
                    self.assertEqual((first, second), (True, False))
                # 얻지 못한 쪽이 빠져나가도 먼저 잡은 잠금은 남는다.
                with sync_lock(state_dir) as third:
                    self.assertFalse(third)
            with sync_lock(state_dir) as again:
                self.assertTrue(again)
            self.assertEqual(state_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual((state_dir / "hub-sync.lock").stat().st_mode & 0o777, 0o600)

    def test_error_in_body_releases_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(BlockingIOError):
                with sync_lock(Path(tmp)):
                    raise BlockingIOError
            with sync_lock(Path(tmp)) as again:
                self.assertTrue(again)


class LogTest(_SyncCase):
    def test_log_lines_are_json_without_secrets(self) -> None:
        token = "tok" + "e" * 30
        forge_url = f"http://user:{token}@forge.invalid"
        self.forge.errors["list_runs"] = GiteaUnavailable(f"{token} {forge_url}")
        self.pm.errors["list_project_infos"] = PlaneAuthError(token)
        stream = io.StringIO()
        sync_repo(self.store, self._ref(), self.forge, forge_url, self.pm, PM_URL, _now, json_logger(stream))
        text = stream.getvalue()
        self.assertTrue(text.endswith("\n"))
        lines = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertEqual(set(line), LOG_KEYS)
            self.assertIsInstance(line["duration_ms"], int)
            self.assertEqual((line["ts"], line["component"], line["action"]), ("2026-11-25T01:02:03Z", "sync", "sync"))
        self.assertEqual(
            [(ln["source"], ln["level"], ln["result"], ln["correlation_id"], ln["api_calls"]) for ln in lines],
            [("gitea", "warning", "partial", "run-1", 4), ("plane", "error", "failed", "run-2", 1)],
        )
        for forbidden in (token, "forge.invalid", "pm.invalid"):
            self.assertNotIn(forbidden, text)
        for path in self.state_dir.iterdir():
            self.assertNotIn(token.encode(), path.read_bytes(), path.name)


if __name__ == "__main__":
    unittest.main()


class WorklogSyncTest(_SyncCase):
    def test_worklog_recent_and_deleted_and_missing_project(self) -> None:
        from ledger.hubsync import sync_worklog
        from ledger.hubstore import WORKSPACE_REPO_ID
        from ledger.plane import PlaneError
        from ledger.worklog import Entry, RemoteEntry

        class Logs:
            def __init__(self) -> None:
                self.items = [
                    RemoteEntry(
                        Entry(str(n), f"2026-{month}-01T12:00:00+00:00", "host", "manual", "본문", "ws"),
                        "작성자",
                        str(n),
                    )
                    for n, month in enumerate(("09", "10", "11"))
                ]
                self.error = False

            def list_entries(self, from_, to, user=None):
                if self.error:
                    raise PlaneError("작업 일지 프로젝트 없음: WLOG")
                return [item for item in self.items if from_ <= item.entry.local_date() <= to]

        pm = Logs()
        self.assertEqual(sync_worklog(self.store, pm, PM_URL, _now, self.events.append, True).result, "ok")
        pm.items = pm.items[-1:]
        self.assertEqual(sync_worklog(self.store, pm, PM_URL, _now, self.events.append, False).result, "ok")
        states = {e.ext_key: e.state for e in self.store.entities(WORKSPACE_REPO_ID, "worklog", states=None)}
        self.assertEqual(states, {"0": "open", "1": "deleted", "2": "open"})
        pm.error = True
        self.assertEqual(sync_worklog(self.store, pm, PM_URL, _now, self.events.append, True).result, "failed")
        self.assertEqual(
            states, {e.ext_key: e.state for e in self.store.entities(WORKSPACE_REPO_ID, "worklog", states=None)}
        )
        self.assertNotIn("본문", str(self.events))

    def test_worklog_full_due(self) -> None:
        from datetime import timedelta
        from ledger.hubsync import mark_worklog_full, worklog_full_due

        self.assertTrue(worklog_full_due(self.state_dir, _now()))
        mark_worklog_full(self.state_dir, _now())
        self.assertFalse(worklog_full_due(self.state_dir, _now()))
        self.assertTrue(worklog_full_due(self.state_dir, _now() + timedelta(hours=25)))

    def test_worklog_skipped_without_token(self) -> None:
        from ledger.hubsync import sync_worklog
        from ledger.hubstore import WORKSPACE_REPO_ID

        outcomes = self._sync(self._ref())
        self.assertEqual(sync_worklog(self.store, None, "", _now, self.events.append, True).result, "skipped")
        self.assertEqual(outcomes["gitea"].result, "ok")
        self.assertEqual(self.store.entities(WORKSPACE_REPO_ID, "worklog"), [])
