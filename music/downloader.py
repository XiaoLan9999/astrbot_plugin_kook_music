"""
歌曲 MP3 下载器。

移植自 astrbot_plugin_music 的 Downloader，
将歌曲下载到本地缓存供 FFmpeg 读取。
"""

import asyncio
import ipaddress
import logging
import shutil
import uuid
from pathlib import Path
from urllib.parse import urlparse

import aiofiles
import aiohttp

from .model import Song

logger = logging.getLogger("astrbot")


class MusicDownloader:
    """歌曲下载器"""

    MAX_AUDIO_BYTES = 200 * 1024 * 1024
    MIN_AUDIO_BYTES = 1024

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
        if not self._is_safe_audio_url(song.audio_url):
            song.unplayable_reason = "解析源返回了不安全的音频地址"
            logger.error(f"[KookMusic] 拒绝不安全的音频地址: {song.audio_url}")
            return song

        session = await self._get_session()
        file_path: Path | None = None

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
                    if resp.status != 206:
                        logger.error(
                            f"[KookMusic] 下载失败 HTTP {resp.status}: {song.name}"
                        )
                        return song
                content_type = resp.headers.get("Content-Type", "").lower()
                if self._is_invalid_content_type(content_type):
                    logger.error(
                        f"[KookMusic] 解析源未返回音频 ({content_type}): {song.name}"
                    )
                    song.unplayable_reason = (
                        song.unplayable_reason or "解析源未返回有效音频，歌曲可能需要会员"
                    )
                    return song

                content_length = resp.headers.get("Content-Length", "")
                try:
                    declared_size = int(content_length)
                except (TypeError, ValueError):
                    declared_size = 0
                if declared_size > self.MAX_AUDIO_BYTES:
                    song.unplayable_reason = "音频文件超过 200MB 下载上限"
                    logger.error(f"[KookMusic] 音频文件过大: {song.name}")
                    return song

                suffix = self._guess_extension(content_type, song.audio_url)
                file_path = self.cache_dir / f"{uuid.uuid4().hex}{suffix}"
                bytes_written = 0
                prefix = bytearray()
                too_large = False
                async with aiofiles.open(file_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(8192):
                        bytes_written += len(chunk)
                        if len(prefix) < 64:
                            prefix.extend(chunk[:64 - len(prefix)])
                        if bytes_written > self.MAX_AUDIO_BYTES:
                            too_large = True
                            break
                        await f.write(chunk)

            if too_large:
                file_path.unlink(missing_ok=True)
                song.unplayable_reason = "音频文件超过 200MB 下载上限"
                logger.error(f"[KookMusic] 音频流超过大小限制: {song.name}")
                return song

            if (
                bytes_written < self.MIN_AUDIO_BYTES
                or not self._has_audio_signature(bytes(prefix))
            ):
                file_path.unlink(missing_ok=True)
                song.unplayable_reason = (
                    song.unplayable_reason
                    or "解析源返回的内容不是有效音频，歌曲可能需要会员"
                )
                logger.error(f"[KookMusic] 下载结果不是有效音频: {song.name}")
                return song

            song.file_path = str(file_path)
            song.unplayable_reason = ""
            logger.info(f"[KookMusic] 下载完成: {song.name} -> {file_path.name}")
            return song

        except asyncio.CancelledError:
            if file_path and file_path.exists():
                file_path.unlink(missing_ok=True)
            raise
        except Exception as e:
            logger.error(f"[KookMusic] 下载异常 '{song.name}': {e}")
            # 清理未完成的文件
            if file_path and file_path.exists():
                file_path.unlink(missing_ok=True)
            return song

    @staticmethod
    def _is_invalid_content_type(content_type: str) -> bool:
        return (
            content_type.startswith("text/")
            or content_type.startswith("image/")
            or "json" in content_type
            or "html" in content_type
            or "xml" in content_type
        )

    @staticmethod
    def _has_audio_signature(sample: bytes) -> bool:
        return (
            sample.startswith((b"ID3", b"OggS", b"fLaC", b"RIFF"))
            or b"ftyp" in sample[:16]
            or (len(sample) >= 2 and sample[0] == 0xFF and sample[1] & 0xE0 == 0xE0)
        )

    @staticmethod
    def _is_safe_audio_url(audio_url: str) -> bool:
        parsed = urlparse(audio_url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return False
        if parsed.username or parsed.password:
            return False
        hostname = parsed.hostname.rstrip(".").lower()
        if hostname == "localhost" or hostname.endswith(".localhost"):
            return False
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            return True
        return not (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        )

    @staticmethod
    def _guess_extension(content_type: str, audio_url: str) -> str:
        mime = content_type.split(";", 1)[0].strip()
        mime_extensions = {
            "audio/mp4": ".m4a",
            "audio/x-m4a": ".m4a",
            "audio/aac": ".aac",
            "audio/ogg": ".ogg",
            "application/ogg": ".ogg",
            "audio/flac": ".flac",
            "audio/x-flac": ".flac",
            "audio/wav": ".wav",
            "audio/x-wav": ".wav",
            "audio/mpeg": ".mp3",
        }
        if mime in mime_extensions:
            return mime_extensions[mime]
        suffix = Path(urlparse(audio_url).path).suffix.lower()
        if suffix in {".mp3", ".m4a", ".aac", ".ogg", ".flac", ".wav"}:
            return suffix
        return ".mp3"
