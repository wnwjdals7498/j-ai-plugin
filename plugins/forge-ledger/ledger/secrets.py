"""Secret pattern detection for forge-ledger."""

from __future__ import annotations

import ipaddress
import re

PATTERNS: dict[str, str] = {
    "ipv4": r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
    "ipv6": r"(?<![0-9A-Za-z])[0-9A-Fa-f:]*:[0-9A-Fa-f:]*:[0-9A-Fa-f:.]*(?![0-9A-Za-z])",
    "plane_token": r"plane_api_[0-9a-f]{32}",
    "github_token": r"ghp_[A-Za-z0-9]{20,}",
    "gitlab_token": r"glpat-[A-Za-z0-9_-]{20,}",
    "figma_token": r"figd_[A-Za-z0-9_-]{20,}",
    "telegram_bot_token": r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b",
}

_ALLOWED_IPV4 = frozenset({"127.0.0.1", "0.0.0.0"})
_ALLOWED_IPV6 = frozenset({ipaddress.IPv6Address("::1"), ipaddress.IPv6Address("::")})


def _is_forbidden_ipv6(candidate: str) -> bool:
    token = candidate.rstrip(".")
    if not any(ch.isdigit() for ch in token):
        return False
    try:
        addr = ipaddress.IPv6Address(token)
    except ValueError:
        return False
    return addr not in _ALLOWED_IPV6


def find_secrets(text: str) -> list[str]:
    """Return sorted unique pattern names matched in text."""
    found: list[str] = []
    for name, pattern in PATTERNS.items():
        matches = re.findall(pattern, text)
        if not matches:
            continue
        if name == "ipv4" and all(m in _ALLOWED_IPV4 for m in matches):
            continue
        if name == "ipv6" and not any(_is_forbidden_ipv6(m) for m in matches):
            continue
        found.append(name)
    return sorted(found)
