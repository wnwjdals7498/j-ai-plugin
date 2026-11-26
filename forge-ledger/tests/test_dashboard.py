from __future__ import annotations

import io
import json
import os
import re
import select
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional
from unittest.mock import patch

from ledger import dashboard, hubcli
from ledger.cli import main
from ledger.dashboard import DashboardApp, make_server, serve
from ledger.hubstore import HubStore, save_registry

_PLUGIN = Path(__file__).resolve().parent.parent / "plugins" / "forge-ledger"
_STATIC = _PLUGIN / "dashboard"
_NOW = datetime(2026, 11, 25, 9, 0, tzinfo=timezone.utc)
_TOKEN = "tok" + "d" * 30
_WARNING = "[!] 사내망에 열린 주소입니다. 접속 토큰을 공유하지 마세요"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _bearer(token: str = _TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class DashboardServerTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = HubStore(Path(tmp.name) / "state")
        self.store.upsert_repo("o", "n", str(Path(tmp.name) / "work"), "forge")
        self.logs = io.StringIO()
        redirect = redirect_stderr(self.logs)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        self.bodies: list[bytes] = []
        app = DashboardApp(self.store, _TOKEN, 600, _STATIC, lambda: _NOW)
        server = make_server(app, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]

    def _request(
        self, path: str, headers: Optional[Mapping[str, str]] = None, method: str = "GET"
    ) -> tuple[int, dict[str, str], bytes]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", headers=dict(headers or {}), method=method
        )
        try:
            with _OPENER.open(request, timeout=5) as response:
                status, found, body = response.status, response.headers, response.read()
        except urllib.error.HTTPError as exc:
            with exc:
                status, found, body = exc.code, exc.headers, exc.read()
        self.bodies.append(body)
        return status, {key.lower(): value for key, value in found.items()}, body

    def _raw(self, request_line: str) -> bytes:
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as conn:
            conn.sendall(f"{request_line}\r\n\r\n".encode("latin-1"))
            chunks = []
            while chunk := conn.recv(4096):
                chunks.append(chunk)
        self.bodies.append(b"".join(chunks))
        return b"".join(chunks)

    def _assert_security_headers(self, headers: Mapping[str, str]) -> None:
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertEqual(headers["content-security-policy"], "default-src 'self'")

    def test_requires_token(self) -> None:
        for path in ("/", "/static/app.js", "/api/v1/summary", "/api/v1/health", "/api/v1/repos/o/n", "/nope"):
            self.assertEqual(self._request(path)[0], 401, path)
        wrong = (
            _bearer("tok" + "x" * 30),
            _bearer(""),
            _bearer("\xe9"),  # ASCII가 아닌 값도 예외 없이 거절한다
            {"Authorization": _TOKEN},
            {"Cookie": "fl_dash=wrong"},
        )
        for headers in wrong:
            self.assertEqual(self._request("/api/v1/summary", headers)[0], 401, headers)
        for path in ("/?token=wrong", "/?token=", "/?token=%C3%A9", f"/api/v1/summary?token={_TOKEN}"):
            status, headers, _ = self._request(path)
            self.assertEqual(status, 401, path)
            self.assertNotIn("set-cookie", headers)
        status, _, body = self._request("/api/v1/summary", _bearer())
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual((data["interval_s"], [repo["repo"] for repo in data["repos"]]), (600, ["o/n"]))

    def test_token_query_sets_cookie_and_redirects(self) -> None:
        status, headers, _ = self._request(f"/?token={_TOKEN}")
        self.assertEqual((status, headers["location"]), (302, "/"))
        self.assertEqual(headers["set-cookie"], f"fl_dash={_TOKEN}; HttpOnly; SameSite=Strict; Path=/")
        self._assert_security_headers(headers)
        # 127.0.0.1의 다른 앱이 남긴 이상한 쿠키가 섞여도 읽는다.
        cookie = {"Cookie": 'broken={"a": 1}; ' + headers["set-cookie"].split(";")[0]}
        self.assertEqual(self._request("/api/v1/summary", cookie)[0], 200)
        self.assertEqual(self._request("/", cookie)[0], 200)

    def test_serves_page_and_static_files(self) -> None:
        for path, name, content_type in (
            ("/", "index.html", "text/html; charset=utf-8"),
            ("/static/app.js", "app.js", "text/javascript; charset=utf-8"),
            ("/static/style.css", "style.css", "text/css; charset=utf-8"),
        ):
            status, headers, body = self._request(path, _bearer())
            self.assertEqual((status, headers["content-type"]), (200, content_type), path)
            self.assertEqual(body, (_STATIC / name).read_bytes())
            self._assert_security_headers(headers)
        self.assertIn("미분류", self._request("/static/app.js", _bearer())[2].decode("utf-8"))

    def test_index_has_no_inline_script_or_style(self) -> None:
        # CSP `default-src 'self'`는 인라인 스크립트와 스타일을 막는다.
        page = (_STATIC / "index.html").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"<script(?![^>]*\ssrc=)|<style|\sstyle=|\son\w+=", page), [])

    def test_methods_and_unknown_paths(self) -> None:
        for method in ("POST", "PUT", "DELETE", "HEAD", "OPTIONS", "BREW"):
            status, headers, _ = self._request("/api/v1/summary", _bearer(), method)
            self.assertEqual((status, headers["allow"]), (405, "GET"), method)
            self._assert_security_headers(headers)
        for path in (
            "/static/../hubstore.py",
            "/static/%2e%2e/hubstore.py",
            "/static/index.html",
            "/static/",
            "/nope",
            "/api/v1/summary/",
            "/api/v1/repos/o",
            "/api/v1/repos/o/n/extra",
        ):
            status, headers, _ = self._request(path, _bearer())
            self.assertEqual(status, 404, path)
            self._assert_security_headers(headers)

    def test_api_is_json_with_nosniff(self) -> None:
        for path, status in (
            ("/api/v1/summary", 200),
            ("/api/v1/health", 200),
            ("/api/v1/repos/o/n", 200),
            ("/api/v1/repos/o/none", 404),
        ):
            got, headers, body = self._request(path, _bearer())
            self.assertEqual((got, headers["content-type"]), (status, "application/json; charset=utf-8"), path)
            self._assert_security_headers(headers)
            self.assertIsInstance(json.loads(body), dict)
        _, headers, _ = self._request("/api/v1/summary")
        self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
        self._assert_security_headers(headers)
        health = json.loads(self._request("/api/v1/health", _bearer())[2])
        self.assertEqual([item["source"] for item in health["sources"]], ["gitea", "plane"])
        repo = json.loads(self._request("/api/v1/repos/o/n", _bearer())[2])
        self.assertEqual((repo["repo"], repo["sources"]["gitea"]["state"]), ("o/n", "never"))

    def test_worklog_api(self) -> None:
        from ledger.hubstore import EntityRow, WORKSPACE_REPO_ID

        source = self.store.source_id("worklog", "")
        attrs = {
            "at": "2026-11-25T09:00:00+00:00",
            "date": "2026-11-25",
            "month": "2026-11",
            "author": "작성자",
            "host": "host",
            "source": "manual",
            "repo": "",
            "item": "",
            "text": "본문",
        }
        self.store.apply_kind(source, WORKSPACE_REPO_ID, "worklog", [EntityRow("worklog", "one", "open", attrs)], "now")
        self.assertEqual(self._request("/api/v1/worklog")[0], 401)
        status, _, body = self._request("/api/v1/worklog?from=2026-11-01&to=2026-11-25", _bearer())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["entries"], [attrs])
        self.assertEqual(json.loads(self._request("/api/v1/worklog?user=other", _bearer())[2])["entries"], [])
        for query in ("from=2026-09-24&to=2026-11-25", "from=bad", "from=2026-11-26&to=2026-11-25"):
            self.assertEqual(self._request("/api/v1/worklog?" + query, _bearer())[0], 400)

    def test_repo_endpoint_404_for_unknown(self) -> None:
        for path in ("/api/v1/repos/o/none", "/api/v1/repos/none/n"):
            self.assertEqual(self._request(path, _bearer())[0], 404, path)

    def test_store_error_is_500_without_detail(self) -> None:
        with patch.object(HubStore, "repos", side_effect=OSError(f"secret {_TOKEN}")):
            status, headers, body = self._request("/api/v1/summary", _bearer())
        self.assertEqual((status, json.loads(body)), (500, {"error": "Internal Server Error"}))
        self._assert_security_headers(headers)
        self.assertIn('"error_kind": "OSError"', self.logs.getvalue())
        self.assertNotIn(_TOKEN, self.logs.getvalue())

    def test_token_not_in_logs_or_responses(self) -> None:
        self._request(f"/?token={_TOKEN}")
        self._request(f"/nope?token={_TOKEN}", _bearer())
        self._request(f"/?token={_TOKEN}", method="POST")
        self._request("/api/v1/summary", _bearer())
        self._request("/", _bearer())
        for line in (
            f"GET /?token={_TOKEN} extra HTTP/1.1",  # 형식이 틀린 요청 줄
            f"GET /?token={_TOKEN} HTTP/x",
            f"POST /?token={_TOKEN}",
        ):
            self.assertIn(b"X-Content-Type-Options: nosniff", self._raw(line), line.split()[-1])
        logs = [json.loads(line) for line in self.logs.getvalue().splitlines()]
        self.assertEqual(len(logs), 8)
        self.assertEqual({line["component"] for line in logs}, {"dashboard"})
        self.assertEqual(
            [(line["method"], line["path"], line["status"]) for line in logs[:3]],
            [("GET", "/", 302), ("GET", "/nope", 404), ("POST", "/", 405)],
        )
        self.assertNotIn(_TOKEN, self.logs.getvalue())
        for body in self.bodies:
            self.assertNotIn(_TOKEN.encode(), body)


