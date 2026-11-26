from __future__ import annotations

import http.client
import socket
import unittest
import urllib.error
from typing import Any
from unittest import mock

from ledger.adapters import CycleInfo, PmAdapter, ProjectInfo, WorkTracker
from ledger.plane import (
    PlaneAuthError,
    PlaneClient,
    PlaneConflict,
    PlaneError,
    PlaneLike,
    PlaneSettings,
    PlaneUnavailable,
)
from tests.fakes import FakePlane, FakePlaneServer

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


_BASE = "/api/v1/workspaces/forge/projects/"
_UPDATE_FIELDS = (
    "state",
    "assignees",
    "labels",
    "parent",
    "description_html",
    "external_source",
    "external_id",
)


def _page(results: list[dict[str, Any]], cursor: str = "") -> dict[str, Any]:
    return {"results": results, "next_cursor": cursor or None, "next_page_results": bool(cursor)}


def _members(protocol: type) -> set[str]:
    return {name for name, value in vars(protocol).items() if callable(value) and not name.startswith("_")}


class PlaneWorkWritesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = FakePlaneServer()
        self.addCleanup(self.server.close)
        self.settings = PlaneSettings(base_url=self.server.base_url, workspace="forge", token=TEST_TOKEN)
        self.client = PlaneClient(self.settings, sleep=lambda _: None)

    def _last(self) -> tuple[str, str, Any]:
        req = self.server.requests[-1]
        return req.method, req.path.split("?")[0], req.json

    def test_list_states_and_labels_follow_cursor(self) -> None:
        for method, segment in (("list_states", "states"), ("list_labels", "labels")):
            with self.subTest(method=method):
                self.server.requests.clear()
                self.server.pages = [
                    _page([{"id": "a", "name": "Todo", "group": "unstarted"}], "c2"),
                    _page([{"id": "b", "name": "Done", "group": "completed"}]),
                ]
                entries = getattr(self.client, method)("p1")
                self.assertEqual([e["id"] for e in entries], ["a", "b"])
                self.assertEqual(self._last()[:2], ("GET", f"{_BASE}p1/{segment}/"))
                self.assertIn("cursor=c2", self.server.requests[-1].path)

    def test_list_states_and_labels_return_stored_entries(self) -> None:
        self.server.states["p1"] = [{"id": "s1", "name": "In Progress", "group": "started"}]
        self.assertEqual(self.client.list_states("p1"), self.server.states["p1"])
        self.client.create_label("p1", "계획서", "#3b82f6")
        self.assertEqual([label["name"] for label in self.client.list_labels("p1")], ["계획서"])

    def test_create_item_with_parent_state_and_external_marks(self) -> None:
        self.client.create_item(
            "p1",
            "W1",
            "<p>b</p>",
            ["l1"],
            parent="P",
            state="s1",
            external_source="forge-ledger-pmt",
            external_id="pmt:s/W1",
        )
        self.assertEqual(
            self._last(),
            (
                "POST",
                f"{_BASE}p1/work-items/",
                {
                    "name": "W1",
                    "description_html": "<p>b</p>",
                    "labels": ["l1"],
                    "parent": "P",
                    "state": "s1",
                    "external_source": "forge-ledger-pmt",
                    "external_id": "pmt:s/W1",
                },
            ),
        )

    def test_create_item_sends_only_given_options(self) -> None:
        self.client.create_item("p1", "W1", "<p>b</p>", [])
        self.assertEqual(self._last()[2], {"name": "W1", "description_html": "<p>b</p>", "labels": []})
        self.client.create_item("p1", "W2", "", [], external_id="pmt:s/W2")
        self.assertEqual(
            self._last()[2], {"name": "W2", "description_html": "", "labels": [], "external_id": "pmt:s/W2"}
        )

    def test_update_item_patches_allowed_fields(self) -> None:
        fields: dict[str, Any] = {
            "state": "s1",
            "assignees": ["u1"],
            "labels": ["l1"],
            "parent": None,
            "description_html": "<p>n</p>",
            "external_source": "forge-ledger-pmt",
            "external_id": "pmt:s/W1",
        }
        self.assertEqual(tuple(fields), _UPDATE_FIELDS)
        updated = self.client.update_item("p1", "i1", fields)
        self.assertEqual(self._last(), ("PATCH", f"{_BASE}p1/work-items/i1/", fields))
        self.assertEqual(updated["state"], "s1")
        self.assertEqual(len(self.server.requests), 1)

    def test_update_item_rejects_other_keys_before_any_request(self) -> None:
        for fields in ({"name": "x"}, {"state": "s1", "priority": "high"}, {"updated_at": "t"}):
            with self.subTest(fields=fields):
                with self.assertRaisesRegex(ValueError, "수정할 수 없는 필드"):
                    self.client.update_item("p1", "i1", fields)
        self.assertEqual(self.server.requests, [])

    def test_find_item_by_external_matches_source_and_id(self) -> None:
        entries = [
            {"id": "a", "external_source": None, "external_id": None},
            {"id": "b", "external_source": "other", "external_id": "pmt:s/W1"},
            {"id": "c", "external_source": "forge-ledger-pmt", "external_id": "pmt:s/W10"},
            {"id": "d"},
        ]
        match = {"id": "e", "external_source": "forge-ledger-pmt", "external_id": "pmt:s/W1"}
        self.server.pages = [_page(entries, "c2"), _page([match])]
        self.assertEqual(self.client.find_item_by_external("p1", "forge-ledger-pmt", "pmt:s/W1"), match)
        self.assertEqual(self._last()[:2], ("GET", f"{_BASE}p1/work-items/"))
        self.server.pages = [_page(entries)]
        self.assertIsNone(self.client.find_item_by_external("p1", "forge-ledger-pmt", "pmt:s/W1"))

    def test_comments_create_and_list(self) -> None:
        created = self.client.create_comment("p1", "i1", "<p>착수</p>")
        self.assertEqual(self._last(), ("POST", f"{_BASE}p1/work-items/i1/comments/", {"comment_html": "<p>착수</p>"}))
        self.client.create_comment("p1", "i1", "<p>인계</p>")
        self.client.create_comment("p1", "i2", "<p>other</p>")
        listed = self.client.list_comments("p1", "i1")
        self.assertEqual(self._last()[:2], ("GET", f"{_BASE}p1/work-items/i1/comments/"))
        self.assertEqual([c["comment_html"] for c in listed], ["<p>착수</p>", "<p>인계</p>"])
        self.assertEqual(listed[0]["id"], created["id"])
        for comment in listed:
            self.assertTrue({"id", "comment_html", "created_at", "actor"} <= set(comment))

    def test_update_comment_patches_one_comment_and_retries_5xx(self) -> None:
        created = self.client.create_comment("p1", "i1", "<p>a</p>")
        self.client.create_comment("p1", "i1", "<p>other</p>")
        self.server.requests.clear()
        self.server.status_queue = [503]  # 수정은 다시 보내도 중복이 생기지 않는다.
        updated = self.client.update_comment("p1", "i1", created["id"], "<p>b</p>")
        expected = ("PATCH", f"{_BASE}p1/work-items/i1/comments/{created['id']}/", {"comment_html": "<p>b</p>"})
        self.assertEqual(self._last(), expected)
        self.assertEqual(len(self.server.requests), 2)
        self.assertEqual(updated["comment_html"], "<p>b</p>")
        listed = self.client.list_comments("p1", "i1")
        self.assertEqual([c["comment_html"] for c in listed], ["<p>b</p>", "<p>other</p>"])

    def test_list_comments_follows_cursor(self) -> None:
        self.server.pages = [_page([{"id": "c1"}], "n2"), _page([{"id": "c2"}])]
        self.assertEqual([c["id"] for c in self.client.list_comments("p1", "i1")], ["c1", "c2"])
        self.assertIn("cursor=n2", self.server.requests[-1].path)

    def _seed_codes(self) -> None:
        self.server.projects = {
            "p-fl": {"id": "p-fl", "identifier": "FL", "name": "a"},
            "p-flx": {"id": "p-flx", "identifier": "FLX", "name": "b"},
        }
        self.server.items = {
            "p-fl": {"i3": {"id": "i3", "sequence_id": 3}, "i12": {"id": "i12", "sequence_id": 12}},
            "p-flx": {"x12": {"id": "x12", "sequence_id": 12}},
        }

    def test_resolve_code_finds_project_and_item(self) -> None:
        self._seed_codes()
        self.assertEqual(self.client.resolve_code("FL-12"), ("p-fl", {"id": "i12", "sequence_id": 12}))
        self.assertEqual([r.path.split("?")[0] for r in self.server.requests], [_BASE, f"{_BASE}p-fl/work-items/"])
        self.assertEqual(self.client.resolve_code("FLX-12")[0], "p-flx")

    def test_resolve_code_missing_or_malformed(self) -> None:
        self._seed_codes()
        for code in ("FL-99", "NO-12", "fl-12", "FL", "FL-", "-12", "FL-abc", "FL-12x", "FL-１２", ""):
            with self.subTest(code=code):
                with self.assertRaises(PlaneError) as ctx:
                    self.client.resolve_code(code)
                self.assertEqual(str(ctx.exception), f"항목 없음: {code}")
                self.assertIs(type(ctx.exception), PlaneError)

    def test_new_calls_keep_auth_retry_and_redirect_rules(self) -> None:
        slept: list[float] = []
        client = PlaneClient(self.settings, sleep=slept.append)
        self.server.status_queue = [503, 200]
        client.update_item("p1", "i1", {"state": "s1"})
        self.assertEqual(slept, [1.0])
        self.assertEqual(self.server.requests[-1].headers["X-API-Key"], TEST_TOKEN)

        self.server.status_queue = [401]
        with self.assertRaises(PlaneAuthError):
            client.update_item("p1", "i1", {"state": "s1"})

        other = FakePlaneServer()
        self.addCleanup(other.close)
        self.server.redirect_location = f"{other.base_url}/x"
        self.server.status_queue = [307]
        with self.assertRaisesRegex(PlaneError, "HTTP 307$"):
            client.list_states("p1")
        self.assertEqual(other.requests, [])

    def test_non_dict_write_response_is_format_error(self) -> None:
        with mock.patch.object(self.client, "_request", return_value=[]):
            with self.assertRaisesRegex(PlaneError, "Plane 응답 형식 오류"):
                self.client.create_comment("p1", "i1", "<p>x</p>")
            with self.assertRaisesRegex(PlaneError, "Plane 응답 형식 오류"):
                self.client.update_item("p1", "i1", {"state": "s1"})


