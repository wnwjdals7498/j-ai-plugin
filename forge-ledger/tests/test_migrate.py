from __future__ import annotations

import base64
import copy
import io
import json
import tempfile
import time
import unittest
import zlib
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ledger.cli import main
from ledger.migrate import (
    ApplyPlan,
    ManifestError,
    Problem,
    check_manifest,
    load_manifest,
    manifest_path,
    render_preview,
    resolve_source_dir,
)
from ledger.secrets import find_exposures

_P = "pmt:forge-wiki-plugin"
_WORK = f"{_P}/_default/Work5"
_ITEM = f"{_P}/_default/Work5-1"
_COMMENT = f"{_ITEM}#c1"
_BACKLOG = f"{_P}/B3"
_D5 = f"{_P}/D5"
_D9 = f"{_P}/D9"
_FACT = f"{_P}/F1"
_RESOURCE = f"{_P}/resources/derived/a.drawio"
_PROJECT = f"{_P}/project"

_CLEAN_DRAWIO = '<mxfile><diagram name="p1"><mxGraphModel><root/></mxGraphModel></diagram></mxfile>\n'


def _deflate(data: bytes) -> bytes:
    compressor = zlib.compressobj(wbits=-15)
    return compressor.compress(data) + compressor.flush()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _packed(xml: str) -> str:
    """draw.io의 압축 본문: URL 인코딩 → raw deflate → base64."""
    return _b64(_deflate(quote(xml, safe="").encode("ascii")))


def _model(label: str) -> str:
    return f'<mxGraphModel><root><mxCell id="1" value="{label}"/></root></mxGraphModel>'


def _mxfile(*bodies: str) -> str:
    return "<mxfile>" + "".join(f'<diagram id="d{i}">{body}</diagram>' for i, body in enumerate(bodies)) + "</mxfile>"


# 브리프의 매니페스트 JSON v1 예시 그대로. 예시의 D5가 가리키는 D9만 시험에서 더한다.
_CONTRACT = """
{"version": 1, "slug": "forge-wiki-plugin", "source_dir": "~/docs/projects/forge-wiki-plugin",
 "records": [
  {"key": "pmt:forge-wiki-plugin/project", "kind": "project", "body_md": "...", "redactions": []},
  {"key": "pmt:forge-wiki-plugin/_default/Work5", "kind": "work", "name": "...", "status": "Done", "labels": ["work"], "description_html": "...", "link_to": null, "redactions": []},
  {"key": "pmt:forge-wiki-plugin/_default/Work5-1", "kind": "item", "parent": "pmt:forge-wiki-plugin/_default/Work5", "name": "...", "status": "Done", "labels": ["kind:job"], "blocked_by": [], "description_html": "...", "link_to": null, "redactions": ["홈 경로 → ~"]},
  {"key": "pmt:forge-wiki-plugin/_default/Work5-1#c1", "kind": "comment", "item": "pmt:forge-wiki-plugin/_default/Work5-1", "at": "2026-11-25T17:20", "body_html": "...", "redactions": []},
  {"key": "pmt:forge-wiki-plugin/B3", "kind": "backlog", "name": "...", "status": "Active", "labels": ["backlog", "kind:req"], "description_html": "...", "link_to": null, "redactions": []},
  {"key": "pmt:forge-wiki-plugin/D5", "kind": "decision", "date": "2026-11-23", "status": "대체", "title": "...", "detail": "...", "replaced_by": "pmt:forge-wiki-plugin/D9", "redactions": []},
  {"key": "pmt:forge-wiki-plugin/F1", "kind": "fact", "date": "2026-11-23", "status": "Active", "text": "...", "ref": "...", "redactions": []},
  {"key": "pmt:forge-wiki-plugin/resources/derived/a.drawio", "kind": "resource", "path": "derived/a.drawio", "redactions": []}
 ]}
"""  # noqa: E501


def _sample() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(_CONTRACT)
    data["records"].append(
        {
            "key": _D9,
            "kind": "decision",
            "date": "2026-11-24",
            "status": "승인",
            "title": "새 결정",
            "detail": "...",
            "redactions": [],
        }
    )
    return data


def _record(data: dict[str, Any], key: str) -> dict[str, Any]:
    return next(r for r in data["records"] if r["key"] == key)


def _item(key: str, parent: str, **changes: Any) -> dict[str, Any]:
    record = copy.deepcopy(_record(_sample(), _ITEM))
    record.update(key=key, parent=parent, redactions=[], **changes)
    return record