class DashboardScriptTest(unittest.TestCase):
    def test_app_js_never_uses_html_sinks(self) -> None:
        source = (_STATIC / "app.js").read_text(encoding="utf-8")
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(sink, source)


class ServeTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.app = DashboardApp(HubStore(Path(tmp.name)), _TOKEN, 600, _STATIC, lambda: _NOW)

    def test_sync_loop_runs_and_survives_errors(self) -> None:
        stop = threading.Event()
        calls: list[int] = []

        def sync_once() -> None:
            calls.append(len(calls))
            if len(calls) == 1:
                raise ValueError(f"secret {_TOKEN}")
            if len(calls) == 3:
                stop.set()

        out, err = io.StringIO(), io.StringIO()
        guard = threading.Timer(5, stop.set)  # 반복이 멈춰도 시험이 끝나게 한다
        guard.start()
        self.addCleanup(guard.cancel)
        with redirect_stderr(err):
            code = serve(self.app, "127.0.0.1", 0, sync_once, out, stop, wait_s=0.01)
        self.assertEqual(code, 0)
        self.assertGreaterEqual(len(calls), 3)
        self.assertRegex(out.getvalue(), rf"\A대시보드: http://127\.0\.0\.1:[1-9]\d*/\?token={_TOKEN}\n\Z")
        logs = [json.loads(line) for line in err.getvalue().splitlines()]
        self.assertEqual(
            [(line["component"], line["result"], line["error_kind"]) for line in logs],
            [("dashboard", "failed", "ValueError")],
        )
        self.assertNotIn(_TOKEN, err.getvalue())

    def test_serve_warns_on_non_loopback(self) -> None:
        stop = threading.Event()
        stop.set()
        out = io.StringIO()
        real = dashboard.make_server
        # 주소 문구만 확인하고 실제로는 127.0.0.1에만 연다.
        with patch.object(dashboard, "make_server", lambda app, bind, port: real(app, "127.0.0.1", port)):
            serve(self.app, "0.0.0.0", 0, lambda: None, out, stop)
        lines = out.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("대시보드: http://0.0.0.0:"))
        self.assertEqual(lines[1:], [_WARNING])


class DashboardCliTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.env = {"FORGE_LEDGER_STATE_DIR": str(self.tmp / "state")}

    def _cli(self, *args: str, env: Optional[Mapping[str, str]] = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(args), env=self.env if env is None else env)
        return code, out.getvalue(), err.getvalue()

    def test_dashboard_interval_below_minimum(self) -> None:
        with patch.object(hubcli, "serve", side_effect=AssertionError("서버를 띄우면 안 된다")):
            code, out, err = self._cli("dashboard", "--interval", "30")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("--interval은 60초 이상", err)

    def test_dashboard_port_out_of_range(self) -> None:
        with patch.object(hubcli, "serve", side_effect=AssertionError("서버를 띄우면 안 된다")):
            for port in ("-1", "65536"):
                code, out, err = self._cli("dashboard", "--port", port)
                self.assertEqual((code, out), (1, ""), port)
                self.assertIn("--port는 0~65535", err)

    def test_dashboard_options(self) -> None:
        calls: list[tuple[DashboardApp, str, int]] = []

        def fake_serve(app: DashboardApp, bind: str, port: int, *_: Any) -> int:
            calls.append((app, bind, port))
            return 0

        with patch.object(hubcli, "serve", fake_serve):
            self.assertEqual(self._cli("dashboard")[0], 0)
            env = {**self.env, "FORGE_LEDGER_DASHBOARD_BIND": "localhost"}
            self.assertEqual(self._cli("dashboard", env=env)[0], 0)
            args = ("dashboard", "--bind", "127.0.0.1", "--port", "8765", "--interval", "60")
            self.assertEqual(self._cli(*args, env=env)[0], 0)
        self.assertEqual(
            [(bind, port, app.interval_s) for app, bind, port in calls],
            [("127.0.0.1", 0, 600), ("localhost", 0, 600), ("127.0.0.1", 8765, 60)],
        )
        tokens = [app.token for app, _, _ in calls]
        self.assertEqual(len(set(tokens)), 3)
        self.assertTrue(all(len(token) >= 43 for token in tokens))  # token_urlsafe(32)
        self.assertEqual(calls[0][0].static_dir, _STATIC)

    def test_dashboard_sync_once_syncs_registered_paths_under_lock(self) -> None:
        state_dir = self.tmp / "state"
        work = self.tmp / "work"
        work.mkdir()
        for command in (["git", "init", "-q"], ["git", "remote", "add", "origin", "git@host:o/n.git"]):
            subprocess.run(command, cwd=work, check=True, capture_output=True)
        save_registry(state_dir, [str(work)])
        seen: list[tuple[int, int]] = []

        def fake_serve(app: DashboardApp, bind: str, port: int, sync_once: Any, out: Any, stop: threading.Event) -> int:
            with hubcli.sync_lock(state_dir) as acquired:
                self.assertTrue(acquired)
                sync_once()  # 다른 대조가 도는 동안에는 건너뛴다
            before = len(app.store.repos())
            sync_once()
            seen.append((before, len(app.store.repos())))
            return 0

        with patch.object(hubcli, "serve", fake_serve):
            code, out, err = self._cli("dashboard")
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(seen, [(0, 1)])
        logs = [json.loads(line) for line in err.splitlines()]
        self.assertEqual([(line["source"], line["result"]) for line in logs], [("gitea", "skipped")])

    def test_script_prints_address_through_pipe(self) -> None:
        # 백그라운드로 실행해도 접속 주소가 버퍼에 머물지 않아야 한다.
        proc = subprocess.Popen(
            [sys.executable, str(_PLUGIN / "scripts" / "ledger.py"), "dashboard"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={"PATH": os.environ.get("PATH", ""), **self.env},
        )
        assert proc.stdout is not None
        self.addCleanup(proc.stdout.close)
        self.addCleanup(proc.wait, 5)
        self.addCleanup(proc.kill)
        self.assertTrue(select.select([proc.stdout], [], [], 10)[0], "접속 주소가 나오지 않았다")
        line = os.read(proc.stdout.fileno(), 4096).decode("utf-8")
        match = re.match(r"대시보드: (http://127\.0\.0\.1:\d+)/\?token=(\S+)\n", line)
        assert match is not None, line
        request = urllib.request.Request(f"{match[1]}/api/v1/summary", headers=_bearer(match[2]))
        with _OPENER.open(request, timeout=5) as response:
            self.assertEqual(json.loads(response.read())["repos"], [])


if __name__ == "__main__":
    unittest.main()


class WorklogIntegrationTest(unittest.TestCase):
    def test_sync_marks_only_successful_full_collection(self) -> None:
        from ledger.hubsync import SyncOutcome

        with tempfile.TemporaryDirectory() as tmp:
            env = {"FORGE_LEDGER_STATE_DIR": tmp}
            store = HubStore(Path(tmp))
            fake_pm = object()
            with (
                patch.object(hubcli, "sync_paths", return_value=[]),
                patch.object(hubcli, "make_worklog_store", return_value=fake_pm),
                patch.object(hubcli, "sync_worklog") as sync,
            ):
                sync.return_value = SyncOutcome("workspace", "worklog", "failed", 0, "error")
                self.assertEqual(hubcli._sync(store, [], env, lambda: _NOW, io.StringIO()), [])
                self.assertFalse((Path(tmp) / "hub-worklog.json").exists())
                sync.return_value = SyncOutcome("workspace", "worklog", "ok", 0, "")
                hubcli._sync(store, [], env, lambda: _NOW, io.StringIO())
                self.assertTrue((Path(tmp) / "hub-worklog.json").exists())
                self.assertTrue(sync.call_args.args[-1])
                hubcli._sync(store, [], env, lambda: _NOW, io.StringIO())
                self.assertFalse(sync.call_args.args[-1])
