"""Secret and exposure pattern detection for forge-ledger."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable

_PLANE_REGEX = r"plane_api_[0-9a-f]{32}"
_GITHUB_REGEX = r"ghp_[A-Za-z0-9]{20,}"
_GITLAB_REGEX = r"glpat-[A-Za-z0-9_-]{20,}"
_FIGMA_REGEX = r"figd_[A-Za-z0-9_-]{20,}"
_TELEGRAM_REGEX = r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"
# `[/]`는 이 파일 자신이 검사에 걸리지 않게 하는 표기다.
_HOME_PATH_REGEX = r"/home/\w[\w.-]*|/data/home[/]|/Users/\w[\w.-]*|(?<![A-Za-z])[A-Za-z]:\\+Users\\+"
# 앞쪽 lookbehind는 긴 한 줄에서 검사 시간이 길이의 제곱으로 늘지 않게 한다.
_EMAIL_REGEX = r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![A-Za-z0-9-])"
# 뒤에 `.글자`·`(`가 오면 파일 이름(settings.local.json)이나 코드(threading.local())로 본다.
_INTERNAL_HOST_REGEX = (
    r"(?<![\w.-])(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+(?i:local|internal|lan)(?![\w-]|\.\w|\()"
)
_PRIVATE_KEY_REGEX = r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"
# 두 번째 lookbehind는 JSON으로 직렬화한 줄바꿈·탭(`\n`·`\t`) 바로 뒤를 허용한다.
_JWT_REGEX = r"(?:(?<![A-Za-z0-9_-])|(?<=\\[nrt]))eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
# 따옴표 앞의 `\`는 JSON으로 직렬화한 본문(PreToolUse 훅)을 위한 것이다.
_ASSIGNMENT_REGEX = (
    r"(?i)(?<![A-Za-z0-9_-])[A-Za-z0-9_-]*(?:token|password|passwd|secret|api[_-]?key)"
    r"(?:\\?[\"'])?[ \t]*[:=](?!=)[ \t]*"
    r"(?:\\\"(?P<escaped_double>.*?)\\\"|\\'(?P<escaped_single>.*?)\\'"
    r"|\"(?P<double>(?:\\.|[^\"\\])*)\"|'(?P<single>(?:\\.|[^'\\])*)'"
    r"|(?P<bare>[^\s\"'`,;]*))"
)

# `이름: 값` 대신 쌍으로 적는다. 토큰 패턴 이름 줄이 이 파일의 secret_assignment 검사에 걸리지 않게 한다.
PATTERNS: dict[str, str] = dict(
    [
        ("ipv4", r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
        ("ipv6", r"(?<![0-9A-Za-z])[0-9A-Fa-f:]*:[0-9A-Fa-f:]*:[0-9A-Fa-f:.]*(?![0-9A-Za-z])"),
        ("plane_token", _PLANE_REGEX),
        ("github_token", _GITHUB_REGEX),
        ("gitlab_token", _GITLAB_REGEX),
        ("figma_token", _FIGMA_REGEX),
        ("telegram_bot_token", _TELEGRAM_REGEX),
        ("home_path", _HOME_PATH_REGEX),
        ("email", _EMAIL_REGEX),
        ("internal_host", _INTERNAL_HOST_REGEX),
        ("private_key", _PRIVATE_KEY_REGEX),
        ("jwt", _JWT_REGEX),
        ("secret_assignment", _ASSIGNMENT_REGEX),
    ]
)

PATTERN_NAMES: tuple[str, ...] = tuple(PATTERNS)

_ALLOWED_IPV4 = frozenset({"127.0.0.1", str(ipaddress.IPv4Address(0))})
_ALLOWED_IPV6 = frozenset({ipaddress.IPv6Address("::1"), ipaddress.IPv6Address("::")})

_MIN_ASSIGNED_LENGTH = 8
# 값 전체가 아래 구문일 때만 변수 참조·자리표시로 보고 제외한다. 값의 글자 모양으로는 제외하지 않는다.
_REFERENCE_SYNTAX = (
    r"\$(?:env:)?[A-Za-z_]\w*"  # $VAR, $env:VAR
    r"|\$\{[A-Za-z_]\w*(?::?-|:?\?[^{}]*)?\}"  # ${VAR}, ${VAR:-}(빈 기본값), ${VAR:?오류 문구}
    r"|\$\([^()]*\)"  # $(명령)
    r"|\$?\{\{[^{}]*\}\}"  # {{ 템플릿 }}, ${{ 식 }}
    r"|\{[A-Za-z_][\w.]*\}"  # f-string·format 자리
    r"|%[A-Za-z_]\w*%"  # %VAR%
    r"|<[^<>]*>"  # <자리표시>
)
_QUOTED_REFERENCE = re.compile(_REFERENCE_SYNTAX)
# 따옴표 없는 값은 코드 구문도 참조다. 공백·따옴표에서 잘린 앞부분과 뒤에 붙은 닫는 기호를 허용한다.
_BARE_REFERENCE = re.compile(
    rf"(?:{_REFERENCE_SYNTAX}"
    r"|\$\(.*"
    r"|[A-Za-z_][\w.]*[(\[].*"  # 호출·첨자: os.getenv(, os.environ[
    r"|(?:self|cls|this|process\.env|os\.environ)\.[A-Za-z_][\w.]*"  # 객체·환경 속성
    r")(?:[)\]}.:]|</[^<>]*>)*"
)


def _is_forbidden_ipv4(match: re.Match[str]) -> bool:
    return match.group() not in _ALLOWED_IPV4


def _is_forbidden_ipv6(match: re.Match[str]) -> bool:
    token = match.group().rstrip(".")
    if not any(ch.isdigit() for ch in token):
        return False
    try:
        addr = ipaddress.IPv6Address(token)
    except ValueError:
        return False
    return addr not in _ALLOWED_IPV6


def _is_forbidden_email(match: re.Match[str]) -> bool:
    local, _, domain = match.group().lower().partition("@")
    start = match.start()
    if start and match.string[start - 1] == "\\" and local[0] in "nrt":
        local = local[1:]  # JSON으로 직렬화한 `\n`·`\r`·`\t`의 글자는 주소가 아니다.
    if local in ("noreply", "no-reply") or "noreply" in domain.split("."):
        return False
    return domain != "example.com" and not domain.endswith(".example.com")


def _is_literal_assignment(match: re.Match[str]) -> bool:
    for group in ("escaped_double", "escaped_single", "double", "single"):
        quoted = match.group(group)
        if quoted is not None:
            return len(quoted) >= _MIN_ASSIGNED_LENGTH and _QUOTED_REFERENCE.fullmatch(quoted) is None
    # 따옴표 없는 값에 붙은 한글 조사·설명은 값이 아니다.
    ascii_prefix = re.match(r"[!-~]*", match.group("bare"))
    value = ascii_prefix.group() if ascii_prefix else ""
    return len(value.rstrip(")]}")) >= _MIN_ASSIGNED_LENGTH and _BARE_REFERENCE.fullmatch(value) is None


# 정규식에 걸린 것 중 실제 노출만 남기는 판정. 없는 패턴은 걸리면 곧 노출이다.
_EXPOSED_IF: dict[str, Callable[[re.Match[str]], bool]] = {
    "ipv4": _is_forbidden_ipv4,
    "ipv6": _is_forbidden_ipv6,
    "email": _is_forbidden_email,
    "secret_assignment": _is_literal_assignment,
}


def find_exposures(text: str) -> list[str]:
    """Return unique pattern names matched in text, in definition order."""
    found: list[str] = []
    for name, pattern in PATTERNS.items():
        is_exposed = _EXPOSED_IF.get(name)
        if any(is_exposed is None or is_exposed(match) for match in re.finditer(pattern, text)):
            found.append(name)
    return found


find_secrets = find_exposures