class _ManifestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.source = self.tmp / "docs" / "projects" / "forge-wiki-plugin"
        self.drawio = self.source / "resources" / "derived" / "a.drawio"
        self.drawio.parent.mkdir(parents=True)
        self.drawio.write_text(_CLEAN_DRAWIO, encoding="utf-8")

    def write(self, data: Any) -> Path:
        path = self.tmp / "manifest.json"
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        path.write_text(text, encoding="utf-8")
        return path

    def check(self, data: Any) -> list[Problem]:
        return check_manifest(load_manifest(self.write(data)), self.source)

    def assert_single(self, data: Any, key: str, fragment: str) -> str:
        problems = self.check(data)
        self.assertEqual([p.key for p in problems], [key], problems)
        self.assertIn(fragment, problems[0].message)
        return problems[0].message


class LoadTest(_ManifestCase):
    def test_contract_fields_are_kept_as_written(self) -> None:
        manifest = load_manifest(self.write(_sample()))
        self.assertEqual((manifest.version, manifest.slug), (1, "forge-wiki-plugin"))
        self.assertEqual(manifest.source_dir, "~/docs/projects/forge-wiki-plugin")
        self.assertEqual(list(manifest.records), _sample()["records"])

    def test_structural_errors_raise_without_echoing_content(self) -> None:
        cases = (
            "{not json secretish",
            "[1, 2]",
            '{"version": 1, "slug": "s", "source_dir": "x"}',
            '{"version": 1, "slug": "s", "source_dir": "x", "records": {}}',
            '{"version": 1, "slug": "s", "source_dir": "x", "records": ["secretish"]}',
            '{"version": 1, "slug": "s", "slug": "secretish", "source_dir": "x", "records": []}',
            "[" * 100000,
        )
        for text in cases:
            with self.subTest(text=text[:60]):
                with self.assertRaises(ManifestError) as ctx:
                    load_manifest(self.write(text))
                self.assertNotIn("secretish", str(ctx.exception))

    def test_deeply_nested_manifest_is_rejected_at_load(self) -> None:
        # Python 3.12부터는 JSON 해석기가 파이썬 재귀 한도보다 깊은 중첩도 읽는다.
        for depth in (30, 1200, 5000):
            with self.subTest(depth=depth):
                nested = "[" * depth + "]" * depth
                text = '{"version": 1, "slug": "s", "source_dir": "x", "records": [{"x": ' + nested + "}]}"
                with self.assertRaises(ManifestError):
                    load_manifest(self.write(text))

    def test_missing_or_non_utf8_file(self) -> None:
        with self.assertRaisesRegex(ManifestError, "읽을 수 없음"):
            load_manifest(self.tmp / "nope.json")
        path = self.tmp / "latin1.json"
        path.write_bytes(b"\xff\xfe{}")
        with self.assertRaisesRegex(ManifestError, "UTF-8"):
            load_manifest(path)


