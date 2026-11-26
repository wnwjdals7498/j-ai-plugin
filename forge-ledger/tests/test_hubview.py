from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from ledger.adapters import CycleInfo, IssueInfo, PullInfo, RunInfo
from ledger.hubstore import WORKSPACE_REPO_ID, EntityRow, HubStore, RunRow
from ledger.hubview import (
    CI_WINDOW,
    PENDING_1ST,
    STATE_LABELS,
    count_ci_failures,
    health,
    render_session,
    render_text,
    repo_summary,
    source_state,
    summary,
    worklog_view,
)

NOW = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ago(**delta: float) -> str:
    return _iso(NOW - timedelta(**delta))


def _run(
    number: int,
    conclusion: str,
    workflow: str = "pr-check.yml",
    pulls: tuple[int, ...] = (),
    status: str = "completed",
    event: str = "pull_request",
    ref: str = "",
) -> dict[str, Any]:
    info = RunInfo(1000 + number, number, event, status, conclusion, workflow, ref, "abc", pulls, _ago(hours=1))
    return asdict(info)


def _push(number: int, conclusion: str, branch: str = "main") -> dict[str, Any]:
    return _run(number, conclusion, event="push", ref=f"refs/heads/{branch}")


class _ViewCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = HubStore(Path(tmp.name) / "state")
        self.gitea = self.store.source_id("gitea", "http://forge.invalid")
        self.plane = self.store.source_id("plane", "http://pm.invalid")

    def _repo(self, name: str = "n") -> int:
        repo_id = self.store.upsert_repo("o", name, f"/work/{name}", "ws")
        self.store.set_repo_meta(repo_id, "main", f"http://forge.invalid/o/{name}")
        return repo_id

    def _put(self, source_id: int, repo_id: int, kind: str, rows: list[EntityRow]) -> None:
        self.store.apply_kind(source_id, repo_id, kind, rows, _iso(NOW))

    def _record(self, source_id: int, repo_id: int, result: str, finished: str, error_kind: str = "") -> None:
        run_id = self.store.start_run(source_id, repo_id, finished)
        self.store.finish_run(run_id, result, 4, error_kind, finished)

    def _project(self, repo_id: int, key: str, found: bool = True, default: bool = False, title: str = "") -> None:
        attrs = {"key": key, "title": title or f"{key} 제목", "default": default, "id": "", "found": found}
        self.store.apply_kind(
            self.plane, repo_id, "plane_project", [EntityRow("plane_project", key, "open", attrs)], _iso(NOW), False
        )

    def _cycle(self, repo_id: int, key: str, end_date: str = "2026-11-29T00:00:00Z", name: str = "Cycle 1") -> None:
        info = CycleInfo(f"cycle-{key}", name, "2026-11-15T00:00:00Z", end_date, 8, 3)
        row = EntityRow("cycle", info.id, "open", asdict(info), parent=("plane_project", key))
        self.store.apply_kind(self.plane, repo_id, "cycle", [row], _iso(NOW), False)

    def _summary_of(self, name: str = "n") -> dict[str, Any]:
        repo = next(repo for repo in self.store.repos() if repo.name == name)
        return repo_summary(self.store, repo, NOW)


