from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "forge-ledger"


class LayoutTest(unittest.TestCase):
    def test_plugin_manifest_user_config(self) -> None:
        m = json.loads((PLUGIN / ".claude-plugin/plugin.json").read_text())
        self.assertEqual(m["name"], "forge-ledger")
        uc = m["userConfig"]
        self.assertEqual(set(uc), {"plane_url", "plane_workspace", "plane_api_key", "plane_mcp_url"})
        self.assertTrue(uc["plane_api_key"]["sensitive"])
        self.assertEqual(uc["plane_workspace"]["default"], "forge")
        for v in uc.values():
            self.assertTrue({"type", "title", "description"} <= set(v))

    def test_marketplace_points_to_plugin(self) -> None:
        mk = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
        self.assertEqual(mk["plugins"][0]["source"], "./plugins/forge-ledger")