class CheckFormatTest(_ManifestCase):
    def test_sample_passes(self) -> None:
        self.assertEqual(self.check(_sample()), [])

    def test_version_must_be_integer_one(self) -> None:
        for version in (2, "1", True, 1.0, None):
            with self.subTest(version=version):
                data = _sample()
                data["version"] = version
                self.assert_single(data, "manifest", "version")

    def test_bad_slug_and_unknown_top_level_field(self) -> None:
        data = _sample()
        data["extra"] = 1
        self.assert_single(data, "manifest", "알 수 없는 필드")
        data = _sample()
        data["slug"] = "../up"
        self.assertIn(Problem("manifest", "slug 형식 오류"), self.check(data))

    def test_duplicate_key(self) -> None:
        data = _sample()
        data["records"].append(copy.deepcopy(_record(data, _FACT)))
        self.assert_single(data, _FACT, "키가 중복")

    def test_key_prefix(self) -> None:
        for key in ("pmt:other/F1", "pmt:forge-wiki-plugin/", "F1"):
            with self.subTest(key=key):
                data = _sample()
                _record(data, _FACT)["key"] = key
                self.assert_single(data, key, "pmt:<slug>/")

    def test_record_without_key_is_named_by_position(self) -> None:
        data = _sample()
        del data["records"][0]["key"]
        self.assert_single(data, "records[0]", "key")

    def test_unknown_kind_is_not_echoed(self) -> None:
        data = _sample()
        _record(data, _FACT)["kind"] = "mystery-kind"
        message = self.assert_single(data, _FACT, "kind")
        self.assertNotIn("mystery-kind", message)

    def test_allowed_status_per_kind(self) -> None:
        work_statuses = ("Planned", "In Progress", "Done", "Paused", "Failed", "Skipped")
        allowed = {
            _WORK: work_statuses,
            _ITEM: work_statuses,
            _BACKLOG: ("Active", "Done", "폐기"),
            _D9: ("승인", "대체", "폐기"),
            _FACT: ("Active", "폐기"),
        }
        every = set(work_statuses) | {"Active", "승인", "대체", "폐기", "done"}
        for key, statuses in allowed.items():
            for status in sorted(every):
                with self.subTest(key=key, status=status):
                    data = _sample()
                    _record(data, key)["status"] = status
                    if status in statuses:
                        self.assertEqual(self.check(data), [])
                    else:
                        self.assert_single(data, key, "status")

    def test_missing_and_mistyped_fields(self) -> None:
        cases: tuple[tuple[str, str, Any], ...] = (
            (_PROJECT, "body_md", None),
            (_WORK, "name", ""),
            (_WORK, "labels", "work"),
            (_ITEM, "blocked_by", [1]),
            (_ITEM, "redactions", None),
            (_COMMENT, "body_html", 3),
            (_BACKLOG, "link_to", 7),
            (_D9, "title", ""),
            (_FACT, "ref", None),
        )
        for key, name, value in cases:
            with self.subTest(key=key, field=name):
                data = _sample()
                _record(data, key)[name] = value
                self.assert_single(data, key, name)
        for key, name in ((_WORK, "description_html"), (_COMMENT, "at"), (_RESOURCE, "path"), (_FACT, "status")):
            with self.subTest(key=key, missing=name):
                data = _sample()
                del _record(data, key)[name]
                self.assert_single(data, key, name)

    def test_optional_link_to_and_replaced_by(self) -> None:
        data = _sample()
        del _record(data, _WORK)["link_to"]
        _record(data, _D9)["replaced_by"] = None
        self.assertEqual(self.check(data), [])

    def test_unknown_record_field_catches_typos(self) -> None:
        data = _sample()
        _record(data, _WORK)["linkto"] = "some-id"
        self.assert_single(data, _WORK, "알 수 없는 필드: linkto")

    def test_date_and_time_format(self) -> None:
        for key, name, value in (
            (_D9, "date", "2026-13-01"),
            (_D9, "date", "20261123"),
            (_FACT, "date", "2026-11-23T10:00"),
            (_COMMENT, "at", "yesterday"),
            (_COMMENT, "at", "2026-11-25"),
        ):
            with self.subTest(field=name, value=value):
                data = _sample()
                _record(data, key)[name] = value
                self.assert_single(data, key, name)


class CheckReferenceTest(_ManifestCase):
    def test_missing_targets(self) -> None:
        for key, name in ((_ITEM, "parent"), (_COMMENT, "item"), (_D5, "replaced_by")):
            with self.subTest(field=name):
                data = _sample()
                _record(data, key)[name] = f"{_P}/nowhere"
                self.assert_single(data, key, f"{name}가 가리키는 키가 없음")

    def test_wrong_target_kind(self) -> None:
        for key, name, target in (
            (_ITEM, "parent", _BACKLOG),
            (_COMMENT, "item", _D9),
            (_D5, "replaced_by", _FACT),
        ):
            with self.subTest(field=name):
                data = _sample()
                _record(data, key)[name] = target
                self.assert_single(data, key, f"{name}가 가리키는 레코드의 kind")

    def test_comment_may_target_work_or_backlog(self) -> None:
        for target in (_WORK, _BACKLOG):
            data = _sample()
            _record(data, _COMMENT)["item"] = target
            self.assertEqual(self.check(data), [])

    def test_link_to_is_a_plane_id_and_not_looked_up(self) -> None:
        data = _sample()
        _record(data, _WORK)["link_to"] = "7f9c1e2a-plane-item"
        self.assertEqual(self.check(data), [])

    def test_depth_up_to_three_below_work(self) -> None:
        data = _sample()
        data["records"] += [_item(f"{_ITEM}-1", _ITEM), _item(f"{_ITEM}-1-1", f"{_ITEM}-1")]
        self.assertEqual(self.check(data), [])
        data["records"].append(_item(f"{_ITEM}-1-1-1", f"{_ITEM}-1-1"))
        self.assert_single(data, f"{_ITEM}-1-1-1", "깊이")

    def test_parent_cycle(self) -> None:
        data = _sample()
        a, b = f"{_P}/a", f"{_P}/b"
        data["records"] += [_item(a, b), _item(b, a)]
        problems = self.check(data)
        self.assertEqual(sorted(p.key for p in problems), [a, b])
        self.assertTrue(all("순환" in p.message for p in problems))

    def test_broken_ancestor_is_reported_once(self) -> None:
        data = _sample()
        _record(data, _ITEM)["parent"] = f"{_P}/nowhere"
        data["records"].append(_item(f"{_ITEM}-1", _ITEM))
        self.assert_single(data, _ITEM, "parent가 가리키는 키가 없음")