class CiFailureTest(_ViewCase):
    def test_ci_failures_per_target_and_workflow(self) -> None:
        runs = [
            _run(1, "failure", pulls=(5,)),
            _run(2, "success", pulls=(5,)),
        ]
        self.assertEqual(count_ci_failures(runs, {5}, "main"), 0)
        runs.append(_run(4, "failure", workflow="lint.yml", pulls=(5,)))
        self.assertEqual(count_ci_failures(runs, {5}, "main"), 1)
        # 진행 중인 실행은 무시하고 직전 완료 결과를 쓴다.
        runs.append(_run(3, "", pulls=(5,), status="running"))
        runs.append(_run(5, "", workflow="lint.yml", pulls=(5,), status="running"))
        self.assertEqual(count_ci_failures(runs, {5}, "main"), 1)
        runs.append(_run(6, "failure", pulls=(4,)))  # 닫힌 PR
        self.assertEqual(count_ci_failures(runs, {5}, "main"), 1)
        runs.extend([_push(7, "success"), _push(8, "failure")])
        self.assertEqual(count_ci_failures(runs, {5}, "main"), 2)

    def test_ci_failures_ignores_other_branch_and_event(self) -> None:
        runs = [
            _push(1, "failure", branch="dev"),
            _run(2, "failure", event="schedule", ref="refs/heads/main"),
        ]
        self.assertEqual(count_ci_failures(runs, set(), "main"), 0)

    def test_ci_failures_only_latest_50(self) -> None:
        old_failure = _run(1, "failure", workflow="lint.yml", pulls=(5,))
        newer = [_run(number, "success", pulls=(5,)) for number in range(2, 2 + CI_WINDOW - 1)]
        self.assertEqual(count_ci_failures([old_failure, *newer], {5}, "main"), 1)  # 50번째까지는 센다
        newer.append(_run(2 + CI_WINDOW, "success", pulls=(5,)))
        self.assertEqual(count_ci_failures([old_failure, *newer], {5}, "main"), 0)


class RepoSummaryTest(_ViewCase):
    def test_review_prs_excludes_draft(self) -> None:
        repo_id = self._repo()
        pulls = [
            PullInfo(1, "draft", True, _ago(days=30)),
            PullInfo(2, "오래됨", False, _ago(days=5, hours=23)),
            PullInfo(3, "최근", False, _ago(days=2)),
        ]
        self._put(self.gitea, repo_id, "pull", [EntityRow("pull", str(p.number), "open", asdict(p)) for p in pulls])
        self.assertEqual(self._summary_of()["review_prs"], {"count": 2, "oldest_days": 5})

    def test_cycle_progress_and_gate_days(self) -> None:
        repo_id = self._repo()
        self._project(repo_id, "ABC", default=True, title="제품")
        self._cycle(repo_id, "ABC")
        expected = {
            "key": "ABC",
            "title": "제품",
            "default": True,
            "found": True,
            "cycle": {
                "name": "Cycle 1",
                "start_date": "2026-11-15T00:00:00Z",
                "end_date": "2026-11-29T00:00:00Z",
                "completed": 3,
                "total": 8,
                "gate_days": 3,
            },
        }
        self.assertEqual(self._summary_of()["projects"], [expected])

    def test_cycle_without_end_date_has_no_gate_days(self) -> None:
        repo_id = self._repo()
        self._project(repo_id, "ABC")
        self._cycle(repo_id, "ABC", end_date="")
        self.assertIsNone(self._summary_of()["projects"][0]["cycle"]["gate_days"])
        self.assertIn("게이트 1 예정: -", render_text(summary(self.store, NOW)))

    def test_shape_counts_and_open_rows_only(self) -> None:
        repo_id = self._repo()
        issues = [IssueInfo(n, "이슈", (), _ago(days=1)) for n in (1, 2)]
        self._put(self.gitea, repo_id, "issue", [EntityRow("issue", str(i.number), "open", asdict(i)) for i in issues])
        self._put(self.gitea, repo_id, "issue", [EntityRow("issue", "2", "open", asdict(issues[1]))])
        pull = PullInfo(5, "PR", False, _ago(days=1))
        self._put(self.gitea, repo_id, "pull", [EntityRow("pull", "5", "open", asdict(pull))])
        runs = [_run(1, "failure", pulls=(5,)), _push(2, "failure")]
        self._put(self.gitea, repo_id, "ci_run", [EntityRow("ci_run", str(r["run_id"]), "open", r) for r in runs])
        self._record(self.gitea, repo_id, "ok", _ago(minutes=3))
        self._record(self.plane, repo_id, "partial", _ago(minutes=9))
        result = self._summary_of()
        self.assertEqual(
            result,
            {
                "repo": "o/n",
                "html_url": "http://forge.invalid/o/n",
                "projects": [],
                "open_issues": 1,
                "review_prs": {"count": 1, "oldest_days": 1},
                "ci_failures": 2,
                "ci_window": 50,
                "sources": {
                    "gitea": {"state": "ok", "label": "정상", "last_ok_at": _ago(minutes=3)},
                    "plane": {"state": "partial", "label": "일부 실패", "last_ok_at": _ago(minutes=9)},
                },
                "last_sync_at": _ago(minutes=3),
                "pending": list(PENDING_1ST),
            },
        )
        json.dumps(result)

    def test_empty_repo(self) -> None:
        self._repo()
        result = self._summary_of()
        self.assertEqual(result["review_prs"], {"count": 0, "oldest_days": None})
        self.assertIsNone(result["last_sync_at"])
        self.assertEqual(result["sources"]["gitea"], {"state": "never", "label": "수집 전", "last_ok_at": None})

    def test_summary_and_health(self) -> None:
        repo_id = self._repo()
        self._record(self.gitea, repo_id, "ok", _ago(minutes=30))
        self._record(self.gitea, repo_id, "failed", _ago(minutes=1), "auth")
        top = summary(self.store, NOW, 900)
        self.assertEqual((top["generated_at"], top["interval_s"]), (_iso(NOW), 900))
        self.assertEqual(top["repos"], [repo_summary(self.store, self.store.repos()[0], NOW, 900)])
        report = health(self.store, NOW)
        self.assertEqual(report["generated_at"], _iso(NOW))
        self.assertEqual(
            report["sources"],
            [
                {
                    "repo": "o/n",
                    "source": "gitea",
                    "state": "failed",
                    "label": "연결 실패",
                    "last_ok_at": _ago(minutes=30),
                    "last_result": "failed",
                    "api_calls": 4,
                },
                {
                    "repo": "o/n",
                    "source": "plane",
                    "state": "never",
                    "label": "수집 전",
                    "last_ok_at": None,
                    "last_result": None,
                    "api_calls": 0,
                },
            ],
        )

    def test_skipped_label_follows_error_kind(self) -> None:
        repo_id = self._repo()
        self._record(self.gitea, repo_id, "skipped", _ago(minutes=1), "no_token")
        self._record(self.plane, repo_id, "skipped", _ago(minutes=1), "path_missing")
        sources = self._summary_of()["sources"]
        self.assertEqual((sources["gitea"]["state"], sources["gitea"]["label"]), ("skipped", "토큰 없음"))
        self.assertEqual((sources["plane"]["state"], sources["plane"]["label"]), ("skipped", "경로 없음"))
        labels = [(row["state"], row["label"]) for row in health(self.store, NOW)["sources"]]
        self.assertEqual(labels, [("skipped", "토큰 없음"), ("skipped", "경로 없음")])