class CreateRetryPolicyTest(unittest.TestCase):
    """생성 POST는 서버가 처리했을 수 있는 실패에서 다시 보내지 않는다(중복 방지)."""

    def setUp(self) -> None:
        self.server = FakePlaneServer()
        self.addCleanup(self.server.close)
        self.server.retry_after = "1"
        self.slept: list[float] = []
        self.client = PlaneClient(
            PlaneSettings(base_url=self.server.base_url, workspace="forge", token=TEST_TOKEN),
            sleep=self.slept.append,
            notify=lambda _: None,
        )

    def _creates(self) -> tuple[tuple[str, Any], ...]:
        return (
            ("create_item", lambda: self.client.create_item("p1", "W", "<p>b</p>", [])),
            ("create_comment", lambda: self.client.create_comment("p1", "i1", "<p>c</p>")),
        )

    def _stored(self, name: str) -> int:
        if name == "create_item":
            return len(self.server.items.get("p1", {}))
        return len(self.server.comments.get("i1", []))

    def _reset(self) -> None:
        self.server.requests.clear()
        self.server.items.clear()
        self.server.comments.clear()
        self.slept.clear()

    def test_server_error_is_not_retried(self) -> None:
        for name, call in self._creates():
            for status in (500, 502, 503, 504):
                with self.subTest(method=name, status=status):
                    self._reset()
                    self.server.status_queue = [status]
                    with self.assertRaisesRegex(PlaneUnavailable, f"HTTP {status}$"):
                        call()
                    self.assertEqual((len(self.server.requests), self.slept), (1, []))

    def test_lost_response_creates_exactly_once(self) -> None:
        for name, call in self._creates():
            with self.subTest(method=name):
                self._reset()
                self.server.drop_next_response = True  # 서버는 저장했지만 응답이 오지 않는다
                with self.assertRaisesRegex(PlaneUnavailable, "Plane 연결 실패"):
                    call()
                self.assertEqual((len(self.server.requests), self._stored(name), self.slept), (1, 1, []))

    def test_connection_errors_are_not_retried(self) -> None:
        errors = (urllib.error.URLError(ConnectionRefusedError("refused")), TimeoutError("timed out"))
        for name, call in self._creates():
            for error in errors:
                with self.subTest(method=name, error=type(error).__name__):
                    self._reset()
                    with mock.patch.object(self.client._opener, "open", side_effect=error) as opened:
                        with self.assertRaisesRegex(PlaneUnavailable, "Plane 연결 실패"):
                            call()
                    self.assertEqual((opened.call_count, self.slept), (1, []))

    def test_rate_limit_is_retried(self) -> None:
        for name, call in self._creates():
            with self.subTest(method=name):
                self._reset()
                self.server.status_queue = [429, 429]
                call()
                self.assertEqual((len(self.server.requests), self._stored(name), self.slept), (3, 1, [1.0, 1.0]))

    def test_rate_limit_retries_are_bounded_and_later_server_error_stops(self) -> None:
        for name, call in self._creates():
            with self.subTest(method=name, case="계속 429"):
                self._reset()
                self.server.status_queue = [429] * 4
                with self.assertRaisesRegex(PlaneUnavailable, "HTTP 429$"):
                    call()
                self.assertEqual(len(self.server.requests), 4)
            with self.subTest(method=name, case="429 뒤 500"):
                self._reset()
                self.server.status_queue = [429, 500]
                with self.assertRaisesRegex(PlaneUnavailable, "HTTP 500$"):
                    call()
                self.assertEqual(len(self.server.requests), 2)

    def test_reads_and_updates_still_retry_server_errors(self) -> None:
        for name, call in (
            ("list_comments", lambda: self.client.list_comments("p1", "i1")),
            ("update_item", lambda: self.client.update_item("p1", "i1", {"state": "s1"})),
        ):
            with self.subTest(method=name):
                self._reset()
                self.server.status_queue = [500, 200]
                call()
                self.assertEqual((len(self.server.requests), self.slept), (2, [1.0]))


class WhoamiIdTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = FakePlaneServer()
        self.addCleanup(self.server.close)
        self.client = PlaneClient(
            PlaneSettings(base_url=self.server.base_url, workspace="forge", token=TEST_TOKEN), sleep=lambda _: None
        )

    def test_returns_user_id_from_users_me(self) -> None:
        self.server.me = {"id": "u-42", "email": "tester@example.com"}
        self.assertEqual(self.client.whoami_id(), "u-42")
        self.assertEqual((self.server.requests[-1].method, self.server.requests[-1].path), ("GET", "/api/v1/users/me/"))
        self.assertEqual(self.client.whoami(), "tester@example.com")

    def test_missing_id_is_format_error(self) -> None:
        for me in ({"email": "tester@example.com"}, {"id": ""}, {"id": 7}):
            with self.subTest(me=me):
                self.server.me = dict(me)
                with self.assertRaisesRegex(PlaneError, "Plane 응답 형식 오류"):
                    self.client.whoami_id()

    def test_fake_server_and_fake_plane_use_comment_actor_id(self) -> None:
        comment = self.client.create_comment("p1", "i1", "<p>x</p>")
        self.assertEqual(self.client.whoami_id(), comment["actor"])
        fake = FakePlane()
        pid = fake.create_project("작업", "FL")["id"]
        item = fake.create_item(pid, "W", "", [])
        self.assertEqual(fake.whoami_id(), fake.create_comment(pid, item["id"], "<p>x</p>")["actor"])


