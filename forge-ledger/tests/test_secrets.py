from __future__ import annotations

import json
import unittest

from ledger.secrets import PATTERN_NAMES, PATTERNS, find_exposures, find_secrets


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
            "home_path": "/home/alice/x",
            "email": "a.b@corp.co.kr",
            "internal_host": "build.lan",
            "private_key": "-----BEGIN OPENSSH PRIVATE KEY-----",
            "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl",
            "secret_assignment": "API_TOKEN=abcd1234efgh",
        }
        self.assertEqual(tuple(cases), PATTERN_NAMES)
        self.assertEqual(tuple(PATTERNS), PATTERN_NAMES)
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

    def assert_each(self, name, positives, negatives):
        for text in positives:
            with self.subTest(positive=text):
                self.assertEqual(find_exposures(text), [name])
        for text in negatives:
            with self.subTest(negative=text):
                self.assertEqual(find_exposures(text), [])

    def test_home_path(self):
        self.assert_each(
            "home_path",
            ["/home/alice/x", "/data/home/u/a", "/Users/bob/a", "C:\\Users\\bob", '"C:\\\\Users\\\\bob"'],
            ["~/docs", "/home", "/home/<이름>", "GET /users/42"],
        )

    def test_email(self):
        self.assert_each(
            "email",
            ["a.b@corp.co.kr", "<a.b@corp.co.kr>", "dev@example.com.evil.io", "dev@notexample.com"],
            [
                "dev@example.com",
                "noreply@github.com",
                "x@noreply.example",
                "1+bot@users.noreply.github.com",
                "actions/checkout@3d3c42e5",
                "forge-ledger@forge",
            ],
        )

    def test_internal_host(self):
        self.assert_each(
            "internal_host",
            ["build.lan", "svc.internal:8080", "pc.local", "http://git.corp.internal/repo", "접속: pc.local."],
            [
                "localhost",
                "~/.local/state",
                ".claude/settings.local.json",
                "CLAUDE.local.md",
                "self._tls = threading.local()",
                "docs.local-setup",
            ],
        )

    def test_private_key(self):
        self.assert_each(
            "private_key",
            ["-----BEGIN OPENSSH PRIVATE KEY-----", "-----BEGIN PRIVATE KEY-----", "-----BEGIN RSA PRIVATE KEY-----"],
            ["-----BEGIN CERTIFICATE-----", "-----BEGIN PUBLIC KEY-----"],
        )

    def test_jwt(self):
        self.assert_each(
            "jwt",
            ["eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl", "Bearer eyJhbGci.eyJzdWIi.sig_-9"],
            ["eyJhbGciOiJIUzI1NiJ9", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0"],
        )

    def test_secret_assignment(self):
        self.assert_each(
            "secret_assignment",
            [
                "API_TOKEN=abcd1234efgh",
                "password: hunter2hunter2",
                'client_secret = "abcd1234efgh"',
                '{"api_key": "abcd1234efgh"}',
                "export DB_PASSWD='p@ss-w0rd-1'",
                "(password=hunter2hunter2)",
            ],
            [
                "TOKEN=${PLANE_TOKEN}",
                "token = os.environ[...]",
                "PASSWORD=",
                "PASSWORD=short1",
                "token = self._settings.token",
                "token = candidate.rstrip('.')",
                "token: Optional[str] = None",
                "api_token: <발급받은 토큰>",
                "token == expected1234",
                "Client(token=get_token())",
                "`TOKEN=`·`PASSWORD:`처럼 비밀값을 대입하는 줄",
            ],
        )

    def test_secret_assignment_ruling_cases(self):
        for text, expected in (
            ("token=settings_token", []),
            ("password=한글비밀번호입니다", ["secret_assignment"]),
            ("api_key=abc123def456", ["secret_assignment"]),
            ('token="two words here"', ["secret_assignment"]),
            ("TOKEN=${PLANE_TOKEN}", []),
        ):
            with self.subTest(text=text):
                self.assertEqual(find_exposures(text), expected)

    def test_secret_assignment_flags_every_long_quoted_value(self):
        self.assert_each(
            "secret_assignment",
            [
                'PASSWORD="CORRECT_HORSE_BATTERY"',
                '_ENV_TOKEN = "FORGE_LEDGER_GITEA_TOKEN"',
                'password = "correct horse battery"',
                "password: 'my pass phrase'",
                '{"password": "abc def ghi"}',
                json.dumps({"body": 'password: "correct horse battery"'}),
                r'password = "ab\"cdefghij"',
                'password = "비밀번호 여덟 글자 이상"',
                'password = "$ecret-Pa55!"',
                'password = "get_token()x y"',
                'TOKEN="$PLANE_TOKEN"',
                'TOKEN="${PLANE_TOKEN}"',
                'TOKEN: "${PLANE_TOKEN:-}"',
                'TOKEN="$(cat token-file)"',
                'token: "${{ secrets.PLANE_TOKEN }}"',
                'password: "{{ vault_password }}"',
                'headers = {"token": "{settings.token}"}',
                'api_token = "<발급받은 토큰>"',
                'token = "settings_token"',
            ],
            ['password = "short1"', 'token = ""', 'token: "일곱글자값임"'],
        )

    def test_secret_assignment_unquoted_references(self):
        self.assert_each(
            "secret_assignment",
            [],
            [
                "TOKEN=$PLANE_API_TOKEN",
                "TOKEN=${PLANE_TOKEN_V2}",
                '{"TOKEN": ${PLANE_TOKEN}}',
                "<code>TOKEN=$PLANE_TOKEN</code>",
                "TOKEN=$(cat token-file)",
                "$env:API_TOKEN = $env:PLANE_TOKEN",
                "token: ${{ secrets.PLANE_TOKEN }}",
                "token={{vault_token}}",
                "TOKEN=%PLANE_TOKEN%",
                "api_token: <your-plane-token>",
                "token=settings_token",
                "PASSWORD=CORRECT_HORSE_BATTERY",
                "PASSWORD=correcthorsebattery",
                "token=workspaceToken",
                "token=cfg.token",
                "token=process.env.PLANE_TOKEN",
                "token = self._settings.token",
                "self.token = this.settings.token",
                "token=get_token()",
                "Client(token=get_token())",
                "client(token=TEST_TOKEN)",
                '{"TOKEN": TEST_TOKEN}',
                "def run(secret: SecretStr):",
                "token: Optional[str]=None",
                'token = os.environ["PLANE_TOKEN"]',
                "token = os.getenv('PLANE_TOKEN')",
                "<code>token=settings_token</code>",
            ],
        )

    def test_secret_assignment_unquoted_literals(self):
        self.assert_each(
            "secret_assignment",
            [
                "api_key=abc123def456",
                "token=settings_token2",
                "token=cfg.v2.token",
                "token=parts[12345]",
                "password=hunter2(abc)",
                "password=correct-horse-battery",
                "password=p@ssword!",
                "password=abc!$defghij",
                "password={abc}defghij",
                "password=한글비밀번호입니다",
                "password: 아홉글자한글비밀값",
                "token=settings_token을",
            ],
            ["password=한글일곱글자임", "token=abc123)", "password: 짧은값"],
        )

    def test_names_keep_definition_order_without_duplicates(self):
        text = "a.b@corp.co.kr c.d@corp.co.kr 10.1.2.3"
        self.assertEqual(find_exposures(text), ["ipv4", "email"])

    def test_one_allowed_match_does_not_hide_another(self):
        self.assertEqual(find_exposures("dev@example.com a.b@corp.co.kr"), ["email"])
        self.assertEqual(find_exposures("TOKEN=${X} PASSWORD=hunter2hunter2"), ["secret_assignment"])

    def test_json_escaped_line_break_before_match(self):
        # PreToolUse 훅은 JSON으로 직렬화한 본문을 검사한다.
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl"
        self.assertEqual(find_exposures(json.dumps("앞\n" + jwt)), ["jwt"])
        self.assertEqual(find_exposures(json.dumps("앞\nnoreply@github.com\tno-reply@corp.co.kr")), [])
        self.assertEqual(find_exposures(json.dumps("앞\na.b@corp.co.kr")), ["email"])

    def test_find_secrets_is_alias(self):
        self.assertIs(find_secrets, find_exposures)


if __name__ == "__main__":
    unittest.main()
