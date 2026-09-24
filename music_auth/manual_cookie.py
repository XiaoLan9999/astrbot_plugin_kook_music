"""Parse explicitly submitted first-party cookie exports without retaining analytics."""

import json
import math
import re
import time

_AUTH_NAMES = frozenset({"MUSIC_U", "__csrf", "MUSIC_A", "MUSIC_R_T"})
_MAX_INPUT_BYTES = 64 * 1024
_MAX_COOKIE_BYTES = 8192
_MAX_ENTRIES = 256
_TOKEN = re.compile(r"^[\x21-\x7e]+$")
_ESCAPED_JSON_NAME = re.compile(
    r'(?<!\\)("name"\s*:\s*")('
    + "|".join(
        re.escape(name).replace("_", r"(?:_|\\_)") for name in sorted(_AUTH_NAMES)
    )
    + r')(")'
)
_ERRORS = {
    "COOKIE_SIZE": "Cookie 导出内容或认证字段过长。",
    "COOKIE_FORMAT": "Cookie 格式无效，请使用 Header String、JSON 或 Netscape 导出。",
    "COOKIE_SCOPE": "Cookie 域名或路径不适用于网易云官方接口。",
    "COOKIE_CONFLICT": "Cookie 中存在同范围但内容不同的账号字段。",
    "COOKIE_MUSIC_U": "请确认 Cookie 包含未过期且有效的 MUSIC_U 登录字段。",
    "COOKIE_VALUE": "Cookie 认证字段包含无效字符，请重新复制原始导出内容。",
}


class CookieInputError(ValueError):
    """A safe fixed diagnostic that never contains submitted cookie material."""

    def __init__(self, code):
        self.code = code if code in _ERRORS else "COOKIE_FORMAT"
        super().__init__(_ERRORS[self.code])


def _fail(code="COOKIE_FORMAT"):
    raise CookieInputError(code) from None


def _name(value):
    if not isinstance(value, str):
        _fail()
    normalized = value.replace("\\_", "_")
    return normalized if normalized in _AUTH_NAMES else value


def _unwrap(text):
    if not isinstance(text, str):
        _fail()
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        _fail()
    if not 1 <= size <= _MAX_INPUT_BYTES:
        _fail("COOKIE_SIZE")
    text = text.lstrip(" \t\r\n\ufeff").rstrip(" \r\n")
    if text.startswith("```"):
        lines = text.splitlines()
        if (
            len(lines) < 3
            or lines[0].strip().lower()
            not in {"```", "```json", "```text", "```txt", "```cookie", "```netscape"}
            or lines[-1].strip() != "```"
        ):
            _fail()
        text = "\n".join(lines[1:-1]).lstrip(" \t\r\n\ufeff").rstrip(" \r\n")
    if not text or "\0" in text:
        _fail()
    return text


def _scope(domain, path, host_only):
    if not isinstance(domain, str) or not isinstance(path, str):
        _fail("COOKIE_SCOPE")
    domain = domain.lower()
    if domain not in {"music.163.com", ".music.163.com", ".163.com", "163.com"}:
        _fail("COOKIE_SCOPE")
    if host_only is not None and type(host_only) is not bool:
        _fail("COOKIE_SCOPE")
    if domain.lstrip(".") == "163.com" and (
        host_only or not domain.startswith(".") and host_only is None
    ):
        _fail("COOKIE_SCOPE")
    if path not in {"/", "/weapi", "/weapi/"}:
        _fail("COOKIE_SCOPE")
    # A flattened request cookie must work for both account and playback APIs.
    return (len(path), int(domain.lstrip(".") == "music.163.com"), int(bool(host_only)))


def _expired(value, now, *, zero_is_session=False):
    if value is None:
        return False
    if type(value) not in {int, float} or value < 0 or value > 10**15:
        _fail()
    if isinstance(value, float) and not math.isfinite(value):
        _fail()
    return not (zero_is_session and value == 0) and value <= now


def _add(selected, key, value, priority):
    key = _name(key)
    if not isinstance(value, str):
        _fail()
    if key not in _AUTH_NAMES or key != "MUSIC_U" and value == "":
        return
    if value and (
        not _TOKEN.fullmatch(value) or any(char in value for char in ';,"\\')
    ):
        _fail("COOKIE_VALUE")
    scopes = selected.setdefault(key, {})
    if priority in scopes and scopes[priority] != value:
        _fail("COOKIE_CONFLICT")
    scopes[priority] = value


def _header(text, selected):
    if any(ord(char) < 32 and char != "\t" for char in text) or "\x7f" in text:
        _fail()
    if text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1].strip()
    parts = text.split(";")
    if len(parts) > _MAX_ENTRIES:
        _fail("COOKIE_SIZE")
    for part in parts:
        if not part.strip():
            continue
        key, separator, value = part.strip().partition("=")
        if not separator:
            _fail()
        _add(selected, key.strip(), value.strip(), (0, 0, 0))


def _json(text, selected, now):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                _fail()
            result[key] = value
        return result

    # Chat Markdown may escape underscores in field names. Never rewrite values.
    text = _ESCAPED_JSON_NAME.sub(
        lambda match: match[1] + match[2].replace("\\_", "_") + match[3], text
    )
    try:
        entries = json.loads(text, object_pairs_hook=unique_object)
    except (ValueError, RecursionError):
        _fail()
    if not isinstance(entries, list):
        _fail()
    if len(entries) > _MAX_ENTRIES:
        _fail("COOKIE_SIZE")
    for entry in entries:
        if not isinstance(entry, dict):
            _fail()
        key, value = entry.get("name"), entry.get("value")
        if not isinstance(key, str) or not isinstance(value, str):
            _fail()
        priority = _scope(entry.get("domain"), entry.get("path"), entry.get("hostOnly"))
        if "session" in entry and type(entry["session"]) is not bool:
            _fail()
        if _expired(entry.get("expirationDate"), now):
            continue
        _add(selected, key, value, priority)


def _netscape(text, selected, now):
    count = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        if line.startswith("\\#HttpOnly_"):
            line = line[1:]
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_") :]
        elif line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 7:
            _fail()
        count += 1
        if count > _MAX_ENTRIES:
            _fail("COOKIE_SIZE")
        domain, include_subdomains, path, secure, expires, key, value = fields
        if include_subdomains not in {"TRUE", "FALSE"} or secure not in {
            "TRUE",
            "FALSE",
        }:
            _fail()
        priority = _scope(domain, path, include_subdomains == "FALSE")
        if not re.fullmatch(r"[0-9]{1,16}", expires):
            _fail()
        if _expired(int(expires), now, zero_is_session=True):
            continue
        _add(selected, key, value, priority)


def parse_netease_cookie(text):
    """Accept Header String, Cookie-Editor JSON, or a Netscape cookie file."""
    text = _unwrap(text)
    selected = {}
    if text.startswith(("[", "{")):
        _json(text, selected, time.time())
    elif text.startswith(("#", "\\#")) or text.split("\n", 1)[0].count("\t") >= 6:
        _netscape(text, selected, time.time())
    else:
        _header(text, selected)
    values = {key: scopes[max(scopes)] for key, scopes in selected.items()}
    if not values.get("MUSIC_U"):
        _fail("COOKIE_MUSIC_U")
    for value in values.values():
        if not _TOKEN.fullmatch(value) or any(char in value for char in ';,"\\'):
            _fail("COOKIE_VALUE")
    if (
        len("; ".join(f"{key}={value}" for key, value in values.items()))
        > _MAX_COOKIE_BYTES
    ):
        _fail("COOKIE_SIZE")
    return {"cookies": values}
