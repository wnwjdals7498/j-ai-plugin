import unittest

from ledger.secrets import PATTERNS, find_secrets


class TestSecrets(unittest.TestCase):
    def test_detects_each_pattern(self):
        cases = {
            "ipv4": "server 10.1.2.3 down",
            "plane_token": "plane_api_" + "a" * 32,
            "github_token": "ghp_" + "A" * 36,
            "gitlab_token": "glpat-" + "x" * 20,
            "figma_token": "figd_" + "y" * 40,
            "telegram_bot_token": "123456789:" + "Z" * 35,
        }
        self.assertEqual(set(PATTERNS), set(cases))
        for name, text in cases.items():
            self.assertEqual(find_secrets(text), [name], name)

    def test_loopback_and_any_allowed(self):
        self.assertEqual(find_secrets("http://127.0.0.1:18090 0.0.0.0"), [])

    def test_plain_text_clean(self):
        self.assertEqual(find_secrets("D-261123-7f3a 버전 1.5.0 APP-12"), [])


if __name__ == "__main__":
    unittest.main()
