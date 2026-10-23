"""Parse administrator-submitted Bilibili cookie exports without analytics."""

import json
import math
import re
import time


COOKIE_NAMES = frozenset(
    {"SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "buvid3", "buvid4"}
)
_MAX_INPUT_BYTES = 64 * 1024
_MAX_COOKIE_BYTES = 8192
_MAX_ENTRIES = 256
_VALUE = re.compile(r"^[\x21-\x7e]+$")
_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_ESCAPED_JSON_NAME = re.compile(
    r'(?<!\\)("name"\s*:\s*")('
    + "|".join(
        re.escape(name).replace("_", r"(?:_|\\_)") for name in sorted(COOKIE_NAMES)
    )
    + r')(")'
)
_ERRORS = {
    "COOKIE_SIZE": "B站 Cookie 导出内容或认证字段过长。",
    "COOKIE_FORMAT": "B站 Cookie 格式无效，请使用 JSON、Header String 或 Netscape 导出。",
    "COOKIE_SCOPE": "Cookie 域名或路径不适用于 B站官方接口。",
    "COOKIE_CONFLICT": "Cookie 中存在内容不同的同名登录字段，请重新导出同一账号。",
    "COOKIE_SESSDATA": "请确认 Cookie 包含未过期且非空的 SESSDATA 登录字段。",
    "COOKIE_VALUE": "B站 Cookie 认证字段包含无效字符，请重新复制原始导出内容。",
}


class BilibiliCookieInputError(ValueError):
    """A fixed diagnostic that cannot echo submitted credential material."""

    def __init__(self, code):
        self.code = code if code in _ERRORS else "COOKIE_FORMAT"
        super().__init__(_ERRORS[self.code])


def _fail(code="COOKIE_FORMAT"):
    raise BilibiliCookieInputError(code) from None


def _name(value):
    if not isinstance(value, str):
        _fail()
    normalized = value.replace("\\_", "_")
    value = normalized if normalized in COOKIE_NAMES else value
    if not _NAME.fullmatch(value):
        _fail()
    return value


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
    host = domain[1:] if domain.startswith(".") else domain
    if (
        len(host) > 253
        or host != "bilibili.com" and not host.endswith(".bilibili.com")
        or not all(_DOMAIN_LABEL.fullmatch(label) for label in host.split("."))
        or path != "/"
        or host_only is not None and type(host_only) is not bool
        or domain.startswith(".") and host_only is True
    ):
        _fail("COOKIE_SCOPE")


def _expired(value, now, *, zero_is_session=False):
    if value is None:
        return False
    if type(value) not in {int, float} or value < 0 or value > 10**15:
        _fail()
    if isinstance(value, float) and not math.isfinite(value):
        _fail()
    return not (zero_is_session and value == 0) and value <= now


def _add(selected, key, value, *, expired=False):
    key = _name(key)
    if not isinstance(value, str):
        _fail()
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        _fail("COOKIE_VALUE")
    if key not in COOKIE_NAMES:
        return
    if value and (
        not _VALUE.fullmatch(value) or any(char in value for char in ';"\\')
    ):
        _fail("COOKIE_VALUE")
    if expired or not value:
        return
    if key in selected and selected[key] != value:
        _fail("COOKIE_CONFLICT")
    selected[key] = value


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
        _add(selected, key.strip(), value.strip())


def _json(text, selected, now):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                _fail()
            result[key] = value
        return result

    # Only names may be unescaped; credential values must remain byte-for-byte.
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
        _scope(entry.get("domain"), entry.get("path"), entry.get("hostOnly"))
        for field in ("session", "secure", "httpOnly"):
            if field in entry and type(entry[field]) is not bool:
                _fail()
        expired = _expired(entry.get("expirationDate"), now)
        _add(selected, key, value, expired=expired)


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
        if include_subdomains not in {"TRUE", "FALSE"} or secure not in {"TRUE", "FALSE"}:
            _fail()
        _scope(domain, path, include_subdomains == "FALSE")
        if not re.fullmatch(r"[0-9]{1,16}", expires):
            _fail()
        expired = _expired(int(expires), now, zero_is_session=True)
        _add(selected, key, value, expired=expired)


def parse_bilibili_cookie(text):
    """Accept Cookie-Editor exports or the legacy bare SESSDATA value."""
    text = _unwrap(text)
    selected = {}
    if text.startswith(("[", "{")):
        _json(text, selected, time.time())
    elif text.startswith(("#", "\\#")) or text.split("\n", 1)[0].count("\t") >= 6:
        _netscape(text, selected, time.time())
    elif "=" not in text and ";" not in text and not text.lower().startswith("cookie:"):
        _add(selected, "SESSDATA", text)
    else:
        _header(text, selected)
    if not selected.get("SESSDATA"):
        _fail("COOKIE_SESSDATA")
    if len("; ".join(f"{key}={value}" for key, value in selected.items())) > _MAX_COOKIE_BYTES:
        _fail("COOKIE_SIZE")
    return {"cookies": selected}
