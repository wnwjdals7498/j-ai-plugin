from __future__ import annotations

import json
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from typing import Optional

from ledger.hubstore import (
    DB_NAME,
    REGISTRY_NAME,
    SCHEMA_VERSION,
    EntityRow,
    HubStore,
    load_registry,
    save_registry,
    url_digest,
)

T1 = "2026-11-25T00:00:00Z"
T2 = "2026-11-25T00:10:00Z"


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _user_version(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def _issue(number: str, state: str = "open") -> EntityRow:
    return EntityRow(kind="issue", ext_key=number, state=state, attrs={"number": int(number), "title": "제목"})


class _StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state_dir = Path(tmp.name) / "state"

    def _seed(self, store: HubStore, name: str = "repo") -> tuple[int, int]:
        source = store.source_id("gitea", "http://forge.invalid")
        repo = store.upsert_repo("owner", name, f"/work/{name}", "")
        return source, repo


class SchemaTest(_StoreCase):
    def test_creates_files_with_private_modes(self) -> None:
        store = HubStore(self.state_dir)
        self.assertEqual(store.path, self.state_dir / DB_NAME)
        self.assertEqual(_mode(self.state_dir), 0o700)
        self.assertEqual(_mode(store.path), 0o600)
        self.assertEqual(SCHEMA_VERSION, 1)
        self.assertEqual(_user_version(store.path), 1)

    def test_reopen_keeps_data(self) -> None:
        self._seed(HubStore(self.state_dir))
        self.assertEqual([r.name for r in HubStore(self.state_dir).repos()], ["repo"])

    def test_corrupt_db_is_rebuilt(self) -> None:
        self.state_dir.mkdir(mode=0o700)
        (self.state_dir / DB_NAME).write_bytes(b"not sqlite")
        store = HubStore(self.state_dir)
        self.assertEqual(store.repos(), [])
        self.assertEqual(_user_version(store.path), 1)
        self.assertEqual(_mode(store.path), 0o600)

    def test_schema_mismatch_rebuilds(self) -> None:
        self.state_dir.mkdir(mode=0o700)
        path = self.state_dir / DB_NAME
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE repo (old TEXT)")
        conn.execute("PRAGMA user_version = 99")
        conn.commit()
        conn.close()
        store = HubStore(self.state_dir)
        self.assertEqual(_user_version(path), 1)
        self.assertEqual(store.repos(), [])
        self.assertEqual(_mode(path), 0o600)

    def test_concurrent_read_during_write(self) -> None:
        store = HubStore(self.state_dir)
        source, repo = self._seed(store)
        store.apply_kind(source, repo, "issue", [_issue("1")], T1)
        writer = sqlite3.connect(store.path, isolation_level=None)
        self.addCleanup(writer.close)
        writer.execute("BEGIN EXCLUSIVE")
        writer.execute("UPDATE entity SET state = 'closed'")
        self.assertEqual([e.ext_key for e in HubStore(self.state_dir).entities(repo, "issue")], ["1"])
        writer.execute("ROLLBACK")

    def test_no_raw_url_stored(self) -> None:
        store = HubStore(self.state_dir)
        first = store.source_id("gitea", "http://h.example/")
        self.assertEqual(store.source_id("gitea", "http://h.example"), first)
        self.assertNotEqual(store.source_id("plane", "http://h.example"), first)
        for path in self.state_dir.iterdir():
            self.assertNotIn(b"h.example", path.read_bytes(), path.name)

    def test_url_digest(self) -> None:
        self.assertEqual(url_digest("http://h.example/"), url_digest("http://h.example"))
        self.assertRegex(url_digest("http://h.example"), r"\A[0-9a-f]{16}\Z")


class EntityTest(_StoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = HubStore(self.state_dir)
        self.source, self.repo = self._seed(self.store)

    def _apply(self, kind: str, rows: list[EntityRow], now: str = T1, repo: Optional[int] = None) -> None:
        self.store.apply_kind(self.source, self.repo if repo is None else repo, kind, rows, now)

    def _keys(self, kind: str, repo: Optional[int] = None) -> set[str]:
        return {e.ext_key for e in self.store.entities(self.repo if repo is None else repo, kind)}

    def test_apply_kind_upserts_and_closes_missing(self) -> None:
        self._apply("issue", [_issue("1"), _issue("2")])
        changed = EntityRow("issue", "2", "open", {"number": 2, "title": "새 제목"})
        self._apply("issue", [changed, _issue("3")], T2)
        self.assertEqual(self._keys("issue"), {"2", "3"})
        closed = self.store.entities(self.repo, "issue", states=("closed",))
        self.assertEqual([e.ext_key for e in closed], ["1"])
        everything = {e.ext_key: e for e in self.store.entities(self.repo, "issue", states=None)}
        self.assertEqual(set(everything), {"1", "2", "3"})
        self.assertEqual(everything["2"].attrs, {"number": 2, "title": "새 제목"})
        self.assertIsNone(everything["2"].parent_ext_key)

    def test_apply_kind_empty_rows_closes_all_of_that_kind_only(self) -> None:
        self._apply("issue", [_issue("1")])
        self._apply("pull", [EntityRow("pull", "1", "open", {})])
        self._apply("issue", [], T2)
        self.assertEqual(self._keys("issue"), set())
        self.assertEqual(self._keys("pull"), {"1"})

    def test_apply_kind_close_missing_false_keeps_rows(self) -> None:
        self.store.apply_kind(self.source, self.repo, "ci_run", [EntityRow("ci_run", "10", "open", {})], T1, False)
        self.store.apply_kind(self.source, self.repo, "ci_run", [EntityRow("ci_run", "11", "open", {})], T2, False)
        self.assertEqual(self._keys("ci_run"), {"10", "11"})

    def test_same_ext_key_in_two_repos_stays_separate(self) -> None:
        other = self.store.upsert_repo("owner", "other", "/work/other", "")
        self._apply("issue", [_issue("1")])
        self._apply("issue", [_issue("1")], repo=other)
        self._apply("issue", [], T2, repo=other)
        self.assertEqual(self._keys("issue"), {"1"})
        self.assertEqual(self._keys("issue", other), set())

    def test_parent_link(self) -> None:
        self._apply("plane_project", [EntityRow("plane_project", "HUB", "open", {"key": "HUB"})])
        self._apply("cycle", [EntityRow("cycle", "c1", "open", {}, parent=("plane_project", "HUB"))])
        self.assertEqual([e.parent_ext_key for e in self.store.entities(self.repo, "cycle")], ["HUB"])

    def test_failed_apply_changes_nothing(self) -> None:
        self._apply("cycle", [EntityRow("cycle", "c1", "open", {})])
        orphan = EntityRow("cycle", "c2", "open", {}, parent=("plane_project", "NOPE"))
        with self.assertRaises(ValueError):
            self._apply("cycle", [EntityRow("cycle", "c3", "open", {}), orphan], T2)
        self.assertEqual(self._keys("cycle"), {"c1"})

    def test_row_of_other_kind_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._apply("pull", [_issue("1")])
        self.assertEqual(self.store.entities(self.repo, "issue", states=None), [])


class RepoTest(_StoreCase):
    def test_upsert_repo_merges_clones_by_owner_and_name(self) -> None:
        store = HubStore(self.state_dir)
        first = store.upsert_repo("owner", "repo", "/b/repo", "")
        self.assertEqual(store.upsert_repo("owner", "repo", "/a/repo", "ws"), first)
        self.assertEqual(store.upsert_repo("owner", "repo", "/a/repo", "ws"), first)
        store.set_repo_meta(first, "main", "http://forge.invalid/owner/repo")
        [row] = store.repos()
        self.assertEqual(
            (row.id, row.owner, row.name, row.default_branch, row.html_url, row.local_paths, row.plane_workspace),
            (first, "owner", "repo", "main", "http://forge.invalid/owner/repo", ("/a/repo", "/b/repo"), "ws"),
        )
        self.assertEqual(store.find_repo_by_path("/b/repo"), row)
        self.assertIsNone(store.find_repo_by_path("/b"))


class RunTest(_StoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = HubStore(self.state_dir)
        self.source, self.repo = self._seed(self.store)

    def _run(self, result: str, error_kind: str = "") -> None:
        run_id = self.store.start_run(self.source, self.repo, T1)
        self.store.finish_run(run_id, result, 3, error_kind, T2)

    def test_last_good_run_skips_failed(self) -> None:
        self._run("ok")
        self._run("failed", "network")
        last = self.store.last_run(self.repo, "gitea")
        good = self.store.last_good_run(self.repo, "gitea")
        assert last is not None and good is not None
        self.assertEqual((last.result, last.error_kind, last.api_calls), ("failed", "network", 3))
        self.assertEqual((good.result, good.started_at, good.finished_at), ("ok", T1, T2))
        self.assertEqual((good.source_kind, good.repo_id), ("gitea", self.repo))

    def test_partial_counts_as_good_and_source_kinds_are_separate(self) -> None:
        self._run("ok")
        self._run("partial")
        self._run("skipped")
        good = self.store.last_good_run(self.repo, "gitea")
        assert good is not None
        self.assertEqual(good.result, "partial")
        self.assertIsNone(self.store.last_run(self.repo, "plane"))
        self.assertIsNone(self.store.last_good_run(self.repo, "plane"))

    def test_unfinished_run(self) -> None:
        self.store.start_run(self.source, self.repo, T1)
        last = self.store.last_run(self.repo, "gitea")
        assert last is not None
        self.assertEqual((last.finished_at, last.result, last.api_calls, last.error_kind), (None, "", 0, ""))
        self.assertIsNone(self.store.last_good_run(self.repo, "gitea"))


class RegistryTest(_StoreCase):
    def test_registry_roundtrip_and_mode(self) -> None:
        self.assertEqual(load_registry(self.state_dir), [])
        save_registry(self.state_dir, ["/b", "/a", "/b"])
        path = self.state_dir / REGISTRY_NAME
        self.assertEqual(load_registry(self.state_dir), ["/a", "/b"])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"version": 1, "paths": ["/a", "/b"]})
        self.assertEqual(_mode(path), 0o600)
        self.assertEqual(_mode(self.state_dir), 0o700)
        save_registry(self.state_dir, ["/c"])
        self.assertEqual(load_registry(self.state_dir), ["/c"])
        self.assertEqual(_mode(path), 0o600)
        self.assertEqual([p.name for p in self.state_dir.iterdir()], [REGISTRY_NAME])

    def test_broken_registry_reads_as_empty(self) -> None:
        self.state_dir.mkdir(mode=0o700)
        for content in (b"{", b"\xff", b"[]", b'{"version": 2, "paths": ["/a"]}', b'{"version": 1, "paths": [1]}'):
            (self.state_dir / REGISTRY_NAME).write_bytes(content)
            self.assertEqual(load_registry(self.state_dir), [], content)


if __name__ == "__main__":
    unittest.main()