class CheckExposureTest(_ManifestCase):
    def test_home_path_in_comment_names_key_and_pattern_only(self) -> None:
        data = _sample()
        _record(data, _COMMENT)["body_html"] = "<p>로그는 /home/alice/run.log 참고</p>"
        message = self.assert_single(data, _COMMENT, "home_path")
        self.assertNotIn("alice", message)
        self.assertIn("body_html", message)

    def test_every_string_field_is_scanned(self) -> None:
        cases: tuple[tuple[str, str, Any, str], ...] = (
            (_PROJECT, "body_md", "담당 a.b@corp.co.kr", "email"),
            (_WORK, "name", "서버 10.20.30.40", "ipv4"),
            (_WORK, "labels", ["work", "host:build.lan"], "internal_host"),
            (_WORK, "link_to", "/home/alice", "home_path"),
            (_ITEM, "blocked_by", ["/home/alice/x"], "home_path"),
            (_ITEM, "redactions", ["10.20.30.40 → 가림"], "ipv4"),
            (_BACKLOG, "description_html", "password: hunter2hunter2", "secret_assignment"),
            (_D9, "detail", "/Users/alice/notes", "home_path"),
            (_FACT, "ref", "https://10.20.30.40/x", "ipv4"),
        )
        for key, name, value, pattern in cases:
            with self.subTest(key=key, field=name):
                data = _sample()
                _record(data, key)[name] = value
                message = self.assert_single(data, key, pattern)
                self.assertIn(name, message)
                for leaked in ("alice", "corp.co.kr", "10.20.30.40", "build.lan", "hunter2hunter2"):
                    self.assertNotIn(leaked, message)

    def test_top_level_strings_and_unknown_fields_are_scanned(self) -> None:
        data = _sample()
        data["source_dir"] = "/home/alice/docs"
        message = self.assert_single(data, "manifest", "home_path")
        self.assertNotIn("alice", message)

        data = _sample()
        _record(data, _FACT)["extra"] = {"nested": ["10.20.30.40"]}
        messages = " ".join(p.message for p in self.check(data))
        self.assertIn("ipv4", messages)
        self.assertNotIn("10.20.30.40", messages)

    def test_value_in_key_or_field_name_is_never_printed(self) -> None:
        data = _sample()
        _record(data, _FACT)["key"] = f"{_P}//home/alice"
        data["records"][0]["/home/bob"] = "x"
        data["/home/carol"] = "x"
        problems = self.check(data)
        printed = " ".join(f"{p.key} {p.message}" for p in problems)
        for leaked in ("alice", "bob", "carol"):
            self.assertNotIn(leaked, printed)
        self.assertIn(f"records[{len(_sample()['records']) - 3}]", [p.key for p in problems])
        self.assertIn("home_path", printed)

    def test_reference_to_exposed_value_is_not_printed(self) -> None:
        data = _sample()
        _record(data, _ITEM)["parent"] = "/home/alice/parent"
        printed = " ".join(f"{p.key} {p.message}" for p in self.check(data))
        self.assertNotIn("alice", printed)


