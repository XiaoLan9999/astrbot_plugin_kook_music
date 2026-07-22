"""Bilibili 视频、分P与收藏夹音频提取模块。"""

import asyncio
import ipaddress
import logging
import random
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import aiohttp
import yt_dlp

from .model import Song

logger = logging.getLogger("astrbot")

_VIDEO_PATH_PATTERN = re.compile(
    r"^/video/(BV[A-Za-z0-9]{10})(?:/|$)", re.IGNORECASE
)
_FAVORITE_PATH_PATTERNS = (
    re.compile(
        r"^/(?:medialist/(?:detail|play)|list)/ml(\d+)(?:/|$)",
        re.IGNORECASE,
    ),
    re.compile(r"^/medialist/(?:detail|play)/(\d+)(?:/|$)", re.IGNORECASE),
)
_HTTP_URL_PATTERN = re.compile(r"https?://[^\s]+", re.IGNORECASE)


@dataclass
class BilibiliCollection:
    """可一次加入队列的 B站收藏夹或多P视频。"""

    id: str
    title: str
    kind: str
    songs: list[Song]


class BilibiliExtractor:
    """B站视频音频提取器（使用 yt-dlp）"""

    VIEW_API_URL = "https://api.bilibili.com/x/web-interface/view"
    SEARCH_API_URL = "https://api.bilibili.com/x/web-interface/search/type"
    SEARCH_PAGE_URL = "https://search.bilibili.com/all"
    FAVORITE_API_URL = "https://api.bilibili.com/x/v3/fav/resource/list"
    FAVORITE_PAGE_SIZE = 20
    MAX_FAVORITE_PAGES = 500
    API_MIN_INTERVAL = 0.9
    SHORT_LINK_HOSTS = {"b23.tv", "bili2233.cn"}
    RETRYABLE_API_CODES = {-412, -509, -352}
    COOKIE_NAMES = {
        "SESSDATA",
        "bili_jct",
        "DedeUserID",
        "DedeUserID__ckMd5",
        "buvid3",
        "buvid4",
    }
    API_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/132.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.bilibili.com/",
    }

    def __init__(self):
        self._ffmpeg_path: str = ""
        self._cookies_file: str = ""
        self._cookie_header: str = ""
        self._session: aiohttp.ClientSession | None = None
        self._session_loop: asyncio.AbstractEventLoop | None = None
        self._video_view_cache: dict[str, tuple[float, dict | None]] = {}
        self._favorite_page_cache: dict[tuple[str, int], tuple[float, dict]] = {}
        self._favorite_page_tasks: dict[tuple[str, int], asyncio.Task] = {}
        self._favorite_page_tasks_lock = asyncio.Lock()
        self._api_lock = asyncio.Lock()
        self._next_api_request_at = 0.0

    def set_ffmpeg_path(self, path: str):
        """设置 FFmpeg 路径，供 yt-dlp 使用"""
        self._ffmpeg_path = path

    def set_cookies_file(self, path: str):
        """设置 B站 cookies 文件路径（Netscape 格式）"""
        if path and Path(path).exists():
            self._cookies_file = path
            self._cookie_header = self._read_cookie_header(Path(path))
            self._video_view_cache.clear()
            self._favorite_page_cache.clear()
            logger.info(f"[KookMusic] 已加载 B站 cookies 文件: {path}")

    def set_cookie(self, sessdata: str, data_dir: Path):
        """从 SESSDATA 值生成 cookies 文件供 yt-dlp 使用。

        Args:
            sessdata: B站 SESSDATA cookie 值
            data_dir: 插件数据目录，用于存放生成的 cookies 文件
        """
        sessdata = sessdata.strip()
        if not sessdata:
            return
        if any(char in sessdata for char in "\r\n\t;"):
            logger.warning("[KookMusic] B站 SESSDATA 含有非法字符，已拒绝")
            return

        data_dir.mkdir(parents=True, exist_ok=True)
        cookies_path = data_dir / "bili_cookies.txt"

        # 写入 Netscape cookies 格式
        with open(cookies_path, "w", encoding="utf-8") as f:
            f.write("# Netscape HTTP Cookie File\n")
            f.write(f".bilibili.com\tTRUE\t/\tTRUE\t0\tSESSDATA\t{sessdata}\n")

        self._cookies_file = str(cookies_path)
        self._cookie_header = f"SESSDATA={sessdata}"
        self._video_view_cache.clear()
        self._favorite_page_cache.clear()
        logger.info("[KookMusic] 已从配置生成 B站 cookies 文件")

    async def close(self):
        tasks = list(self._favorite_page_tasks.values())
        self._favorite_page_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        session = self._session
        self._session = None
        self._session_loop = None
        if session and not session.closed:
            await session.close()

    async def _get_session(self) -> aiohttp.ClientSession:
        current_loop = asyncio.get_running_loop()
        if self._session is not None and self._session.closed:
            self._session = None
            self._session_loop = None
        if self._session is not None and self._session_loop is not current_loop:
            old_session = self._session
            self._session = None
            self._session_loop = None
            try:
                await old_session.close()
            except Exception:
                pass
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._session_loop = current_loop
        return self._session

    @staticmethod
    def _read_cookie_header(path: Path) -> str:
        cookies: list[str] = []
        try:
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if line.startswith("#HttpOnly_"):
                    line = line[len("#HttpOnly_"):]
                elif not line or line.startswith("#"):
                    continue
                fields = line.split("\t")
                if len(fields) < 7:
                    continue
                domain = fields[0].lstrip(".").lower()
                if (
                    domain != "bilibili.com"
                    and not domain.endswith(".bilibili.com")
                ):
                    continue
                name, value = fields[5], fields[6]
                if (
                    name in BilibiliExtractor.COOKIE_NAMES
                    and value
                    and not any(char in value for char in "\r\n;")
                ):
                    cookies.append(f"{name}={value}")
        except OSError as e:
            logger.warning(f"[KookMusic] 读取 B站 cookies 文件失败: {e}")
        return "; ".join(cookies)

    @staticmethod
    def _extract_http_url(text: str) -> str:
        match = _HTTP_URL_PATTERN.search(text)
        return match.group(0).rstrip(",.;，。；>)]】）") if match else ""

    @staticmethod
    def _is_bilibili_host(hostname: str) -> bool:
        host = hostname.rstrip(".").lower()
        return host == "bilibili.com" or host.endswith(".bilibili.com")

    @staticmethod
    def _parse_page_query(query: str) -> tuple[int, bool, bool]:
        """返回 ``(page, 是否存在 p, p 是否有效)``。"""
        params = {
            key.lower(): values
            for key, values in parse_qs(query, keep_blank_values=True).items()
        }
        if "p" not in params:
            return 1, False, True
        values = params["p"]
        if not values or not values[0].isdigit():
            return 1, True, False
        page = int(values[0])
        return (page, True, page >= 1)

    @classmethod
    def parse_video_input(cls, text: str) -> tuple[str | None, int, bool]:
        """返回 ``(bvid, page, 是否显式指定分P)``。"""
        text = text.strip()
        url = cls._extract_http_url(text)
        if url:
            parsed = urlparse(url)
            if not cls._is_bilibili_host(parsed.hostname or ""):
                return None, 1, False
            match = _VIDEO_PATH_PATTERN.search(parsed.path)
            if not match:
                return None, 1, False
            page, explicit_page, valid_page = cls._parse_page_query(parsed.query)
            if not valid_page:
                return None, 1, True
            return match.group(1), page, explicit_page

        match = re.search(r"BV[A-Za-z0-9]{10}", text, re.IGNORECASE)
        if not match:
            return None, 1, False
        page, explicit_page, valid_page = cls._parse_page_query(
            text[match.end():].lstrip("?")
        )
        if not valid_page:
            return None, 1, True
        return match.group(0), page, explicit_page

    async def resolve_input(self, text: str) -> str:
        """安全展开 B站官方短链；普通关键词与官方长链接原样返回。"""
        url = self._extract_http_url(text)
        if not url:
            return text
        parsed = urlparse(url)
        host = (parsed.hostname or "").rstrip(".").lower()
        if host not in self.SHORT_LINK_HOSTS:
            return text

        current = url
        session = await self._get_session()
        for _ in range(5):
            try:
                async with session.get(
                    current,
                    headers=self.API_HEADERS,
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=12),
                ) as resp:
                    if resp.status not in {301, 302, 303, 307, 308}:
                        logger.warning(
                            f"[KookMusic] B站短链返回非重定向状态: {resp.status}"
                        )
                        return ""
                    location = resp.headers.get("Location", "")
            except Exception as e:
                logger.warning(f"[KookMusic] B站短链展开失败: {e}")
                return ""

            target = urljoin(current, location)
            target_parsed = urlparse(target)
            target_host = (target_parsed.hostname or "").rstrip(".").lower()
            if target_parsed.scheme not in {"http", "https"}:
                return ""
            if self._is_bilibili_host(target_host):
                return target
            if target_host not in self.SHORT_LINK_HOSTS:
                logger.warning("[KookMusic] B站短链跳转到非官方域名，已拒绝")
                return ""
            current = target

        logger.warning("[KookMusic] B站短链重定向次数过多")
        return ""

    @classmethod
    def parse_favorite_input(cls, text: str) -> str:
        """从常见收藏夹链接中提取 media_id。"""
        url = cls._extract_http_url(text)
        if not url:
            return ""
        parsed = urlparse(url)
        if not cls._is_bilibili_host(parsed.hostname or ""):
            return ""
        if re.fullmatch(r"/\d+/favlist/?", parsed.path, re.IGNORECASE):
            query = {
                key.lower(): values
                for key, values in parse_qs(parsed.query).items()
            }
            values = query.get("fid", [])
            if values and values[0].isdigit():
                return values[0]
        for pattern in _FAVORITE_PATH_PATTERNS:
            match = pattern.search(parsed.path)
            if match:
                return match.group(1)
        return ""

    def parse_input(self, text: str) -> tuple[str | None, int]:
        """解析用户输入，提取 BV号和分P信息。

        Returns:
            (bvid, page) — bvid 为 None 表示无法识别
        """
        bvid, page, _ = self.parse_video_input(text)
        return bvid, page

    def _build_url(
        self,
        bvid: str,
        page: int = 1,
        include_page: bool = False,
    ) -> str:
        """构建 B站视频 URL"""
        url = f"https://www.bilibili.com/video/{bvid}"
        if include_page or page > 1:
            url += f"?p={page}"
        return url

    @staticmethod
    def _https_url(value: object) -> str:
        url = str(value or "")
        if url.startswith("http://"):
            url = "https://" + url[len("http://"):]
        elif url.startswith("//"):
            url = "https:" + url
        return url if urlparse(url).scheme == "https" else ""

    @classmethod
    def _safe_stream_url(cls, value: object) -> str:
        """只接受 yt-dlp 从官方页面解析出的公网 HTTPS 音频地址。"""
        url = str(value or "").strip()
        if any(ord(char) < 32 or ord(char) == 127 for char in url):
            return ""
        try:
            parsed = urlparse(url)
            hostname = (parsed.hostname or "").rstrip(".").lower()
            port = parsed.port
        except ValueError:
            return ""
        if (
            parsed.scheme != "https"
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or port not in {None, 443}
            or hostname == "localhost"
            or hostname.endswith((".localhost", ".local"))
        ):
            return ""
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
            labels = hostname.split(".")
            if labels and all(
                re.fullmatch(r"(?:0x[0-9a-f]+|[0-9]+)", label, re.IGNORECASE)
                for label in labels
            ):
                return ""
        if address is not None and not address.is_global:
            return ""
        return url

    @classmethod
    def _safe_stream_headers(cls, value: object) -> dict[str, str]:
        """保留 B站 CDN 必需的非敏感请求头，不把 Cookie 交给 FFmpeg。"""
        raw_headers = value if isinstance(value, dict) else {}
        lowered = {
            str(name).lower(): str(header_value)
            for name, header_value in raw_headers.items()
        }

        def clean(name: str, fallback: str) -> str:
            header_value = lowered.get(name.lower(), fallback).strip()
            if (
                not header_value
                or len(header_value) > 1024
                or any(char in header_value for char in "\r\n\0")
            ):
                return fallback
            return header_value

        user_agent = clean("user-agent", cls.API_HEADERS["User-Agent"])
        referer = clean("referer", cls.API_HEADERS["Referer"])
        parsed_referer = urlparse(referer)
        if (
            parsed_referer.scheme != "https"
            or not cls._is_bilibili_host(parsed_referer.hostname or "")
        ):
            referer = cls.API_HEADERS["Referer"]
        return {"User-Agent": user_agent, "Referer": referer}

    @staticmethod
    def _safe_int(value: object, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return default

    async def _api_get_json(self, url: str, params: dict) -> dict | None:
        headers = dict(self.API_HEADERS)
        if self._cookie_header:
            headers["Cookie"] = self._cookie_header
        for attempt in range(3):
            retry_delay = 0.0
            try:
                async with self._api_lock:
                    throttle_delay = self._next_api_request_at - time.monotonic()
                    if throttle_delay > 0:
                        await asyncio.sleep(throttle_delay)
                    session = await self._get_session()
                    async with session.get(
                        url,
                        params=params,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=20),
                    ) as resp:
                        status = resp.status
                        retry_after = resp.headers.get("Retry-After", "")
                        if status == 200:
                            result = await resp.json(content_type=None)
                            if not isinstance(result, dict):
                                self._schedule_next_api_request()
                                return None
                            api_code = result.get("code")
                            if api_code not in self.RETRYABLE_API_CODES:
                                self._schedule_next_api_request()
                                return result
                            status = 412
                            logger.warning(
                                f"[KookMusic] B站 API 风控码 {api_code}"
                                f"（第 {attempt + 1}/3 次）"
                            )
                        retryable = status in {412, 429} or status >= 500
                        if resp.status != 200:
                            logger.warning(
                                f"[KookMusic] B站 API HTTP {status}"
                                f"（第 {attempt + 1}/3 次）"
                            )
                        self._schedule_next_api_request()
                    if retry_after:
                        try:
                            retry_delay = min(30.0, max(0.0, float(retry_after)))
                        except ValueError:
                            retry_delay = 0.0
                    if status in {412, 429}:
                        retry_delay = max(retry_delay, 5.0 * (2 ** attempt))
                    elif status >= 500:
                        retry_delay = max(retry_delay, 0.75 * (2 ** attempt))
                    if not retryable:
                        return None
                    self._next_api_request_at = max(
                        self._next_api_request_at,
                        time.monotonic() + retry_delay,
                    )
            except Exception as e:
                logger.warning(
                    f"[KookMusic] B站 API 请求失败（第 {attempt + 1}/3 次）: {e}"
                )
                retry_delay = max(retry_delay, 0.75 * (2 ** attempt))
            if attempt < 2:
                await asyncio.sleep(retry_delay)
        return None

    def _schedule_next_api_request(self):
        next_request_at = (
            time.monotonic() + self.API_MIN_INTERVAL + random.uniform(0.0, 0.25)
        )
        self._next_api_request_at = max(
            self._next_api_request_at,
            next_request_at,
        )

    async def _fetch_video_view(self, bvid: str) -> dict | None:
        cached = self._video_view_cache.get(bvid)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        result = await self._api_get_json(self.VIEW_API_URL, {"bvid": bvid})
        if not result or result.get("code") != 0 or not isinstance(result.get("data"), dict):
            self._cache_video_view(bvid, None, ttl=15)
            return None
        view = result["data"]
        self._cache_video_view(bvid, view, ttl=120)
        return view

    def _cache_video_view(self, bvid: str, view: dict | None, ttl: float):
        self._video_view_cache[bvid] = (time.monotonic() + ttl, view)
        while len(self._video_view_cache) > 256:
            self._video_view_cache.pop(next(iter(self._video_view_cache)))

    def _song_from_view(self, view: dict, page: int) -> Song | None:
        bvid = str(view.get("bvid", "") or "")
        pages = view.get("pages") if isinstance(view.get("pages"), list) else []
        page_info = next(
            (
                item
                for item in pages
                if isinstance(item, dict)
                and self._safe_int(item.get("page"), 0) == page
            ),
            None,
        )
        if (
            not re.fullmatch(r"BV[A-Za-z0-9]{10}", bvid, re.IGNORECASE)
            or not page_info
        ):
            return None
        title = str(view.get("title", "") or "未知视频")
        part = str(page_info.get("part", "") or f"P{page}")
        name = f"{title} - {part}" if len(pages) > 1 else title
        owner = view.get("owner") if isinstance(view.get("owner"), dict) else {}
        duration = max(0, self._safe_int(page_info.get("duration"), 0)) * 1000
        cover_url = self._https_url(page_info.get("first_frame") or view.get("pic"))
        return Song(
            id=bvid if len(pages) == 1 and page == 1 else f"{bvid}_p{page}",
            name=name,
            artists=str(owner.get("name", "") or "未知UP主"),
            duration=duration,
            audio_url=self._build_url(bvid, page, include_page=True),
            cover_url=cover_url,
            platform="bilibili",
            provider_data={"bvid": bvid, "page": page},
        )

    async def extract_collection(self, text: str) -> BilibiliCollection | None:
        """解析收藏夹或未指定分P的多P视频；单视频返回 ``None``。"""
        text = await self.resolve_input(text)
        if not text:
            return None
        favorite_id = self.parse_favorite_input(text)
        if favorite_id:
            return await self._extract_favorite_collection(favorite_id)

        bvid, _, explicit_page = self.parse_video_input(text)
        if not bvid or explicit_page:
            return None
        view = await self._fetch_video_view(bvid)
        if not view:
            return None
        pages = view.get("pages") if isinstance(view.get("pages"), list) else []
        if len(pages) <= 1:
            return None
        songs = [
            song
            for page_info in pages
            if isinstance(page_info, dict)
            and (
                song := self._song_from_view(
                    view,
                    self._safe_int(page_info.get("page"), 0),
                )
            )
        ]
        return BilibiliCollection(
            id=bvid,
            title=str(view.get("title", "") or bvid),
            kind="分P视频",
            songs=songs,
        )

    async def _extract_favorite_collection(
        self,
        favorite_id: str,
    ) -> BilibiliCollection | None:
        data = await self._fetch_favorite_page(favorite_id, 1)
        if data is None:
            return None
        info = data.get("info") if isinstance(data.get("info"), dict) else {}
        title = str(info.get("title", "") or f"收藏夹 {favorite_id}")
        medias = data.get("medias") if isinstance(data.get("medias"), list) else []
        total = max(0, self._safe_int(info.get("media_count"), 0))
        if total <= 0:
            total = len(medias)
        max_items = self.FAVORITE_PAGE_SIZE * self.MAX_FAVORITE_PAGES
        if total > max_items:
            logger.warning(
                f"[KookMusic] B站收藏夹共有 {total} 项，超过读取上限 {max_items}"
            )
            total = max_items
        songs = [
            Song(
                id=f"bili_favorite_{favorite_id}_{index}",
                name=f"{title} #{index}",
                artists="B站收藏夹",
                platform="bilibili",
                provider_data={
                    "favorite_id": favorite_id,
                    "favorite_index": index,
                    "collection_id": favorite_id,
                    "collection_kind": "favorite",
                    "needs_materialize": True,
                },
            )
            for index in range(1, total + 1)
        ]
        return BilibiliCollection(
            id=favorite_id,
            title=title,
            kind="收藏夹",
            songs=songs,
        )

    async def _fetch_favorite_page(
        self,
        favorite_id: str,
        page_num: int,
    ) -> dict | None:
        cache_key = (favorite_id, page_num)
        cached = self._favorite_page_cache.get(cache_key)
        if cached and cached[0] > time.monotonic():
            return cached[1]

        async with self._favorite_page_tasks_lock:
            cached = self._favorite_page_cache.get(cache_key)
            if cached and cached[0] > time.monotonic():
                return cached[1]
            task = self._favorite_page_tasks.get(cache_key)
            if task is None:
                task = asyncio.create_task(
                    self._request_favorite_page(favorite_id, page_num)
                )
                self._favorite_page_tasks[cache_key] = task
                task.add_done_callback(
                    lambda completed, key=cache_key: self._discard_favorite_page_task(
                        key, completed
                    )
                )

        return await asyncio.shield(task)

    def _discard_favorite_page_task(
        self,
        cache_key: tuple[str, int],
        task: asyncio.Task,
    ):
        if self._favorite_page_tasks.get(cache_key) is task:
            self._favorite_page_tasks.pop(cache_key, None)

    async def _request_favorite_page(
        self,
        favorite_id: str,
        page_num: int,
    ) -> dict | None:
        cache_key = (favorite_id, page_num)
        result = await self._api_get_json(
            self.FAVORITE_API_URL,
            {
                "media_id": favorite_id,
                "pn": page_num,
                "ps": self.FAVORITE_PAGE_SIZE,
                "keyword": "",
                "order": "mtime",
                "type": 0,
                "tid": 0,
                "platform": "web",
            },
        )
        if not result:
            return None
        if result.get("code") == -403:
            logger.warning("[KookMusic] B站收藏夹为私密内容，请配置有效 Cookie")
            return None
        if result.get("code") != 0 or not isinstance(result.get("data"), dict):
            logger.warning(
                f"[KookMusic] B站收藏夹解析失败 {favorite_id}: "
                f"{result.get('message', '未知错误')}"
            )
            return None
        data = result["data"]
        self._favorite_page_cache[cache_key] = (time.monotonic() + 120, data)
        while len(self._favorite_page_cache) > 256:
            self._favorite_page_cache.pop(next(iter(self._favorite_page_cache)))
        return data

    def _favorite_item_to_song(
        self,
        item: object,
        favorite_id: str,
    ) -> Song | None:
        if not isinstance(item, dict) or item.get("type") != 2:
            return None
        bvid = str(item.get("bvid", "") or "")
        if not re.fullmatch(r"BV[A-Za-z0-9]{10}", bvid, re.IGNORECASE):
            return None
        upper = item.get("upper") if isinstance(item.get("upper"), dict) else {}
        page_count = max(1, self._safe_int(item.get("page"), 1))
        duration = (
            0
            if page_count > 1
            else max(0, self._safe_int(item.get("duration"), 0)) * 1000
        )
        return Song(
            id=f"{bvid}_p1",
            name=str(item.get("title", "") or "未知视频"),
            artists=str(upper.get("name", "") or "未知UP主"),
            duration=duration,
            audio_url=self._build_url(bvid, 1, include_page=True),
            cover_url=self._https_url(item.get("cover")),
            platform="bilibili",
            provider_data={
                "bvid": bvid,
                "page": 1,
                "collection_id": favorite_id,
                "collection_kind": "favorite",
            },
        )

    async def materialize_collection_songs(
        self,
        songs: list[Song],
    ) -> list[Song] | None:
        """仅读取用户最终选中区间覆盖的收藏夹分页。"""
        favorite_entries = [
            song
            for song in songs
            if song.provider_data.get("needs_materialize")
        ]
        if not favorite_entries:
            return songs
        page_data: dict[tuple[str, int], dict] = {}
        for song in favorite_entries:
            favorite_id = str(song.provider_data.get("favorite_id", "") or "")
            index = self._safe_int(song.provider_data.get("favorite_index"), 0)
            if not favorite_id or index <= 0:
                return None
            page_num = (index - 1) // self.FAVORITE_PAGE_SIZE + 1
            key = (favorite_id, page_num)
            if key not in page_data:
                data = await self._fetch_favorite_page(favorite_id, page_num)
                if data is None:
                    return None
                page_data[key] = data

        resolved: list[Song] = []
        for song in songs:
            if not song.provider_data.get("needs_materialize"):
                resolved.append(song)
                continue
            favorite_id = str(song.provider_data["favorite_id"])
            index = self._safe_int(song.provider_data.get("favorite_index"), 0)
            if index <= 0:
                return None
            page_num = (index - 1) // self.FAVORITE_PAGE_SIZE + 1
            offset = (index - 1) % self.FAVORITE_PAGE_SIZE
            data = page_data[(favorite_id, page_num)]
            medias = data.get("medias") if isinstance(data.get("medias"), list) else []
            item = medias[offset] if offset < len(medias) else None
            materialized = self._favorite_item_to_song(item, favorite_id)
            if materialized:
                resolved.append(materialized)
        return resolved

    def _base_ydl_opts(self) -> dict:
        """基础 yt-dlp 选项"""
        opts = {
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 20,
            "retries": 3,
            "fragment_retries": 3,
        }
        if self._ffmpeg_path:
            opts["ffmpeg_location"] = self._ffmpeg_path
        if self._cookies_file:
            opts["cookiefile"] = self._cookies_file
        return opts

    async def extract(self, text: str) -> Song | None:
        """从 BV号或链接提取视频元数据（不下载）。

        Returns:
            Song 对象（audio_url 设为视频URL作为标记），失败返回 None
        """
        text = await self.resolve_input(text)
        if not text:
            return None
        bvid, page, explicit_page = self.parse_video_input(text)
        if bvid is None:
            if explicit_page:
                logger.warning(f"[KookMusic] B站分P参数无效: {text}")
                return None
            if self._extract_http_url(text):
                logger.warning(f"[KookMusic] 暂不支持此视频链接: {text}")
                return None
            bvid = await self._search_first_bvid(text)
            if bvid is None:
                search_url = await asyncio.to_thread(self._yt_search_first_url, text)
                if not search_url:
                    logger.warning(f"[KookMusic] B站搜索无结果: {text}")
                    return None
                bvid, page, explicit_page = self.parse_video_input(search_url)
                if bvid is None:
                    return None
            else:
                page = 1
                explicit_page = True

        view = await self._fetch_video_view(bvid)
        if view:
            pages = view.get("pages") if isinstance(view.get("pages"), list) else []
            if not explicit_page and len(pages) > 1:
                logger.warning(
                    f"[KookMusic] 未指定分P的多P视频应通过批量流程添加: {bvid}"
                )
                return None
            song = self._song_from_view(view, page)
            if song:
                logger.info(f"[KookMusic] B站视频解析成功: {song.display_name}")
                return song
        elif not explicit_page:
            logger.warning(
                f"[KookMusic] 无法确认视频是否包含多P，已停止单P降级: {bvid}"
            )
            return None

        url = self._build_url(bvid, page, include_page=True)

        try:
            info = await asyncio.to_thread(self._yt_extract_info, url)
        except Exception as e:
            logger.warning(f"[KookMusic] yt-dlp 解析B站视频失败: {e}")
            return None

        if not info:
            return None

        title = info.get("title", "未知视频")
        uploader = info.get("uploader", "") or info.get("channel", "未知UP主")
        try:
            duration_ms = max(0, int(float(info.get("duration") or 0) * 1000))
        except (TypeError, ValueError, OverflowError):
            duration_ms = 0
        thumbnail = info.get("thumbnail", "")

        logger.info(f"[KookMusic] B站视频解析成功: {title} - {uploader}")

        return Song(
            id=bvid if page == 1 else f"{bvid}_p{page}",
            name=title,
            artists=uploader,
            duration=duration_ms,
            audio_url=url,  # 视频 URL 作为标记，后续由 download_audio 下载
            cover_url=thumbnail,
            platform="bilibili",
            provider_data={"bvid": bvid, "page": page},
        )

    async def _search_first_bvid(self, keyword: str) -> str | None:
        keyword = keyword.strip()
        if not keyword or len(keyword) > 100:
            return None
        page_bvid = await self._search_page_first_bvid(keyword)
        if page_bvid:
            return page_bvid
        result = await self._api_get_json(
            self.SEARCH_API_URL,
            {
                "search_type": "video",
                "keyword": keyword,
                "page": 1,
            },
        )
        if not result or result.get("code") != 0:
            return None
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        entries = data.get("result") if isinstance(data.get("result"), list) else []
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("type") != "video":
                continue
            bvid = str(entry.get("bvid", "") or "")
            if re.fullmatch(r"BV[A-Za-z0-9]{10}", bvid, re.IGNORECASE):
                return bvid
        return None

    async def _search_page_first_bvid(self, keyword: str) -> str | None:
        headers = dict(self.API_HEADERS)
        headers.update({
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        try:
            async with self._api_lock:
                throttle_delay = self._next_api_request_at - time.monotonic()
                if throttle_delay > 0:
                    await asyncio.sleep(throttle_delay)
                session = await self._get_session()
                async with session.get(
                    self.SEARCH_PAGE_URL,
                    params={"keyword": keyword},
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    self._schedule_next_api_request()
                    if resp.status != 200:
                        return None
                    if resp.content_length and resp.content_length > 2_000_000:
                        return None
                    html = await resp.text(errors="replace")
        except Exception as e:
            logger.warning(f"[KookMusic] B站搜索页请求失败: {e}")
            return None

        return self._parse_search_page_bvid(html)

    @staticmethod
    def _parse_search_page_bvid(html: str) -> str | None:
        match = re.search(
            r"(?:https?:)?//www\.bilibili\.com/video/"
            r"(BV[A-Za-z0-9]{10})",
            html,
            re.IGNORECASE,
        )
        return match.group(1) if match else None

    def _yt_search_first_url(self, keyword: str) -> str:
        opts = self._base_ydl_opts()
        opts.update({
            "skip_download": True,
            "extract_flat": True,
            "playlistend": 1,
        })
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"bilisearch1:{keyword}", download=False)
            entries = info.get("entries") if isinstance(info, dict) else None
            entry = entries[0] if isinstance(entries, list) and entries else None
            if not isinstance(entry, dict):
                return ""
            bvid = str(entry.get("id", "") or "")
            if re.fullmatch(r"BV[A-Za-z0-9]{10}", bvid, re.IGNORECASE):
                return self._build_url(bvid, 1, include_page=True)
            return str(entry.get("url", "") or entry.get("webpage_url", ""))
        except Exception as e:
            logger.warning(f"[KookMusic] yt-dlp 搜索 B站视频失败: {e}")
            return ""

    def _yt_extract_info(self, url: str) -> dict | None:
        """同步调用 yt-dlp 提取视频信息"""
        opts = self._base_ydl_opts()
        opts["skip_download"] = True
        opts["noplaylist"] = True
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception as e:
            logger.warning(f"[KookMusic] yt-dlp extract_info 异常: {e}")
            return None

    @staticmethod
    def should_stream_audio(song: Song, threshold_minutes: int) -> bool:
        """未知时长或超过阈值的单个视频使用即时流式播放。"""
        if threshold_minutes <= 0:
            return False
        threshold_ms = threshold_minutes * 60 * 1000
        return song.duration <= 0 or song.duration >= threshold_ms

    def _video_url_from_song(self, song: Song) -> str:
        bvid = str(song.provider_data.get("bvid", "") or "")
        if not bvid:
            match = re.match(r"^(BV[A-Za-z0-9]{10})", song.id or "", re.IGNORECASE)
            bvid = match.group(1) if match else ""
        if not re.fullmatch(r"BV[A-Za-z0-9]{10}", bvid, re.IGNORECASE):
            return ""
        page = self._safe_int(song.provider_data.get("page"), 1)
        if page < 1:
            return ""
        return self._build_url(bvid, page, include_page=True)

    async def prepare_audio(
        self,
        song: Song,
        cache_dir: Path,
        stream_threshold_minutes: int,
    ) -> Song:
        """按单个分P时长选择即时 HTTPS 流或完整本地下载。"""
        if song.playback_source:
            return song
        if self.should_stream_audio(song, stream_threshold_minutes):
            song = await self.resolve_audio_stream(song)
            if song.stream_url:
                return song
            logger.warning(
                f"[KookMusic] B站流地址解析失败，回退到完整下载: {song.name}"
            )
        return await self.download_audio(song, cache_dir)

    async def resolve_audio_stream(self, song: Song) -> Song:
        """在歌曲即将播放时解析一次短期有效的 B站音频 CDN 地址。"""
        url = self._video_url_from_song(song)
        song.stream_url = ""
        if not url:
            logger.error(f"[KookMusic] B站流解析缺少有效 BVID 或分P: {song.id}")
            return song
        song.audio_url = url

        payload: dict = {}
        for attempt in range(2):
            try:
                payload = await asyncio.to_thread(self._yt_resolve_audio_stream, url)
            except Exception as e:
                logger.warning(
                    f"[KookMusic] B站流地址解析异常（第 {attempt + 1}/2 次）: {e}"
                )
                payload = {}
            stream_url = self._safe_stream_url(payload.get("url"))
            if stream_url:
                song.stream_url = stream_url
                song.extra_headers = self._safe_stream_headers(payload.get("http_headers"))
                if not song.name or song.name == "未知视频":
                    song.name = str(payload.get("title", "") or song.name)
                if not song.artists or song.artists == "未知UP主":
                    song.artists = str(
                        payload.get("uploader", "")
                        or payload.get("channel", "")
                        or song.artists
                    )
                if song.duration <= 0:
                    try:
                        song.duration = max(
                            0,
                            int(float(payload.get("duration") or 0) * 1000),
                        )
                    except (TypeError, ValueError, OverflowError):
                        pass
                if not song.cover_url:
                    song.cover_url = self._https_url(payload.get("thumbnail"))
                logger.info(
                    f"[KookMusic] B站音频流已就绪: {song.display_name}"
                )
                return song
            if attempt == 0:
                await asyncio.sleep(0.5)

        song.extra_headers = {}
        logger.error(f"[KookMusic] 无法获取安全的 B站 HTTPS 音频流: {song.name}")
        return song

    def _yt_resolve_audio_stream(self, url: str) -> dict:
        """同步调用 yt-dlp 选择一条纯音频流，不下载媒体文件。"""
        opts = self._base_ydl_opts()
        opts.update({
            "format": "ba[ext=m4a]/ba/b",
            "skip_download": True,
            "noplaylist": True,
        })
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not isinstance(info, dict):
            return {}
        return {
            "url": info.get("url", ""),
            "http_headers": info.get("http_headers", {}),
            "title": info.get("title", ""),
            "uploader": info.get("uploader", ""),
            "channel": info.get("channel", ""),
            "duration": info.get("duration", 0),
            "thumbnail": info.get("thumbnail", ""),
        }

    async def download_audio(self, song: Song, cache_dir: Path) -> Song:
        """使用 yt-dlp 下载B站视频音频到本地。

        Args:
            song: 包含 B站视频信息的 Song 对象
            cache_dir: 音频缓存目录

        Returns:
            更新了 file_path 的 Song 对象
        """
        url = self._video_url_from_song(song)
        if not url:
            logger.error(f"[KookMusic] B站下载缺少有效 BVID: {song.id}")
            song.file_path = ""
            return song
        song.stream_url = ""
        song.extra_headers = {}
        song.audio_url = url
        cache_dir.mkdir(parents=True, exist_ok=True)
        output_base = str(cache_dir / uuid.uuid4().hex)

        download_task = asyncio.create_task(
            asyncio.to_thread(self._yt_download_audio, url, output_base)
        )
        try:
            result_path = await asyncio.shield(download_task)
            if result_path and Path(result_path).exists():
                song.file_path = result_path
                logger.info(
                    f"[KookMusic] B站音频下载完成: {song.name} -> "
                    f"{Path(result_path).name}"
                )
            else:
                logger.error(f"[KookMusic] B站音频下载失败: 文件不存在")
        except asyncio.CancelledError:
            # to_thread 无法强制停止；让后台下载完成后删除这一批无人引用的文件。
            download_task.add_done_callback(
                lambda task: self._finish_cancelled_download(task, output_base)
            )
            raise
        except Exception as e:
            self._cleanup_cancelled_download(output_base)
            logger.error(f"[KookMusic] yt-dlp 下载失败: {e}")

        return song

    @staticmethod
    def _cleanup_cancelled_download(output_base: str):
        base = Path(output_base)
        for path in base.parent.glob(f"{base.name}.*"):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    @classmethod
    def _finish_cancelled_download(cls, task: asyncio.Task, output_base: str):
        try:
            task.result()
        except (asyncio.CancelledError, Exception):
            pass
        cls._cleanup_cancelled_download(output_base)

    def _yt_download_audio(self, url: str, output_base: str) -> str:
        """同步调用 yt-dlp 下载音频"""
        opts = self._base_ydl_opts()
        opts.update({
            "format": "ba[ext=m4a]/ba/b",  # 优先 m4a 音频，回退到最佳音频
            "outtmpl": output_base + ".%(ext)s",
            "noplaylist": True,
        })

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)

            # 从 yt-dlp 返回中获取实际文件路径
            if info and "requested_downloads" in info:
                return info["requested_downloads"][0]["filepath"]

            # 回退：根据 ext 推断路径
            ext = info.get("ext", "m4a") if info else "m4a"
            return output_base + "." + ext
