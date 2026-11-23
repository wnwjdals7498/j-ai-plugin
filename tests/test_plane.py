from __future__ import annotations

import http.client
import socket
import unittest
import urllib.error
from unittest import mock

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

        self.server.status_queue = [503, 429, 200]
        self.server.pages = [{"results": [{"id": "ok"}], "next_cursor": None, "next_page_results": False}]
        self.assertEqual(client.list_projects(), [{"id": "ok"}])
        self.assertEqual(slept, [1.0, 2.0])

        slept.clear()
        self.server.force_status = 429
        with self.assertRaises(PlaneUnavailable):
            client.list_projects()
        self.assertEqual(slept, [1.0, 2.0, 4.0])

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
