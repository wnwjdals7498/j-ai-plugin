from __future__ import annotations

import unittest

from ledger.secrets import PATTERNS, find_secrets


class TestSecrets(unittest.TestCase):
    def test_detects_each_pattern(self):
        cases = {
            "ipv4": "server 10.1.2.3 down",
            "ipv6": "server 2001:db8::42 down",
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
        self.assertEqual(find_secrets("http://127.0.0.1:18090 0.0.0.0 ::1 :: http://[::1]:8080"), [])

    def test_ipv6_detection_and_exclusions(self):
        self.assertEqual(find_secrets("2001:db8::42"), ["ipv6"])
        self.assertEqual(find_secrets("fe80::1%eth0 앞부분"), ["ipv6"])
        self.assertEqual(find_secrets("http://[2001:db8::42]:8080/api"), ["ipv6"])
        self.assertEqual(find_secrets("10:30:00"), [])
        self.assertEqual(find_secrets("std::vector"), [])
        self.assertEqual(find_secrets("::1"), [])
        self.assertEqual(find_secrets("::"), [])
        self.assertEqual(find_secrets("foo1::bar cafe:babe:: 2026-11-23T10:30:00Z"), [])

    def test_plain_text_clean(self):
        self.assertEqual(find_secrets("D-261123-7f3a 버전 1.5.0 APP-12"), [])


if __name__ == "__main__":
    unittest.main()
