"""
歌单导入模块。

支持通过网易云、QQ 音乐和酷狗音乐歌单链接/ID 批量导入歌曲到播放队列。
支持导入网易云电台(djradio)节目列表。
使用免登录第三方 API，无需本地部署 NeteaseCloudMusicApi。
"""

import asyncio
import hashlib
import json
import logging
import re
import time
from urllib.parse import parse_qs, urljoin, urlparse

import aiohttp

from .model import Song
from .searcher import MusicSearcher

logger = logging.getLogger("astrbot")

# 从链接中提取歌单 ID 的正则
_NETEASE_PLAYLIST_ID_PATTERN = re.compile(
    r"(?:playlist\?|playlist/|id=)(\d+)", re.IGNORECASE
)

# 从链接中提取电台 ID 的正则
_NETEASE_DJRADIO_ID_PATTERN = re.compile(
    r"(?:djradio\?|djradio/|djradio.*id=)(\d+)", re.IGNORECASE
)

# 从链接中提取单曲/电台节目 ID 的正则
_NETEASE_SONG_LINK_ID_PATTERN = re.compile(
    r"(?:song\?id=|song/|song.*?[?&]id=)(\d+)", re.IGNORECASE
)
_NETEASE_PROGRAM_LINK_ID_PATTERN = re.compile(
    r"(?:program\?id=|program/|program.*?[?&]id=|/dj\?id=|dj\?id=)(\d+)",
    re.IGNORECASE,
)

# 从电台网页中提取节目链接的正则
_PROGRAM_ID_FROM_PAGE = re.compile(r'program\?id=(\d+)')

# 从 meting 返回的 url/pic/lrc 地址中提取歌曲 ID
_METING_SONG_ID_PATTERN = re.compile(r"[?&]id=(\d+)")

_QQ_PLAYLIST_PATH_PATTERN = re.compile(
    r"/(?:n/)?(?:ryqq(?:_v2)?/playlist|yqq/playlist)/(\d+)(?:\.html)?(?:/|$)",
    re.IGNORECASE,
)
_KUGOU_GCID_PATTERN = re.compile(r"\b(gcid_[a-z0-9]+)\b", re.IGNORECASE)
_KUGOU_COLLECTION_PATTERN = re.compile(
    r"\b(collection_[a-z0-9_-]+)\b", re.IGNORECASE
)
_KUGOU_SPECIAL_PATH_PATTERNS = (
    re.compile(r"/yy/special/single/(\d+)(?:\.html)?(?:/|$)", re.IGNORECASE),
    re.compile(r"/plist/list/(\d+)(?:\.html)?(?:/|$)", re.IGNORECASE),
    re.compile(r"/(?:special|playlist)/(\d+)(?:\.html)?(?:/|$)", re.IGNORECASE),
)

_PLAYLIST_PLATFORM_ALIASES = {
    "netease": "netease",
    "网易": "netease",
    "网易云": "netease",
    "网易云音乐": "netease",
    "qq": "qq",
    "qq音乐": "qq",
    "腾讯": "qq",
    "腾讯音乐": "qq",
    "kugou": "kugou",
    "kg": "kugou",
    "酷狗": "kugou",
    "酷狗音乐": "kugou",
}