class SourceStateTest(unittest.TestCase):
    @staticmethod
    def _row(result: str, age_s: int = 0, finished: bool = True) -> RunRow:
        at = _ago(seconds=age_s)
        return RunRow("gitea", 1, at, at if finished else None, result, 0, "")

    def _state(self, last: Optional[RunRow], good: Optional[RunRow]) -> str:
        return source_state(last, good, NOW, 600)

    def test_source_state_rules(self) -> None:
        old_good = self._row("ok", 901)
        self.assertEqual(self._state(None, None), "never")
        self.assertEqual(self._state(self._row("failed"), old_good), "failed")
        self.assertEqual(self._state(self._row("skipped"), old_good), "skipped")
        stale_partial = self._row("partial", 901)
        self.assertEqual(self._state(stale_partial, stale_partial), "stale")
        partial = self._row("partial", 10)
        self.assertEqual(self._state(partial, partial), "partial")
        edge = self._row("ok", 900)  # interval_s + 300초까지는 지연이 아니다
        self.assertEqual(self._state(edge, edge), "ok")
        self.assertEqual(self._state(old_good, old_good), "stale")
        self.assertEqual(set(STATE_LABELS), {"never", "failed", "skipped", "stale", "partial", "ok"})

    def test_unfinished_run_is_judged_by_last_good(self) -> None:
        running = self._row("", finished=False)
        self.assertEqual(self._state(running, None), "never")
        self.assertEqual(self._state(running, self._row("ok", 10)), "ok")
        self.assertEqual(self._state(running, self._row("partial", 10)), "partial")
        self.assertEqual(self._state(running, self._row("ok", 901)), "stale")


