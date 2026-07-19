"""
Bilibili 视频音频提取模块。

使用 yt-dlp 提取 B站视频元数据并下载音频流。
支持 BV号、完整链接（含分P）。
"""

import asyncio
import logging
import re
import uuid
from pathlib import Path

import yt_dlp

from .model import Song

logger = logging.getLogger("astrbot")

# BV号匹配正则：12位 BV 号 + 可选分P参数
_BV_PATTERN = re.compile(r"BV\w{10}(?:\?p=(\d+))?", re.IGNORECASE)
# 从完整 URL 中提取 BV号
_URL_BV_PATTERN = re.compile(
    r"bilibili\.com/video/(BV\w{10})(?:.*?[?&]p=(\d+))?", re.IGNORECASE
)


class BilibiliExtractor:
    """B站视频音频提取器（使用 yt-dlp）"""

    def __init__(self):
        self._ffmpeg_path: str = ""
        self._cookies_file: str = ""

    def set_ffmpeg_path(self, path: str):
        """设置 FFmpeg 路径，供 yt-dlp 使用"""
        self._ffmpeg_path = path

    def set_cookies_file(self, path: str):
        """设置 B站 cookies 文件路径（Netscape 格式）"""
        if path and Path(path).exists():
            self._cookies_file = path
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

        data_dir.mkdir(parents=True, exist_ok=True)
        cookies_path = data_dir / "bili_cookies.txt"

        # 写入 Netscape cookies 格式
        with open(cookies_path, "w", encoding="utf-8") as f:
            f.write("# Netscape HTTP Cookie File\n")
            f.write(f".bilibili.com\tTRUE\t/\tFALSE\t0\tSESSDATA\t{sessdata}\n")

        self._cookies_file = str(cookies_path)
        logger.info("[KookMusic] 已从配置生成 B站 cookies 文件")

    async def close(self):
        """兼容接口，yt-dlp 无需持久连接"""
        pass

    def parse_input(self, text: str) -> tuple[str | None, int]:
        """解析用户输入，提取 BV号和分P信息。

        Returns:
            (bvid, page) — bvid 为 None 表示无法识别
        """
        text = text.strip()

        # 1. 尝试从完整 URL 中匹配
        m = _URL_BV_PATTERN.search(text)
        if m:
            bvid = m.group(1)
            page = int(m.group(2)) if m.group(2) else 1
            return bvid, page

        # 2. 尝试直接匹配 BV号
        m = _BV_PATTERN.search(text)
        if m:
            bvid = m.group(0).split("?")[0]
            page = int(m.group(1)) if m.group(1) else 1
            return bvid, page

        # 3. 无法识别
        return None, 1

    def _build_url(self, bvid: str, page: int = 1) -> str:
        """构建 B站视频 URL"""
        url = f"https://www.bilibili.com/video/{bvid}"
        if page > 1:
            url += f"?p={page}"
        return url

    def _base_ydl_opts(self) -> dict:
        """基础 yt-dlp 选项"""
        opts = {
            "quiet": True,
            "no_warnings": True,
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
        bvid, page = self.parse_input(text)
        if bvid is None:
            logger.warning(f"[KookMusic] 无法从输入中解析 BV号: {text}")
            return None

        url = self._build_url(bvid, page)

        try:
            info = await asyncio.to_thread(self._yt_extract_info, url)
        except Exception as e:
            logger.warning(f"[KookMusic] yt-dlp 解析B站视频失败: {e}")
            return None

        if not info:
            return None

        title = info.get("title", "未知视频")
        uploader = info.get("uploader", "") or info.get("channel", "未知UP主")
        duration_sec = info.get("duration") or 0
        thumbnail = info.get("thumbnail", "")

        logger.info(f"[KookMusic] B站视频解析成功: {title} - {uploader}")

        return Song(
            id=bvid,
            name=title,
            artists=uploader,
            duration=int(duration_sec * 1000),
            audio_url=url,  # 视频 URL 作为标记，后续由 download_audio 下载
            cover_url=thumbnail,
            platform="bilibili",
        )

    def _yt_extract_info(self, url: str) -> dict | None:
        """同步调用 yt-dlp 提取视频信息"""
        opts = self._base_ydl_opts()
        opts["skip_download"] = True
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception as e:
            logger.warning(f"[KookMusic] yt-dlp extract_info 异常: {e}")
            return None

    async def download_audio(self, song: Song, cache_dir: Path) -> Song:
        """使用 yt-dlp 下载B站视频音频到本地。

        Args:
            song: 包含 B站视频信息的 Song 对象
            cache_dir: 音频缓存目录

        Returns:
            更新了 file_path 的 Song 对象
        """
        url = song.audio_url or self._build_url(song.id)
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
        })

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)

            # 从 yt-dlp 返回中获取实际文件路径
            if info and "requested_downloads" in info:
                return info["requested_downloads"][0]["filepath"]

            # 回退：根据 ext 推断路径
            ext = info.get("ext", "m4a") if info else "m4a"
            return output_base + "." + ext
