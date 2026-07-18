"""
多平台免登录歌曲搜索器。

移植自 astrbot_plugin_music 的 SearcherMusic + NetEaseMusic，
使用第三方聚合 API 实现免登录搜索。
"""

import asyncio
import logging

import aiohttp

from .model import Song

logger = logging.getLogger("astrbot")

# 平台名称映射
PLATFORM_ALIASES: dict[str, str] = {
    "网易": "netease",
    "网易云": "netease",
    "netease": "netease",
    "qq": "qq",
    "QQ": "qq",
    "QQ音乐": "qq",
    "酷狗": "kugou",
    "kugou": "kugou",
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


class MusicSearcher:
    """多平台音乐搜索器（免登录）"""

    SEARCH_API_URL = "https://music.txqq.pro/"

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/132.0.0.0 Safari/537.36 Edg/132.0.0.0"
        ),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
    }

    # 网易云 Web API（备用直连搜索）
    NETEASE_SEARCH_URL = "http://music.163.com/api/search/get/web"
    NETEASE_DETAIL_URL = "https://api.qijieya.cn/meting/?type=song&id={song_id}"

    def __init__(self):
        self._session: aiohttp.ClientSession | None = None

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
        """将平台别名解析为标准名称"""
        return PLATFORM_ALIASES.get(name.strip(), name.strip().lower())

    async def search(
        self,
        keyword: str,
        platform: str = "netease",
        limit: int = 5,
    ) -> list[Song]:
        """
        搜索歌曲。

        Args:
            keyword: 搜索关键词
            platform: 平台标识 (netease/qq/kugou/kuwo/migu/baidu)
            limit: 返回数量上限

        Returns:
            歌曲列表
        """
        platform = self.resolve_platform(platform)

        # 优先尝试聚合 API
        songs = await self._search_via_aggregator(keyword, platform, limit)
        if songs:
            return songs

        # 聚合 API 失败时，网易云平台尝试直连
        if platform == "netease":
            songs = await self._search_netease_direct(keyword, limit)
            if songs:
                return songs

        logger.warning(f"[KookMusic] 搜索 '{keyword}' ({platform}) 无结果")
        return []

    async def _search_via_aggregator(
        self, keyword: str, platform: str, limit: int
    ) -> list[Song]:
        """通过聚合 API 搜索"""
        session = await self._get_session()
        data = {
            "input": keyword,
            "filter": "name",
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
                raw_songs = result.get("songs", [])[:limit]
                return [
                    Song(
                        id=str(s.get("songid", "")),
                        name=s.get("title", "未知"),
                        artists=s.get("author", "未知"),
                        audio_url=s.get("url", ""),
                        cover_url=s.get("pic", ""),
                        platform=platform,
                    )
                    for s in raw_songs
                ]
        except Exception as e:
            logger.warning(f"[KookMusic] 聚合API搜索异常: {e}")
            return []

    async def _search_netease_direct(
        self, keyword: str, limit: int
    ) -> list[Song]:
        """直连网易云 Web API 搜索"""
        session = await self._get_session()
        try:
            async with session.post(
                self.NETEASE_SEARCH_URL,
                data={"s": keyword, "limit": limit, "type": 1, "offset": 0},
                cookies={"appver": "2.0.2"},
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; WOW64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/55.0.2883.87 Safari/537.36"
                    )
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return []
                result = await resp.json(content_type=None)
                if (
                    not isinstance(result, dict)
                    or "result" not in result
                    or "songs" not in result["result"]
                ):
                    return []
                raw_songs = result["result"]["songs"][:limit]
                return [
                    Song(
                        id=str(s.get("id", "")),
                        name=s.get("name", "未知"),
                        artists="、".join(
                            a.get("name", "") for a in s.get("artists", [])
                        ),
                        duration=s.get("duration", 0),
                        platform="netease",
                    )
                    for s in raw_songs
                ]
        except Exception as e:
            logger.warning(f"[KookMusic] 网易云直连搜索异常: {e}")
            return []

    async def fetch_audio_url(self, song: Song) -> Song:
        """获取歌曲的播放 URL（如果搜索时未返回）。

        对于网易云平台使用专用 API，其他平台尝试通过聚合 API 重新获取。
        """
        if song.audio_url:
            return song

        # 网易云平台：使用专用详情 API
        if song.platform == "netease" and song.id:
            song = await self._fetch_netease_audio_url(song)
            if song.audio_url:
                return song

        # 其他平台或网易云 fallback：尝试通过聚合 API 按歌名重新搜索
        if not song.audio_url and song.name:
            platform = song.platform or "netease"
            try:
                results = await self._search_via_aggregator(song.name, platform, 5)
                # 尝试匹配同名歌曲
                for result in results:
                    if result.audio_url and (
                        result.name == song.name
                        or result.id == song.id
                    ):
                        song.audio_url = result.audio_url
                        if not song.cover_url and result.cover_url:
                            song.cover_url = result.cover_url
                        break
                # 如果没有精确匹配，取第一个有 URL 的结果
                if not song.audio_url and results and results[0].audio_url:
                    song.audio_url = results[0].audio_url
                    if not song.cover_url and results[0].cover_url:
                        song.cover_url = results[0].cover_url
            except Exception as e:
                logger.warning(f"[KookMusic] 聚合API获取音频URL异常: {e}")

        return song

    async def _fetch_netease_audio_url(self, song: Song) -> Song:
        """通过网易云专用 API 获取音频 URL"""
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
                if isinstance(result, list) and len(result) > 0:
                    data = result[0]
                    song.audio_url = data.get("url", "")
                    if not song.cover_url:
                        song.cover_url = data.get("pic", "")
                elif isinstance(result, dict):
                    song.audio_url = result.get("url", "")
                    if not song.cover_url:
                        song.cover_url = result.get("pic", "")
        except Exception as e:
            logger.warning(f"[KookMusic] 网易云获取音频URL异常: {e}")

        return song
