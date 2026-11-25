from __future__ import annotations

import socket
import unittest

from ledger.adapters import IssueInfo, PullInfo, RepoInfo, RunInfo, parse_run_path
from ledger.gitea import GiteaAuthError, GiteaClient, GiteaError, GiteaSettings, GiteaUnavailable
from tests.hub_fakes import FakeGiteaServer

TEST_TOKEN = "tok" + "e" * 30


def _issue(number: int) -> dict[str, object]:
    return {
        "number": number,
        "title": f"이슈 {number}",
        "labels": [{"name": "bug"}, {"name": "p1"}],
        "created_at": "2026-11-20T09:00:00+09:00",
    }


class ParseRunPathTest(unittest.TestCase):
    def test_parse_run_path(self) -> None:
        self.assertEqual(parse_run_path("pr-check.yml@refs/pull/5/head"), ("pr-check.yml", "refs/pull/5/head"))
        self.assertEqual(parse_run_path("pr-check.yml"), ("pr-check.yml", ""))
        self.assertEqual(parse_run_path("a@b@c"), ("a", "b@c"))


class GiteaSettingsTest(unittest.TestCase):
    def test_settings_from_env_missing(self) -> None:
        with self.assertRaisesRegex(GiteaError, "설정 없음: FORGE_LEDGER_GITEA_URL"):
            GiteaSettings.from_env({})

    def test_settings_from_env_reads_values_and_hides_token(self) -> None:
        settings = GiteaSettings.from_env(
            {"FORGE_LEDGER_GITEA_URL": "http://127.0.0.1:1", "FORGE_LEDGER_GITEA_TOKEN": TEST_TOKEN}
        )
        self.assertEqual((settings.base_url, settings.token), ("http://127.0.0.1:1", TEST_TOKEN))
        self.assertNotIn(TEST_TOKEN, repr(settings))


class GiteaClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = FakeGiteaServer()
        self.addCleanup(self.server.close)
        self.sleeps: list[float] = []
        self.client = GiteaClient(
            GiteaSettings(base_url=self.server.base_url, token=TEST_TOKEN),
            sleep=self.sleeps.append,
            notify=lambda _: None,
        )

    def test_get_repo_and_whoami(self) -> None:
        self.assertEqual(
            self.client.get_repo("o", "n"),
            RepoInfo(full_name="o/n", default_branch="main", html_url=self.server.repo["html_url"]),
        )
        self.assertEqual(self.client.whoami(), "tester")
        for _, _, headers in self.server.requests:
            self.assertEqual(headers["Authorization"], "token " + TEST_TOKEN)

    def test_list_open_issues_paginates(self) -> None:
        self.server.issues = [_issue(n) for n in range(1, 121)]
        issues = self.client.list_open_issues("o", "n")
        self.assertEqual([i.number for i in issues], list(range(1, 121)))
        self.assertEqual(
            issues[0],
            IssueInfo(number=1, title="이슈 1", labels=("bug", "p1"), created_at="2026-11-20T09:00:00+09:00"),
        )
        self.assertEqual(len(self.server.requests), 3)
        self.assertEqual(self.client.calls, 3)
        path = self.server.requests[2][1]
        for part in ("state=open", "type=issues", "page=3", "limit=50"):
            self.assertIn(part, path)

    def test_list_stops_at_total_count(self) -> None:
        self.server.issues = [_issue(n) for n in range(1, 101)]
        self.assertEqual(len(self.client.list_open_issues("o", "n")), 100)
        self.assertEqual(self.client.calls, 2)

    def test_list_open_pulls_maps_draft(self) -> None:
        self.server.pulls = [
            {"number": 1, "title": "a", "draft": True, "created_at": "2026-11-21T00:00:00Z"},
            {"number": 2, "title": "b", "draft": False, "created_at": "2026-11-22T00:00:00Z"},
        ]
        self.assertEqual(
            self.client.list_open_pulls("o", "n"),
            [
                PullInfo(number=1, title="a", draft=True, created_at="2026-11-21T00:00:00Z"),
                PullInfo(number=2, title="b", draft=False, created_at="2026-11-22T00:00:00Z"),
            ],
        )
        self.assertIn("state=open", self.server.requests[0][1])

    def test_list_runs_parses_path_and_prs(self) -> None:
        self.server.runs = [
            {
                "id": 900,
                "run_number": 12,
                "event": "pull_request",
                "status": "completed",
                "conclusion": "failure",
                "path": "pr-check.yml@refs/pull/5/head",
                "head_branch": "",
                "head_sha": "abc123",
                "pull_requests": [{"number": 5}],
                "started_at": "2026-11-24T01:00:00Z",
            }
        ]
        self.assertEqual(
            self.client.list_runs("o", "n"),
            [
                RunInfo(
                    run_id=900,
                    run_number=12,
                    event="pull_request",
                    status="completed",
                    conclusion="failure",
                    workflow="pr-check.yml",
                    ref="refs/pull/5/head",
                    head_sha="abc123",
                    pull_numbers=(5,),
                    started_at="2026-11-24T01:00:00Z",
                )
            ],
        )
        self.assertEqual(self.server.requests[0][1], "/api/v1/repos/o/n/actions/runs?limit=50")

    def test_list_runs_tolerates_null_fields(self) -> None:
        self.server.runs = [
            {"id": 1, "run_number": 1, "status": "in_progress", "conclusion": None, "pull_requests": None}
        ]
        run = self.client.list_runs("o", "n")[0]
        self.assertEqual((run.conclusion, run.pull_numbers, run.workflow, run.ref), ("", (), "", ""))

    def test_auth_error(self) -> None:
        self.server.status_queue = [401]
        with self.assertRaises(GiteaAuthError):
            self.client.whoami()
        self.assertEqual(self.sleeps, [])

    def test_retry_5xx_then_ok(self) -> None:
        self.server.status_queue = [502]
        self.assertEqual(self.client.whoami(), "tester")
        self.assertEqual(self.sleeps, [1.0])
        self.assertEqual(self.client.calls, 2)

    def test_retry_5xx_gives_up(self) -> None:
        self.server.status_queue = [503] * 4
        with self.assertRaisesRegex(GiteaUnavailable, "HTTP 503"):
            self.client.whoami()
        self.assertEqual(self.sleeps, [1.0, 2.0, 4.0])

    def test_rate_limit_retries_three_times(self) -> None:
        self.server.status_queue = [429] * 4
        with self.assertRaisesRegex(GiteaUnavailable, "HTTP 429"):
            self.client.whoami()
        self.assertEqual(self.sleeps, [60.0, 60.0, 60.0])

    def test_other_status_is_plain_error(self) -> None:
        self.server.status_queue = [404]
        with self.assertRaisesRegex(GiteaError, "HTTP 404") as ctx:
            self.client.get_repo("o", "n")
        self.assertNotIsInstance(ctx.exception, (GiteaAuthError, GiteaUnavailable))

    def test_invalid_header_value_does_not_leak_token(self) -> None:
        token = TEST_TOKEN + "\n" + "x"
        client = GiteaClient(GiteaSettings(base_url=self.server.base_url, token=token), sleep=self.sleeps.append)
        with self.assertRaises(GiteaError) as ctx:
            client.whoami()
        self.assertNotIn(TEST_TOKEN, str(ctx.exception))

    def test_errors_never_contain_token(self) -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        client = GiteaClient(
            GiteaSettings(base_url=f"http://127.0.0.1:{port}", token=TEST_TOKEN),
            sleep=self.sleeps.append,
        )
        with self.assertRaises(GiteaUnavailable) as ctx:
            client.whoami()
        self.assertNotIn(TEST_TOKEN, str(ctx.exception))
        self.assertIn("Gitea 연결 실패", str(ctx.exception))
        self.assertEqual(self.sleeps, [1.0, 2.0, 4.0])
        self.assertEqual(client.calls, 4)


if __name__ == "__main__":
    unittest.main()
