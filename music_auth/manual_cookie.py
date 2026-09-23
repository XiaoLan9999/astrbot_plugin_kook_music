"""Parse only explicitly submitted first-party NetEase cookies, never browser stores."""

from .netease_backend import _COOKIE_NAMES, _cookies


def parse_netease_cookie(text):
    if not isinstance(text, str) or not 1 <= len(text) <= 8192:
        raise ValueError("Cookie 长度无效。")
    if "\r" in text or "\n" in text or "\0" in text:
        raise ValueError("请粘贴单行网易云 Cookie 请求头值，不要粘贴完整请求。")
    text = text.strip()
    if text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1].strip()
    values = {}
    for part in text.split(";"):
        part = part.strip()
        if not part:
            continue
        key, separator, value = part.partition("=")
        key, value = key.strip(), value.strip()
        if not separator:
            raise ValueError("Cookie 格式无效，请复制请求头 Cookie 的值。")
        if key not in _COOKIE_NAMES:
            continue
        if key in values:
            raise ValueError("Cookie 中存在重复的账号字段。")
        values[key] = value
    clean = _cookies(values)
    if len(clean) != len(values) or not clean.get("MUSIC_U"):
        raise ValueError("请确认 Cookie 包含有效的网易云 MUSIC_U 登录字段。")
    return {"cookies": clean}
