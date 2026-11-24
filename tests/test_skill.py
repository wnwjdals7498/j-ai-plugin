from __future__ import annotations

import json
import unittest
from pathlib import Path

from ledger.secrets import find_secrets

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "forge-ledger"
SKILL = PLUGIN / "skills" / "ledger-plan"
COMMANDS = PLUGIN / "commands"


class SkillTest(unittest.TestCase):
    def test_skill_frontmatter(self) -> None:
        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        fm = text.split("---")[1]
        self.assertIn("name: ledger-plan", fm)
        self.assertIn("description:", fm)
        self.assertLess(len(text.splitlines()), 120)

    def test_skill_mentions_each_command_and_reference(self) -> None:
        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        for s in (
            "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py",
            "plan new",
            "plan get",
            "plan put",
            "plan check",
            "decide",
            "references/docs.md",
            "references/flows.md",
            "references/gates.md",
            "references/review-prompt.md",
            "examples/forge-ledger-design.html",
        ):
            self.assertIn(s, text)

    def test_question_order(self) -> None:
        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("PRD 1 → 2 → 9 → 3 → 5 → 4 → 6 → 7", text)

    def test_example_section_ids_match_doc_codes(self) -> None:
        html = (SKILL / "examples/forge-ledger-design.html").read_text(encoding="utf-8")
        for sid in ("prd-goals", "prd-out", "impl-data", "impl-security", "impl-phases"):
            self.assertIn(f'id="{sid}"', html)

    def test_mcp_config_uses_user_config(self) -> None:
        mcp = json.loads((PLUGIN / ".mcp.json").read_text(encoding="utf-8"))
        s = json.dumps(mcp)
        for k in (
            "${user_config.plane_mcp_url}",
            "Bearer ${user_config.plane_api_key}",
            "${user_config.plane_workspace}",
        ):
            self.assertIn(k, s)

    def test_commands_and_references_exist(self) -> None:
        for rel in ("references/flows.md", "references/gates.md", "references/review-prompt.md"):
            content = (SKILL / rel).read_text(encoding="utf-8")
            self.assertTrue(content.strip(), rel)

        plan_cmd = (COMMANDS / "plan.md").read_text(encoding="utf-8")
        plan_fm = plan_cmd.split("---")[1]
        self.assertIn("description:", plan_fm)
        self.assertIn("argument-hint:", plan_fm)
        self.assertIn('python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" plan $ARGUMENTS', plan_cmd)

        decide_cmd = (COMMANDS / "decide.md").read_text(encoding="utf-8")
        decide_fm = decide_cmd.split("---")[1]
        self.assertIn("description:", decide_fm)
        self.assertIn("argument-hint:", decide_fm)
        self.assertIn('python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" decide $ARGUMENTS', decide_cmd)

    def test_no_secrets_in_plugin_docs(self) -> None:
        paths = [
            SKILL / "SKILL.md",
            SKILL / "references/flows.md",
            SKILL / "references/gates.md",
            SKILL / "references/review-prompt.md",
            COMMANDS / "plan.md",
            COMMANDS / "decide.md",
            PLUGIN / ".mcp.json",
        ]
        for path in paths:
            text = path.read_text(encoding="utf-8")
            self.assertEqual(find_secrets(text), [], str(path))
