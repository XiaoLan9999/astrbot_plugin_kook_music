"""
歌单导入模块。

支持通过网易云歌单链接/ID 批量导入歌曲到播放队列。
支持导入网易云电台(djradio)节目列表。
使用免登录第三方 API，无需本地部署 NeteaseCloudMusicApi。
"""

import asyncio
import logging
import re

import aiohttp

from .model import Song

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


class PlaylistImporter:
    """歌单导入器"""

    # 免登录歌单 API（meting API）
    NETEASE_PLAYLIST_API = "https://api.qijieya.cn/meting/?type=playlist&id={playlist_id}"
    NETEASE_SONG_API = "https://api.qijieya.cn/meting/?type=song&id={song_id}"
    NETEASE_SONG_DETAIL_API = "https://music.163.com/api/song/detail/?id={song_id}&ids=[{song_ids}]"

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
        """解析用户输入的歌单链接或 ID。

        Args:
            text: 用户输入（歌单 ID 或包含 ID 的链接）

        Returns:
            (type, id) — type 为 "netease" 或 "djradio"
            id 为空字符串表示解析失败
        """
        text = text.strip()

        # 优先检测电台链接（djradio 关键词）
        m = _NETEASE_DJRADIO_ID_PATTERN.search(text)
        if m:
            return "djradio", m.group(1)

        # 纯数字 → 直接作为歌单 ID
        if text.isdigit():
            return "netease", text

        # 从链接中提取歌单 ID
        m = _NETEASE_PLAYLIST_ID_PATTERN.search(text)
        if m:
            return "netease", m.group(1)

        return "netease", ""

    @staticmethod
    def parse_direct_song_input(text: str) -> tuple[str, str]:
        """解析点歌输入中的网易云单曲或电台节目链接。

        Returns:
            (type, id) — type 为 "song" 或 "program"，id 为空表示未识别。
        """
        text = text.strip()

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
                    return []

                data = await resp.json(content_type=None)

                if not isinstance(data, list):
                    logger.warning("[KookMusic] 歌单 API 返回格式异常")
                    return []

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

                await self._fill_netease_song_durations(session, songs)

                logger.info(
                    f"[KookMusic] 歌单 {playlist_id} 导入成功: {len(songs)} 首歌曲"
                )
                return songs

        except Exception as e:
            logger.error(f"[KookMusic] 导入歌单异常: {e}")
            return []

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