class CommentTouchesItemTest(unittest.TestCase):
    """댓글을 달면 항목의 updated_at이 바뀐다. 이전 시각으로 하는 수정은 충돌이다."""

    def test_fake_server(self) -> None:
        server = FakePlaneServer()
        self.addCleanup(server.close)
        client = PlaneClient(
            PlaneSettings(base_url=server.base_url, workspace="forge", token=TEST_TOKEN), sleep=lambda _: None
        )
        item = client.create_item("p1", "W", "<p>b</p>", [])
        other = client.create_item("p1", "O", "<p>b</p>", [])
        comment = client.create_comment("p1", item["id"], "<p>c</p>")
        after = client.get_item("p1", item["id"])
        self.assertNotEqual(after["updated_at"], item["updated_at"])
        self.assertEqual(after["updated_at"], comment["created_at"])
        self.assertEqual(client.get_item("p1", other["id"])["updated_at"], other["updated_at"])
        with self.assertRaises(PlaneConflict):
            client.update_item_description("p1", item["id"], "<p>n</p>", expected_updated_at=item["updated_at"])
        client.update_item_description("p1", item["id"], "<p>n</p>", expected_updated_at=after["updated_at"])

    def test_fake_plane(self) -> None:
        fake = FakePlane()
        pid = fake.create_project("작업", "FL")["id"]
        item = fake.create_item(pid, "W", "<p>b</p>", [])
        other = fake.create_item(pid, "O", "<p>b</p>", [])
        comment = fake.create_comment(pid, item["id"], "<p>c</p>")
        after = fake.get_item(pid, item["id"])
        self.assertNotEqual(after["updated_at"], item["updated_at"])
        self.assertEqual(after["updated_at"], comment["created_at"])
        self.assertEqual(fake.get_item(pid, other["id"])["updated_at"], other["updated_at"])
        with self.assertRaises(PlaneConflict):
            fake.update_item_description(pid, item["id"], "<p>n</p>", expected_updated_at=item["updated_at"])
        fake.update_item_description(pid, item["id"], "<p>n</p>", expected_updated_at=after["updated_at"])


