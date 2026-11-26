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
    r"(?:\\?\"(?P<double>[^\"\s]*?)\\?\"|\\?'(?P<single>[^'\s]*?)\\?'|(?P<bare>[^\s\"'`,;]*))"
)

PATTERNS: dict[str, str] = {
    "ipv4": r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
    "ipv6": r"(?<![0-9A-Za-z])[0-9A-Fa-f:]*:[0-9A-Fa-f:]*:[0-9A-Fa-f:.]*(?![0-9A-Za-z])",
    "plane_token": _PLANE_REGEX,
    "github_token": _GITHUB_REGEX,
    "gitlab_token": _GITLAB_REGEX,
    "figma_token": _FIGMA_REGEX,
    "telegram_bot_token": _TELEGRAM_REGEX,
    "home_path": _HOME_PATH_REGEX,
    "email": _EMAIL_REGEX,
    "internal_host": _INTERNAL_HOST_REGEX,
    "private_key": _PRIVATE_KEY_REGEX,
    "jwt": _JWT_REGEX,
    "secret_assignment": _ASSIGNMENT_REGEX,
}

PATTERN_NAMES: tuple[str, ...] = tuple(PATTERNS)

_ALLOWED_IPV4 = frozenset({"127.0.0.1", str(ipaddress.IPv4Address(0))})
_ALLOWED_IPV6 = frozenset({ipaddress.IPv6Address("::1"), ipaddress.IPv6Address("::")})

_MIN_ASSIGNED_LENGTH = 8
# 환경 변수·템플릿 참조, 자리표시, 가린 값의 시작 글자
_REFERENCE_PREFIXES = ("$", "<", "{", "[", "%", "*")
# 대문자 snake_case는 환경 변수·상수의 이름이다.
_CONSTANT_NAME = re.compile(r"[A-Z]+(?:_[A-Z]+)+")
# 따옴표 없는 값이 코드인 경우: 호출·첨자, 점으로 이은 속성, 숫자 없는 snake_case·camelCase 이름
_CODE_EXPRESSION = re.compile(
    r"[A-Za-z_][\w.]*[(\[].*"
    r"|[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+"
    r"|[A-Za-z_]*_[A-Za-z_]*"
    r"|[A-Za-z]*[a-z][A-Z][A-Za-z]*"
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
    quoted = match.group("double") or match.group("single")
    raw = quoted or (match.group("bare") or "").rstrip(")]}")
    # 뒤에 붙은 한글 조사 등은 값이 아니다.
    ascii_prefix = re.match(r"[!-~]*", raw)
    value = ascii_prefix.group() if ascii_prefix else ""
    if len(value) < _MIN_ASSIGNED_LENGTH or value.startswith(_REFERENCE_PREFIXES):
        return False
    if _CONSTANT_NAME.fullmatch(value):
        return False
    return bool(quoted) or _CODE_EXPRESSION.fullmatch(value) is None


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