class RenderTest(_ViewCase):
    def test_render_text_empty(self) -> None:
        self.assertEqual(render_text(summary(self.store, NOW)), "등록된 저장소 없음 — ledger sync로 등록하세요")

    def test_render_text_values(self) -> None:
        repo_id = self._repo()
        self._project(repo_id, "ABC", title="제품")
        self._cycle(repo_id, "ABC")
        self._project(repo_id, "DEF")
        self._record(self.gitea, repo_id, "ok", _ago(minutes=3))
        text = render_text(summary(self.store, NOW))
        for expected in (
            "o/n",
            "ABC 제품: Cycle 1 3/8, 게이트 1 예정: 3일",
            "DEF DEF 제목: cycle 없음, 게이트 1 예정: -",
            "열린 결함 (Gitea): 0",
            "리뷰 대기 PR: 0",
            "CI 실패: 0 (최근 실행 50건 기준)",
            f"Gitea: 정상 (마지막 성공 {_ago(minutes=3)})",
            "Plane: 수집 전",
            "1차에서 제공: 빠진 문서, Penpot 변경, 열린 알림, 미분류",
        ):
            self.assertIn(expected, text)

    def test_render_project_not_found(self) -> None:
        repo_id = self._repo()
        self._project(repo_id, "ZZZ", found=False)
        lines = render_text(summary(self.store, NOW)).splitlines()
        self.assertEqual([line for line in lines if "ZZZ" in line], ["  ZZZ ZZZ 제목: 프로젝트 없음"])

    def test_render_session_first_line(self) -> None:
        repo_id = self._repo()
        self._record(self.gitea, repo_id, "ok", _ago(minutes=7, seconds=59))
        text = render_session(self._summary_of(), NOW)
        self.assertEqual(text.splitlines()[0], "forge-ledger 허브: o/n (마지막 수집 7분 전)")
        self.assertNotIn("…", text)

    def test_render_session_never_synced(self) -> None:
        self._repo()
        text = render_session(self._summary_of(), NOW)
        self.assertEqual(text.splitlines()[0], "forge-ledger 허브: o/n (수집 전)")

    def test_render_session_limit(self) -> None:
        for index in range(5):
            repo_id = self._repo(f"n{index}")
            for number in range(40):
                self._project(repo_id, f"P{number}", title="긴 제목" * 10)
        for repo_sum in summary(self.store, NOW)["repos"]:
            text = render_session(repo_sum, NOW)
            self.assertEqual(len(text), 1500)
            self.assertTrue(text.endswith("…"))
            # 잘려도 고정 길이 수치는 남는다.
            self.assertIn("CI 실패: 0", text)
            self.assertIn("Plane: 수집 전", text)
        self.assertEqual(len(render_session(self._summary_of("n0"), NOW, limit=100)), 100)


class WorklogViewTest(_ViewCase):
    def test_range_state_sort_and_user(self) -> None:
        def attrs(at: str, author: str) -> dict[str, Any]:
            return {
                "at": at,
                "date": at[:10],
                "month": at[:7],
                "author": author,
                "host": "host",
                "source": "manual",
                "repo": "",
                "item": "",
                "text": author,
                "private": "숨김",
            }

        rows = [
            EntityRow("worklog", "late", "open", attrs("2026-11-25T09:00:00+00:00", "첫째")),
            EntityRow("worklog", "early", "open", attrs("2026-11-25T10:00:00+09:00", "둘째")),
            EntityRow("worklog", "deleted", "deleted", attrs("2026-11-25T08:00:00+00:00", "삭제")),
            EntityRow("worklog", "outside", "open", attrs("2026-11-24T08:00:00+00:00", "범위 밖")),
        ]
        self.store.apply_kind(self.plane, WORKSPACE_REPO_ID, "worklog", rows, _iso(NOW), False)
        result = worklog_view(self.store, date(2026, 11, 25), date(2026, 11, 25), None)
        self.assertEqual([e["author"] for e in result["entries"]], ["둘째", "첫째"])
        self.assertTrue(all("private" not in row for row in result["entries"]))
        self.assertEqual(len(worklog_view(self.store, date(2026, 11, 25), date(2026, 11, 25), "첫째")["entries"]), 1)


if __name__ == "__main__":
    unittest.main()
