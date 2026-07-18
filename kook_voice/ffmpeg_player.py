"""
FFmpeg 进程管理器。
将本地音频文件转码为 Opus 并通过 RTP 推流到 KOOK 语音频道。

支持两种推流模式：
  - DirectFFmpegPlayer：每首歌启动独立 FFmpeg 直连 RTP（简单，但歌曲切换时 RTP 会中断）
  - RelayFFmpegPlayer：基于 UDP 的双进程中继推流（RTP 不中断，切歌无缝衔接）

RelayFFmpegPlayer 架构（借鉴 KO-ON-Bot）：
  歌曲进程: ffmpeg -re -i song.mp3 -acodec libopus -f mpegts udp://127.0.0.1:{port}
  中继进程: ffmpeg -i udp://127.0.0.1:{port} -c:a copy -f rtp {rtp_url}
  切歌时只需 kill 歌曲进程并启新进程，中继进程的 RTP 连接始终保持。
"""
import asyncio
import logging
import socket
from pathlib import Path
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger("astrbot")


class DirectFFmpegPlayer:
    """直连 RTP 模式：每首歌启动独立 FFmpeg 进程

    每首歌播放时启动 FFmpeg 将音频转码为 Opus 并直接推送到 RTP 地址。
    歌曲切换时需要重建 mediasoup Transport（通过 VoiceClient.refresh_rtp）。
    """

    def __init__(self, ffmpeg_path: str = "ffmpeg", volume: float = 0.15):
        self.volume = volume
        self.ffmpeg_path = ffmpeg_path
        self._process: asyncio.subprocess.Process | None = None
        self._current_file: str = ""

    @property
    def is_playing(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def play(self, file_path: str, rtp_url: str, ssrc: int) -> bool:
        await self.stop()
        if not Path(file_path).exists():
            logger.error(f"[KookMusic] 音频文件不存在: {file_path}")
            return False
        self._current_file = file_path

        # 解析 rtp_url: rtp://ip:port?rtcpport=xxx
        parsed = urlparse(rtp_url)
        rtp_host = parsed.hostname or ""
        rtp_port = parsed.port or 0
        qs = parse_qs(parsed.query)
        rtcp_port = qs.get("rtcpport", ["0"])[0]

        # 构建 FFmpeg 命令
        cmd = [
            self.ffmpeg_path,
            "-re",
            "-nostats",
            "-loglevel", "warning",
            "-i", file_path,
            "-map", "0:a",
            "-acodec", "libopus",
            "-ab", "128k",
            "-filter:a", f"volume={self.volume}",
            "-ac", "2",
            "-ar", "48000",
            "-ssrc", str(ssrc),
            "-payload_type", "100",
            "-f", "rtp",
            f"rtp://{rtp_host}:{rtp_port}?rtcpport={rtcp_port}",
        ]

        cmd_str = " ".join(f'"{c}"' if " " in c or "?" in c else c for c in cmd)
        logger.info(f"[KookMusic] FFmpeg 启动: {cmd_str[:200]}...")

        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            # 等待一小段时间检查是否立即退出（参数错误等）
            await asyncio.sleep(1.5)
            if self._process.returncode is not None:
                # 进程已退出，说明出错（正常播放不会这么快结束）
                stderr_data = b""
                if self._process.stderr:
                    stderr_data = await self._process.stderr.read()
                stderr_text = stderr_data.decode("utf-8", errors="replace") if stderr_data else ""
                exit_code = self._process.returncode
                logger.error(
                    f"[KookMusic] FFmpeg 提前退出 (exit={exit_code}): "
                    f"{stderr_text[:500]}"
                )
                self._process = None
                return False

            logger.info(f"[KookMusic] FFmpeg PID: {self._process.pid}")

            # 启动后台 stderr 读取任务（捕获 stderr 引用，防止 self._process 被置 None 后引用悬垂）
            stderr_stream = self._process.stderr
            if stderr_stream:
                asyncio.create_task(self._read_stderr(stderr_stream))

            return True
        except FileNotFoundError:
            logger.error(f"[KookMusic] FFmpeg 未找到: {self.ffmpeg_path}")
            self._process = None
            return False
        except Exception as e:
            logger.error(f"[KookMusic] FFmpeg 异常: {e}")
            self._process = None
            return False

    async def _read_stderr(self, stderr_stream):
        """后台读取 FFmpeg stderr 输出

        Args:
            stderr_stream: 已捕获的 stderr 流引用（避免通过 self._process 访问）
        """
        try:
            async for line in stderr_stream:
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    logger.debug(f"[KookMusic] FFmpeg: {text}")
        except Exception:
            pass

    async def stop(self):
        """停止 FFmpeg 进程"""
        if self._process is not None:
            try:
                self._process.kill()
                await self._process.wait()
            except ProcessLookupError:
                pass
            except Exception as e:
                logger.debug(f"[KookMusic] 停止 FFmpeg 异常: {e}")
            self._process = None
        self._current_file = ""

    async def wait_until_done(self, timeout: float | None = None) -> bool:
        """等待 FFmpeg 进程结束。

        Args:
            timeout: 最长等待秒数。超时后强制终止。

        Returns:
            True 表示正常结束，False 表示超时/异常。
        """
        if not self._process:
            return False
        try:
            if timeout is not None:
                try:
                    exit_code = await asyncio.wait_for(
                        self._process.wait(), timeout=timeout
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        f"[KookMusic] FFmpeg 播放超时 ({timeout:.0f}s)，强制终止"
                    )
                    await self.stop()
                    return False
            else:
                exit_code = await self._process.wait()
            self._process = None
            self._current_file = ""
            return exit_code == 0
        except Exception:
            return False


class RelayFFmpegPlayer:
    """UDP 中继模式：常驻中继进程 + 每首歌独立歌曲进程

    架构（借鉴 KO-ON-Bot，使用 UDP 本地通信）：
      歌曲进程: ffmpeg -re -i song.mp3 -acodec libopus -f mpegts udp://127.0.0.1:{port}
      中继进程: ffmpeg -i udp://127.0.0.1:{port} -c:a copy -f rtp {rtp_url}

    中继进程在加入语音频道时启动，退出时才停止，RTP 流始终保持。
    歌曲切换时只需 kill 旧歌曲进程 + 启动新歌曲进程，中继进程完全不受影响。
    """

    def __init__(self, ffmpeg_path: str = "ffmpeg", volume: float = 0.15):
        self.volume = volume
        self.ffmpeg_path = ffmpeg_path
        # 常驻中继进程（RTP 推流）
        self._relay: asyncio.subprocess.Process | None = None
        # 当前歌曲进程（编码 → UDP）
        self._song_proc: asyncio.subprocess.Process | None = None
        # 歌曲播放完成事件
        self._song_done: asyncio.Event = asyncio.Event()
        self._song_done.set()  # 初始为已完成状态
        # 歌曲完成监控任务
        self._song_monitor: asyncio.Task | None = None
        self._current_file: str = ""
        # UDP 中继端口
        self._udp_port: int = 0
        # 保存 RTP 参数（用于切歌时重建中继）
        self._rtp_url: str = ""
        self._ssrc: int = 0

    @property
    def is_playing(self) -> bool:
        """当前是否有歌曲在播放"""
        return self._song_proc is not None and self._song_proc.returncode is None

    @property
    def is_relay_running(self) -> bool:
        """中继进程是否在运行"""
        return self._relay is not None and self._relay.returncode is None

    async def start_relay(self, rtp_url: str, ssrc: int) -> bool:
        """启动常驻中继进程（加入语音频道时调用）。

        中继进程通过 UDP 接收 mpegts 数据，然后通过 RTP 推送到 KOOK。

        Args:
            rtp_url: KOOK 语音服务器 RTP 地址 (rtp://ip:port?rtcpport=xxx)
            ssrc: KOOK 分配的 SSRC 值
        """
        # 保存参数，切歌时用于重建中继
        self._rtp_url = rtp_url
        self._ssrc = ssrc

        if self.is_relay_running:
            logger.warning("[KookMusic] 中继进程已在运行")
            return True

        # 分配 UDP 中继端口
        self._udp_port = _find_free_port()
        if not self._udp_port:
            logger.error("[KookMusic] 无法分配 UDP 端口")
            return False

        return await self._start_relay_internal()

    async def _start_relay_internal(self) -> bool:
        """启动中继进程的内部实现（使用当前的 _udp_port 和 _rtp_url/_ssrc）"""
        # 解析 RTP 地址
        parsed = urlparse(self._rtp_url)
        rtp_host = parsed.hostname or ""
        rtp_port = parsed.port or 0
        qs = parse_qs(parsed.query)
        rtcp_port = qs.get("rtcpport", ["0"])[0]

        # 中继进程命令：从 UDP 读取 mpegts → 直接转发 → RTP 推流
        # 中继 bind UDP 端口（持久），歌曲进程 send 到此端口（无状态）
        # UDP 无连接态，kill 歌曲进程后无 TIME_WAIT，新歌曲直接复用端口
        # -fflags nobuffer: 减少输入缓冲，降低延迟
        # -c:a copy: 直接复制 opus 数据，不重新解码/编码
        udp_input = (
            f"udp://127.0.0.1:{self._udp_port}"
            f"?overrun_nonfatal=1&fifo_size=50&timeout=0"
        )
        cmd = [
            self.ffmpeg_path,
            "-f", "mpegts",
            "-fflags", "nobuffer",
            "-probesize", "32768",
            "-analyzeduration", "0",
            "-loglevel", "warning",
            "-nostats",
            "-i", udp_input,
            "-map", "0:a:0",
            "-c:a", "copy",
            "-f", "tee",
            f"[select=a:f=rtp:ssrc={self._ssrc}:payload_type=100]"
            f"rtp://{rtp_host}:{rtp_port}?rtcpport={rtcp_port}",
        ]

        cmd_str = " ".join(f'"{c}"' if " " in c or "?" in c else c for c in cmd)
        logger.info(f"[KookMusic] 中继进程启动: {cmd_str[:300]}...")

        try:
            self._relay = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            # 检查是否立即退出
            await asyncio.sleep(1.5)
            if self._relay.returncode is not None:
                stderr_data = b""
                if self._relay.stderr:
                    stderr_data = await self._relay.stderr.read()
                stderr_text = stderr_data.decode("utf-8", errors="replace") if stderr_data else ""
                logger.error(
                    f"[KookMusic] 中继进程提前退出 "
                    f"(exit={self._relay.returncode}): {stderr_text[:500]}"
                )
                self._relay = None
                return False

            logger.info(
                f"[KookMusic] 中继进程 PID: {self._relay.pid}, "
                f"UDP 端口: {self._udp_port}"
            )

            # 后台读取 stderr（捕获引用防止悬垂）
            relay_stderr = self._relay.stderr
            if relay_stderr:
                asyncio.create_task(self._read_relay_stderr(relay_stderr))
            return True

        except FileNotFoundError:
            logger.error(f"[KookMusic] FFmpeg 未找到: {self.ffmpeg_path}")
            self._relay = None
            return False
        except Exception as e:
            logger.error(f"[KookMusic] 中继进程启动异常: {e}")
            self._relay = None
            return False

    async def play(
        self, file_path: str, rtp_url: str = "", ssrc: int = 0
    ) -> bool:
        """播放一首歌（启动歌曲进程将音频通过 UDP 推入中继）。

        Args:
            file_path: 本地音频文件路径
            rtp_url: 兼容 Direct 模式参数，relay 模式忽略
            ssrc: 兼容 Direct 模式参数，relay 模式忽略
        """
        await self.stop()

        if not self.is_relay_running:
            logger.error("[KookMusic] 中继进程未运行，无法播放")
            return False

        if not Path(file_path).exists():
            logger.error(f"[KookMusic] 音频文件不存在: {file_path}")
            return False

        return await self._try_start_song(file_path)

    async def _try_start_song(self, file_path: str) -> bool:
        """尝试启动歌曲进程（单次尝试）"""
        self._current_file = file_path
        self._song_done.clear()

        # 歌曲进程命令：读取音频 → 编码为 Opus（含音量调整）→ 通过 UDP 推入中继
        # -re 保证按实时速率推流（因为输入文件有时间戳）
        cmd = [
            self.ffmpeg_path,
            "-re",
            "-nostats",
            "-loglevel", "warning",
            "-i", file_path,
            "-acodec", "libopus",
            "-ab", "128k",
            "-filter:a", f"volume={self.volume}",
            "-ac", "2",
            "-ar", "48000",
            "-f", "mpegts",
            f"udp://127.0.0.1:{self._udp_port}?pkt_size=1316",
        ]

        logger.info(f"[KookMusic] 歌曲进程启动: {Path(file_path).name}")

        try:
            self._song_proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            # 检查是否立即退出（非零退出码才算失败）
            await asyncio.sleep(1.0)
            if self._song_proc.returncode is not None and self._song_proc.returncode != 0:
                stderr_data = b""
                if self._song_proc.stderr:
                    stderr_data = await self._song_proc.stderr.read()
                stderr_text = stderr_data.decode("utf-8", errors="replace") if stderr_data else ""
                logger.error(f"[KookMusic] 歌曲进程提前退出: {stderr_text[:300]}")
                self._song_proc = None
                self._song_done.set()
                return False

            logger.info(f"[KookMusic] 歌曲进程 PID: {self._song_proc.pid}")

            # 后台读取 stderr（捕获引用防止悬垂）
            song_stderr = self._song_proc.stderr
            if song_stderr:
                asyncio.create_task(self._read_song_stderr(song_stderr))

            # 启动歌曲完成监控
            self._song_monitor = asyncio.create_task(self._monitor_song())

            return True

        except FileNotFoundError:
            logger.error(f"[KookMusic] FFmpeg 未找到: {self.ffmpeg_path}")
            self._song_proc = None
            self._song_done.set()
            return False
        except Exception as e:
            logger.error(f"[KookMusic] 歌曲进程启动异常: {e}")
            self._song_proc = None
            self._song_done.set()
            return False

    async def _monitor_song(self):
        """监控歌曲进程，结束时设置 _song_done 事件"""
        try:
            if self._song_proc:
                await self._song_proc.wait()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"[KookMusic] 歌曲监控异常: {e}")
        finally:
            self._song_done.set()

    async def stop(self):
        """停止当前歌曲进程（不影响中继进程）"""
        await self._kill_song_proc()

    async def _kill_song_proc(self):
        """强制终止歌曲进程"""
        # 取消监控任务
        if self._song_monitor and not self._song_monitor.done():
            self._song_monitor.cancel()
            try:
                await self._song_monitor
            except (asyncio.CancelledError, Exception):
                pass
        self._song_monitor = None

        # 直接 kill 歌曲进程（UDP 无端口冲突问题）
        if self._song_proc is not None:
            try:
                self._song_proc.kill()
                await self._song_proc.wait()
            except ProcessLookupError:
                pass
            except Exception as e:
                logger.debug(f"[KookMusic] 停止歌曲进程异常: {e}")
            self._song_proc = None

        self._current_file = ""
        self._song_done.set()


    async def stop_relay(self):
        """停止中继进程和所有相关进程（退出语音频道时调用）"""
        await self.stop()

        if self._relay is not None:
            try:
                self._relay.kill()
                await self._relay.wait()
            except ProcessLookupError:
                pass
            except Exception as e:
                logger.debug(f"[KookMusic] 停止中继进程异常: {e}")
            self._relay = None

        self._udp_port = 0
        logger.info("[KookMusic] 中继进程已停止")

    async def wait_until_done(self, timeout: float | None = None) -> bool:
        """等待当前歌曲播放完成。

        注意：本方法只负责等待，不负责清理进程引用。
        调用方应在本方法返回后调用 stop() 确保进程已清理。

        Args:
            timeout: 最长等待秒数。超时后强制终止歌曲进程。

        Returns:
            True 表示正常播放完成，False 表示超时或异常。
        """
        if not self._song_proc:
            return False
        try:
            if timeout is not None:
                try:
                    await asyncio.wait_for(self._song_done.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    logger.warning(
                        f"[KookMusic] 播放超时 ({timeout:.0f}s)，强制终止歌曲进程"
                    )
                    await self.stop()
                    return False
            else:
                await self._song_done.wait()
            return True
        except Exception:
            return False

    async def _read_relay_stderr(self, stderr_stream):
        """后台读取中继进程 stderr

        Args:
            stderr_stream: 已捕获的 stderr 流引用
        """
        try:
            async for line in stderr_stream:
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    logger.debug(f"[KookMusic] 中继: {text}")
        except Exception:
            pass

    async def _read_song_stderr(self, stderr_stream):
        """后台读取歌曲进程 stderr

        Args:
            stderr_stream: 已捕获的 stderr 流引用
        """
        try:
            async for line in stderr_stream:
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    logger.debug(f"[KookMusic] 歌曲: {text}")
        except Exception:
            pass


# ============ 工具函数 ============


def _find_free_port() -> int:
    """自动分配一个可用的 UDP 端口"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]
    except OSError as e:
        logger.error(f"[KookMusic] 端口分配失败: {e}")
        return 0


def create_player(
    mode: str = "relay",
    ffmpeg_path: str = "ffmpeg",
    volume: float = 0.15,
) -> DirectFFmpegPlayer | RelayFFmpegPlayer:
    """工厂函数：根据配置创建播放器实例。

    Args:
        mode: "direct" 或 "relay"
        ffmpeg_path: FFmpeg 可执行文件路径
        volume: 播放音量

    Returns:
        播放器实例
    """
    if mode == "direct":
        logger.info("[KookMusic] 使用推流模式: direct (每首歌独立 RTP)")
        return DirectFFmpegPlayer(ffmpeg_path=ffmpeg_path, volume=volume)
    else:
        logger.info("[KookMusic] 使用推流模式: relay (UDP 中继)")
        return RelayFFmpegPlayer(ffmpeg_path=ffmpeg_path, volume=volume)
