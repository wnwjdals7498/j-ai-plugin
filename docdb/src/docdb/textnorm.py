"""텍스트 정규화, 직렬화, 토큰 추정 및 해시 유틸리티 모듈."""
import hashlib
import json
import math
import unicodedata
from typing import Any

ASCII_PER_TOK = 3.5
NONASCII_PER_TOK = 1.0
REMOVE_KEY_CHARS = set("_-·.:()[]/")


def normalize_text(s: str | None) -> str | None:
    """줄바꿈을 LF로 변환하고 NFC로 정규화한다. None은 None을 반환한다."""
    if s is None:
        return None
    norm = s.replace("\r\n", "\n").replace("\r", "\n")
    return unicodedata.normalize("NFC", norm)


def _normalize_obj(val: Any) -> Any:
    """객체 내의 모든 문자열 키와 값을 정규화한다."""
    if isinstance(val, str):
        return normalize_text(val)
    if isinstance(val, dict):
        return {
            (normalize_text(k) if isinstance(k, str) else k): _normalize_obj(v)
            for k, v in val.items()
        }
    if isinstance(val, (list, tuple)):
        return [_normalize_obj(item) for item in val]
    return val


def canonical_json(obj: object) -> str:
    """모든 키와 문자열을 정규화한 후 정렬·압축된 표준 JSON 문자열을 생성한다."""
    norm_obj = _normalize_obj(obj)
    return json.dumps(norm_obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest16(text: str | None) -> str:
    """정규화된 텍스트의 sha256 해시 앞 16자리 소문자 16진수 문자열을 반환한다."""
    norm = normalize_text(text or "") or ""
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def sha256_hex(data: bytes) -> str:
    """바이트 열의 sha256 64자리 16진수 문자열을 반환한다."""
    return hashlib.sha256(data).hexdigest()


def tok_est(text: str | None) -> int:
    """정규화된 텍스트의 토큰 수를 휴리스틱으로 추정한다."""
    if not text:
        return 0
    norm = normalize_text(text)
    if not norm:
        return 0
    a = sum(1 for ch in norm if ord(ch) < 128)
    return math.ceil(a / ASCII_PER_TOK + (len(norm) - a) / NONASCII_PER_TOK)


def q(value: object) -> str:
    """개행 문자를 치환하고 공백이나 빈 문자열인 경우 따옴표로 감싼다."""
    s = str(value).replace("\r\n", "\n").replace("\r", "\n").replace("\n", " ⏎ ")
    return f'"{s}"' if (s == "" or " " in s) else s


def norm_key(s: str) -> str:
    """비교용 키 형식으로 텍스트를 정규화하고 소문자화 및 특수문자를 제거한다."""
    norm = normalize_text(s) or ""
    norm = norm.casefold()
    return "".join(ch for ch in norm if not ch.isspace() and ch not in REMOVE_KEY_CHARS)
