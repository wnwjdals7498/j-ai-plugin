from __future__ import annotations

import re
import unittest
from pathlib import Path

from ledger.secrets import find_secrets

ROOT = Path(__file__).resolve().parent.parent
RUNNER_DIR = ROOT / "ci" / "runner"
COMPOSE_PATH = RUNNER_DIR / "compose.yml"
CONFIG_PATH = RUNNER_DIR / "config.yaml"
README_PATH = RUNNER_DIR / "README.md"
TAG_PATH = ROOT / "ci" / "image" / "TAG"


def _extract_env_value(compose_text: str, key: str) -> str | None:
    patterns = (
        rf"(?m)^\s*-\s*{re.escape(key)}=(.+?)\s*$",
        rf"(?m)^\s*{re.escape(key)}:\s*(.+?)\s*$",
    )
    for pattern in patterns:
        match = re.search(pattern, compose_text)
        if match is not None:
            raw = match.group(1).strip()
            if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
                return raw[1:-1]
            return raw
    return None


class TestRunnerFiles(unittest.TestCase):
    def test_config_isolation_values(self) -> None:
        text = CONFIG_PATH.read_text(encoding="utf-8")
        expected_tokens = (
            'docker_host: "-"',
            "privileged: false",
            "capacity: 1",
            "force_pull: false",
            "network: forge-ci-net",
            "--memory=3g --cpus=4",
        )
        for token in expected_tokens:
            with self.subTest(token=token):
                self.assertIn(token, text)

    def test_compose_runner_image_pinned(self) -> None:
        text = COMPOSE_PATH.read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^\s+image:\s*gitea/runner:6\.0\.1\s*$")
        self.assertRegex(text, r"(?m)^\s+restart:\s*unless-stopped\s*$")
        self.assertRegex(text, r"(?m)^\s+container_name:\s*forge-ledger-runner\s*$")

    def test_label_tag_matches_tag_file(self) -> None:
        tag = TAG_PATH.read_text(encoding="utf-8").strip()
        text = COMPOSE_PATH.read_text(encoding="utf-8")
        expected_label = f"forge-ci:docker://forge-ledger-ci:{tag}"
        labels_val = _extract_env_value(text, "GITEA_RUNNER_LABELS")
        self.assertIsNotNone(labels_val)
        assert labels_val is not None
        self.assertIn(expected_label, labels_val)

    def test_secrets_only_from_env(self) -> None:
        text = COMPOSE_PATH.read_text(encoding="utf-8")
        for key in ("GITEA_INSTANCE_URL", "GITEA_RUNNER_REGISTRATION_TOKEN"):
            with self.subTest(key=key):
                value = _extract_env_value(text, key)
                self.assertIsNotNone(value, f"{key} missing from compose.yml")
                assert value is not None
                self.assertRegex(value, r"^\$\{[^}]+\}$")

    def test_no_secret_literals(self) -> None:
        for path in (COMPOSE_PATH, CONFIG_PATH, README_PATH):
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertEqual(find_secrets(text), [], str(path))


if __name__ == "__main__":
    unittest.main()
