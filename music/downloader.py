"""
歌曲 MP3 下载器。

移植自 astrbot_plugin_music 的 Downloader，
将歌曲下载到本地缓存供 FFmpeg 读取。
"""

import asyncio
import logging
import shutil
import uuid
from pathlib import Path

import aiofiles
import aiohttp

from .model import Song

logger = logging.getLogger("astrbot")


class MusicDownloader:
    """歌曲下载器"""

    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
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

    def clear_cache(self):
        """清空缓存目录"""
        if self.cache_dir.exists():
            shutil.rmtree(self.cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        logger.debug(f"[KookMusic] 缓存目录已清空: {self.cache_dir}")

    async def download(self, song: Song) -> Song:
        """
        下载歌曲 MP3 到本地缓存。

        Args:
            song: 包含 audio_url 的 Song 对象

        Returns:
            更新了 file_path 的 Song 对象
        """
        if not song.audio_url:
            logger.error(f"[KookMusic] 歌曲 '{song.name}' 无音频 URL，无法下载")
            return song

        session = await self._get_session()
        file_name = f"{uuid.uuid4().hex}.mp3"
        file_path = self.cache_dir / file_name

        try:
            # 合并额外请求头（如 B站防盗链 Referer）
            headers = {}
            if song.extra_headers:
                headers.update(song.extra_headers)

            async with session.get(
                song.audio_url,
                headers=headers or None,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status != 200:
                    logger.error(
                        f"[KookMusic] 下载失败 HTTP {resp.status}: {song.name}"
                    )
                    return song

                # 流式写入
                async with aiofiles.open(file_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(8192):
                        await f.write(chunk)

            song.file_path = str(file_path)
            logger.info(f"[KookMusic] 下载完成: {song.name} -> {file_path.name}")
            return song

        except asyncio.CancelledError:
            if file_path.exists():
                file_path.unlink(missing_ok=True)
            raise
        except Exception as e:
            logger.error(f"[KookMusic] 下载异常 '{song.name}': {e}")
            # 清理未完成的文件
            if file_path.exists():
                file_path.unlink(missing_ok=True)
            return song