class WorkTrackerTest(unittest.TestCase):
    def test_protocol_members(self) -> None:
        self.assertEqual(
            _members(WorkTracker),
            {
                "whoami",
                "whoami_id",
                "resolve_code",
                "update_item",
                "list_comments",
                "create_comment",
                "list_states",
                "list_items",
                "list_projects",
                "list_labels",
                "create_label",
            },
        )

    def test_client_and_fake_provide_both_protocols(self) -> None:
        for protocol in (WorkTracker, PlaneLike):
            for implementation in (PlaneClient, FakePlane):
                with self.subTest(protocol=protocol.__name__, implementation=implementation.__name__):
                    missing = {n for n in _members(protocol) if not callable(getattr(implementation, n, None))}
                    self.assertEqual(missing, set())


class FakePlaneWorkTest(unittest.TestCase):
    """메모리 대역은 실제 클라이언트와 같은 동작을 해야 뒤 작업의 시험을 믿을 수 있다."""

    def setUp(self) -> None:
        self.fake = FakePlane()
        self.pid = self.fake.create_project("작업", "FL")["id"]

    def test_whoami_and_default_states(self) -> None:
        self.assertEqual(self.fake.whoami(), "tester@example.com")
        self.assertEqual(
            [state["group"] for state in self.fake.list_states(self.pid)],
            ["backlog", "unstarted", "started", "completed", "cancelled"],
        )
        self.fake.create_label(self.pid, "계획서", "#3b82f6")
        self.assertEqual([label["name"] for label in self.fake.list_labels(self.pid)], ["계획서"])

    def test_items_keep_parent_state_and_external_marks(self) -> None:
        parent = self.fake.create_item(self.pid, "W", "", [])
        child = self.fake.create_item(
            self.pid, "I", "", [], parent=parent["id"], state="s1", external_source="src", external_id="k"
        )
        found = self.fake.find_item_by_external(self.pid, "src", "k")
        assert found is not None
        self.assertEqual((found["id"], found["parent"], found["state"]), (child["id"], parent["id"], "s1"))
        self.assertIsNone(self.fake.find_item_by_external(self.pid, "src", "other"))
        self.assertIsNone(self.fake.find_item_by_external(self.pid, "other", "k"))
        self.assertEqual(self.fake.resolve_code("FL-2"), (self.pid, found))
        with self.assertRaisesRegex(PlaneError, "항목 없음: FL-9"):
            self.fake.resolve_code("FL-9")

    def test_update_item_and_comments(self) -> None:
        item = self.fake.create_item(self.pid, "W", "", [])
        updated = self.fake.update_item(self.pid, item["id"], {"assignees": ["u1"], "state": "s2"})
        self.assertEqual((updated["assignees"], updated["state"]), (["u1"], "s2"))
        self.assertNotEqual(updated["updated_at"], item["updated_at"])
        updated["assignees"].append("u2")  # 돌려준 사본을 고쳐도 저장된 값은 그대로다
        self.assertEqual(self.fake.get_item(self.pid, item["id"])["assignees"], ["u1"])
        with self.assertRaisesRegex(ValueError, "수정할 수 없는 필드"):
            self.fake.update_item(self.pid, item["id"], {"name": "x"})
        first = self.fake.create_comment(self.pid, item["id"], "<p>1</p>")
        second = self.fake.create_comment(self.pid, item["id"], "<p>2</p>")
        listed = self.fake.list_comments(self.pid, item["id"])
        self.assertEqual([c["id"] for c in listed], [first["id"], second["id"]])
        self.assertLess(listed[0]["created_at"], listed[1]["created_at"])
        self.assertTrue(listed[0]["actor"])
        with self.assertRaisesRegex(PlaneError, "item not found"):
            self.fake.create_comment(self.pid, "missing", "<p>x</p>")

    def test_fail_on_applies_to_new_methods(self) -> None:
        self.fake.fail_on["create_comment"] = PlaneUnavailable("down")
        with self.assertRaises(PlaneUnavailable):
            self.fake.create_comment(self.pid, "any", "<p>x</p>")
