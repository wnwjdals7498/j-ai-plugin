from __future__ import annotations

import re
from typing import Any

_INT_RE = re.compile(r"-?[0-9]+")


class MiniYamlError(ValueError):
    """Raised when MiniYAML text cannot be parsed or validated."""


def _strip_comment(line: str) -> str:
    quote: str | None = None
    escaped = False
    for idx, ch in enumerate(line):
        if quote == '"':
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                quote = None
        elif quote == "'":
            if ch == "'":
                quote = None
        else:
            if ch in ('"', "'"):
                quote = ch
            elif ch == "#" and (idx == 0 or line[idx - 1] in (" ", "\t")):
                return line[:idx].rstrip()
    return line.rstrip()


def _parse_double_quoted(raw: str, lineno: int) -> str:
    chars: list[str] = []
    escaped = False
    idx = 1
    while idx < len(raw):
        ch = raw[idx]
        if escaped:
            escapes = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"'}
            chars.append(escapes.get(ch, ch))
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == '"':
            if idx != len(raw) - 1:
                raise MiniYamlError(f"line {lineno}: unexpected trailing characters after quoted string")
            return "".join(chars)
        else:
            chars.append(ch)
        idx += 1
    raise MiniYamlError(f"line {lineno}: unterminated double-quoted string")


def _parse_single_quoted(raw: str, lineno: int) -> str:
    chars: list[str] = []
    idx = 1
    while idx < len(raw):
        ch = raw[idx]
        if ch == "'":
            if idx + 1 < len(raw) and raw[idx + 1] == "'":
                chars.append("'")
                idx += 2
                continue
            if idx != len(raw) - 1:
                raise MiniYamlError(f"line {lineno}: unexpected trailing characters after quoted string")
            return "".join(chars)
        chars.append(ch)
        idx += 1
    raise MiniYamlError(f"line {lineno}: unterminated single-quoted string")


def _parse_scalar(raw: str, lineno: int) -> Any:
    s = raw.strip()
    if s.startswith('"'):
        return _parse_double_quoted(s, lineno)
    if s.startswith("'"):
        return _parse_single_quoted(s, lineno)
    if s == "true":
        return True
    if s == "false":
        return False
    if _INT_RE.fullmatch(s):
        return int(s)
    return s


def _parse_flow_list(raw: str, lineno: int) -> list[Any]:
    s = raw.strip()
    if not s.endswith("]"):
        raise MiniYamlError(f"line {lineno}: unterminated list")
    inner = s[1:-1].strip()
    if not inner:
        return []

    items: list[str] = []
    start = 0
    quote: str | None = None
    escaped = False
    for idx, ch in enumerate(inner):
        if quote == '"':
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                quote = None
        elif quote == "'":
            if ch == "'":
                quote = None
        else:
            if ch in ('"', "'"):
                quote = ch
            elif ch == ",":
                items.append(inner[start:idx].strip())
                start = idx + 1
    if quote is not None:
        raise MiniYamlError(f"line {lineno}: unterminated quote in list")
    items.append(inner[start:].strip())

    result: list[Any] = []
    for item in items:
        if not item:
            raise MiniYamlError(f"line {lineno}: empty item in list")
        result.append(_parse_scalar(item, lineno))
    return result


def _parse_value(raw: str, lineno: int) -> Any:
    s = raw.strip()
    if s.startswith("["):
        return _parse_flow_list(s, lineno)
    return _parse_scalar(s, lineno)


def _find_colon(content: str) -> int | None:
    quote: str | None = None
    escaped = False
    for idx, ch in enumerate(content):
        if quote == '"':
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                quote = None
        elif quote == "'":
            if ch == "'":
                quote = None
        else:
            if ch in ('"', "'"):
                quote = ch
            elif ch == ":" and (idx + 1 == len(content) or content[idx + 1] == " "):
                return idx
    return None


def _split_key_value(content: str, lineno: int) -> tuple[str, str | None]:
    idx = _find_colon(content)
    if idx is None:
        raise MiniYamlError(f"line {lineno}: expected 'key: value'")
    raw_key = content[:idx].strip()
    if not raw_key:
        raise MiniYamlError(f"line {lineno}: empty key")
    if raw_key.startswith('"'):
        key = _parse_double_quoted(raw_key, lineno)
    elif raw_key.startswith("'"):
        key = _parse_single_quoted(raw_key, lineno)
    else:
        key = raw_key
    rest = content[idx + 1 :].strip()
    return key, (rest if rest else None)


def _has_key_value(content: str) -> bool:
    return _find_colon(content) is not None


def loads(text: str) -> dict[str, Any]:
    """Parse a restricted 2-space-indented YAML subset into a dictionary."""
    root: dict[str, Any] = {}
    stack: list[tuple[str, int, Any]] = [("dict", 0, root)]
    pending: tuple[dict[str, Any], str, int] | None = None

    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        stripped_left = raw_line.lstrip(" \t")
        leading = raw_line[: len(raw_line) - len(stripped_left)]
        if "\t" in leading:
            raise MiniYamlError(f"line {lineno}: tabs are not allowed for indentation")

        clean = _strip_comment(raw_line)
        if not clean.strip():
            continue

        indent = len(clean) - len(clean.lstrip(" "))
        if indent % 2 != 0:
            raise MiniYamlError(f"line {lineno}: odd indentation ({indent})")
        content = clean[indent:]

        if pending is not None:
            p_dict, p_key, p_indent = pending
            pending = None
            if indent > p_indent:
                if indent != p_indent + 2:
                    raise MiniYamlError(f"line {lineno}: unexpected indentation ({indent})")
                if content.startswith("- ") or content == "-":
                    new_list: list[Any] = []
                    p_dict[p_key] = new_list
                    stack.append(("list", indent, new_list))
                else:
                    new_dict: dict[str, Any] = {}
                    p_dict[p_key] = new_dict
                    stack.append(("dict", indent, new_dict))
            else:
                p_dict[p_key] = {}

        while len(stack) > 1 and stack[-1][1] > indent:
            stack.pop()

        kind, top_indent, container = stack[-1]
        if top_indent != indent:
            raise MiniYamlError(f"line {lineno}: unexpected indentation ({indent})")

        if kind == "list":
            if not (content.startswith("- ") or content == "-"):
                raise MiniYamlError(f"line {lineno}: expected list item '-'")
            item_str = content[2:].lstrip(" ") if content != "-" else ""
            if item_str == "":
                item_dict: dict[str, Any] = {}
                container.append(item_dict)
                stack.append(("dict", indent + 2, item_dict))
            elif _has_key_value(item_str):
                k, v_str = _split_key_value(item_str, lineno)
                item_dict = {}
                container.append(item_dict)
                stack.append(("dict", indent + 2, item_dict))
                if v_str is None:
                    pending = (item_dict, k, indent + 2)
                else:
                    item_dict[k] = _parse_value(v_str, lineno)
            else:
                container.append(_parse_value(item_str, lineno))
        else:
            if content.startswith("- ") or content == "-":
                raise MiniYamlError(f"line {lineno}: unexpected list item in mapping")
            k, v_str = _split_key_value(content, lineno)
            if v_str is None:
                pending = (container, k, indent)
            else:
                container[k] = _parse_value(v_str, lineno)

    if pending is not None:
        p_dict, p_key, _ = pending
        p_dict[p_key] = {}

    return root
