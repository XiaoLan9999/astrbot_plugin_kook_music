"""Classify music links before search, with bounded anonymous official redirects."""

import asyncio
import re
from dataclasses import dataclass, field
from time import monotonic
from urllib.parse import parse_qs, urljoin, urlparse

import aiohttp

from .searcher import MusicSearcher

_NUMBER = re.compile(r"[1-9][0-9]{0,19}\Z")
_MID = re.compile(r"[A-Za-z0-9]{1,64}\Z")
_HASH = re.compile(r"[0-9a-fA-F]{32}\Z")
_BV = re.compile(r"BV[A-Za-z0-9]{10}\Z", re.IGNORECASE)
_NULLS = {"null", "undefined", "none", "nan"}
_REDIRECTS = {301, 302, 303, 307, 308}
_LINK_HINT = re.compile(
    r"[A-Za-z][A-Za-z0-9+.-]*://|\b(?:https?|ftp|file|javascript|data):|(?:^|\s)//|\b(?:www\.|[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}(?:[/:?#]|$))",
    re.IGNORECASE,
)
_INVALID = "链接无效或无法解析，请重新复制平台提供的完整链接；不会将链接作为歌名搜索。"
_PLAYLIST = "这是歌单链接，请使用 #导入歌单 加上该链接。"
_BAD_PLAYLIST = (
    "歌单链接缺少有效 ID（例如 id=null），请重新复制有效歌单链接并使用 #导入歌单。"
)
_VIDEO = "这是视频链接，请使用 #播放 加上该链接或 BV 号。"
_VIDEO_LIST = "这是 B站收藏夹或合集链接，请使用 #播放 加上该链接。"
_UNSUPPORTED = (
    "暂不支持此链接类型，请使用支持的平台单曲链接或直接输入歌名；视频请使用 #播放。"
)


@dataclass(frozen=True)
class MusicLinkResult:
    kind: str
    platform: str = ""
    url: str = field(default="", repr=False)
    id: str = ""
    reason: str = ""
    resource_type: str = ""


def _family(host, suffix):
    return host == suffix or host.endswith("." + suffix)


def _platform(host):
    if _family(host, "music.163.com") or _family(host, "163cn.tv"):
        return "netease"
    if _family(host, "y.qq.com"):
        return "qq"
    if _family(host, "kugou.com"):
        return "kugou"
    if _family(host, "bilibili.com") or host in {"b23.tv", "bili2233.cn"}:
        return "bilibili"
    return ""


def _safe_url(url, expected="", *, upgrade=False):
    try:
        if (
            not isinstance(url, str)
            or not 1 <= len(url) <= 8192
            or any(ord(char) < 32 or ord(char) == 127 for char in url)
        ):
            return "", "", "invalid"
        parsed = urlparse(url)
        if (
            parsed.username is not None
            or parsed.password is not None
            or not parsed.hostname
            or "\\" in parsed.netloc
        ):
            return "", "", "invalid"
        scheme, host, port = parsed.scheme.lower(), parsed.hostname.lower(), parsed.port
        if scheme == "http" and upgrade and port in (None, 80):
            parsed = parsed._replace(scheme="https", netloc=host)
        elif scheme != "https" or port not in (None, 443):
            return "", "", "invalid"
        platform = _platform(host)
        if not platform:
            return "", "", "unsupported"
        if expected and platform != expected:
            return "", expected, "invalid"
        return parsed.geturl(), platform, ""
    except (ValueError, UnicodeError):
        return "", "", "invalid"


def _params(parsed):
    fragment = urlparse(parsed.fragment) if parsed.fragment.startswith("/") else None
    groups = [parse_qs(parsed.query, keep_blank_values=True)]
    groups.append(
        parse_qs(
            fragment.query if fragment else parsed.fragment, keep_blank_values=True
        )
    )
    normalized = []
    for group in groups:
        parameters = {}
        for key, values in group.items():
            parameters.setdefault(key.lower(), []).extend(values)
        normalized.append(parameters)
    return fragment.path if fragment else parsed.path, normalized


def _identifier(groups, keys, pattern=_NUMBER):
    values = [value for group in groups for key in keys for value in group.get(key, [])]
    if not values:
        return "", False
    if (
        len(set(values)) != 1
        or values[0].casefold() in _NULLS
        or not pattern.fullmatch(values[0])
    ):
        return "", True
    return values[0], False


