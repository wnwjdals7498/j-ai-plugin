from __future__ import annotations

import re
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from ledger.sections import section_state, split_html_sections, split_md_sections
from ledger.spec import DOCS, REPO_FILES, TEMPLATE_FOR

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "plugins" / "forge-ledger" / "skills" / "ledger-plan" / "templates"
PLANE_T = TEMPLATES / "plane"
LEDGER_T = TEMPLATES / "ledger"

ALLOWED = {
    "h1",
    "h2",
    "h3",
    "p",
    "strong",
    "em",
    "code",
    "pre",
    "ul",
    "ol",
    "li",
    "table",
    "tbody",
    "thead",
    "tr",
    "td",
    "th",
    "a",
    "hr",
    "blockquote",
    "br",
}


class TemplatesTest(unittest.TestCase):
    def test_every_unit_doc_has_a_section(self) -> None:
        for d in DOCS:
            if d.scope == "unit":
                html = (PLANE_T / TEMPLATE_FOR[d.container]).read_text(encoding="utf-8")
                self.assertIn(d.code, split_html_sections(html), d.code)

    def test_every_repo_doc_has_a_section(self) -> None:
        for code, rel in REPO_FILES.items():
            md = (LEDGER_T / Path(rel).relative_to(".ledger")).read_text(encoding="utf-8")
            self.assertIn(code, split_md_sections(md), code)

    def test_templates_start_missing(self) -> None:
        html = (PLANE_T / "prd.html").read_text(encoding="utf-8")
        for code, body in split_html_sections(html).items():
            self.assertEqual(section_state(body, True)[0], "missing", code)

    def test_only_plane_tags(self) -> None:
        for f in PLANE_T.glob("*.html"):
            tags = set(re.findall(r"</?([a-z0-9]+)", f.read_text(encoding="utf-8")))
            self.assertLessEqual(tags, ALLOWED, f.name)

    def test_drawio_templates_parse(self) -> None:
        for name in ("design/data-model.drawio", "design/logging-flow.drawio"):
            ET.parse(LEDGER_T / name)