class CheckResourceTest(_ManifestCase):
    def _with_path(self, value: str) -> dict[str, Any]:
        data = _sample()
        _record(data, _RESOURCE)["path"] = value
        return data

    def test_unsafe_paths(self) -> None:
        (self.tmp / "outside.txt").write_text("ok\n", encoding="utf-8")
        for value in (
            "../outside.txt",
            "derived/../../outside.txt",
            "derived/..",
            str(self.tmp / "outside.txt"),
            "derived\\..\\a.drawio",
            "C:/data/a.drawio",
            "./derived/a.drawio",
            "derived//a.drawio",
        ):
            with self.subTest(path=value):
                problems = self.check(self._with_path(value))
                self.assertEqual({p.key for p in problems}, {_RESOURCE}, problems)
                self.assertTrue(any("안전하지 않은 resource 경로" in p.message for p in problems), problems)

    def test_missing_file_and_directory(self) -> None:
        for value in ("derived/none.drawio", "derived"):
            with self.subTest(path=value):
                self.assert_single(self._with_path(value), _RESOURCE, "파일이 없음")

    def test_symlink_leaving_resources_is_rejected(self) -> None:
        outside = self.tmp / "outside.txt"
        outside.write_text("ok\n", encoding="utf-8")
        (self.drawio.parent / "link.drawio").symlink_to(outside)
        self.assert_single(self._with_path("derived/link.drawio"), _RESOURCE, "resources 폴더 밖")

    def test_file_content_is_scanned_without_echo(self) -> None:
        self.drawio.write_text("<mxfile>\n/home/alice/a 10.20.30.40\n</mxfile>\n", encoding="utf-8")
        message = self.assert_single(_sample(), _RESOURCE, "home_path")
        self.assertIn("ipv4", message)
        self.assertNotIn("alice", message)
        self.assertNotIn("10.20.30.40", message)

    def test_unscannable_file_is_rejected(self) -> None:
        for content in (b"\x89PNG\r\n\x00\x00data", b"\xff\xfe broken"):
            with self.subTest(content=content[:4]):
                self.drawio.write_bytes(content)
                self.assert_single(_sample(), _RESOURCE, "검사할 수 없는 파일")


class CheckHtmlEntityTest(_ManifestCase):
    """HTML 필드는 저장된 글자와 화면에 보이는 글자가 다를 수 있다."""

    def test_entity_encoded_values_are_detected_without_echo(self) -> None:
        cases = (
            (_WORK, "description_html", "<p>&#47;home&#47;alice&#47;run.log</p>", "home_path"),
            (_ITEM, "description_html", "<p>&#x2F;home&#x2F;alice</p>", "home_path"),
            (_BACKLOG, "description_html", "<p>&sol;home&sol;alice</p>", "home_path"),
            (_COMMENT, "body_html", "<p>a.b&#64;corp.co.kr</p>", "email"),
            (_COMMENT, "body_html", "<p>10&#46;20&#46;30&#46;40</p>", "ipv4"),
            (_D9, "title", "&#47;Users&#47;alice", "home_path"),
            (_FACT, "text", "password: &quot;correct horse alice&quot;", "secret_assignment"),
        )
        for key, name, value, pattern in cases:
            with self.subTest(key=key, value=value):
                data = _sample()
                _record(data, key)[name] = value
                message = self.assert_single(data, key, pattern)
                self.assertIn(name, message)
                for leaked in ("alice", "corp.co.kr", "10.20.30.40", "&#"):
                    self.assertNotIn(leaked, message)

    def test_raw_text_is_still_scanned_and_names_are_not_repeated(self) -> None:
        data = _sample()
        _record(data, _COMMENT)["body_html"] = "<p>/home/alice &#47;home&#47;bob 10.20.30.40</p>"
        message = self.assert_single(data, _COMMENT, "home_path")
        self.assertEqual(message, "노출 패턴(ipv4, home_path): body_html")

    def test_escaped_markup_without_values_passes(self) -> None:
        data = _sample()
        _record(data, _COMMENT)["body_html"] = "<p>&lt;b&gt; a &amp; b &quot;c&quot; &#47;opt</p>"
        self.assertEqual(self.check(data), [])

    def test_entity_encoded_key_and_preview_are_masked(self) -> None:
        data = _sample()
        _record(data, _FACT)["key"] = f"{_P}/&#47;home&#47;alice"
        _record(data, _WORK).update(name="&#47;home&#47;bob", redactions=["&#47;home&#47;carol → ~"])
        problems = self.check(data)
        self.assertIn("records[6]", [p.key for p in problems])
        printed = " ".join(f"{p.key} {p.message}" for p in problems)
        printed += render_preview(load_manifest(self.write(data)), ApplyPlan())
        for leaked in ("alice", "bob", "carol"):
            self.assertNotIn(leaked, printed)


