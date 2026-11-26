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
        self.assertEqual(
            set(uc),
            {
                "plane_url",
                "plane_workspace",
                "plane_api_key",
                "plane_mcp_url",
                "gitea_url",
                "gitea_token",
                "dashboard_bind",
                "worklog_project_key",
                "worklog_host_label",
                "worklog_auto",
            },
        )
        self.assertTrue(uc["gitea_url"]["required"])
        self.assertTrue(uc["gitea_token"]["required"])
        self.assertIs(uc["gitea_token"]["sensitive"], True)
        self.assertEqual(uc["dashboard_bind"]["default"], "127.0.0.1")
        self.assertTrue(uc["plane_api_key"]["sensitive"])
        self.assertEqual(uc["plane_workspace"]["default"], "forge")
        for v in uc.values():
            self.assertTrue({"type", "title", "description"} <= set(v))

    def test_marketplace_points_to_plugin(self) -> None:
        mk = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
        self.assertEqual(mk["plugins"][0]["source"], "./plugins/forge-ledger")