def _classified(kind, platform, url, identifier="", invalid=False, resource_type=""):
    if invalid:
        return MusicLinkResult(
            "invalid",
            platform,
            url,
            reason=_BAD_PLAYLIST
            if kind == "playlist" and platform != "bilibili"
            else _INVALID,
        )
    reason = (
        _VIDEO_LIST
        if kind == "playlist" and platform == "bilibili"
        else _PLAYLIST
        if kind == "playlist"
        else _VIDEO
        if kind == "video"
        else ""
    )
    return MusicLinkResult(kind, platform, url, identifier, reason, resource_type)


def _classify(url, platform):
    parsed = urlparse(url)
    host = parsed.hostname.lower()
    path, groups = _params(parsed)
    path = path.rstrip("/") or "/"
    if platform == "netease":
        match = re.fullmatch(
            r"/(?:m/)?(song|program|dj|playlist|djradio)(?:/([0-9A-Za-z_-]+))?",
            path,
            re.IGNORECASE,
        )
        if match:
            kind = "playlist" if match[1].lower() in {"playlist", "djradio"} else "song"
            identifier, bad = _identifier(groups, ("id",))
            if match[2]:
                bad |= not bool(_NUMBER.fullmatch(match[2])) or bool(
                    identifier and identifier != match[2]
                )
                identifier = match[2] if not bad else ""
            resource = "program" if match[1].lower() == "dj" else match[1].lower()
            return _classified(
                kind, platform, url, identifier, bad or not identifier, resource
            ), False
        short = _family(host, "163cn.tv") and path != "/"
    elif platform == "qq":
        song = re.fullmatch(
            r"/(?:n/)?(?:ryqq(?:_v2)?/songDetail|yqq/song)/([A-Za-z0-9]+)(?:\.html)?",
            path,
            re.IGNORECASE,
        )
        playlist = re.fullmatch(
            r"/(?:n/)?(?:ryqq(?:_v2)?/playlist|yqq/playlist)/([A-Za-z0-9_-]+)(?:\.html)?",
            path,
            re.IGNORECASE,
        )
        if playlist or re.search(
            r"/(?:playlist|taoge)(?:\.html)?$", path, re.IGNORECASE
        ):
            identifier, bad = _identifier(groups, ("id", "disstid", "playlistid"))
            if playlist:
                bad |= not bool(_NUMBER.fullmatch(playlist[1])) or bool(
                    identifier and identifier != playlist[1]
                )
                identifier = playlist[1] if not bad else ""
            return _classified(
                "playlist", platform, url, identifier, bad or not identifier
            ), False
        if song or re.search(
            r"/playsong(?:\.html|/index\.html)?$", path, re.IGNORECASE
        ):
            mid, bad = _identifier(groups, ("songmid", "mid"), _MID)
            numeric, numeric_bad = _identifier(groups, ("songid",))
            identifier = song[1] if song else mid or numeric
            if song:
                bad |= (
                    not bool(_MID.fullmatch(identifier))
                    or identifier.casefold() in _NULLS
                    or identifier.isdigit()
                    and not bool(_NUMBER.fullmatch(identifier))
                )
            return _classified(
                "song", platform, url, identifier, bad or numeric_bad or not identifier
            ), False
        short = path.lower() == "/base/fcgi-bin/u"
    elif platform == "kugou":
        playlist = re.fullmatch(
            r"/(?:yy/special/single|plist/list|special|playlist|songlist)/([A-Za-z0-9_-]+)(?:\.html)?",
            path,
            re.IGNORECASE,
        )
        playlist_path = bool(
            playlist
            or re.search(
                r"/(?:playlist|special|songlist|plist)(?:\.html)?$", path, re.IGNORECASE
            )
        )
        if playlist_path:
            identifier, bad = _identifier(
                groups, ("specialid", "special_id", "playlistid", "listid", "id")
            )
            if playlist:
                raw = playlist[1]
                valid = _NUMBER.fullmatch(raw) or re.fullmatch(
                    r"(?:gcid_[a-z0-9]+|collection_[a-z0-9_-]+)", raw, re.IGNORECASE
                )
                bad |= not bool(valid) or bool(identifier and identifier != raw)
                identifier = raw.lower() if not bad else ""
            return _classified(
                "playlist", platform, url, identifier, bad or not identifier
            ), False
        if re.search(r"/(?:song|mixsong)(?:/[^/]+|\.html)?$", path, re.IGNORECASE):
            identifier, bad = _identifier(groups, ("hash",), _HASH)
            return _classified("song", platform, url, identifier.upper(), bad), False
        short = (
            host == "t.kugou.com"
            and path != "/"
            or host == "m3ws.kugou.com"
            and path == "/share/index.php"
        )
    else:
        video = re.fullmatch(r"/video/(BV[A-Za-z0-9]{10})", path, re.IGNORECASE)
        if video:
            return _classified("video", platform, url, video[1]), False
        if re.fullmatch(r"/\d+/favlist", path) or re.match(
            r"/(?:medialist/(?:detail|play)|list)/", path
        ):
            identifier, bad = _identifier(groups, ("fid",))
            if "/favlist" in path:
                return _classified(
                    "playlist", platform, url, identifier, bad or not identifier
                ), False
            return _classified("playlist", platform, url), False
        short = host in {"b23.tv", "bili2233.cn"} and path != "/"
    return MusicLinkResult("unsupported", platform, url, reason=_UNSUPPORTED), short


