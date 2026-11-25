from __future__ import annotations

import re
import unittest
from pathlib import Path

from ledger.secrets import find_secrets

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = ROOT.parent / ".gitea" / "workflows" / "pr-check.yml"
TAG_PATH = ROOT / "ci" / "image" / "TAG"
EXPECTED_JOBS = {"lint", "test", "secrets", "sast", "deps", "matrix", "install"}
PY39_JOBS = {"lint", "test"}
PY3X_JOBS = {"sast", "deps"}
UV_JOBS = PY39_JOBS | PY3X_JOBS
NO_CACHE_JOBS = {"secrets", "matrix", "install"}
CACHE_ACTION = "actions/cache@caa296126883cff596d87d8935842f9db880ef25"
EXPECTED_ACTIONS = {
    "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
    CACHE_ACTION,
}


def _read_workflow() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def _extract_job_blocks(text: str) -> dict[str, str]:
    jobs_match = re.search(r"^jobs:\s*\n(.*)\Z", text, re.MULTILINE | re.DOTALL)
    if jobs_match is None:
        return {}
    jobs_body = jobs_match.group(1)
    headers = list(re.finditer(r"^  ([a-zA-Z0-9_-]+):\s*$", jobs_body, re.MULTILINE))
    blocks: dict[str, str] = {}
    for idx, match in enumerate(headers):
        start = match.end()
        end = headers[idx + 1].start() if idx + 1 < len(headers) else len(jobs_body)
        blocks[match.group(1)] = jobs_body[start:end]
    return blocks


class TestWorkflow(unittest.TestCase):
    def test_name_and_trigger(self) -> None:
        text = _read_workflow()
        self.assertRegex(text, r"(?m)^name:\s*pr-check\s*$")
        self.assertRegex(text, r"(?m)^on:\s*$")
        self.assertRegex(text, r"(?m)^  pull_request:\s*$")
        self.assertRegex(text, r"(?m)^    types:\s*\[opened,\s*synchronize,\s*reopened\]\s*$")
        self.assertRegex(text, r"(?m)^    branches:\s*\[main\]\s*$")

    def test_permissions_read_only(self) -> None:
        text = _read_workflow()
        self.assertRegex(text, r"(?m)^permissions:\s*\n  contents:\s*read\s*$")
        self.assertNotIn("write", text.lower())

    def test_run_steps_use_subfolder(self) -> None:
        text = _read_workflow()
        self.assertRegex(text, r"(?m)^defaults:\n  run:\n    working-directory: forge-ledger$")
        self.assertEqual(text.count("working-directory:"), 1)

    def test_seven_jobs(self) -> None:
        text = _read_workflow()
        blocks = _extract_job_blocks(text)
        self.assertEqual(set(blocks.keys()), EXPECTED_JOBS)

    def test_container_tag_matches_tag_file(self) -> None:
        text = _read_workflow()
        tag = TAG_PATH.read_text(encoding="utf-8").strip()
        expected_image = f"forge-ledger-ci:{tag}"
        containers = re.findall(r"(?m)^\s+container:\s*(\S+)\s*$", text)
        self.assertEqual(len(containers), len(EXPECTED_JOBS))
        for image in containers:
            self.assertEqual(image, expected_image)

    def test_runs_on_label(self) -> None:
        text = _read_workflow()
        labels = re.findall(r"(?m)^\s+runs-on:\s*(\S+)\s*$", text)
        self.assertEqual(labels, ["forge-ci"] * len(EXPECTED_JOBS))

    def test_actions_pinned_by_sha(self) -> None:
        text = _read_workflow()
        uses = re.findall(r"(?m)^\s+(?:-\s+)?uses:\s*(\S+)\s*$", text)
        self.assertEqual(set(uses), EXPECTED_ACTIONS)
        self.assertEqual(len(uses), len(EXPECTED_JOBS) + len(UV_JOBS))

    def test_cache_keys_per_job(self) -> None:
        text = _read_workflow()
        blocks = _extract_job_blocks(text)
        for job_id in PY39_JOBS:
            block = blocks[job_id]
            with self.subTest(job=job_id):
                self.assertIn(CACHE_ACTION, block)
                self.assertRegex(block, r"(?m)^\s+path:\s*/root/\.cache/uv\s*$")
                self.assertIn(
                    "key: uv-py39-${{ hashFiles('forge-ledger/ci/requirements-py39.txt') }}",
                    block,
                )
        for job_id in PY3X_JOBS:
            block = blocks[job_id]
            with self.subTest(job=job_id):
                self.assertIn(CACHE_ACTION, block)
                self.assertRegex(block, r"(?m)^\s+path:\s*/root/\.cache/uv\s*$")
                self.assertIn(
                    "key: uv-py3x-${{ hashFiles('forge-ledger/ci/requirements-py3x.txt') }}",
                    block,
                )
        for job_id in NO_CACHE_JOBS:
            with self.subTest(job=job_id):
                self.assertIn(job_id, blocks)
                block = blocks[job_id]
                self.assertNotIn("actions/cache", block)
                self.assertNotIn("UV_CACHE_DIR", block)

    def test_each_job_runs_its_check(self) -> None:
        text = _read_workflow()
        blocks = _extract_job_blocks(text)
        self.assertEqual(set(blocks.keys()), EXPECTED_JOBS)
        for job_id in EXPECTED_JOBS:
            block = blocks[job_id]
            with self.subTest(job=job_id):
                self.assertRegex(block, rf"ci/run-checks\.sh\s+{re.escape(job_id)}\b")
                self.assertRegex(block, r"(?m)^\s+fetch-depth:\s*0\s*$")
                if job_id in UV_JOBS:
                    self.assertRegex(block, r"(?m)^\s+UV_CACHE_DIR:\s*/root/\.cache/uv\s*$")
        secrets_block = blocks["secrets"]
        self.assertIn("BASE_SHA: ${{ github.event.pull_request.base.sha }}", secrets_block)
        self.assertIn("HEAD_SHA: ${{ github.event.pull_request.head.sha }}", secrets_block)

    def test_no_secret_literals(self) -> None:
        text = _read_workflow()
        self.assertEqual(find_secrets(text), [])


if __name__ == "__main__":
    unittest.main()
