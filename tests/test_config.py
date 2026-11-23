from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ledger.config import (
    LedgerConfig,
    PlaneProject,
    load_config,
    load_config_text,
    render_config,
    save_config,
)
from ledger.miniyaml import MiniYamlError, loads

SAMPLE = """# 비밀값과 접속 주소는 넣지 않는다
method_version: 1              # 작성방식 규격 버전
plane:
  workspace: forge
  projects:
    - key: SAMPM
      title: sample-app 유지보수
      default: true
    - key: APP
      title: 앱 첫 출시
screens:
  tool: penpot
  team: design
  files: ["f1", "f2"]
hub: dev-hub
"""


class ConfigTest(unittest.TestCase):
    def test_load_sample(self) -> None:
        cfg = load_config_text(SAMPLE)
        self.assertEqual(cfg.workspace, "forge")
        self.assertEqual([p.key for p in cfg.projects], ["SAMPM", "APP"])
        self.assertEqual(cfg.default_project().key, "SAMPM")
        self.assertEqual(cfg.screens_files, ["f1", "f2"])

    def test_round_trip(self) -> None:
        cfg = load_config_text(SAMPLE)
        self.assertEqual(load_config_text(render_config(cfg)), cfg)
        self.assertIn("# 비밀값과 접속 주소는 넣지 않는다", render_config(cfg))

    def test_unknown_key_is_error(self) -> None:
        with self.assertRaisesRegex(MiniYamlError, "plane.colour"):
            load_config_text(SAMPLE.replace("  workspace: forge", "  workspace: forge\n  colour: red"))

    def test_default_project_missing_raises_lookup_error(self) -> None:
        cfg = LedgerConfig(
            method_version=1,
            workspace="forge",
            projects=[PlaneProject(key="APP", title="앱 첫 출시")],
        )
        with self.assertRaises(LookupError):
            cfg.default_project()

    def test_save_and_load_file(self) -> None:
        cfg = load_config_text(SAMPLE)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".ledger" / "ledger.yml"
            save_config(cfg, path)
            loaded = load_config(path)
            self.assertEqual(loaded, cfg)

    def test_miniyaml_tabs_and_odd_indent(self) -> None:
        with self.assertRaisesRegex(MiniYamlError, "2"):
            loads("plane:\n\tworkspace: forge\n")
        with self.assertRaisesRegex(MiniYamlError, "2"):
            loads("plane:\n   workspace: forge\n")

    def test_miniyaml_quoted_hash_and_scalars(self) -> None:
        data = loads('a: "x # y" # trailing\nb: true\nc: 42\nd: []\n')
        self.assertEqual(data, {"a": "x # y", "b": True, "c": 42, "d": []})

    def test_empty_projects_and_special_chars_round_trip(self) -> None:
        cfg = LedgerConfig(
            method_version=1,
            workspace="team:one",
            projects=[],
            screens_tool="penpot",
            screens_team="",
            screens_files=[],
            hub="",
        )
        self.assertEqual(load_config_text(render_config(cfg)), cfg)