async def _expand(url, platform, session):
    visited = set()
    deadline = monotonic() + 10
    for _ in range(5):
        if url in visited:
            return MusicLinkResult("invalid", platform, reason=_INVALID)
        visited.add(url)
        remaining = deadline - monotonic()
        if remaining <= 0:
            return MusicLinkResult("invalid", platform, reason=_INVALID)
        try:
            async with asyncio.timeout(remaining):
                async with session.get(
                    url,
                    allow_redirects=False,
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=aiohttp.ClientTimeout(total=remaining),
                ) as response:
                    status = response.status
                    location = response.headers.get("Location", "")
        except asyncio.CancelledError:
            raise
        except Exception:
            return MusicLinkResult("invalid", platform, reason=_INVALID)
        if status not in _REDIRECTS:
            if status == 200 and platform in {"qq", "kugou"}:
                return MusicLinkResult("song", platform, url)
            if status == 200 and platform == "bilibili":
                return MusicLinkResult("video", platform, url, reason=_VIDEO)
            return MusicLinkResult("invalid", platform, reason=_INVALID)
        if not isinstance(location, str) or not location:
            return MusicLinkResult("invalid", platform, reason=_INVALID)
        try:
            candidate, _, error = _safe_url(urljoin(url, location), platform)
        except (ValueError, UnicodeError):
            return MusicLinkResult("invalid", platform, reason=_INVALID)
        if error:
            return MusicLinkResult("invalid", platform, reason=_INVALID)
        try:
            result, short = _classify(candidate, platform)
        except (ValueError, UnicodeError):
            return MusicLinkResult("invalid", platform, reason=_INVALID)
        if not short and result.kind != "unsupported":
            return result
        url = candidate
    return MusicLinkResult("invalid", platform, reason=_INVALID)


async def resolve_music_link(text, session=None) -> MusicLinkResult:
    """Only ``none`` permits keyword search; all link failures are terminal."""
    if not isinstance(text, str) or len(text) > 16384:
        return MusicLinkResult("invalid", reason=_INVALID)
    text = text.strip()
    if _BV.fullmatch(text):
        return _classified(
            "video", "bilibili", f"https://www.bilibili.com/video/{text}", text
        )
    try:
        markdown = re.search(r"\[[^\]\r\n]*\]\(\s*([^\s)]+)", text)
        destination = markdown[1] if markdown else text
        url = MusicSearcher._extract_http_url(destination)
    except (ValueError, UnicodeError):
        return MusicLinkResult("invalid", reason=_INVALID)
    if not url:
        return (
            MusicLinkResult("unsupported", reason=_UNSUPPORTED)
            if markdown or _LINK_HINT.search(text)
            else MusicLinkResult("none")
        )
    url, platform, error = _safe_url(url, upgrade=True)
    if error:
        return MusicLinkResult(
            error, platform, reason=_INVALID if error == "invalid" else _UNSUPPORTED
        )
    try:
        result, short = _classify(url, platform)
    except (ValueError, UnicodeError):
        return MusicLinkResult("invalid", platform, reason=_INVALID)
    if not short:
        return result
    if session is None:
        async with aiohttp.ClientSession(
            cookie_jar=aiohttp.DummyCookieJar(), trust_env=False
        ) as anonymous:
            return await _expand(url, platform, anonymous)
    if isinstance(session, aiohttp.ClientSession) and (
        not isinstance(session.cookie_jar, aiohttp.DummyCookieJar)
        or session.auth is not None
        or session.trust_env
        or any(key.lower() in {"authorization", "cookie"} for key in session.headers)
    ):
        return MusicLinkResult("invalid", platform, reason=_INVALID)
    return await _expand(url, platform, session)
