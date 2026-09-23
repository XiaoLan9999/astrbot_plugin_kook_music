"""
多平台免登录歌曲搜索器。

QQ 音乐与酷狗优先使用各自网页接口获取可播放的临时地址；现有聚合
解析源与 Meting 只作为同歌曲 ID 的故障兜底。网易云保留 Web 搜索兜底。
"""

import asyncio
import html
import ipaddress
import logging
import re
import uuid
from urllib.parse import parse_qs, urljoin, urlparse

import aiohttp

from .model import Song

logger = logging.getLogger("astrbot")


PLATFORM_ALIASES: dict[str, str] = {
    "网易": "netease",
    "网易云": "netease",
    "网易云音乐": "netease",
    "netease": "netease",
    "qq": "qq",
    "QQ": "qq",
    "QQ音乐": "qq",
    "qq音乐": "qq",
    "腾讯": "qq",
    "腾讯音乐": "qq",
    "酷狗": "kugou",
    "酷狗音乐": "kugou",
    "kugou": "kugou",
    "kg": "kugou",
    "酷我": "kuwo",
    "kuwo": "kuwo",
    "咪咕": "migu",
    "migu": "migu",
    "百度": "baidu",
    "baidu": "baidu",
    "b站": "bilibili",
    "B站": "bilibili",
    "bili": "bilibili",
    "Bili": "bilibili",
    "bilibili": "bilibili",
    "Bilibili": "bilibili",
    "哔哩哔哩": "bilibili",
}

_QQ_SONG_PATH_PATTERN = re.compile(
    r"/(?:n/)?(?:ryqq(?:_v2)?/songDetail|yqq/song)/([A-Za-z0-9]+)(?:\.html)?(?:/|$)",
    re.IGNORECASE,
)
_KUGOU_HASH_PATTERN = re.compile(r"(?:^|[?&#])hash=([0-9a-f]{32})", re.IGNORECASE)
_KUGOU_PAGE_HASH_PATTERNS = (
    re.compile(r"\[hash:([0-9a-f]{32})\]", re.IGNORECASE),
    re.compile(r'"hash"\s*:\s*"([0-9a-f]{32})"', re.IGNORECASE),
)


class _QQResolverFailure(Exception):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = True,
        retry_after: float = 0.25,
        user_reason: str = "",
        source_failure: bool = True,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after
        self.user_reason = user_reason
        self.source_failure = source_failure