class PlaylistImporter:
    """歌单导入器"""

    # 免登录歌单 API（meting API）
    NETEASE_PLAYLIST_API = "https://api.qijieya.cn/meting/?type=playlist&id={playlist_id}"
    NETEASE_PLAYLIST_DETAIL_API = (
        "https://music.163.com/api/v6/playlist/detail?id={playlist_id}&n=100000&s=8"
    )
    NETEASE_SONG_API = "https://api.qijieya.cn/meting/?type=song&id={song_id}"
    NETEASE_SONG_DETAIL_API = "https://music.163.com/api/song/detail/?id={song_id}&ids=[{song_ids}]"
    QQ_PLAYLIST_API = "https://u.y.qq.com/cgi-bin/musicu.fcg"
    QQ_LEGACY_PLAYLIST_API = (
        "https://c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg"
    )
    QQ_PLAYLIST_PAGE_SIZE = 500
    KUGOU_SPECIAL_SONG_API = "https://mobiles.kugou.com/api/v3/special/song"
    KUGOU_PLAYLIST_INFO_V2_API = (
        "https://mobiles.kugou.com/api/v5/special/info_v2"
    )
    KUGOU_PLAYLIST_SONG_V2_API = (
        "https://mobiles.kugou.com/api/v5/special/song_v2"
    )
    KUGOU_GCID_DECODE_API = "https://t.kugou.com/v1/songlist/batch_decode"
    KUGOU_COLLECTION_API = (
        "https://gateway.kugou.com/pubsongs/v2/get_other_list_file_nofilt"
    )
    KUGOU_COLLECTION_PAGE_SIZE = 300
    KUGOU_APP_ID = 1005
    KUGOU_CLIENT_VERSION = 20489
    KUGOU_DECODE_CLIENT_VERSION = 20109
    KUGOU_SIGNATURE_SALT = "OIlwieks28dk2k092lksi2UIkp"
    KUGOU_WEB_APP_ID = 1058
    KUGOU_WEB_SOURCE_APP_ID = 2919
    KUGOU_WEB_CLIENT_VERSION = 20000
    KUGOU_WEB_DEVICE_ID = "1586163242519"
    KUGOU_WEB_SIGNATURE_SALT = "NVPh5oo715z5DIWAeQlhMDsWXXQV4hwt"
    KUGOU_PLAYLIST_WEB_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 11_0 like Mac OS X) "
            "AppleWebKit/604.1.38 (KHTML, like Gecko) Version/11.0 "
            "Mobile/15A372 Safari/604.1"
        ),
        "Referer": "https://m3ws.kugou.com/share/index.php",
    }

    def __init__(self):
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    @staticmethod
    def parse_playlist_input(text: str) -> tuple[str, str]:
        """解析网易云、QQ 音乐或酷狗音乐歌单链接/ID。

        Args:
            text: 用户输入（歌单 ID 或包含 ID 的链接）

        Returns:
            (type, id) — type 为 netease、djradio、qq 或 kugou
            id 为空字符串表示解析失败
        """
        text = text.strip()
        if not text:
            return "netease", ""

        url_match = re.search(r"https?://[^\s]+", text, re.IGNORECASE)
        if url_match:
            url = url_match.group(0).rstrip(",.;，。；>)]】）")
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
            path_and_fragment = f"{parsed.path}#{parsed.fragment}"
            query = {
                key.lower(): values for key, values in parse_qs(parsed.query).items()
            }
            fragment_query = {
                key.lower(): values
                for key, values in parse_qs(
                    parsed.fragment.split("?", 1)[1]
                    if "?" in parsed.fragment
                    else ""
                ).items()
            }

            if PlaylistImporter._host_matches(host, "music.163.com"):
                if "djradio" in path_and_fragment.lower():
                    target_id = PlaylistImporter._first_numeric_param(
                        query, fragment_query
                    )
                    if not target_id:
                        match = _NETEASE_DJRADIO_ID_PATTERN.search(url)
                        target_id = match.group(1) if match else ""
                    return "djradio", target_id

                target_id = PlaylistImporter._first_numeric_param(
                    query, fragment_query
                )
                if not target_id:
                    match = _NETEASE_PLAYLIST_ID_PATTERN.search(url)
                    target_id = match.group(1) if match else ""
                return "netease", target_id

            if PlaylistImporter._host_matches(host, "y.qq.com"):
                match = _QQ_PLAYLIST_PATH_PATTERN.search(parsed.path)
                target_id = match.group(1) if match else ""
                if not target_id and (
                    "playlist" in parsed.path.lower()
                    or "taoge" in parsed.path.lower()
                ):
                    target_id = PlaylistImporter._first_numeric_param(
                        query, fragment_query
                    )
                return "qq", target_id

            if PlaylistImporter._host_matches(host, "kugou.com"):
                match = _KUGOU_GCID_PATTERN.search(url)
                if match:
                    return "kugou", match.group(1).lower()
                match = _KUGOU_COLLECTION_PATTERN.search(url)
                if match:
                    return "kugou", match.group(1).lower()
                for pattern in _KUGOU_SPECIAL_PATH_PATTERNS:
                    match = pattern.search(parsed.path)
                    if match:
                        return "kugou", match.group(1)
                for key in ("specialid", "special_id", "playlistid", "listid"):
                    values = query.get(key)
                    if values and str(values[0]).isdigit():
                        return "kugou", str(values[0])
                if any(
                    keyword in parsed.path.lower()
                    for keyword in ("playlist", "special", "songlist", "plist")
                ):
                    target_id = PlaylistImporter._first_numeric_param(query)
                    return "kugou", target_id
                return "kugou", ""

            return "netease", ""

        normalized = re.sub(r"\s*[:：]\s*", " ", text).strip()
        parts = normalized.split()
        if len(parts) == 2:
            first_platform = _PLAYLIST_PLATFORM_ALIASES.get(parts[0].lower())
            last_platform = _PLAYLIST_PLATFORM_ALIASES.get(parts[1].lower())
            if first_platform:
                return PlaylistImporter._parse_explicit_playlist_id(
                    first_platform, parts[1]
                )
            if last_platform:
                return PlaylistImporter._parse_explicit_playlist_id(
                    last_platform, parts[0]
                )

        match = _KUGOU_GCID_PATTERN.fullmatch(text)
        if match:
            return "kugou", match.group(1).lower()
        match = _KUGOU_COLLECTION_PATTERN.fullmatch(text)
        if match:
            return "kugou", match.group(1).lower()

        # 兼容旧版无协议网易云输入，例如 playlist?id=123。
        if "djradio" in text.lower():
            match = _NETEASE_DJRADIO_ID_PATTERN.search(text)
            if match:
                return "djradio", match.group(1)
        if "playlist" in text.lower():
            match = _NETEASE_PLAYLIST_ID_PATTERN.search(text)
            if match:
                return "netease", match.group(1)

        # 裸数字保持历史行为，默认视为网易云歌单 ID。
        if text.isdigit():
            return "netease", text
        return "netease", ""

    async def resolve_playlist_input(self, text: str) -> tuple[str, str]:
        """解析歌单输入，并安全展开不含歌单 ID 的 QQ 官方短链接。"""
        parsed = self.parse_playlist_input(text)
        if parsed[1]:
            return parsed

        url_match = re.search(r"https?://[^\s]+", text, re.IGNORECASE)
        if not url_match:
            return parsed
        current_url = url_match.group(0).rstrip(",.;，。；>)]】）")
        initial_url = urlparse(current_url)
        host = (initial_url.hostname or "").lower()
        if initial_url.scheme.lower() != "https" or not self._host_matches(
            host, "y.qq.com"
        ):
            return parsed

        session = await self._get_session()
        for _ in range(6):
            try:
                async with session.get(
                    current_url,
                    allow_redirects=False,
                    headers={
                        "User-Agent": MusicSearcher.HEADERS["User-Agent"],
                        "Referer": "https://y.qq.com/",
                    },
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    location = resp.headers.get("Location", "")
                    response_url = str(resp.url)
                    if resp.status not in {301, 302, 303, 307, 308}:
                        location = ""
            except Exception as e:
                logger.warning(f"[KookMusic] QQ音乐短链接解析异常: {e}")
                return parsed

            candidate = urljoin(response_url or current_url, location)
            if not location:
                return self.parse_playlist_input(response_url or current_url)
            candidate_url = urlparse(candidate)
            candidate_host = (candidate_url.hostname or "").lower()
            if candidate_url.scheme.lower() != "https" or not self._host_matches(
                candidate_host, "y.qq.com"
            ):
                logger.warning(
                    f"[KookMusic] 拒绝 QQ 短链接跳转到非官方域名: {candidate_host}"
                )
                return parsed
            resolved = self.parse_playlist_input(candidate)
            if resolved[1]:
                return resolved
            current_url = candidate

        logger.warning("[KookMusic] QQ音乐短链接重定向次数过多")
        return parsed

    @staticmethod
    def _host_matches(host: str, domain: str) -> bool:
        return host == domain or host.endswith("." + domain)

    @staticmethod
    def _first_numeric_param(*param_groups: dict) -> str:
        for params in param_groups:
            for key in ("id", "playlistid", "disstid", "specialid", "listid"):
                values = params.get(key)
                if values and str(values[0]).isdigit():
                    return str(values[0])
        return ""

    @staticmethod
    def _parse_explicit_playlist_id(platform: str, value: str) -> tuple[str, str]:
        value = value.strip()
        if platform in {"netease", "qq"}:
            return platform, value if value.isdigit() else ""
        if platform == "kugou":
            if value.isdigit():
                return platform, value
            match = _KUGOU_GCID_PATTERN.fullmatch(value)
            if match:
                return platform, match.group(1).lower()
            match = _KUGOU_COLLECTION_PATTERN.fullmatch(value)
            if match:
                return platform, match.group(1).lower()
        return platform, ""

    async def import_qq_playlist(
        self,
        playlist_id: str,
        requester_id: str = "",
        requester_name: str = "",
    ) -> list[Song]:
        """分页导入 QQ 音乐歌单，播放地址在真正播放前再解析。"""
        session = await self._get_session()
        legacy_songs = await self._fetch_qq_legacy_playlist(
            session, playlist_id, requester_id, requester_name
        )
        if legacy_songs is not None:
            logger.info(
                f"[KookMusic] QQ音乐歌单 {playlist_id} 导入成功: "
                f"{len(legacy_songs)} 首歌曲"
            )
            return legacy_songs

        # 旧接口故障时回退到当前 musicu 分页接口；分页按固定步长推进。
        for attempt in range(2):
            songs = await self._fetch_qq_playlist_once(
                session, playlist_id, requester_id, requester_name
            )
            if songs is not None:
                logger.info(
                    f"[KookMusic] QQ音乐歌单 {playlist_id} 导入成功: "
                    f"{len(songs)} 首歌曲"
                )
                return songs
            if attempt == 0:
                logger.warning(
                    f"[KookMusic] QQ音乐歌单 {playlist_id} 分页期间发生变化，"
                    "正在重新读取"
                )
        return []

    async def _fetch_qq_legacy_playlist(
        self,
        session: aiohttp.ClientSession,
        playlist_id: str,
        requester_id: str,
        requester_name: str,
    ) -> list[Song] | None:
        try:
            async with session.get(
                self.QQ_LEGACY_PLAYLIST_API,
                params={
                    "type": 1,
                    "utf8": 1,
                    "format": "json",
                    "disstid": playlist_id,
                },
                headers={
                    "User-Agent": MusicSearcher.HEADERS["User-Agent"],
                    "Referer": "https://y.qq.com/",
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    return None
                result = await resp.json(content_type=None)
        except Exception as e:
            logger.warning(f"[KookMusic] QQ音乐完整歌单接口读取异常: {e}")
            return None

        cdlist = result.get("cdlist", []) if isinstance(result, dict) else []
        if (
            not isinstance(result, dict)
            or result.get("code") != 0
            or not isinstance(cdlist, list)
            or not cdlist
        ):
            return None
        playlist = cdlist[0] if isinstance(cdlist[0], dict) else {}
        response_id = str(playlist.get("disstid", "") or "")
        if response_id and response_id != playlist_id:
            logger.warning(
                f"[KookMusic] QQ音乐完整歌单 ID 不匹配: "
                f"请求 {playlist_id}, 返回 {response_id}"
            )
            return None
        raw_songs = playlist.get("songlist", [])
        if not isinstance(raw_songs, list):
            return None
        try:
            total = int(playlist.get("total_song_num", len(raw_songs)))
        except (TypeError, ValueError):
            return None
        if len(raw_songs) != total:
            logger.warning(
                f"[KookMusic] QQ音乐完整歌单返回不完整: "
                f"expected={total}, actual={len(raw_songs)}"
            )
            return None

        songs: list[Song] = []
        for index, item in enumerate(raw_songs, start=1):
            song = MusicSearcher._parse_qq_track(item, require_free=False)
            if song is None:
                song = self._qq_unavailable_placeholder(item, index)
            song.requester_id = requester_id
            song.requester_name = requester_name
            songs.append(song)
        return songs

    @staticmethod
    def _qq_unavailable_placeholder(item: object, index: int) -> Song:
        item = item if isinstance(item, dict) else {}
        song_id = str(
            item.get("mid", "")
            or item.get("songmid", "")
            or item.get("id", "")
            or item.get("songid", "")
            or f"unavailable-{index}"
        )
        singers = item.get("singer") if isinstance(item.get("singer"), list) else []
        artists = "、".join(
            str(singer.get("name", ""))
            for singer in singers
            if isinstance(singer, dict) and singer.get("name")
        )
        return Song(
            id=song_id,
            name=str(
                item.get("name", "")
                or item.get("songname", "")
                or item.get("title", "")
                or "已下架歌曲"
            ),
            artists=artists or "未知歌手",
            platform="qq",
            extra_headers=dict(MusicSearcher.QQ_AUDIO_HEADERS),
            unplayable_reason="QQ 音乐已下架或缺少可解析歌曲 ID",
            provider_data={"resolver_status": "denied"},
        )

    async def _fetch_qq_playlist_once(
        self,
        session: aiohttp.ClientSession,
        playlist_id: str,
        requester_id: str,
        requester_name: str,
    ) -> list[Song] | None:
        songs: list[Song] = []
        begin = 0
        first_mtime: str | None = None
        expected_total: int | None = None
        consumed_count = 0
        filtered_count = 0
        invalid_count = 0

        for _page in range(2000):
            body = {
                "comm": MusicSearcher._qq_web_comm(),
                "req_0": {
                    "module": "music.srfDissInfo.aiDissInfo",
                    "method": "uniform_get_Dissinfo",
                    "param": {
                        "disstid": int(playlist_id),
                        "enc_host_uin": "",
                        "tag": 1,
                        "userinfo": 1,
                        "song_begin": begin,
                        "song_num": self.QQ_PLAYLIST_PAGE_SIZE,
                    },
                },
            }
            try:
                async with session.post(
                    self.QQ_PLAYLIST_API,
                    json=body,
                    headers={
                        **MusicSearcher.HEADERS,
                        "Referer": f"https://y.qq.com/n/ryqq/playlist/{playlist_id}",
                        "Origin": "https://y.qq.com",
                        "Content-Type": "application/json",
                    },
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status != 200:
                        logger.warning(
                            f"[KookMusic] QQ音乐歌单读取失败: HTTP {resp.status}"
                        )
                        return []
                    result = await resp.json(content_type=None)
            except Exception as e:
                logger.warning(f"[KookMusic] QQ音乐歌单读取异常: {e}")
                return []

            request_data = result.get("req_0") if isinstance(result, dict) else None
            data = request_data.get("data") if isinstance(request_data, dict) else None
            if (
                not isinstance(result, dict)
                or result.get("code") != 0
                or not isinstance(request_data, dict)
                or request_data.get("code") != 0
                or not isinstance(data, dict)
                or data.get("code") != 0
            ):
                logger.warning("[KookMusic] QQ音乐歌单接口返回格式或状态异常")
                return []

            dirinfo = data.get("dirinfo") if isinstance(data.get("dirinfo"), dict) else {}
            response_id = str(dirinfo.get("id", "") or "")
            if response_id and response_id != playlist_id:
                logger.warning(
                    f"[KookMusic] QQ音乐歌单 ID 不匹配: "
                    f"请求 {playlist_id}, 返回 {response_id}"
                )
                return []

            current_mtime = str(dirinfo.get("mtime", "") or "")
            if first_mtime is None:
                first_mtime = current_mtime
            elif current_mtime and first_mtime and current_mtime != first_mtime:
                return None
            try:
                current_total = int(
                    dirinfo.get("songnum", data.get("total_song_num", 0)) or 0
                )
            except (TypeError, ValueError):
                current_total = 0
            if current_total > 0:
                if expected_total is None:
                    expected_total = current_total
                elif current_total != expected_total:
                    return None

            raw_songs = data.get("songlist", [])
            if not isinstance(raw_songs, list):
                return []
            filtered = data.get("filtered_song", [])
            invalid = data.get("invalid_song", [])
            if isinstance(filtered, list):
                filtered_count += len(filtered)
            if isinstance(invalid, list):
                invalid_count += len(invalid)
            consumed_count += len(raw_songs)
            if isinstance(filtered, list):
                consumed_count += len(filtered)
            if isinstance(invalid, list):
                consumed_count += len(invalid)

            for item_index, item in enumerate(raw_songs, start=1):
                song = MusicSearcher._parse_qq_track(item, require_free=False)
                if song is None:
                    invalid_count += 1
                    song = self._qq_unavailable_placeholder(
                        item, begin + item_index
                    )
                song.requester_id = requester_id
                song.requester_name = requester_name
                songs.append(song)
            unavailable = []
            if isinstance(filtered, list):
                unavailable.extend(filtered)
            if isinstance(invalid, list):
                unavailable.extend(invalid)
            for item_index, item in enumerate(unavailable, start=1):
                song = self._qq_unavailable_placeholder(
                    item, begin + len(raw_songs) + item_index
                )
                song.requester_id = requester_id
                song.requester_name = requester_name
                songs.append(song)

            has_more = bool(data.get("hasmore"))
            if not has_more:
                if expected_total is not None and consumed_count != expected_total:
                    logger.warning(
                        f"[KookMusic] QQ音乐分页歌单内容不完整: "
                        f"expected={expected_total}, actual={consumed_count}"
                    )
                    return None
                if filtered_count or invalid_count:
                    logger.info(
                        f"[KookMusic] QQ音乐歌单 {playlist_id} 已忽略 "
                        f"{filtered_count + invalid_count} 条平台过滤/无效记录"
                    )
                return songs
            if not raw_songs and not filtered and not invalid:
                logger.warning("[KookMusic] QQ音乐歌单分页提前返回空页")
                return []

            # 即使当前页有平台过滤项，也必须按请求页长推进，不能按返回数推进。
            begin += self.QQ_PLAYLIST_PAGE_SIZE

        logger.warning("[KookMusic] QQ音乐歌单分页超过安全上限")
        return []

    async def import_kugou_playlist(
        self,
        playlist_id: str,
        requester_id: str = "",
        requester_name: str = "",
    ) -> list[Song]:
        """导入酷狗数字 specialid 或官网 gcid/global collection 歌单。"""
        session = await self._get_session()
        if playlist_id.isdigit():
            collection_id = await self._resolve_kugou_special_id(
                session, playlist_id
            )
            if collection_id:
                songs = await self._fetch_kugou_collection_playlist(
                    session, collection_id, requester_id, requester_name
                )
            else:
                # 旧数字接口作为临时兼容兜底；正常路径统一使用 song_v2。
                songs = await self._fetch_kugou_special_playlist(
                    session, playlist_id, requester_id, requester_name
                )
        else:
            collection_id = playlist_id
            if playlist_id.lower().startswith("gcid_"):
                collection_id = await self._decode_kugou_gcid(session, playlist_id)
            if not _KUGOU_COLLECTION_PATTERN.fullmatch(collection_id or ""):
                logger.warning(f"[KookMusic] 无法解析酷狗歌单 ID: {playlist_id}")
                return []
            songs = await self._fetch_kugou_collection_playlist(
                session, collection_id, requester_id, requester_name
            )

        if songs:
            logger.info(
                f"[KookMusic] 酷狗音乐歌单 {playlist_id} 导入成功: "
                f"{len(songs)} 首歌曲"
            )
        return songs

    async def _resolve_kugou_special_id(
        self,
        session: aiohttp.ClientSession,
        special_id: str,
    ) -> str:
        params = self._kugou_web_params()
        params.update({"specialid": special_id, "format": "jsonp"})
        params["signature"] = self._kugou_web_signature(params)
        try:
            async with session.get(
                self.KUGOU_PLAYLIST_INFO_V2_API,
                params=params,
                headers=self._kugou_web_headers(params),
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    return ""
                result = await resp.json(content_type=None)
        except Exception as e:
            logger.warning(f"[KookMusic] 酷狗 specialid 转换异常: {e}")
            return ""

        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, dict):
            return ""
        collection_id = str(
            data.get("global_specialid", "")
            or data.get("global_collection_id", "")
            or ""
        ).lower()
        return (
            collection_id
            if _KUGOU_COLLECTION_PATTERN.fullmatch(collection_id)
            else ""
        )

    async def _fetch_kugou_special_playlist(
        self,
        session: aiohttp.ClientSession,
        special_id: str,
        requester_id: str,
        requester_name: str,
    ) -> list[Song]:
        raw_items: list[dict] = []
        total: int | None = None
        page = 1
        page_size = self.KUGOU_COLLECTION_PAGE_SIZE
        while page <= 2000:
            try:
                async with session.get(
                    self.KUGOU_SPECIAL_SONG_API,
                    params={
                        "specialid": special_id,
                        "area_code": 1,
                        "page": page,
                        "plat": 2,
                        "pagesize": page_size,
                        "version": 8990,
                    },
                    headers={
                        "User-Agent": MusicSearcher.HEADERS["User-Agent"],
                        "Referer": "https://www.kugou.com/",
                    },
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status != 200:
                        logger.warning(
                            f"[KookMusic] 酷狗歌单读取失败: HTTP {resp.status}"
                        )
                        return []
                    result = await resp.json(content_type=None)
            except Exception as e:
                logger.warning(f"[KookMusic] 酷狗歌单读取异常: {e}")
                return []

            data = result.get("data") if isinstance(result, dict) else None
            info = data.get("info", []) if isinstance(data, dict) else None
            if (
                not isinstance(result, dict)
                or result.get("status") != 1
                or not isinstance(info, list)
            ):
                return []
            try:
                current_total = int(data.get("total", len(info)))
            except (TypeError, ValueError):
                return []
            if total is None:
                total = current_total
            elif current_total != total:
                logger.warning("[KookMusic] 酷狗歌单在分页读取期间发生变化")
                return []
            raw_items.extend(item for item in info if isinstance(item, dict))
            if len(raw_items) >= total:
                break
            if not info:
                logger.warning("[KookMusic] 酷狗歌单分页提前返回空页")
                return []
            page += 1

        if total is None or len(raw_items) != total:
            logger.warning(
                f"[KookMusic] 酷狗歌单内容不完整: "
                f"expected={total}, actual={len(raw_items)}"
            )
            return []
        return self._parse_kugou_playlist_songs(
            raw_items, requester_id, requester_name
        )

    async def _decode_kugou_gcid(
        self,
        session: aiohttp.ClientSession,
        gcid: str,
    ) -> str:
        body = json.dumps(
            {"ret_info": 1, "data": [{"id": gcid.lower(), "id_type": 2}]},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        params = {
            "dfid": "-",
            "appid": self.KUGOU_APP_ID,
            "mid": "0",
            "clientver": self.KUGOU_DECODE_CLIENT_VERSION,
            "clienttime": int(time.time()),
            "uuid": "-",
        }
        params["signature"] = self._kugou_signature(params, body)
        try:
            async with session.post(
                self.KUGOU_GCID_DECODE_API,
                params=params,
                data=body.encode("utf-8"),
                headers={
                    "User-Agent": MusicSearcher.HEADERS["User-Agent"],
                    "Referer": "https://www.kugou.com/",
                    "Content-Type": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    return ""
                result = await resp.json(content_type=None)
        except Exception as e:
            logger.warning(f"[KookMusic] 酷狗 gcid 解码异常: {e}")
            return ""

        data = result.get("data") if isinstance(result, dict) else None
        items = data.get("list", []) if isinstance(data, dict) else []
        if (
            not isinstance(result, dict)
            or result.get("status") != 1
            or not isinstance(items, list)
            or not items
        ):
            return ""
        item = items[0] if isinstance(items[0], dict) else {}
        return str(item.get("global_collection_id", "") or "").lower()

    async def _fetch_kugou_collection_playlist(
        self,
        session: aiohttp.ClientSession,
        collection_id: str,
        requester_id: str,
        requester_name: str,
    ) -> list[Song]:
        # nofilt 保留官网歌单中的原始顺序和已下架占位，区间序号不会偏移。
        for attempt in range(2):
            raw_items = await self._fetch_kugou_nofilt_once(
                session, collection_id
            )
            if raw_items is not None:
                return self._parse_kugou_playlist_songs(
                    raw_items, requester_id, requester_name
                )
            if attempt == 0:
                logger.warning(
                    f"[KookMusic] 酷狗歌单 {collection_id} 分页期间发生变化，"
                    "正在重新读取"
                )

        logger.warning(
            f"[KookMusic] 酷狗完整歌单接口暂时不可用，"
            f"使用过滤后的 song_v2 兜底: {collection_id}"
        )
        for _attempt in range(2):
            raw_items = await self._fetch_kugou_playlist_v2_once(
                session, collection_id
            )
            if raw_items is not None:
                return self._parse_kugou_playlist_songs(
                    raw_items, requester_id, requester_name
                )
        return []

    async def _fetch_kugou_nofilt_once(
        self,
        session: aiohttp.ClientSession,
        collection_id: str,
    ) -> list[dict] | None:
        raw_items: list[dict] = []
        total: int | None = None
        update_time: str | None = None
        begin = 0

        for _page in range(2000):
            params = {
                "dfid": "-",
                "mid": "0",
                "uuid": "-",
                "appid": self.KUGOU_APP_ID,
                "clientver": self.KUGOU_CLIENT_VERSION,
                "clienttime": int(time.time()),
                "area_code": 1,
                "begin_idx": begin,
                "plat": 1,
                "type": 1,
                "mode": 1,
                "personal_switch": 1,
                "extend_fields": "abtags,hot_cmt,popularization",
                "pagesize": self.KUGOU_COLLECTION_PAGE_SIZE,
                "global_collection_id": collection_id,
            }
            params["signature"] = self._kugou_signature(params)
            try:
                async with session.get(
                    self.KUGOU_COLLECTION_API,
                    params=params,
                    headers={
                        "User-Agent": (
                            "Android15-1070-11083-46-0-DiscoveryDRADProtocol-wifi"
                        ),
                        "dfid": "-",
                        "mid": "0",
                        "clienttime": str(params["clienttime"]),
                    },
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status != 200:
                        return None
                    result = await resp.json(content_type=None)
            except Exception as e:
                logger.warning(f"[KookMusic] 酷狗 nofilt 歌单读取异常: {e}")
                return None

            data = result.get("data") if isinstance(result, dict) else None
            page_items = data.get("songs", []) if isinstance(data, dict) else None
            if (
                not isinstance(result, dict)
                or result.get("error_code") != 0
                or not isinstance(page_items, list)
            ):
                return None
            try:
                current_total = int(data.get("count", len(page_items)))
            except (TypeError, ValueError):
                return None
            list_info = (
                data.get("list_info")
                if isinstance(data.get("list_info"), dict)
                else {}
            )
            current_update = str(list_info.get("update_time", "") or "")
            if total is None:
                total = current_total
                update_time = current_update
            elif current_total != total or (
                update_time and current_update and current_update != update_time
            ):
                return None

            raw_items.extend(item for item in page_items if isinstance(item, dict))
            if len(raw_items) >= total:
                break
            if not page_items:
                logger.warning("[KookMusic] 酷狗 nofilt 歌单分页提前返回空页")
                return None
            begin += self.KUGOU_COLLECTION_PAGE_SIZE

        if total is None or len(raw_items) != total:
            logger.warning(
                f"[KookMusic] 酷狗 nofilt 歌单内容不完整: "
                f"expected={total}, actual={len(raw_items)}"
            )
            return None
        return raw_items

    async def _fetch_kugou_playlist_v2_once(
        self,
        session: aiohttp.ClientSession,
        collection_id: str,
    ) -> list[dict] | None:
        raw_items: list[dict] = []
        total: int | None = None

        for page in range(1, 2001):
            params = self._kugou_web_params()
            params.update({
                "global_specialid": collection_id,
                "specialid": 0,
                "plat": 0,
                "version": 8000,
                "page": page,
                "pagesize": self.KUGOU_COLLECTION_PAGE_SIZE,
            })
            params["signature"] = self._kugou_web_signature(params)
            try:
                async with session.get(
                    self.KUGOU_PLAYLIST_SONG_V2_API,
                    params=params,
                    headers=self._kugou_web_headers(params),
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status != 200:
                        return None
                    result = await resp.json(content_type=None)
            except Exception as e:
                logger.warning(f"[KookMusic] 酷狗 song_v2 歌单读取异常: {e}")
                return None

            data = result.get("data") if isinstance(result, dict) else None
            page_items = data.get("info", []) if isinstance(data, dict) else None
            if isinstance(data, dict) and not page_items:
                page_items = data.get("list", []) or data.get("data", [])
            if (
                not isinstance(result, dict)
                or result.get("status") != 1
                or result.get("errcode", 0) != 0
                or not isinstance(page_items, list)
            ):
                return None
            try:
                current_total = int(data.get("total", len(page_items)))
            except (TypeError, ValueError):
                return None
            if total is None:
                total = current_total
            elif current_total != total:
                return None

            raw_items.extend(item for item in page_items if isinstance(item, dict))
            if len(raw_items) >= total:
                break
            if not page_items:
                logger.warning("[KookMusic] 酷狗 song_v2 歌单分页提前返回空页")
                return None

        if total is None or len(raw_items) != total:
            logger.warning(
                f"[KookMusic] 酷狗 song_v2 歌单内容不完整: "
                f"expected={total}, actual={len(raw_items)}"
            )
            return None
        return raw_items

    @classmethod
    def _parse_kugou_playlist_songs(
        cls,
        items: list[dict],
        requester_id: str,
        requester_name: str,
    ) -> list[Song]:
        songs: list[Song] = []
        for index, item in enumerate(items, start=1):
            song_id = str(
                item.get("hash", "") or item.get("FileHash", "")
            ).upper()
            if not re.fullmatch(r"[0-9A-F]{32}", song_id):
                songs.append(
                    cls._kugou_unavailable_placeholder(
                        item, index, requester_id, requester_name
                    )
                )
                continue

            full_name = str(
                item.get("filename", "")
                or item.get("name", "")
                or item.get("FileName", "")
                or ""
            )
            name = str(
                item.get("songname", "")
                or item.get("SongName", "")
                or ""
            )
            artists = str(
                item.get("singername", "")
                or item.get("SingerName", "")
                or ""
            )
            if " - " in full_name:
                inferred_artist, inferred_name = full_name.split(" - ", 1)
                artists = artists or inferred_artist
                name = name or inferred_name
            name = name or str(item.get("remark", "") or "") or full_name or "未知歌曲"
            artists = artists or "未知歌手"

            trans_param = item.get("trans_param")
            if not isinstance(trans_param, dict):
                trans_param = {}
            cover_url = str(
                item.get("imgurl", "")
                or item.get("Image", "")
                or trans_param.get("union_cover", "")
                or ""
            ).replace("{size}", "400")
            if cover_url.startswith("http://"):
                cover_url = "https://" + cover_url[len("http://"):]

            duration = cls._kugou_duration_ms(item)
            songs.append(
                Song(
                    id=song_id,
                    name=name,
                    artists=artists,
                    duration=duration,
                    cover_url=cover_url,
                    platform="kugou",
                    requester_id=requester_id,
                    requester_name=requester_name,
                    extra_headers=dict(MusicSearcher.KUGOU_API_HEADERS),
                    provider_data={
                        "album_id": str(item.get("album_id", "") or ""),
                        "audio_id": str(
                            item.get("album_audio_id", "")
                            or item.get("audio_id", "")
                            or ""
                        ),
                    },
                )
            )
        return songs

    @classmethod
    def _kugou_unavailable_placeholder(
        cls,
        item: dict,
        index: int,
        requester_id: str,
        requester_name: str,
    ) -> Song:
        full_name = str(
            item.get("filename", "")
            or item.get("name", "")
            or item.get("FileName", "")
            or ""
        )
        artists = "未知歌手"
        name = str(
            item.get("songname", "")
            or item.get("SongName", "")
            or item.get("remark", "")
            or ""
        )
        if " - " in full_name:
            inferred_artist, inferred_name = full_name.split(" - ", 1)
            artists = inferred_artist or artists
            name = name or inferred_name
        return Song(
            id=f"unavailable-{index}",
            name=name or full_name or "已下架歌曲",
            artists=artists,
            duration=cls._kugou_duration_ms(item),
            platform="kugou",
            requester_id=requester_id,
            requester_name=requester_name,
            extra_headers=dict(MusicSearcher.KUGOU_API_HEADERS),
            unplayable_reason="酷狗音乐已下架或缺少可解析 FileHash",
            provider_data={"resolver_status": "denied"},
        )

    @staticmethod
    def _kugou_duration_ms(item: dict) -> int:
        raw_ms = item.get("timelen", 0)
        if raw_ms:
            try:
                return max(0, int(raw_ms))
            except (TypeError, ValueError):
                return 0
        raw_seconds = item.get("duration", 0) or item.get("Duration", 0)
        try:
            return max(0, int(raw_seconds) * 1000)
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _kugou_web_params(cls) -> dict:
        clienttime = str(int(time.time() * 1000))
        return {
            "appid": cls.KUGOU_WEB_APP_ID,
            "srcappid": cls.KUGOU_WEB_SOURCE_APP_ID,
            "clientver": cls.KUGOU_WEB_CLIENT_VERSION,
            "clienttime": clienttime,
            "mid": cls.KUGOU_WEB_DEVICE_ID,
            "uuid": cls.KUGOU_WEB_DEVICE_ID,
            "dfid": "-",
        }

    @classmethod
    def _kugou_web_headers(cls, params: dict) -> dict:
        return {
            **cls.KUGOU_PLAYLIST_WEB_HEADERS,
            "mid": str(params["mid"]),
            "clienttime": str(params["clienttime"]),
            "dfid": str(params["dfid"]),
        }

    @classmethod
    def _kugou_web_signature(cls, params: dict) -> str:
        return cls._kugou_signature(
            params, salt=cls.KUGOU_WEB_SIGNATURE_SALT
        )

    @classmethod
    def _kugou_signature(
        cls,
        params: dict,
        body: str = "",
        salt: str | None = None,
    ) -> str:
        salt = salt or cls.KUGOU_SIGNATURE_SALT
        params_string = "".join(
            f"{key}={params[key]}" for key in sorted(params)
        )
        payload = salt + params_string + body + salt
        return hashlib.md5(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def parse_direct_song_input(text: str) -> tuple[str, str]:
        """解析点歌输入中的网易云单曲或电台节目链接。

        Returns:
            (type, id) — type 为 "song" 或 "program"，id 为空表示未识别。
        """
        text = text.strip()

        # 只允许网易云官方域名进入网易链接正则，避免旧式 QQ 链接中的
        # `/song/0039Mn...` 被截断误识别为网易歌曲 0039。
        url_match = re.search(r"https?://[^\s]+", text, re.IGNORECASE)
        if url_match:
            host = (urlparse(url_match.group(0)).hostname or "").lower()
            is_netease_host = (
                host == "music.163.com"
                or host.endswith(".music.163.com")
                or host == "163cn.tv"
                or host.endswith(".163cn.tv")
            )
            if not is_netease_host:
                return "", ""

        # 电台节目链接中也可能带多个参数，必须优先识别 program。
        m = _NETEASE_PROGRAM_LINK_ID_PATTERN.search(text)
        if m:
            return "program", m.group(1)

        m = _NETEASE_SONG_LINK_ID_PATTERN.search(text)
        if m:
            return "song", m.group(1)

        return "", ""

    async def fetch_netease_song(
        self,
        song_id: str,
        requester_id: str = "",
        requester_name: str = "",
    ) -> Song | None:
        """获取网易云单曲链接对应的 Song。"""
        session = await self._get_session()
        url = self.NETEASE_SONG_API.format(song_id=song_id)

        song: Song | None = None
        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    item = data[0] if isinstance(data, list) and data else data
                    if isinstance(item, dict):
                        song = Song(
                            id=song_id,
                            name=item.get("name", "未知歌曲") or item.get("title", "未知歌曲"),
                            artists=item.get("artist", "未知歌手") or item.get("author", "未知歌手"),
                            audio_url=item.get("url", ""),
                            cover_url=item.get("pic", ""),
                            duration=self._normalize_duration(item.get("duration", 0)),
                            platform="netease",
                            requester_id=requester_id,
                            requester_name=requester_name,
                        )
        except Exception as e:
            logger.debug(f"[KookMusic] 获取单曲 {song_id} 异常: {e}")

        if song is None:
            song = await self._fetch_song_detail_as_song(
                session, song_id, requester_id, requester_name
            )

        if song is None:
            return None

        await self._fill_netease_song_durations(session, [song])
        return song

    async def fetch_netease_program(
        self,
        program_id: str,
        requester_id: str = "",
        requester_name: str = "",
    ) -> Song | None:
        """获取网易云电台节目链接对应的 Song。"""
        session = await self._get_session()
        return await self._fetch_program_detail(
            session, program_id, requester_id, requester_name
        )

    async def import_netease_playlist(
        self,
        playlist_id: str,
        requester_id: str = "",
        requester_name: str = "",
        fill_durations: bool = True,
    ) -> list[Song]:
        """通过免登录 API 获取网易云歌单中的所有歌曲。

        Args:
            playlist_id: 网易云歌单 ID
            requester_id: 请求用户 ID
            requester_name: 请求用户昵称

        Returns:
            Song 对象列表（按歌单原始顺序）
        """
        session = await self._get_session()
        url = self.NETEASE_PLAYLIST_API.format(playlist_id=playlist_id)

        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        f"[KookMusic] 获取歌单失败: HTTP {resp.status}"
                    )
                    track_ids = await self._fetch_playlist_track_ids(
                        session, playlist_id
                    )
                    return self._merge_playlist_order(
                        track_ids, [], requester_id, requester_name
                    ) if track_ids else []

                data = await resp.json(content_type=None)

                if not isinstance(data, list):
                    logger.warning("[KookMusic] 歌单 API 返回格式异常")
                    track_ids = await self._fetch_playlist_track_ids(
                        session, playlist_id
                    )
                    return self._merge_playlist_order(
                        track_ids, [], requester_id, requester_name
                    ) if track_ids else []

                songs: list[Song] = []
                for item in data:
                    song_id = self._extract_meting_song_id(item)
                    song = Song(
                        id=song_id,
                        name=item.get("name", "未知歌曲") or item.get("title", "未知歌曲"),
                        artists=item.get("artist", "未知歌手") or item.get("author", "未知歌手"),
                        audio_url=item.get("url", ""),
                        cover_url=item.get("pic", ""),
                        duration=self._normalize_duration(item.get("duration", 0)),
                        platform="netease",
                        requester_id=requester_id,
                        requester_name=requester_name,
                    )
                    songs.append(song)

                # meting 偶尔可能只返回部分歌曲。用网易云官方详情中的
                # trackIds 校验总数并恢复完整顺序；缺失项先放轻量占位，
                # 用户选定区间后再批量补元数据。
                track_ids = await self._fetch_playlist_track_ids(
                    session, playlist_id
                )
                if track_ids:
                    songs = self._merge_playlist_order(
                        track_ids,
                        songs,
                        requester_id,
                        requester_name,
                    )

                if fill_durations:
                    await self._fill_netease_song_durations(session, songs)

                logger.info(
                    f"[KookMusic] 歌单 {playlist_id} 导入成功: {len(songs)} 首歌曲"
                )
                return songs

        except Exception as e:
            logger.error(f"[KookMusic] 导入歌单异常: {e}")
            return []

    async def fill_netease_song_durations(self, songs: list[Song]):
        """仅为最终选中的歌曲补全时长，避免大型歌单选择前发起大量请求。"""
        if not songs:
            return
        session = await self._get_session()
        await self._fill_netease_song_durations(session, songs)

    async def enrich_netease_songs(self, songs: list[Song]):
        """批量补全最终选中歌曲的名称、歌手、封面和时长。"""
        need_details = [
            song
            for song in songs
            if song.id
            and (
                song.duration <= 0
                or not song.name
                or song.name == "未知歌曲"
                or not song.artists
                or song.artists == "未知歌手"
            )
        ]
        if not need_details:
            return

        session = await self._get_session()
        by_id = {song.id: song for song in need_details}
        for start in range(0, len(need_details), 50):
            batch = need_details[start:start + 50]
            details = await self._fetch_song_details(
                session, [song.id for song in batch]
            )
            for detail in details:
                song_id = str(detail.get("id", ""))
                song = by_id.get(song_id)
                if not song:
                    continue
                name = detail.get("name", "")
                artists_data = detail.get("artists") or detail.get("ar") or []
                artists = "、".join(
                    artist.get("name", "")
                    for artist in artists_data
                    if artist.get("name")
                )
                album = detail.get("album") or detail.get("al") or {}
                if name:
                    song.name = name
                if artists:
                    song.artists = artists
                if not song.cover_url:
                    song.cover_url = album.get("picUrl", "")
                duration = self._extract_duration_from_detail(detail)
                if duration > 0:
                    song.duration = duration

    async def _fetch_playlist_track_ids(
        self,
        session: aiohttp.ClientSession,
        playlist_id: str,
    ) -> list[str]:
        """读取官方 trackIds，并仅在数量完整时用于校验第三方列表。"""
        url = self.NETEASE_PLAYLIST_DETAIL_API.format(playlist_id=playlist_id)
        try:
            async with session.get(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://music.163.com/",
                },
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json(content_type=None)
        except Exception as e:
            logger.debug(f"[KookMusic] 校验歌单完整性失败: {e}")
            return []

        playlist = data.get("playlist", {}) if isinstance(data, dict) else {}
        raw_ids = playlist.get("trackIds", [])
        track_ids = [
            str(item.get("id", ""))
            for item in raw_ids
            if isinstance(item, dict) and item.get("id")
        ]
        try:
            track_count = int(playlist.get("trackCount", len(track_ids)))
        except (TypeError, ValueError):
            track_count = len(track_ids)
        if not track_ids or len(track_ids) != track_count:
            logger.warning(
                f"[KookMusic] 官方歌单曲目列表不完整: "
                f"trackIds={len(track_ids)}, trackCount={track_count}"
            )
            return []
        return track_ids

    @staticmethod
    def _merge_playlist_order(
        track_ids: list[str],
        songs: list[Song],
        requester_id: str,
        requester_name: str,
    ) -> list[Song]:
        """按官方顺序合并 meting 数据，并为被截断的条目建立占位。"""
        songs_by_id = {song.id: song for song in songs if song.id}
        merged = []
        for song_id in track_ids:
            song = songs_by_id.get(song_id)
            if song is None:
                song = Song(
                    id=song_id,
                    name="未知歌曲",
                    artists="未知歌手",
                    platform="netease",
                    requester_id=requester_id,
                    requester_name=requester_name,
                )
            merged.append(song)
        return merged

    async def _fetch_song_details(
        self,
        session: aiohttp.ClientSession,
        song_ids: list[str],
    ) -> list[dict]:
        if not song_ids:
            return []
        url = self.NETEASE_SONG_DETAIL_API.format(
            song_id=song_ids[0],
            song_ids=",".join(song_ids),
        )
        try:
            async with session.get(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://music.163.com/",
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json(content_type=None)
        except Exception as e:
            logger.debug(f"[KookMusic] 批量获取歌曲详情异常: {e}")
            return []
        return data.get("songs", []) if isinstance(data, dict) else []

    @staticmethod
    def _normalize_duration(duration) -> int:
        """统一把时长转成毫秒。"""
        try:
            duration_ms = int(duration)
        except (TypeError, ValueError):
            return 0
        if duration_ms <= 0:
            return 0
        if duration_ms < 1000:
            return duration_ms * 1000
        return duration_ms

    @staticmethod
    def _extract_meting_song_id(item: dict) -> str:
        """从 meting 歌曲条目中提取网易云歌曲 ID。"""
        direct_id = item.get("id") or item.get("songid") or item.get("song_id")
        if direct_id:
            return str(direct_id)
        for key in ("url", "lrc", "pic"):
            value = item.get(key, "")
            if not isinstance(value, str):
                continue
            match = _METING_SONG_ID_PATTERN.search(value)
            if match:
                return match.group(1)
        return ""

    async def _fill_netease_song_durations(
        self,
        session: aiohttp.ClientSession,
        songs: list[Song],
    ):
        """批量补全普通网易云歌单歌曲时长。"""
        need_duration = [song for song in songs if song.id and song.duration <= 0]
        if not need_duration:
            return

        for start in range(0, len(need_duration), 50):
            batch = need_duration[start:start + 50]
            ids = [song.id for song in batch]
            url = self.NETEASE_SONG_DETAIL_API.format(
                song_id=ids[0],
                song_ids=",".join(ids),
            )
            try:
                async with session.get(
                    url,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/120.0.0.0 Safari/537.36"
                        ),
                        "Referer": "https://music.163.com/",
                    },
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        logger.debug(
                            f"[KookMusic] 获取歌曲时长失败: HTTP {resp.status}"
                        )
                        continue
                    data = await resp.json(content_type=None)
            except Exception as e:
                logger.debug(f"[KookMusic] 获取歌曲时长异常: {e}")
                continue

            details = data.get("songs", []) if isinstance(data, dict) else []
            duration_by_id = {}
            for detail in details:
                song_id = str(detail.get("id", ""))
                duration = self._extract_duration_from_detail(detail)
                if song_id and duration > 0:
                    duration_by_id[song_id] = duration

            for song in batch:
                duration = duration_by_id.get(song.id, 0)
                if duration > 0:
                    song.duration = duration

    @classmethod
    def _extract_duration_from_detail(cls, detail: dict) -> int:
        """从网易云 song detail 响应里提取时长。"""
        for key in ("duration", "dt"):
            duration = cls._normalize_duration(detail.get(key, 0))
            if duration > 0:
                return duration
        for key in ("hMusic", "mMusic", "lMusic", "bMusic"):
            music = detail.get(key)
            if not isinstance(music, dict):
                continue
            duration = cls._normalize_duration(
                music.get("playTime", 0) or music.get("duration", 0)
            )
            if duration > 0:
                return duration
        return 0

    async def _fetch_song_detail_as_song(
        self,
        session: aiohttp.ClientSession,
        song_id: str,
        requester_id: str,
        requester_name: str,
    ) -> Song | None:
        """使用网易云 song detail 接口构造 Song，作为 meting 单曲接口失败时的兜底。"""
        url = self.NETEASE_SONG_DETAIL_API.format(
            song_id=song_id,
            song_ids=song_id,
        )
        try:
            async with session.get(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    "Referer": "https://music.163.com/",
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
        except Exception as e:
            logger.debug(f"[KookMusic] 获取单曲详情 {song_id} 异常: {e}")
            return None

        details = data.get("songs", []) if isinstance(data, dict) else []
        if not details:
            return None
        detail = details[0]
        artists = "、".join(
            a.get("name", "") for a in detail.get("artists", []) if a.get("name")
        )
        cover_url = detail.get("album", {}).get("picUrl", "")
        return Song(
            id=song_id,
            name=detail.get("name", "未知歌曲"),
            artists=artists or "未知歌手",
            duration=self._extract_duration_from_detail(detail),
            cover_url=cover_url,
            platform="netease",
            requester_id=requester_id,
            requester_name=requester_name,
        )

    async def import_netease_djradio(
        self,
        radio_id: str,
        requester_id: str = "",
        requester_name: str = "",
    ) -> list[Song]:
        """通过网页爬取 + 免登录 API 获取网易云电台的所有节目。

        流程：
        1. 爬取电台网页，从 HTML 中解析出所有节目 ID
        2. 对每个节目调用免登录的 /api/dj/program/detail 获取歌曲信息

        Args:
            radio_id: 网易云电台 ID (djradio ID)
            requester_id: 请求用户 ID
            requester_name: 请求用户昵称

        Returns:
            Song 对象列表
        """
        session = await self._get_session()

        # Step 1: 爬取电台网页获取节目 ID 列表
        page_url = f"https://music.163.com/djradio?id={radio_id}"
        try:
            async with session.get(
                page_url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        f"[KookMusic] 获取电台页面失败: HTTP {resp.status}"
                    )
                    return []
                html = await resp.text()
        except Exception as e:
            logger.error(f"[KookMusic] 爬取电台页面异常: {e}")
            return []

        # 提取所有 program ID
        program_ids = _PROGRAM_ID_FROM_PAGE.findall(html)
        if not program_ids:
            logger.warning(f"[KookMusic] 电台 {radio_id} 未找到任何节目")
            return []

        # 去重并保持顺序
        seen = set()
        unique_ids = []
        for pid in program_ids:
            if pid not in seen:
                seen.add(pid)
                unique_ids.append(pid)

        logger.info(
            f"[KookMusic] 电台 {radio_id} 发现 {len(unique_ids)} 个节目，开始获取详情..."
        )

        # Step 2: 并发获取每个节目的详情
        songs: list[Song] = []
        # 限制并发数，避免被限流
        semaphore = asyncio.Semaphore(5)

        async def fetch_program(program_id: str) -> Song | None:
            async with semaphore:
                return await self._fetch_program_detail(
                    session, program_id, requester_id, requester_name
                )

        tasks = [fetch_program(pid) for pid in unique_ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Song):
                songs.append(result)
            elif isinstance(result, Exception):
                logger.debug(f"[KookMusic] 获取节目详情异常: {result}")

        logger.info(
            f"[KookMusic] 电台 {radio_id} 导入成功: {len(songs)} 首节目"
        )
        return songs

    async def _fetch_program_detail(
        self,
        session: aiohttp.ClientSession,
        program_id: str,
        requester_id: str,
        requester_name: str,
    ) -> Song | None:
        """通过免登录 API 获取单个电台节目的歌曲信息。"""
        url = f"https://music.163.com/api/dj/program/detail?id={program_id}"
        try:
            async with session.get(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    "Referer": "https://music.163.com/",
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)

                if data.get("code") != 200:
                    return None

                program = data.get("program", {})
                main_song = program.get("mainSong", {})

                if not main_song:
                    return None

                # 提取歌曲信息
                song_id = str(main_song.get("id", ""))
                name = main_song.get("name", "未知节目")
                duration = main_song.get("duration", 0)
                try:
                    duration = int(duration)
                except (TypeError, ValueError):
                    duration = 0
                # 网易云电台节目接口有时返回秒，Song.duration/card countdown
                # 使用毫秒；不转换会导致 KOOK 认为 countdown endTime 已过期。
                if 0 < duration < 1000:
                    duration *= 1000

                # 提取歌手（电台节目的 artists 通常是主播）
                artists_list = main_song.get("artists", [])
                artists = "、".join(
                    a.get("name", "") for a in artists_list if a.get("name")
                ) or program.get("dj", {}).get("nickname", "未知主播")

                # 封面图
                cover_url = (
                    program.get("coverUrl", "")
                    or main_song.get("album", {}).get("picUrl", "")
                )

                return Song(
                    id=song_id,
                    name=name,
                    artists=artists,
                    duration=duration,
                    cover_url=cover_url,
                    platform="netease",
                    requester_id=requester_id,
                    requester_name=requester_name,
                )

        except Exception as e:
            logger.debug(f"[KookMusic] 获取节目 {program_id} 详情异常: {e}")
            return None
