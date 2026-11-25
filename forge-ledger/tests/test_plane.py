from __future__ import annotations

import http.client
import socket
import unittest
import urllib.error
from unittest import mock

from ledger.adapters import CycleInfo, PmAdapter, ProjectInfo
from ledger.plane import (
    PlaneAuthError,
    PlaneClient,
    PlaneConflict,
    PlaneError,
    PlaneLike,
    PlaneSettings,
    PlaneUnavailable,
)
from tests.fakes import FakePlaneServer

TEST_TOKEN = "plane_api_" + "0" * 32


class PlaneClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = FakePlaneServer()
        self.addCleanup(self.server.close)
        self.settings = PlaneSettings(
            base_url=self.server.base_url,
            workspace="forge",
            token=TEST_TOKEN,
        )
        self.client = PlaneClient(self.settings, sleep=lambda _: None)

    def test_create_project_forbidden_name_is_400(self) -> None:
        with self.assertRaisesRegex(PlaneError, "HTTP 400"):
            self.client.create_project("sample-app 유지보수", "APP")

    def test_create_project_request(self) -> None:
        self.client.create_project("앱 첫 출시", "APP")
        req = self.server.requests[-1]
        self.assertEqual((req.method, req.path), ("POST", "/api/v1/workspaces/forge/projects/"))
        self.assertEqual(req.headers["X-API-Key"], "plane_api_" + "0" * 32)
        self.assertEqual(
            req.json,
            {"name": "앱 첫 출시", "identifier": "APP", "cycle_view": True, "module_view": True},
        )

    def test_add_to_module_payload(self) -> None:
        self.client.add_to_module("p1", "m1", ["i1", "i2"])
        req = self.server.requests[-1]
        self.assertEqual(req.path, "/api/v1/workspaces/forge/projects/p1/modules/m1/module-issues/")
        self.assertEqual(req.json, {"issues": ["i1", "i2"]})

    def test_list_items_follows_cursor(self) -> None:
        self.server.pages = [
            {"results": [{"id": "a"}], "next_cursor": "c2", "next_page_results": True},
            {"results": [{"id": "b"}], "next_cursor": None, "next_page_results": False},
        ]
        self.assertEqual([i["id"] for i in self.client.list_items("p1")], ["a", "b"])
        self.assertEqual(len(self.server.requests), 2)
        self.assertIn("per_page=100", self.server.requests[0].path)
        self.assertIn("cursor=c2", self.server.requests[1].path)

    def test_update_conflict(self) -> None:
        self.server.item = {
            "id": "i1",
            "updated_at": "2026-11-23T10:00:00Z",
            "description_html": "<p>x</p>",
        }
        with self.assertRaises(PlaneConflict):
            self.client.update_item_description(
                "p1",
                "i1",
                "<p>y</p>",
                expected_updated_at="2026-11-23T09:00:00Z",
            )
        self.assertNotIn("PATCH", [r.method for r in self.server.requests])

    def test_rate_limit_waits(self) -> None:
        t = [0.0]
        slept: list[float] = []
        c = PlaneClient(self.settings, per_minute=2, clock=lambda: t[0], sleep=lambda s: slept.append(s))
        c.list_projects()
        c.list_projects()
        c.list_projects()
        self.assertTrue(slept and slept[0] > 0)

        t2 = [0.0]
        slept2: list[float] = []

        def advance(s: float) -> None:
            slept2.append(s)
            t2[0] += s

        c2 = PlaneClient(self.settings, per_minute=2, clock=lambda: t2[0], sleep=advance)
        c2.list_projects()
        t2[0] = 10.0
        c2.list_projects()
        t2[0] = 15.0
        c2.list_projects()
        self.assertEqual(slept2, [45.0])

    def test_status_mapping(self) -> None:
        for status, exc in (
            (401, PlaneAuthError),
            (403, PlaneAuthError),
            (503, PlaneUnavailable),
            (400, PlaneError),
        ):
            self.server.force_status = status
            with self.assertRaises(exc) as ctx:
                self.client.list_projects()
            if status in (401, 403):
                self.assertEqual(str(ctx.exception), "Plane 토큰 확인 필요")

    def test_settings_from_env(self) -> None:
        env = {
            "FORGE_LEDGER_PLANE_URL": "http://127.0.0.1:8080/",
            "FORGE_LEDGER_PLANE_WORKSPACE": "forge",
            "FORGE_LEDGER_PLANE_TOKEN": TEST_TOKEN,
        }
        settings = PlaneSettings.from_env(env)
        self.assertEqual(settings.workspace, "forge")
        self.assertEqual(settings.token, TEST_TOKEN)
        self.assertNotIn(TEST_TOKEN, repr(settings))

        for key in (
            "FORGE_LEDGER_PLANE_URL",
            "FORGE_LEDGER_PLANE_WORKSPACE",
            "FORGE_LEDGER_PLANE_TOKEN",
        ):
            partial = {k: v for k, v in env.items() if k != key}
            with self.assertRaises(PlaneError) as ctx:
                PlaneSettings.from_env(partial)
            self.assertEqual(str(ctx.exception), f"설정 없음: {key}")

    def test_crud_endpoints_and_update_success(self) -> None:
        self.server.pages = [
            {"results": [{"id": "p1"}], "next_cursor": "n2", "next_page_results": True},
            {"results": [{"id": "p2"}], "next_cursor": None, "next_page_results": False},
        ]
        self.assertEqual([p["id"] for p in self.client.list_projects()], ["p1", "p2"])

        self.client.delete_project("p1")
        req = self.server.requests[-1]
        self.assertEqual((req.method, req.path), ("DELETE", "/api/v1/workspaces/forge/projects/p1/"))

        self.client.create_label("p1", "계획서", "#3b82f6")
        req = self.server.requests[-1]
        self.assertEqual((req.method, req.path), ("POST", "/api/v1/workspaces/forge/projects/p1/labels/"))
        self.assertEqual(req.json, {"name": "계획서", "color": "#3b82f6"})

        self.client.create_module("p1", "P1")
        req = self.server.requests[-1]
        self.assertEqual((req.method, req.path), ("POST", "/api/v1/workspaces/forge/projects/p1/modules/"))
        self.assertEqual(req.json, {"name": "P1"})

        self.client.create_cycle("p1", "S1")
        req = self.server.requests[-1]
        self.assertEqual((req.method, req.path), ("POST", "/api/v1/workspaces/forge/projects/p1/cycles/"))
        self.assertEqual(req.json, {"name": "S1"})

        self.client.create_item("p1", "[계획서] PRD", "<p>body</p>", ["l1"])
        req = self.server.requests[-1]
        self.assertEqual((req.method, req.path), ("POST", "/api/v1/workspaces/forge/projects/p1/work-items/"))
        self.assertEqual(
            req.json,
            {"name": "[계획서] PRD", "description_html": "<p>body</p>", "labels": ["l1"]},
        )

        self.server.item = {
            "id": "i1",
            "sequence_id": 1,
            "updated_at": "2026-11-23T10:00:00Z",
            "name": "[계획서] PRD",
            "description_html": "<p>old</p>",
        }
        got = self.client.get_item("p1", "i1")
        self.assertEqual(got["id"], "i1")
        req = self.server.requests[-1]
        self.assertEqual((req.method, req.path), ("GET", "/api/v1/workspaces/forge/projects/p1/work-items/i1/"))

        updated = self.client.update_item_description(
            "p1",
            "i1",
            "<p>new</p>",
            expected_updated_at="2026-11-23T10:00:00Z",
        )
        req = self.server.requests[-1]
        self.assertEqual((req.method, req.path), ("PATCH", "/api/v1/workspaces/forge/projects/p1/work-items/i1/"))
        self.assertEqual(req.json, {"description_html": "<p>new</p>"})
        self.assertEqual(updated["description_html"], "<p>new</p>")

    def test_retry_backoff_and_recovery(self) -> None:
        slept: list[float] = []
        client = PlaneClient(self.settings, sleep=lambda s: slept.append(s))

        self.server.status_queue = [503, 503, 200]
        self.server.pages = [{"results": [{"id": "ok"}], "next_cursor": None, "next_page_results": False}]
        self.assertEqual(client.list_projects(), [{"id": "ok"}])
        self.assertEqual(slept, [1.0, 2.0])

        slept.clear()
        self.server.force_status = 503
        with self.assertRaises(PlaneUnavailable):
            client.list_projects()
        self.assertEqual(slept, [1.0, 2.0, 4.0])

    def _rate_limited_client(self) -> tuple[PlaneClient, list[float], list[str]]:
        slept: list[float] = []
        notes: list[str] = []
        client = PlaneClient(self.settings, sleep=lambda s: slept.append(s), notify=notes.append)
        return client, slept, notes

    def test_429_honors_retry_after(self) -> None:
        client, slept, notes = self._rate_limited_client()
        self.server.retry_after = "7"
        self.server.status_queue = [429, 200]
        self.assertEqual(client.list_projects(), [])
        self.assertEqual(slept, [7.0])
        self.assertEqual(notes, ["Plane 호출 한도에 걸려 7초 기다립니다"])

    def test_429_without_header_waits_60(self) -> None:
        client, slept, _ = self._rate_limited_client()
        self.server.status_queue = [429, 200]
        client.list_projects()
        self.assertEqual(slept, [60.0])

    def test_429_invalid_header_waits_60(self) -> None:
        client, slept, _ = self._rate_limited_client()
        self.server.retry_after = "soon"
        self.server.status_queue = [429, 200]
        client.list_projects()
        self.assertEqual(slept, [60.0])

    def test_429_wait_capped_at_65(self) -> None:
        client, slept, _ = self._rate_limited_client()
        self.server.retry_after = "500"
        self.server.status_queue = [429, 200]
        client.list_projects()
        self.assertEqual(slept, [65.0])

    def test_429_gives_up_after_three_retries(self) -> None:
        client, slept, _ = self._rate_limited_client()
        self.server.retry_after = "5"
        self.server.force_status = 429
        with self.assertRaisesRegex(PlaneUnavailable, "HTTP 429"):
            client.list_projects()
        self.assertEqual(slept, [5.0, 5.0, 5.0])

    def test_connection_failure_retries_and_hides_token(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            closed_port = sock.getsockname()[1]

        slept: list[float] = []
        bad_settings = PlaneSettings(
            base_url=f"http://127.0.0.1:{closed_port}",
            workspace="forge",
            token=TEST_TOKEN,
        )
        client = PlaneClient(bad_settings, sleep=lambda s: slept.append(s))
        with self.assertRaises(PlaneUnavailable) as ctx:
            client.list_projects()
        self.assertEqual(slept, [1.0, 2.0, 4.0])
        self.assertNotIn(TEST_TOKEN, str(ctx.exception))

    def test_error_messages_never_leak_token(self) -> None:
        self.server.error_body = f"echo {TEST_TOKEN}"
        for status, exc in (
            (400, PlaneError),
            (401, PlaneAuthError),
            (500, PlaneUnavailable),
        ):
            self.server.force_status = status
            with self.assertRaises(exc) as ctx:
                self.client.list_projects()
            self.assertNotIn(TEST_TOKEN, str(ctx.exception))

    def test_plane_like_protocol(self) -> None:
        def accepts(client: PlaneLike) -> list[dict[str, object]]:
            return client.list_projects()

        self.assertEqual(accepts(self.client), [])

    def test_invalid_base_url_scheme_rejected(self) -> None:
        private_ip = ".".join(("10", "20", "30", "40"))
        for bad_url in (f"{private_ip}:8000", "ftp://127.0.0.1:8000", "http://"):
            bad_settings = PlaneSettings(base_url=bad_url, workspace="forge", token=TEST_TOKEN)
            with self.assertRaises(PlaneError) as ctx:
                PlaneClient(bad_settings, sleep=lambda _: None)
            self.assertEqual(str(ctx.exception), "설정 오류: FORGE_LEDGER_PLANE_URL")
            self.assertNotIn(private_ip, str(ctx.exception))

    def test_connection_error_hides_ipv4_address(self) -> None:
        private_ip = ".".join(("10", "20", "30", "40"))
        slept: list[float] = []
        client = PlaneClient(self.settings, sleep=lambda s: slept.append(s))
        err = urllib.error.URLError(f"connection failed to {private_ip}:8080")
        with mock.patch.object(client._opener, "open", side_effect=err):
            with self.assertRaises(PlaneUnavailable) as ctx:
                client.list_projects()
        self.assertEqual(slept, [1.0, 2.0, 4.0])
        self.assertEqual(str(ctx.exception), "Plane 연결 실패")
        self.assertNotIn(private_ip, str(ctx.exception))

    def test_incomplete_read_retries_and_raises_unavailable(self) -> None:
        for exc in (http.client.IncompleteRead(b"partial"), http.client.BadStatusLine("bad")):
            slept: list[float] = []
            client = PlaneClient(self.settings, sleep=lambda s: slept.append(s))
            with mock.patch.object(client._opener, "open", side_effect=exc):
                with self.assertRaises(PlaneUnavailable):
                    client.list_projects()
            self.assertEqual(slept, [1.0, 2.0, 4.0])

    def test_current_cycle_maps_counts(self) -> None:
        self.server.cycles["p1"] = [
            {
                "id": "c1",
                "name": "허브 골격",
                "start_date": "2026-11-25",
                "end_date": "2026-11-29",
                "total_issues": 8,
                "completed_issues": 3,
            }
        ]
        cycle = self.client.current_cycle("p1")
        self.assertEqual(
            cycle,
            CycleInfo(
                id="c1",
                name="허브 골격",
                start_date="2026-11-25T00:00:00Z",
                end_date="2026-11-29T00:00:00Z",
                total_issues=8,
                completed_issues=3,
            ),
        )
        req = self.server.requests[-1]
        self.assertEqual(req.method, "GET")
        self.assertIn("/projects/p1/cycles/", req.path)
        self.assertIn("cycle_view=current", req.path)

    def test_current_cycle_iso_timestamps_and_defaults(self) -> None:
        self.server.cycles["p1"] = [
            {
                "id": "c2",
                "name": "실서버 형식",
                "start_date": "2026-11-25T05:50:20.427362Z",
                "end_date": "2026-11-29T23:59:00Z",
            }
        ]
        cycle = self.client.current_cycle("p1")
        self.assertEqual(
            cycle,
            CycleInfo(
                id="c2",
                name="실서버 형식",
                start_date="2026-11-25T05:50:20Z",
                end_date="2026-11-29T23:59:00Z",
                total_issues=0,
                completed_issues=0,
            ),
        )

    def test_current_cycle_accepts_list_and_rejects_bad_date(self) -> None:
        with mock.patch.object(
            self.client,
            "_request",
            return_value=[
                {
                    "id": "c3",
                    "name": "목록 응답",
                    "start_date": "2026-11-25T09:00:00+09:00",
                    "end_date": "2026-11-29T09:00:00+09:00",
                    "total_issues": 5,
                    "completed_issues": 2,
                }
            ],
        ):
            cycle = self.client.current_cycle("p1")
        self.assertEqual(
            cycle,
            CycleInfo(
                id="c3",
                name="목록 응답",
                start_date="2026-11-25T00:00:00Z",
                end_date="2026-11-29T00:00:00Z",
                total_issues=5,
                completed_issues=2,
            ),
        )

        self.server.cycles["p1"] = [{"id": "c4", "name": "잘못된 날짜", "start_date": "bad", "end_date": ""}]
        with self.assertRaisesRegex(PlaneError, "Plane 응답 형식 오류"):
            self.client.current_cycle("p1")

    def test_current_cycle_none_when_empty(self) -> None:
        self.server.cycles["p1"] = []
        self.assertIsNone(self.client.current_cycle("p1"))
        with mock.patch.object(self.client, "_request", return_value=[]):
            self.assertIsNone(self.client.current_cycle("p1"))

    def test_current_cycle_rejects_malformed_response(self) -> None:
        bad_payloads: list[object] = [
            [None, {"id": "c2"}],
            [None],
            {},
            {"results": "x"},
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with mock.patch.object(self.client, "_request", return_value=payload):
                    with self.assertRaisesRegex(PlaneError, "Plane 응답 형식 오류"):
                        self.client.current_cycle("p1")

    def test_whoami_and_calls_counter(self) -> None:
        self.server.me = {"email": "dev@example.com"}
        self.assertEqual(self.client.calls, 0)
        self.assertEqual(self.client.whoami(), "dev@example.com")
        self.assertEqual(self.client.calls, 1)
        req = self.server.requests[-1]
        self.assertEqual((req.method, req.path), ("GET", "/api/v1/users/me/"))

    def test_whoami_rejects_missing_or_invalid_email(self) -> None:
        for payload in ({}, {"email": ""}, {"email": None}, {"email": 123}):
            with self.subTest(payload=payload):
                self.server.me = payload
                with self.assertRaisesRegex(PlaneError, "Plane 응답 형식 오류"):
                    self.client.whoami()

    def test_list_project_infos(self) -> None:
        self.server.pages = [
            {
                "results": [{"id": "p1", "identifier": "HUB", "name": "허브"}],
                "next_cursor": None,
                "next_page_results": False,
            }
        ]

        def accepts(adapter: PmAdapter) -> list[ProjectInfo]:
            return adapter.list_project_infos()

        self.assertEqual(
            accepts(self.client),
            [ProjectInfo(id="p1", identifier="HUB", name="허브")],
        )

    def test_list_project_infos_validates_required_fields(self) -> None:
        self.server.pages = [
            {"results": [{"id": "p1", "identifier": "HUB"}], "next_cursor": None, "next_page_results": False}
        ]
        self.assertEqual(
            self.client.list_project_infos(),
            [ProjectInfo(id="p1", identifier="HUB", name="")],
        )

        bad_items: list[dict[str, object]] = [
            {"identifier": "HUB"},
            {"id": "", "identifier": "HUB"},
            {"id": None, "identifier": "HUB"},
            {"id": 123, "identifier": "HUB"},
            {"id": "p1"},
            {"id": "p1", "identifier": ""},
            {"id": "p1", "identifier": None},
            {"id": "p1", "identifier": 456},
        ]
        for item in bad_items:
            with self.subTest(item=item):
                self.server.pages = [{"results": [item], "next_cursor": None, "next_page_results": False}]
                with self.assertRaisesRegex(PlaneError, "Plane 응답 형식 오류"):
                    self.client.list_project_infos()

    def test_redirect_not_followed(self) -> None:
        other = FakePlaneServer()
        self.addCleanup(other.close)
        slept: list[float] = []
        client = PlaneClient(self.settings, sleep=slept.append)
        self.server.redirect_location = f"{other.base_url}/x"
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                self.server.status_queue = [status]
                with self.assertRaisesRegex(PlaneError, f"Plane 요청 오류: HTTP {status}$") as ctx:
                    client.whoami()
                self.assertNotIsInstance(ctx.exception, (PlaneAuthError, PlaneUnavailable))
                self.assertEqual(other.requests, [])
        self.assertEqual(client.calls, 5)
        self.assertEqual(slept, [])