class CheckDrawioTest(_ManifestCase):
    def check_file(self, content: str, name: str = "a.drawio") -> list[Problem]:
        target = self.drawio.parent / name
        target.write_text(content, encoding="utf-8")
        data = _sample()
        _record(data, _RESOURCE)["path"] = f"derived/{name}"
        return self.check(data)

    def assert_found(self, content: str, pattern: str, name: str = "a.drawio") -> None:
        problems = self.check_file(content, name)
        self.assertEqual([p.key for p in problems], [_RESOURCE], problems)
        self.assertEqual(problems[0].message, f"노출 패턴({pattern}): resource 파일 내용")

    def assert_unscannable(self, content: str, name: str = "a.drawio") -> None:
        problems = self.check_file(content, name)
        self.assertEqual([p.key for p in problems], [_RESOURCE], problems)
        self.assertIn("검사할 수 없음", problems[0].message)
        self.assertNotIn("alice", problems[0].message)

    def test_compressed_body_is_decoded_and_scanned(self) -> None:
        content = _mxfile(_packed(_model("/home/alice/notes")))
        self.assertEqual(find_exposures(content), [])  # 풀지 않으면 보이지 않는다
        self.assert_found(content, "home_path")

    def test_clean_compressed_and_wrapped_base64_pass(self) -> None:
        body = _packed(_model("정상 라벨 & <b>"))
        wrapped = "\n  " + "\n  ".join(body[i : i + 20] for i in range(0, len(body), 20)) + "\n"
        for content in (_mxfile(body), _mxfile(wrapped), _mxfile(""), _mxfile("  \n "), ""):
            with self.subTest(content=content[:40]):
                self.assertEqual(self.check_file(content), [])

    def test_every_diagram_page_is_scanned(self) -> None:
        content = _mxfile(_model("ok"), _packed(_model("ok")), _packed(_model("10.20.30.40")))
        self.assert_found(content, "ipv4")

    def test_compressed_body_is_found_without_drawio_extension(self) -> None:
        mxfile = _mxfile(_packed(_model("/home/alice")))
        escaped = mxfile.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
        svg = (
            '<?xml version="1.0"?><!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "svg11.dtd">'
            f'<svg xmlns="http://www.w3.org/2000/svg" content="{escaped}"><g/></svg>'
        )
        for name, content in (("a.xml", mxfile), ("a.drawio.svg", svg), ("a.svg", svg), ("a.dio", mxfile)):
            with self.subTest(name=name):
                self.assert_found(content, "home_path", name)

    def test_many_leading_comments_do_not_stall_the_check(self) -> None:
        # 깨진 XML 앞의 주석들이 정규식의 역추적을 지수로 늘리면 안 된다.
        comments = "<!--a-->\n" * 26
        started = time.perf_counter()
        self.assert_unscannable(comments + "<mxfile>", "a.xml")
        self.assertEqual(self.check_file(comments + "<broken", "b.xml"), [])
        self.assertLess(time.perf_counter() - started, 2.0)

    def test_svg_content_in_unknown_form_is_a_problem(self) -> None:
        packed = _mxfile(_packed(_model("/home/alice")))
        for name in ("a.drawio.svg", "a.svg"):
            for label, content in (("base64", _b64(packed.encode())), ("URL 인코딩", quote(packed, safe=""))):
                with self.subTest(name=name, content=label):
                    svg = f'<svg xmlns="http://www.w3.org/2000/svg" content="{content}"/>'
                    self.assert_unscannable(svg, name)

    def test_svg_content_with_bare_graph_model_is_scanned(self) -> None:
        body = _packed(_model("/home/alice"))
        content = f"&lt;mxGraphModel&gt;&lt;diagram&gt;{body}&lt;/diagram&gt;&lt;/mxGraphModel&gt;"
        self.assert_found(f'<svg content="{content}"/>', "home_path", "a.svg")
        self.assertEqual(self.check_file('<svg content="  "><text>ok</text></svg>', "b.svg"), [])

    def test_cdata_and_text_after_child_are_decoded(self) -> None:
        body = _packed(_model("/home/alice"))
        for content in (_mxfile(f"<![CDATA[{body}]]>"), _mxfile(f"<mxGraphModel/>{body}")):
            with self.subTest(content=content[:40]):
                self.assert_found(content, "home_path")

    def test_corrupted_compressed_body_is_a_problem(self) -> None:
        stream = _deflate(quote(_model("/home/alice"), safe="").encode("ascii"))
        cases = {
            "base64가 아님": "%%% not base64 %%%",
            "deflate가 아님": _b64(b"plain bytes, not deflate"),
            "중간에 잘림": _b64(stream[: len(stream) // 2]),
            "뒤에 남는 데이터": _b64(stream + b"hidden /home/alice"),
            "UTF-8이 아님": _b64(_deflate(b"\xff\xfe")),
            "URL 인코딩이 UTF-8이 아님": _b64(_deflate(b"%FF%FE")),
            "너무 큼": _b64(_deflate(b"a" * (21 * 1024 * 1024))),
        }
        for label, body in cases.items():
            with self.subTest(case=label):
                self.assert_unscannable(_mxfile(body))

    def test_unparsable_drawio_is_a_problem(self) -> None:
        for name, content in (
            ("a.drawio", "<mxfile><diagram>"),
            ("a.drawio", "not xml at all"),
            ("a.xml", '<?xml version="1.0"?>\n<mxfile><diagram>'),
            ("a.drawio", '<!DOCTYPE m [<!ENTITY x "y">]><mxfile>&x;</mxfile>'),
        ):
            with self.subTest(name=name, content=content[:30]):
                self.assert_unscannable(content, name)

    def test_displayed_labels_are_decoded_in_plain_and_compressed_diagrams(self) -> None:
        once = "&#47;home&#47;alice"  # XML 문자 참조
        twice = "&amp;#47;home&amp;#47;alice"  # HTML 라벨 안의 문자 참조
        for label in (once, twice):
            for body in (_model(label), _packed(_model(label))):
                with self.subTest(label=label, body=body[:20]):
                    self.assert_found(_mxfile(body), "home_path")

    def test_other_files_with_diagram_words_are_not_treated_as_drawio(self) -> None:
        for name, content in (
            ("a.xml", "<report><diagram>흐름 설명</diagram></report>"),
            ("a.md", "draw.io는 `<mxfile><diagram>` 형식이다\n"),
            ("a.svg", '<svg xmlns="http://www.w3.org/2000/svg"><text>ok</text></svg>'),
        ):
            with self.subTest(name=name):
                self.assertEqual(self.check_file(content, name), [])

    def test_cli_reports_compressed_finding_without_value(self) -> None:
        self.drawio.write_text(_mxfile(_packed(_model("/home/alice/notes"))), encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = main(["migrate", "check", str(self.write(_sample()))], env={"HOME": str(self.tmp)})
        self.assertEqual(rc, 1)
        self.assertIn(f"{_RESOURCE}: 노출 패턴(home_path)", out.getvalue())
        self.assertNotIn("alice", out.getvalue() + err.getvalue())


class PreviewTest(_ManifestCase):
    def test_preview_has_counts_links_redactions_and_samples(self) -> None:
        data = _sample()
        _record(data, _WORK)["link_to"] = "plane-item-77"
        _record(data, _WORK)["name"] = "허브 골격"
        _record(data, _FACT)["text"] = "긴 사실 " + "가" * 200
        _record(data, _D9)["redactions"] = ["주소 → 환경 변수 이름"]
        plan = ApplyPlan(create=[_ITEM, _COMMENT, _BACKLOG], update=[_WORK], skip=[_FACT, _D9], conflict=[_D5])
        text = render_preview(load_manifest(self.write(data)), plan)
        lines = text.splitlines()
        for kind, count in (
            ("project", 1),
            ("work", 1),
            ("item", 1),
            ("comment", 1),
            ("backlog", 1),
            ("decision", 2),
            ("fact", 1),
            ("resource", 1),
        ):
            self.assertIn(f"- {kind}: {count}", lines)
        for line in ("- 새로 만들 것: 3", "- 바꿀 것: 1", "- 그대로 둘 것: 2", "- 충돌: 1", f"- {_D5}"):
            self.assertIn(line, lines)
        self.assertIn(f"- {_WORK} → plane-item-77", lines)
        self.assertIn(f"- {_ITEM}: 홈 경로 → ~", lines)
        self.assertIn(f"- {_D9}: 주소 → 환경 변수 이름", lines)
        self.assertIn(f"- [work] {_WORK}: 허브 골격", lines)
        self.assertLessEqual(max(len(line) for line in lines), 200)

    def test_preview_of_empty_plan_says_none(self) -> None:
        data = _sample()
        _record(data, _ITEM)["redactions"] = []
        text = render_preview(load_manifest(self.write(data)), ApplyPlan())
        self.assertIn("- 새로 만들 것: 0", text.splitlines())
        self.assertEqual(text.splitlines().count("- 없음"), 3)  # 연결, 치환, 충돌

    def test_preview_survives_mistyped_redactions_and_links(self) -> None:
        for value in (None, 0, 1.5, True, "secretish", {"a": 1}, [1, None]):
            with self.subTest(value=value):
                data = _sample()
                _record(data, _ITEM).update(redactions=value, link_to=value, name=value)
                text = render_preview(load_manifest(self.write(data)), ApplyPlan())
                self.assertIn("- item: 1", text.splitlines())
                self.assertNotIn(f"- {_ITEM}: s", text.splitlines())  # 글자 하나씩 늘어놓지 않는다

    def test_preview_never_prints_exposed_values(self) -> None:
        data = _sample()
        _record(data, _WORK).update(name="/home/alice", link_to="/home/bob", redactions=["/home/carol → ~"])
        _record(data, _FACT)["key"] = f"{_P}//home/dave"
        text = render_preview(load_manifest(self.write(data)), ApplyPlan(conflict=["/home/erin"]))
        for leaked in ("alice", "bob", "carol", "dave", "erin"):
            self.assertNotIn(leaked, text)
        self.assertIn("home_path", text)


class PathTest(unittest.TestCase):
    def test_manifest_path_follows_xdg_state_home(self) -> None:
        tail = Path("forge-ledger") / "migrations" / "demo.json"
        self.assertEqual(manifest_path("demo", {"XDG_STATE_HOME": "/x/state", "HOME": "/h"}), Path("/x/state") / tail)
        for env in ({"HOME": "/h"}, {"XDG_STATE_HOME": "  ", "HOME": "/h"}):
            self.assertEqual(manifest_path("demo", env), Path("/h/.local/state") / tail)

    def test_manifest_path_rejects_unsafe_slug(self) -> None:
        for slug in ("", "..", "../x", "a/b", ".hidden", "a..b", "a b"):
            with self.subTest(slug=slug):
                with self.assertRaisesRegex(ValueError, "slug"):
                    manifest_path(slug, {"HOME": "/h"})

    def test_source_dir_tilde_uses_env_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.json"
            for written, expected in (("~/docs/p", Path("/h/docs/p")), ("~", Path("/h")), ("/abs/p", Path("/abs/p"))):
                path.write_text(
                    json.dumps({"version": 1, "slug": "s", "source_dir": written, "records": []}), encoding="utf-8"
                )
                self.assertEqual(resolve_source_dir(load_manifest(path), {"HOME": "/h"}), expected)


class MigrateCliTest(_ManifestCase):
    def run_cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            # Plane 설정도 git 저장소도 없이 동작해야 한다.
            rc = main(["--repo", str(self.tmp), *args], env={"HOME": str(self.tmp)})
        return rc, out.getvalue(), err.getvalue()

    def test_check_passes(self) -> None:
        rc, out, err = self.run_cli("migrate", "check", str(self.write(_sample())))
        self.assertEqual((rc, err), (0, ""), out)
        self.assertIn("검사 통과: 레코드 9건", out)

    def test_check_failure_lists_key_and_pattern_without_value(self) -> None:
        data = _sample()
        _record(data, _COMMENT)["body_html"] = "<p>/home/alice/run.log</p>"
        rc, out, err = self.run_cli("migrate", "check", str(self.write(data)))
        self.assertEqual(rc, 1)
        self.assertIn(f"{_COMMENT}: ", out)
        self.assertIn("home_path", out)
        self.assertIn("문제 1건", out)
        self.assertNotIn("alice", out + err)

    def test_unreadable_manifest(self) -> None:
        rc, _, err = self.run_cli("migrate", "check", str(self.tmp / "nope.json"))
        self.assertEqual(rc, 1)
        self.assertIn("매니페스트 파일을 읽을 수 없음", err)

    def test_preview_and_apply_are_not_wired_yet(self) -> None:
        for command in ("preview", "apply"):
            with self.subTest(command=command):
                rc, _, err = self.run_cli("migrate", command, str(self.write(_sample())))
                self.assertEqual(rc, 1)
                self.assertTrue(err)


if __name__ == "__main__":
    unittest.main()