class MusicSearcher:
    """多平台音乐搜索及临时播放地址解析器。"""

    SEARCH_API_URL = "https://music.txqq.pro/"
    METING_API_URL = "https://api.qijieya.cn/meting/"
    DEFAULT_QQ_VIP_RESOLVER_URL = "https://meting.mikus.ink/api"
    QQ_VIP_RESOLVER_QUALITIES = {"128", "320", "flac"}
    QQ_VIP_RESOLVER_TIMEOUT = 15.0
    QQ_VIP_RESOLVER_RETRY_DELAY = 0.25
    QQ_VIP_RESOLVER_MAX_SOURCES = 4
    QQ_VIP_RESOLVER_FAILURE_THRESHOLD = 2

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/132.0.0.0 Safari/537.36 Edg/132.0.0.0"
        ),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
    }
    QQ_AUDIO_HEADERS = {
        "User-Agent": HEADERS["User-Agent"],
        "Referer": "https://y.qq.com/",
    }

    NETEASE_SEARCH_URL = "http://music.163.com/api/search/get/web"
    NETEASE_DETAIL_URL = "https://api.qijieya.cn/meting/?type=song&id={song_id}"
    QQ_MUSICU_URL = "https://u.y.qq.com/cgi-bin/musicu.fcg"
    QQ_STREAM_BASE_URL = "https://isure.stream.qqmusic.qq.com/"
    QQ_AUDIO_HOSTS = {"aqqmusic.tc.qq.com"}
    QQ_AUDIO_HOST_SUFFIXES = (
        ".stream.qqmusic.qq.com",
        ".music.tc.qq.com",
    )
    KUGOU_SEARCH_URL = "https://songsearch.kugou.com/song_search_v2"
    KUGOU_PLAY_URL = "https://m.kugou.com/app/i/getSongInfo.php"
    KUGOU_API_HEADERS = {
        "User-Agent": "IPhone-8990-searchSong",
        "UNI-UserAgent": "iOS11.4-Phone8990-1009-0-WiFi",
    }

    METING_SERVERS = {
        "qq": "tencent",
        "kugou": "kugou",
    }

    def __init__(
        self,
        qq_vip_resolver_url: str = "",
        qq_vip_resolver_quality: str = "320",
        qq_vip_resolver_backups: list[str] | None = None,
        qq_vip_resolver_cooldown_seconds: int = 60,
    ):
        self._session: aiohttp.ClientSession | None = None
        self.account_resolver = None
        self.qq_vip_resolver_url = str(qq_vip_resolver_url or "").strip()
        sources = []
        seen = set()
        if self.qq_vip_resolver_url:
            backups = qq_vip_resolver_backups if isinstance(qq_vip_resolver_backups, list) else []
            for value in [self.qq_vip_resolver_url, *backups]:
                if not isinstance(value, str) or not value.strip():
                    continue
                url = value.strip()
                try:
                    parsed = urlparse(url)
                    key = parsed._replace(
                        scheme=parsed.scheme.lower(), netloc=parsed.netloc.lower(),
                        path=parsed.path.rstrip("/"), fragment="",
                    ).geturl()
                except ValueError:
                    key = url
                if key not in seen:
                    seen.add(key)
                    sources.append(url)
                    if len(sources) == self.QQ_VIP_RESOLVER_MAX_SOURCES:
                        break
        self._qq_resolver_urls = tuple(sources)
        self.qq_vip_resolver_backups = sources[1:]
        try:
            self.qq_vip_resolver_cooldown_seconds = max(
                0, min(int(qq_vip_resolver_cooldown_seconds), 3600)
            )
        except (TypeError, ValueError):
            self.qq_vip_resolver_cooldown_seconds = 60
        self._qq_resolver_failures: dict[str, int] = {}
        self._qq_resolver_cooldowns: dict[str, float] = {}
        quality = str(qq_vip_resolver_quality or "320").strip().lower()
        self.qq_vip_resolver_quality = (
            quality if quality in self.QQ_VIP_RESOLVER_QUALITIES else "320"
        )

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is not None and not self._session.closed:
            try:
                session_loop = self._session._loop  # type: ignore[attr-defined]
                if session_loop is not asyncio.get_running_loop():
                    await self._session.close()
                    self._session = None
            except Exception:
                pass
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    @staticmethod
    def resolve_platform(name: str) -> str:
        """将平台别名解析为标准名称。"""
        value = name.strip()
        return PLATFORM_ALIASES.get(value, value.lower())

    async def search(
        self,
        keyword: str,
        platform: str = "netease",
        limit: int = 5,
    ) -> list[Song]:
        """搜索歌曲，并返回可交给统一下载/播放管线的 Song。"""
        platform = self.resolve_platform(platform)
        try:
            limit = max(1, min(int(limit), 30))
        except (TypeError, ValueError):
            limit = 5

        if platform == "qq":
            songs = await self._search_qq_direct(keyword, limit)
            if not songs:
                songs = await self._search_via_aggregator(keyword, platform, limit)
        elif platform == "kugou":
            songs = await self._search_kugou_direct(keyword, limit)
        else:
            songs = await self._search_via_aggregator(keyword, platform, limit)
        if songs:
            return songs

        if platform in self.METING_SERVERS:
            songs = await self._search_via_meting(keyword, platform, limit)
            if songs:
                return songs

        if platform == "netease":
            songs = await self._search_netease_direct(keyword, limit)
            if songs:
                return songs

        logger.warning(f"[KookMusic] 搜索 '{keyword}' ({platform}) 无结果")
        return []

    @staticmethod
    def _qq_web_comm() -> dict:
        return {
            "ct": 24,
            "cv": 4747474,
            "platform": "yqq.json",
            "chid": "0",
            "uin": 0,
            "g_tk": 5381,
            "g_tk_new_20200303": 5381,
            "format": "json",
            "inCharset": "utf-8",
            "outCharset": "utf-8",
            "notice": 0,
            "need_new_code": 1,
        }

    async def _search_qq_direct(
        self,
        keyword: str,
        limit: int,
    ) -> list[Song]:
        """调用 QQ 音乐网页接口，并仅展示匿名可完整播放的歌曲。"""
        session = await self._get_session()
        body = {
            "comm": {"ct": 19, "cv": 2201, "uin": 0},
            "req_0": {
                "module": "music.search.SearchCgiService",
                "method": "DoSearchForQQMusicDesktop",
                "param": {
                    "grp": 1,
                    "num_per_page": min(30, max(12, limit * 3)),
                    "page_num": 1,
                    "query": keyword,
                    "search_type": 0,
                },
            },
        }
        try:
            async with session.post(
                self.QQ_MUSICU_URL,
                json=body,
                headers=self.HEADERS,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return []
                result = await resp.json(content_type=None)
        except Exception as e:
            logger.warning(f"[KookMusic] QQ音乐直连搜索异常: {e}")
            return []
        return self._parse_qq_search_songs(
            result,
            limit,
            include_paid=bool(self.qq_vip_resolver_url or self.account_resolver),
        )

    @classmethod
    def _parse_qq_search_songs(
        cls,
        result: object,
        limit: int,
        include_paid: bool = False,
    ) -> list[Song]:
        if not isinstance(result, dict):
            return []
        req = result.get("req_0")
        if not isinstance(req, dict) or req.get("code") != 0:
            return []
        data = req.get("data")
        body = data.get("body") if isinstance(data, dict) else None
        song_data = body.get("song") if isinstance(body, dict) else None
        raw_songs = song_data.get("list", []) if isinstance(song_data, dict) else []
        if not isinstance(raw_songs, list):
            return []

        songs: list[Song] = []
        for item in raw_songs:
            song = cls._parse_qq_track(item, require_free=not include_paid)
            if song is None:
                continue
            songs.append(song)
            if len(songs) >= limit:
                break
        return songs

    @staticmethod
    def _qq_track_title(item: dict, default: str = "未知歌曲") -> str:
        # QQ's name omits edition labels; title is the client-facing full title.
        for key in ("title", "name", "songname"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return default

    @classmethod
    def _parse_qq_track(
        cls,
        item: object,
        require_free: bool,
    ) -> Song | None:
        if not isinstance(item, dict):
            return None
        song_id = str(item.get("mid", "") or item.get("songmid", ""))
        file_info = item.get("file") if isinstance(item.get("file"), dict) else {}
        media_mid = str(
            file_info.get("media_mid", "")
            or item.get("strMediaMid", "")
            or song_id
        )
        if not song_id or not media_mid:
            return None
        pay = item.get("pay") if isinstance(item.get("pay"), dict) else {}
        try:
            pay_play = int(pay.get("pay_play", 0) or 0)
        except (TypeError, ValueError):
            return None
        # pay_month 表示会员权益，并不等于匿名不可播放；真实可播性由 vkey 决定。
        if require_free and pay_play != 0:
            return None
        singers = item.get("singer") if isinstance(item.get("singer"), list) else []
        artists = "、".join(
            str(singer.get("name", ""))
            for singer in singers
            if isinstance(singer, dict) and singer.get("name")
        )
        album = item.get("album") if isinstance(item.get("album"), dict) else {}
        album_mid = str(album.get("mid", "") or item.get("albummid", ""))
        cover_url = (
            f"https://y.gtimg.cn/music/photo_new/"
            f"T002R300x300M000{album_mid}.jpg"
            if album_mid
            else ""
        )
        try:
            duration = max(0, int(item.get("interval", 0) or 0) * 1000)
        except (TypeError, ValueError):
            duration = 0
        return Song(
            id=song_id,
            name=cls._qq_track_title(item),
            artists=artists or "未知歌手",
            duration=duration,
            cover_url=cover_url,
            platform="qq",
            extra_headers=dict(cls.QQ_AUDIO_HEADERS),
            provider_data={"media_mid": media_mid, "album_mid": album_mid},
        )

    async def _search_kugou_direct(
        self,
        keyword: str,
        limit: int,
    ) -> list[Song]:
        """使用酷狗网页搜索接口获取免登录可播放的 128K 曲目。"""
        session = await self._get_session()
        try:
            async with session.get(
                self.KUGOU_SEARCH_URL,
                params={
                    "keyword": keyword,
                    "platform": "WebFilter",
                    "format": "json",
                    "page": 1,
                    "pagesize": min(30, max(12, limit * 3)),
                },
                headers=self.HEADERS,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return []
                result = await resp.json(content_type=None)
        except Exception as e:
            logger.warning(f"[KookMusic] 酷狗直连搜索异常: {e}")
            return []
        candidates = self._parse_kugou_search_songs(
            result, min(30, max(12, limit * 3))
        )
        return await self._filter_kugou_playable_songs(candidates, limit)

    @classmethod
    def _parse_kugou_search_songs(
        cls,
        result: object,
        limit: int,
    ) -> list[Song]:
        if not isinstance(result, dict) or result.get("status") != 1:
            return []
        data = result.get("data")
        raw_songs = data.get("lists", []) if isinstance(data, dict) else []
        if not isinstance(raw_songs, list):
            return []

        songs: list[Song] = []
        for item in raw_songs:
            if not isinstance(item, dict):
                continue
            song_id = str(item.get("FileHash", "") or "").upper()
            if not song_id:
                continue
            trans_param = item.get("trans_param")
            if not isinstance(trans_param, dict):
                trans_param = {}
            cover_url = str(
                item.get("Image", "")
                or trans_param.get("union_cover", "")
            ).replace("{size}", "400")
            if cover_url.startswith("http://"):
                cover_url = "https://" + cover_url[len("http://"):]
            songs.append(
                Song(
                    id=song_id,
                    name=cls._strip_html(
                        str(item.get("SongName", "") or "未知歌曲")
                    ),
                    artists=cls._strip_html(
                        str(item.get("SingerName", "") or "未知歌手")
                    ),
                    duration=cls._normalize_duration(item.get("Duration", 0)),
                    cover_url=cover_url,
                    platform="kugou",
                    extra_headers=dict(cls.KUGOU_API_HEADERS),
                )
            )
            if len(songs) >= limit:
                break
        return songs

    async def _filter_kugou_playable_songs(
        self,
        songs: list[Song],
        limit: int,
    ) -> list[Song]:
        """Privilege/PayType 不可靠，以播放接口是否返回完整 URL 为准。"""
        semaphore = asyncio.Semaphore(5)

        async def resolve(song: Song) -> Song | None:
            async with semaphore:
                resolved = await self._fetch_kugou_song_by_id(song.id)
            if not resolved or not resolved.audio_url:
                return None
            self._merge_song(song, resolved)
            return song

        results = await asyncio.gather(
            *(resolve(song) for song in songs),
            return_exceptions=True,
        )
        playable = [result for result in results if isinstance(result, Song)]
        return playable[:limit]

    async def _search_via_aggregator(
        self,
        input_value: str,
        platform: str,
        limit: int,
        filter_type: str = "name",
    ) -> list[Song]:
        """通过聚合 API 搜索；兼容旧 songs 和当前 data 响应。"""
        session = await self._get_session()
        data = {
            "input": input_value,
            "filter": filter_type,
            "type": platform,
            "page": 1,
        }
        try:
            async with session.post(
                self.SEARCH_API_URL,
                data=data,
                headers=self.HEADERS,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        f"[KookMusic] 聚合API搜索失败 HTTP {resp.status}"
                    )
                    return []
                result = await resp.json(content_type=None)
        except Exception as e:
            logger.warning(f"[KookMusic] 聚合API搜索异常: {e}")
            return []

        return self._parse_aggregator_songs(result, platform, limit)

    @classmethod
    def _parse_aggregator_songs(
        cls,
        result: object,
        platform: str,
        limit: int,
    ) -> list[Song]:
        if not isinstance(result, dict):
            return []
        raw_songs = result.get("songs")
        if not isinstance(raw_songs, list):
            raw_songs = result.get("data")
        if not isinstance(raw_songs, list):
            return []

        songs: list[Song] = []
        for item in raw_songs:
            if not isinstance(item, dict):
                continue
            song_id = str(item.get("songid", "") or item.get("id", ""))
            if platform in {"qq", "kugou"} and not song_id:
                continue
            name = str(
                item.get("title", "")
                or item.get("name", "")
                or item.get("songname", "")
                or "未知"
            )
            artists = str(
                item.get("author", "")
                or item.get("artist", "")
                or item.get("singername", "")
                or "未知"
            )
            audio_url = str(item.get("url", "") or "")
            if audio_url and not cls._is_http_url(audio_url):
                audio_url = ""
            if platform == "qq" and audio_url.startswith("http://"):
                audio_url = "https://" + audio_url[len("http://"):]
            if not song_id and not name:
                continue
            songs.append(
                Song(
                    id=song_id,
                    name=name,
                    artists=artists,
                    duration=cls._normalize_duration(item.get("duration", 0)),
                    audio_url=audio_url,
                    cover_url=str(item.get("pic", "") or ""),
                    platform=platform,
                    extra_headers=(
                        dict(cls.QQ_AUDIO_HEADERS) if platform == "qq" else {}
                    ),
                )
            )
            if len(songs) >= limit:
                break
        return songs

    async def _search_via_meting(
        self,
        keyword: str,
        platform: str,
        limit: int,
    ) -> list[Song]:
        """通过 Meting 搜索，并过滤返回空页面的不可播放条目。"""
        server = self.METING_SERVERS.get(platform)
        if not server:
            return []
        session = await self._get_session()
        try:
            async with session.get(
                self.METING_API_URL,
                params={"server": server, "type": "search", "id": keyword},
                headers=self.HEADERS,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return []
                result = await resp.json(content_type=None)
        except Exception as e:
            logger.warning(f"[KookMusic] {platform} Meting搜索异常: {e}")
            return []

        candidate_limit = min(30, max(12, limit * 3))
        candidates = self._parse_meting_songs(
            result, platform, candidate_limit
        )
        if not candidates:
            return []
        return await self._filter_playable_songs(candidates, limit)

    @classmethod
    def _parse_meting_songs(
        cls,
        result: object,
        platform: str,
        limit: int,
    ) -> list[Song]:
        if not isinstance(result, list):
            return []
        songs: list[Song] = []
        for item in result:
            if not isinstance(item, dict):
                continue
            audio_url = str(item.get("url", "") or "")
            if audio_url and not cls._is_http_url(audio_url):
                continue
            song_id = cls._extract_meting_id(audio_url)
            if not song_id:
                song_id = cls._extract_meting_id(str(item.get("lrc", "") or ""))
            if not song_id:
                continue
            songs.append(
                Song(
                    id=song_id,
                    name=str(item.get("name", "未知歌曲") or "未知歌曲"),
                    artists=str(
                        item.get("artist", "未知歌手") or "未知歌手"
                    ),
                    duration=cls._normalize_duration(item.get("duration", 0)),
                    audio_url=audio_url,
                    cover_url=str(item.get("pic", "") or ""),
                    platform=platform,
                )
            )
            if len(songs) >= limit:
                break
        return songs

    async def _filter_playable_songs(
        self,
        songs: list[Song],
        limit: int,
    ) -> list[Song]:
        session = await self._get_session()
        semaphore = asyncio.Semaphore(5)

        async def probe(song: Song) -> bool:
            async with semaphore:
                return await self._is_playable_audio_url(session, song.audio_url)

        results = await asyncio.gather(
            *(probe(song) for song in songs),
            return_exceptions=True,
        )
        playable = [
            song
            for song, result in zip(songs, results)
            if result is True
        ]
        return playable[:limit]

    @classmethod
    async def _is_playable_audio_url(
        cls,
        session: aiohttp.ClientSession,
        audio_url: str,
    ) -> bool:
        if not audio_url:
            return False
        if not cls._is_http_url(audio_url):
            return False
        headers = dict(cls.HEADERS)
        headers["Range"] = "bytes=0-63"
        try:
            async with session.get(
                audio_url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status not in (200, 206):
                    return False
                content_type = resp.headers.get("Content-Type", "").lower()
                sample = await resp.content.read(64)
        except Exception:
            return False

        if not sample:
            return False
        if content_type.startswith("text/") or "json" in content_type:
            return False
        return content_type.startswith("audio/") or cls._has_audio_signature(sample)

    @staticmethod
    def _has_audio_signature(sample: bytes) -> bool:
        return (
            sample.startswith((b"ID3", b"OggS", b"fLaC", b"RIFF"))
            or b"ftyp" in sample[:16]
            or (len(sample) >= 2 and sample[0] == 0xFF and sample[1] & 0xE0 == 0xE0)
        )

    async def _search_netease_direct(
        self, keyword: str, limit: int
    ) -> list[Song]:
        """直连网易云 Web API 搜索。"""
        session = await self._get_session()
        try:
            async with session.post(
                self.NETEASE_SEARCH_URL,
                data={"s": keyword, "limit": limit, "type": 1, "offset": 0},
                cookies={"appver": "2.0.2"},
                headers={"User-Agent": self.HEADERS["User-Agent"]},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return []
                result = await resp.json(content_type=None)
        except Exception as e:
            logger.warning(f"[KookMusic] 网易云直连搜索异常: {e}")
            return []

        if (
            not isinstance(result, dict)
            or not isinstance(result.get("result"), dict)
            or not isinstance(result["result"].get("songs"), list)
        ):
            return []
        raw_songs = result["result"]["songs"][:limit]
        return [
            Song(
                id=str(item.get("id", "")),
                name=item.get("name", "未知"),
                artists="、".join(
                    artist.get("name", "")
                    for artist in item.get("artists", [])
                ),
                duration=item.get("duration", 0),
                platform="netease",
            )
            for item in raw_songs
        ]

    async def fetch_audio_url(self, song: Song) -> Song:
        """按稳定歌曲 ID 刷新临时播放地址。"""
        if song.audio_url:
            return song

        platform = self.resolve_platform(song.platform or "netease")
        song.platform = platform
        if (
            song.provider_data.get("resolver_status") == "denied"
            and not (platform == "qq" and self.qq_vip_resolver_url)
            and not (platform in {"qq", "netease"} and self.account_resolver)
        ):
            return song

        if platform == "netease" and song.id:
            if await self._fill_account_audio(song):
                return song
            song = await self._fetch_netease_audio_url(song)
            if song.audio_url:
                return song

        if song.id:
            resolved = await self.fetch_song_by_id(song.platform, song.id)
            if resolved:
                self._merge_song(song, resolved)
                if song.audio_url:
                    return song
            # QQ/酷狗必须保持用户选中的稳定 ID，禁止按同名结果盲取其他版本。
            if platform in {"qq", "kugou"}:
                return song

        if song.name:
            results = await self.search(song.name, platform, 10)
            for result in results:
                if self._same_song(song, result) and result.audio_url:
                    self._merge_song(song, result)
                    break
        return song

    async def fetch_song_by_id(
        self,
        platform: str,
        song_id: str,
    ) -> Song | None:
        """按平台稳定 ID 获取元数据和新的播放地址。"""
        platform = self.resolve_platform(platform)
        song_id = song_id.strip()
        if not song_id:
            return None

        direct_song: Song | None = None
        if platform == "qq":
            direct_song = await self._fetch_qq_song_by_id(song_id)
            if direct_song:
                if direct_song.audio_url:
                    return direct_song
                if await self._fill_account_audio(direct_song):
                    return direct_song
                if self.qq_vip_resolver_url:
                    await self._fill_qq_vip_resolver_url(direct_song)
                    if direct_song.audio_url:
                        return direct_song
                if (
                    direct_song.provider_data.get("resolver_status") == "denied"
                    or direct_song.provider_data.get("qq_vip_resolver_status")
                    == "transient"
                ):
                    return direct_song
            results = await self._search_via_aggregator(
                song_id, platform, 5, filter_type="id"
            )
            for result in results:
                if (
                    result.id.casefold() == song_id.casefold()
                    and result.audio_url
                ):
                    if direct_song and direct_song.name:
                        result.name = direct_song.name
                    return result
        elif platform == "kugou":
            direct_song = await self._fetch_kugou_song_by_id(song_id)
            if direct_song and (
                direct_song.audio_url
                or direct_song.provider_data.get("resolver_status") == "denied"
            ):
                return direct_song
        else:
            results = await self._search_via_aggregator(
                song_id, platform, 5, filter_type="id"
            )
            for result in results:
                if result.id.casefold() == song_id.casefold():
                    return result

        if platform in self.METING_SERVERS:
            meting_song = await self._fetch_meting_song_by_id(platform, song_id)
            if meting_song:
                if platform == "qq" and direct_song and direct_song.name:
                    meting_song.name = direct_song.name
                return meting_song
        return direct_song

    async def _fill_account_audio(self, song: Song) -> bool:
        if not self.account_resolver or song.platform not in {"qq", "netease"} or not song.id:
            return False
        try:
            result = await asyncio.wait_for(self.account_resolver(song), timeout=12)
            url = str(getattr(result, "url", "") or "")
            if getattr(result, "status", "") != "resolved" or not url:
                return False
            valid = self._is_qq_audio_url(url) if song.platform == "qq" else self._is_netease_audio_url(url)
            if not valid:
                return False
            song.audio_url = url
            song.unplayable_reason = ""
            song.provider_data["resolver_status"] = "resolved"
            song.provider_data["account_audio"] = True
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[KookMusic] 自有账号取歌暂时不可用 (%s)", type(exc).__name__)
            return False

    @classmethod
    def _is_netease_audio_url(cls, value: str) -> bool:
        if not cls._is_http_url(value):
            return False
        parsed = urlparse(value)
        return parsed.scheme.lower() == "https" and (parsed.hostname or "").lower().endswith(".music.126.net")

    async def _fetch_qq_song_by_id(self, song_id: str) -> Song | None:
        session = await self._get_session()
        body = {
            "comm": self._qq_web_comm(),
            "req_0": {
                "module": "music.pf_song_detail_svr",
                "method": "get_song_detail_yqq",
                "param": (
                    {"song_id": int(song_id)}
                    if re.fullmatch(r"[0-9]{1,20}", song_id)
                    else {"song_mid": song_id}
                ),
            },
        }
        try:
            async with session.post(
                self.QQ_MUSICU_URL,
                json=body,
                headers=self.HEADERS,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return None
                result = await resp.json(content_type=None)
        except Exception as e:
            logger.warning(f"[KookMusic] QQ音乐详情解析异常: {e}")
            return None

        req = result.get("req_0") if isinstance(result, dict) else None
        data = req.get("data") if isinstance(req, dict) else None
        track = data.get("track_info") if isinstance(data, dict) else None
        song = self._parse_qq_track(track, require_free=False)
        if song is None:
            return None
        await self._fill_qq_audio_url(session, song)
        return song

    async def _fill_qq_audio_url(
        self,
        session: aiohttp.ClientSession,
        song: Song,
    ):
        media_mid = str(song.provider_data.get("media_mid", "") or song.id)
        filename = f"M500{media_mid}.mp3"
        body = {
            "comm": self._qq_web_comm(),
            "req_0": {
                "module": "music.vkey.GetVkey",
                "method": "UrlGetVkey",
                "param": {
                    "uin": "",
                    "filename": [filename],
                    "guid": uuid.uuid4().hex,
                    "songmid": [song.id],
                    "songtype": [0],
                    "ctx": 0,
                },
            },
        }
        try:
            async with session.post(
                self.QQ_MUSICU_URL,
                json=body,
                headers=self.HEADERS,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    song.provider_data["resolver_status"] = "transient"
                    return
                result = await resp.json(content_type=None)
        except Exception as e:
            song.provider_data["resolver_status"] = "transient"
            logger.warning(f"[KookMusic] QQ音乐播放地址解析异常: {e}")
            return

        req = result.get("req_0") if isinstance(result, dict) else None
        data = req.get("data") if isinstance(req, dict) else None
        infos = data.get("midurlinfo") if isinstance(data, dict) else None
        info = infos[0] if isinstance(infos, list) and infos else {}
        purl = str(info.get("purl", "") or "") if isinstance(info, dict) else ""
        result_code = info.get("result") if isinstance(info, dict) else None
        try:
            result_code = int(result_code)
        except (TypeError, ValueError):
            result_code = None
        sip = data.get("sip") if isinstance(data, dict) else None
        stream_base = (
            str(sip[0])
            if isinstance(sip, list) and sip and str(sip[0]).startswith(("http://", "https://"))
            else self.QQ_STREAM_BASE_URL
        )
        if result_code == 0 and purl:
            song.audio_url = stream_base.rstrip("/") + "/" + purl.lstrip("/")
            song.unplayable_reason = ""
            song.provider_data["resolver_status"] = "resolved"
            return
        if result_code == 104003:
            song.unplayable_reason = "该歌曲需要 QQ 音乐会员或受版权/地区限制"
            song.provider_data["resolver_status"] = "denied"
        else:
            song.unplayable_reason = "QQ 音乐播放地址解析暂时失败"
            song.provider_data["resolver_status"] = "transient"
        logger.info(
            f"[KookMusic] QQ音乐歌曲不可播放 {song.id}: result={result_code}"
        )

    async def _fill_qq_vip_resolver_url(self, song: Song) -> bool:
        """在共享时间预算内依次尝试同 MID 解析源，不替换歌曲版本。"""
        if not self.qq_vip_resolver_url or not song.id:
            return False

        song.provider_data.pop("qq_vip_resolver_status", None)
        song.provider_data.pop("qq_vip_resolver_source_host", None)
        song.provider_data.pop("qq_vip_resolver_source_index", None)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.QQ_VIP_RESOLVER_TIMEOUT
        sources = [
            (index, url) for index, url in enumerate(self._qq_resolver_urls, 1)
            if self._qq_resolver_cooldowns.get(url, 0) <= loop.time()
        ]
        timed_out = False
        failed = len(sources) < len(self._qq_resolver_urls)
        failure_reason = (
            "QQ 音乐解析服务暂时不可用，故障源正在冷却，请稍后重试"
            if not sources else ""
        )
        for position, (source_index, resolver_url) in enumerate(sources):
            host = self._qq_resolver_host(resolver_url)
            # Prefer another source to waiting for a retry on a broken source.
            attempts = 2 if len(sources) == 1 else 1
            for attempt in range(attempts):
                remaining = deadline - loop.time()
                if remaining <= 0:
                    timed_out = failed = True
                    break
                if attempts == 2 and attempt == 0:
                    attempt_budget = min(
                        remaining,
                        max(0.001, (remaining - self.QQ_VIP_RESOLVER_RETRY_DELAY) / 2),
                    )
                else:
                    attempt_budget = remaining / (len(sources) - position)
                retryable = False
                source_failure = False
                retry_delay = self.QQ_VIP_RESOLVER_RETRY_DELAY
                session = None
                try:
                    attempt_deadline = loop.time() + attempt_budget
                    session = await asyncio.wait_for(
                        self._get_session(), timeout=attempt_budget
                    )
                    audio_url = await asyncio.wait_for(
                        self._request_qq_vip_resolver_url(
                            session, song, attempt_deadline, resolver_url=resolver_url
                        ),
                        timeout=max(0.001, attempt_deadline - loop.time()),
                    )
                    self._qq_resolver_failures.pop(resolver_url, None)
                    self._qq_resolver_cooldowns.pop(resolver_url, None)
                    if not audio_url:
                        break
                    song.audio_url = audio_url
                    song.unplayable_reason = ""
                    song.provider_data["resolver_status"] = "resolved"
                    song.provider_data["qq_vip_resolver_status"] = "resolved"
                    song.provider_data["qq_vip_resolver_source_host"] = host
                    song.provider_data["qq_vip_resolver_source_index"] = source_index
                    logger.info(
                        "[KookMusic] QQ音乐同MID解析成功 %s (source=%s host=%s)",
                        song.id, source_index, host,
                    )
                    return True
                except asyncio.CancelledError:
                    raise
                except (asyncio.TimeoutError, aiohttp.ClientError, _QQResolverFailure) as exc:
                    failed = True
                    timed_out = timed_out or isinstance(exc, asyncio.TimeoutError)
                    if isinstance(exc, _QQResolverFailure):
                        retryable = exc.retryable
                        retry_delay = max(retry_delay, exc.retry_after)
                        failure_reason = exc.user_reason
                        source_failure = exc.source_failure
                    else:
                        retryable = isinstance(
                            exc,
                            (asyncio.TimeoutError, aiohttp.ClientConnectionError, aiohttp.ClientPayloadError),
                        ) and not isinstance(exc, aiohttp.ClientSSLError)
                        source_failure = retryable
                    if retryable and isinstance(exc, aiohttp.ClientConnectorError) and session:
                        self._clear_qq_resolver_dns_cache(session, resolver_url=resolver_url)
                    detail = (
                        str(exc) if isinstance(exc, _QQResolverFailure)
                        else f"errno={exc.os_error.errno}"
                        if retryable and isinstance(exc, aiohttp.ClientConnectorError)
                        else type(exc).__name__
                    )
                    logger.warning(
                        "[KookMusic] QQ音乐解析源暂时不可用 %s: %s "
                        "(source=%s host=%s attempt=%s)",
                        song.id, detail, source_index, host, attempt + 1,
                    )
                except Exception as exc:
                    failed = True
                    logger.warning(
                        "[KookMusic] QQ音乐解析源异常 %s: %s (source=%s host=%s)",
                        song.id, type(exc).__name__, source_index, host,
                    )
                if source_failure:
                    count = self._qq_resolver_failures.get(resolver_url, 0) + 1
                    self._qq_resolver_failures[resolver_url] = count
                    if count >= self.QQ_VIP_RESOLVER_FAILURE_THRESHOLD:
                        self._qq_resolver_cooldowns[resolver_url] = (
                            loop.time() + self.qq_vip_resolver_cooldown_seconds
                        )
                else:
                    self._qq_resolver_failures.pop(resolver_url, None)
                    self._qq_resolver_cooldowns.pop(resolver_url, None)
                remaining = deadline - loop.time()
                if not retryable or attempt + 1 == attempts or retry_delay >= remaining:
                    break
                await asyncio.sleep(retry_delay)

        if not failed:
            return False

        song.provider_data["resolver_status"] = "transient"
        song.provider_data["qq_vip_resolver_status"] = "transient"
        song.unplayable_reason = failure_reason or (
            "QQ 音乐解析服务暂时超时，请稍后重试"
            if timed_out
            else "QQ 音乐解析服务暂时不可用，请稍后重试"
        )
        return False

    @staticmethod
    def _qq_resolver_host(resolver_url: str) -> str:
        try:
            return urlparse(resolver_url).hostname or "invalid-host"
        except ValueError:
            return "invalid-host"

    def _clear_qq_resolver_dns_cache(
        self, session: aiohttp.ClientSession, resolver_url: str | None = None
    ):
        connector = getattr(session, "connector", None)
        if connector is None or not hasattr(connector, "clear_dns_cache"):
            return
        try:
            parsed = urlparse(resolver_url or self.qq_vip_resolver_url)
            connector.clear_dns_cache(
                parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
            )
        except (KeyError, ValueError):
            pass

    @staticmethod
    def _check_qq_resolver_status(resp, *, redirect: bool = False):
        if resp.status == 200 and not redirect:
            return
        if redirect and 300 <= resp.status < 400:
            return
        retry_after = 0.25
        if resp.status == 429:
            try:
                retry_after = max(retry_after, float(resp.headers.get("Retry-After", 0)))
            except (TypeError, ValueError):
                pass
        raise _QQResolverFailure(
            f"HTTP {resp.status}",
            retryable=resp.status == 429 or resp.status >= 500 or resp.status == 200,
            retry_after=retry_after,
            user_reason=(
                "QQ 音乐解析服务拒绝访问，请检查解析源配置"
                if resp.status in {401, 403} else ""
            ),
            source_failure=resp.status == 429 or resp.status >= 500,
        )

    async def _request_qq_vip_resolver_url(
        self,
        session: aiohttp.ClientSession,
        song: Song,
        deadline: float,
        resolver_url: str | None = None,
    ) -> str:
        def timeout():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            return aiohttp.ClientTimeout(total=remaining)

        resolver_url = resolver_url or self.qq_vip_resolver_url
        resolver_path = urlparse(resolver_url).path.rstrip("/").lower()
        if resolver_path.endswith("/song/url"):
            params = {
                "mid": song.id,
                "quality": self.qq_vip_resolver_quality,
            }
        else:
            params = {
                "server": "tencent",
                "type": "song",
                "id": song.id,
            }
        async with session.get(
            resolver_url,
            params=params,
            headers=self.HEADERS,
            timeout=timeout(),
        ) as resp:
            self._check_qq_resolver_status(resp)
            try:
                result = await resp.json(content_type=None)
            except (ValueError, UnicodeError) as exc:
                raise _QQResolverFailure("Invalid JSON response") from exc
        if not result:
            raise _QQResolverFailure("Empty response", source_failure=False)

        audio_url = ""
        if isinstance(result, dict) and result.get("code") in {0, "0"}:
            data = result.get("data")
            if not data:
                raise _QQResolverFailure("Empty song data", source_failure=False)
            if isinstance(data, dict):
                candidate = data.get(song.id)
                if isinstance(candidate, str):
                    audio_url = candidate.strip()
                    if not audio_url:
                        raise _QQResolverFailure("Empty audio URL", source_failure=False)
        elif isinstance(result, list):
            exact_match = False
            for item in result:
                if not isinstance(item, dict):
                    continue
                result_mid = str(item.get("songmid", "") or "")
                if not result_mid:
                    result_mid = self._extract_meting_id(
                        str(item.get("url", "") or "")
                    ) or self._extract_meting_id(
                        str(item.get("lrc", "") or "")
                    )
                if result_mid == song.id:
                    exact_match = True
                    break
            if not exact_match:
                return ""
            async with session.get(
                resolver_url,
                params={
                    "server": "tencent",
                    "type": "url",
                    "id": song.id,
                },
                headers=self.HEADERS,
                timeout=timeout(),
                allow_redirects=False,
            ) as resp:
                self._check_qq_resolver_status(resp, redirect=True)
                audio_url = str(resp.headers.get("Location", "") or "").strip()
                if not audio_url:
                    raise _QQResolverFailure("Empty redirect", source_failure=False)
        else:
            code = str(result.get("code", "")) if isinstance(result, dict) else ""
            raise _QQResolverFailure(
                "Invalid resolver response",
                retryable=code == "429" or code.startswith("5"),
                user_reason=(
                    "QQ 音乐解析服务拒绝访问，请检查解析源配置"
                    if code in {"401", "403"} else ""
                ),
                source_failure=code == "429" or code.startswith("5"),
            )

        if audio_url.startswith("http://"):
            audio_url = "https://" + audio_url[len("http://"):]
        return audio_url if audio_url and self._is_qq_audio_url(audio_url) else ""

    async def _fetch_kugou_song_by_id(self, song_id: str) -> Song | None:
        """按酷狗 FileHash 获取当次有效的完整歌曲 URL。"""
        session = await self._get_session()
        try:
            async with session.get(
                self.KUGOU_PLAY_URL,
                params={"cmd": "playInfo", "hash": song_id, "from": "mkugou"},
                headers=self.KUGOU_API_HEADERS,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
        except Exception as e:
            logger.warning(f"[KookMusic] 酷狗按ID解析异常: {e}")
            return None

        if not isinstance(data, dict):
            return None
        backup_url = data.get("backup_url")
        if isinstance(backup_url, list):
            backup_url = backup_url[0] if backup_url else ""
        elif not isinstance(backup_url, str):
            backup_url = ""
        status_ok = data.get("status") == 1
        audio_url = str(data.get("url", "") or backup_url or "") if status_ok else ""
        cover_url = str(data.get("album_img", "") or "").replace(
            "{size}", "400"
        )
        if cover_url.startswith("http://"):
            cover_url = "https://" + cover_url[len("http://"):]
        reason = ""
        error_text = str(data.get("error", "") or "")
        denied = (not status_ok) and any(
            keyword in error_text for keyword in ("付费", "会员", "版权")
        )
        resolver_status = "resolved" if audio_url else ("denied" if denied else "transient")
        if not audio_url:
            reason = error_text or "酷狗播放地址解析暂时失败"
            logger.info(f"[KookMusic] 酷狗歌曲不可播放 {song_id}: {reason}")
        return Song(
            id=song_id.upper(),
            name=str(data.get("songName", "") or data.get("fileName", "") or "未知歌曲"),
            artists=str(data.get("author_name", "") or "未知歌手"),
            duration=self._normalize_duration(data.get("timeLength", 0)),
            audio_url=audio_url,
            cover_url=cover_url,
            platform="kugou",
            extra_headers=dict(self.KUGOU_API_HEADERS),
            unplayable_reason=reason,
            provider_data={"resolver_status": resolver_status},
        )

    async def _fetch_meting_song_by_id(
        self,
        platform: str,
        song_id: str,
    ) -> Song | None:
        server = self.METING_SERVERS.get(platform)
        if not server:
            return None
        session = await self._get_session()
        try:
            async with session.get(
                self.METING_API_URL,
                params={"server": server, "type": "song", "id": song_id},
                headers=self.HEADERS,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return None
                result = await resp.json(content_type=None)
        except Exception as e:
            logger.warning(f"[KookMusic] {platform} 按ID解析异常: {e}")
            return None

        songs = self._parse_meting_songs(result, platform, 5)
        for song in songs:
            if song.id.casefold() == song_id.casefold():
                return song
        return None

    async def fetch_direct_song(self, text: str) -> Song | None:
        """解析常见 QQ 音乐或酷狗单曲网页链接。"""
        platform = self.detect_direct_platform(text)
        if not platform:
            return None
        url = self._extract_http_url(text)
        if not url:
            return None

        if platform == "qq":
            song_id = await self._resolve_qq_song_id(url)
        else:
            song_id = self._extract_kugou_hash(url)
            if not song_id:
                song_id = await self._fetch_kugou_page_hash(url)
        if not song_id:
            return None
        return await self.fetch_song_by_id(platform, song_id)

    @classmethod
    def detect_direct_platform(cls, text: str) -> str:
        url = cls._extract_http_url(text)
        if not url:
            return ""
        host = (urlparse(url).hostname or "").lower()
        if host == "y.qq.com" or host.endswith(".y.qq.com"):
            return "qq"
        if host == "kugou.com" or host.endswith(".kugou.com"):
            return "kugou"
        return ""

    @staticmethod
    def _extract_http_url(text: str) -> str:
        text = html.unescape(text)
        text = re.sub(r"\\([_*\[\]()<>])", r"\1", text)
        # Prefer a Markdown link's destination, not its potentially different label.
        markdown = re.search(r"\[[^\]\r\n]*\]\(\s*(https?://[^\s)]+)", text, re.IGNORECASE)
        if markdown:
            text = markdown.group(1)
        match = re.search(r"https?://[^\s<>\[\]()\"'`*]+", text, re.IGNORECASE)
        return match.group(0).rstrip(",.;，。；>)]】）") if match else ""

    @staticmethod
    def _is_qq_page_url(url: str) -> bool:
        try:
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
            return (
                parsed.scheme.lower() == "https"
                and (host == "y.qq.com" or host.endswith(".y.qq.com"))
                and not parsed.username
                and not parsed.password
                and parsed.port in (None, 443)
            )
        except ValueError:
            return False

    async def _resolve_qq_song_id(self, url: str) -> str:
        """展开官方分享短链；每次跳转先检查域名，再提取稳定歌曲 ID。"""
        current_url = url
        try:
            parsed = urlparse(url)
            if parsed.scheme == "http" and parsed.port in (None, 80):
                current_url = parsed._replace(
                    scheme="https", netloc=parsed.netloc.removesuffix(":80")
                ).geturl()
        except ValueError:
            return ""
        visited: set[str] = set()
        deadline = asyncio.get_running_loop().time() + 20
        for _ in range(6):
            if not self._is_qq_page_url(current_url) or current_url in visited:
                return ""
            song_id = self._extract_qq_song_id(current_url)
            if song_id:
                return song_id
            visited.add(current_url)
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return ""
            session = await self._get_session()
            try:
                async with session.get(
                    current_url,
                    allow_redirects=False,
                    headers=self.QQ_AUDIO_HEADERS,
                    timeout=aiohttp.ClientTimeout(total=min(10, remaining)),
                ) as response:
                    if response.status not in {301, 302, 303, 307, 308}:
                        return ""
                    location = response.headers.get("Location", "")
                    if not location:
                        return ""
                current_url = urljoin(current_url, location)
            except Exception as exc:
                logger.warning(f"[KookMusic] QQ音乐分享短链解析失败: {type(exc).__name__}")
                return ""
        logger.warning("[KookMusic] QQ音乐分享短链重定向次数过多")
        return ""

    @staticmethod
    def _extract_qq_song_id(url: str) -> str:
        parsed = urlparse(url)
        for raw_params in (parsed.query, parsed.fragment):
            params = {
                key.lower(): value
                for key, value in parse_qs(raw_params).items()
            }
            for key in ("songmid", "mid"):
                values = params.get(key)
                if values and re.fullmatch(r"[A-Za-z0-9]+", values[0]):
                    return values[0]
            values = params.get("songid")
            if values and re.fullmatch(r"[0-9]{1,20}", values[0]):
                return values[0]
        match = _QQ_SONG_PATH_PATTERN.search(parsed.path)
        return match.group(1) if match else ""

    @staticmethod
    def _extract_kugou_hash(url: str) -> str:
        parsed = urlparse(url)
        for value in (parsed.query, parsed.fragment):
            match = _KUGOU_HASH_PATTERN.search("&" + value)
            if match:
                return match.group(1).upper()
        match = _KUGOU_HASH_PATTERN.search(url)
        return match.group(1).upper() if match else ""

    async def _fetch_kugou_page_hash(self, url: str) -> str:
        session = await self._get_session()
        try:
            async with session.get(
                url,
                headers={
                    "User-Agent": self.HEADERS["User-Agent"],
                    "Referer": "https://www.kugou.com/",
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return ""
                html = await resp.text()
        except Exception as e:
            logger.warning(f"[KookMusic] 读取酷狗单曲页面异常: {e}")
            return ""

        for pattern in _KUGOU_PAGE_HASH_PATTERNS:
            match = pattern.search(html)
            if match:
                return match.group(1).upper()
        return ""

    async def _fetch_netease_audio_url(self, song: Song) -> Song:
        session = await self._get_session()
        try:
            url = self.NETEASE_DETAIL_URL.format(song_id=song.id)
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return song
                result = await resp.json(content_type=None)
                if isinstance(result, list) and result:
                    data = result[0]
                elif isinstance(result, dict):
                    data = result
                else:
                    return song
                song.audio_url = data.get("url", "")
                if not song.cover_url:
                    song.cover_url = data.get("pic", "")
        except Exception as e:
            logger.warning(f"[KookMusic] 网易云获取音频URL异常: {e}")
        return song

    @staticmethod
    def _extract_meting_id(url: str) -> str:
        if not url:
            return ""
        values = parse_qs(urlparse(url).query).get("id", [])
        return str(values[0]) if values else ""

    @staticmethod
    def _normalize_duration(value: object) -> int:
        try:
            duration = int(float(value or 0))
        except (TypeError, ValueError):
            return 0
        if 0 < duration < 1000:
            duration *= 1000
        return max(0, duration)

    @staticmethod
    def _strip_html(value: str) -> str:
        return re.sub(r"<[^>]+>", "", value).strip()

    @staticmethod
    def _is_http_url(value: str) -> bool:
        parsed = urlparse(value)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            return False
        hostname = parsed.hostname.rstrip(".").lower()
        if hostname == "localhost" or hostname.endswith(".localhost"):
            return False
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            labels = hostname.split(".")
            if labels and all(
                re.fullmatch(r"(?:0x[0-9a-f]+|[0-9]+)", label, re.IGNORECASE)
                for label in labels
            ):
                return False
            return True
        return not (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        )

    @classmethod
    def _is_qq_audio_url(cls, value: str) -> bool:
        """会员解析源最终只允许返回 QQ 官方 HTTPS 音频 CDN。"""
        if not cls._is_http_url(value):
            return False
        parsed = urlparse(value)
        if parsed.scheme.lower() != "https":
            return False
        hostname = (parsed.hostname or "").rstrip(".").lower()
        return hostname in cls.QQ_AUDIO_HOSTS or any(
            hostname.endswith(suffix) for suffix in cls.QQ_AUDIO_HOST_SUFFIXES
        )

    @staticmethod
    def _merge_song(target: Song, source: Song):
        target.audio_url = source.audio_url
        if source.extra_headers:
            target.extra_headers = dict(source.extra_headers)
        if not target.name:
            target.name = source.name
        if not target.artists:
            target.artists = source.artists
        if not target.cover_url:
            target.cover_url = source.cover_url
        if target.duration <= 0:
            target.duration = source.duration
        if source.audio_url:
            target.unplayable_reason = ""
        elif source.unplayable_reason:
            target.unplayable_reason = source.unplayable_reason
        if source.provider_data:
            target.provider_data.update(source.provider_data)

    @staticmethod
    def _same_song(left: Song, right: Song) -> bool:
        if left.id and right.id and left.id.casefold() == right.id.casefold():
            return True
        if left.name.strip().casefold() != right.name.strip().casefold():
            return False
        if not left.artists or not right.artists:
            return True
        return left.artists.strip().casefold() == right.artists.strip().casefold()
