"""
FFmpeg 进程管理器。
将本地音频文件转码为 Opus 并通过 RTP 推流到 KOOK 语音频道。

支持两种推流模式：
  - DirectFFmpegPlayer：每首歌启动独立 FFmpeg 直连 RTP（简单，但歌曲切换时 RTP 会中断）
  - RelayFFmpegPlayer：PCM 管道接入常驻 Opus 编码器（RTP 时间戳持续递增）

歌曲解码为有界缓冲的 PCM，由 20ms 节拍写入常驻编码器；无歌曲时填充静音。
避免 MPEGTS 探测丢失首段音频，以及切歌时各歌曲时间戳归零。
"""
import asyncio
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger("astrbot")
_HTTPS_URL_PATTERN = re.compile(r"https://[^\s\"'<>]+", re.IGNORECASE)


def _is_https_source(source: str) -> bool:
    try:
        parsed = urlparse(source)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
    )


def _source_exists(source: str) -> bool:
    if source.lower().startswith(("http://", "https://")):
        return _is_https_source(source)
    return Path(source).exists()


def _source_label(source: str) -> str:
    """返回不会泄漏 CDN 签名参数的日志标签。"""
    if _is_https_source(source):
        return f"https://{urlparse(source).hostname}/<signed-audio>"
    return Path(source).name


def _redact_signed_urls(text: str) -> str:
    """FFmpeg 错误有时会回显完整输入 URL，日志中统一去除。"""
    return _HTTPS_URL_PATTERN.sub("https://<redacted>/<signed-audio>", text)


def _http_input_args(
    source: str,
    extra_headers: dict | None,
    start_seconds: float = 0.0,
) -> list[str]:
    if not _is_https_source(source):
        args = []
        if start_seconds > 0:
            args.extend(["-ss", f"{start_seconds:.3f}"])
        args.extend(["-i", source])
        return args

    # B站 CDN 需要 Referer 与 User-Agent。只允许这两个非敏感头，Cookie、
    # Authorization 等登录信息不会进入 FFmpeg 命令行或被转发给 CDN。
    lowered = {
        str(name).lower(): str(value)
        for name, value in (extra_headers or {}).items()
    }
    header_lines = []
    for name in ("User-Agent", "Referer"):
        value = lowered.get(name.lower(), "").strip()
        if value and len(value) <= 1024 and not any(
            char in value for char in "\r\n\0"
        ):
            header_lines.append(f"{name}: {value}")

    args = [
        "-rw_timeout", "15000000",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
    ]
    if header_lines:
        args.extend(["-headers", "\r\n".join(header_lines) + "\r\n"])
    if start_seconds > 0:
        args.extend(["-ss", f"{start_seconds:.3f}"])
    args.extend(["-i", source])
    return args


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

    async def play(
        self,
        file_path: str,
        rtp_url: str,
        ssrc: int,
        extra_headers: dict | None = None,
        start_seconds: float = 0.0,
    ) -> bool:
        await self.stop()
        if not _source_exists(file_path):
            logger.error(f"[KookMusic] 音频播放源无效: {_source_label(file_path)}")
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
            "-nostdin",
            "-re",
            "-nostats",
            "-loglevel", "warning",
            *_http_input_args(file_path, extra_headers, start_seconds),
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

        logger.info(
            f"[KookMusic] FFmpeg 启动: source={_source_label(file_path)}, "
            f"rtp={rtp_host}:{rtp_port}"
        )

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
                    f"{_redact_signed_urls(stderr_text)[:500]}"
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
                    logger.debug(f"[KookMusic] FFmpeg: {_redact_signed_urls(text)}")
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


@dataclass
class _PCMTrack:
    process: asyncio.subprocess.Process | None = None
    frames: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=25))
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    has_audio: bool = False
    played_bytes: int = 0
    decoded: bool = False
    exit_code: int | None = None
    decoder_task: asyncio.Task | None = None
    stderr_task: asyncio.Task | None = None


class RelayFFmpegPlayer:
    """Decode songs to PCM while one encoder maintains the RTP clock."""

    SAMPLE_RATE = 48000
    FRAME_SECONDS = 0.02
    FRAME_BYTES = 3840  # 960 stereo signed 16-bit samples.
    WARMUP_SECONDS = 2.0
    SOURCE_READY_TIMEOUT = 30.0

    def __init__(self, ffmpeg_path: str = "ffmpeg", volume: float = 0.15):
        self.volume = volume
        self.ffmpeg_path = ffmpeg_path
        self._relay: asyncio.subprocess.Process | None = None
        self._feed_task: asyncio.Task | None = None
        self._relay_stderr_task: asyncio.Task | None = None
        self._relay_failed = False
        self._song: _PCMTrack | None = None
        self._current_file = ""

    @property
    def is_playing(self) -> bool:
        return self._song is not None and not self._song.done.is_set()

    @property
    def played_seconds(self) -> float:
        """Media emitted this play(), excluding relay warmup/underflow silence."""
        return self._song.played_bytes / (self.SAMPLE_RATE * 4) if self._song else 0.0

    @property
    def is_relay_running(self) -> bool:
        return (
            self._relay is not None
            and self._relay.returncode is None
            and not self._relay_failed
        )

    async def start_relay(self, rtp_url: str, ssrc: int) -> bool:
        if self.is_relay_running:
            return True
        await self.stop_relay()
        parsed = urlparse(rtp_url)
        host = parsed.hostname or ""
        port = parsed.port or 0
        rtcp_port = parse_qs(parsed.query).get("rtcpport", ["0"])[0]
        cmd = [
            self.ffmpeg_path, "-nostdin", "-nostats", "-loglevel", "warning",
            "-f", "s16le", "-ar", str(self.SAMPLE_RATE), "-ac", "2",
            "-probesize", "32", "-analyzeduration", "0", "-i", "pipe:0",
            "-map", "0:a:0", "-acodec", "libopus", "-ab", "128k",
            "-frame_duration", "20", "-flush_packets", "1",
            "-ssrc", str(ssrc), "-payload_type", "100", "-f", "rtp",
            f"rtp://{host}:{port}?rtcpport={rtcp_port}",
        ]
        try:
            self._relay = await self._spawn(
                cmd, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            self._relay_failed = False
            self._relay_stderr_task = asyncio.create_task(
                self._read_relay_stderr(self._relay.stderr)
            )
            self._feed_task = asyncio.create_task(self._feed_pcm(self._relay))
            # Warm up the voice transport using silence, not the song's opening.
            await asyncio.sleep(self.WARMUP_SECONDS)
            if not self.is_relay_running:
                await self.stop_relay()
                return False
            logger.info("[KookMusic] PCM relay ready, PID: %s", self._relay.pid)
            return True
        except asyncio.CancelledError:
            await self.stop_relay()
            raise
        except Exception as exc:
            logger.error("[KookMusic] PCM relay failed (%s): %s", type(exc).__name__, _redact_signed_urls(str(exc)))
            await self.stop_relay()
            return False

    @staticmethod
    async def _spawn(command, **kwargs):
        # Cancellation during process creation must not orphan a decoder/encoder.
        task = asyncio.create_task(asyncio.create_subprocess_exec(*command, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            async def reap_late_process():
                process = await task
                await RelayFFmpegPlayer._terminate_process(process)

            await RelayFFmpegPlayer._finish_cleanup(reap_late_process())
            raise

    async def _feed_pcm(self, relay):
        silence = bytes(self.FRAME_BYTES)
        loop = asyncio.get_running_loop()
        deadline = loop.time()
        try:
            while relay is self._relay and relay.returncode is None:
                track = self._song
                frame = silence
                song_frame = False
                if track and not track.done.is_set():
                    try:
                        frame = track.frames.get_nowait()
                        song_frame = True
                    except asyncio.QueueEmpty:
                        if track.decoded:
                            track.done.set()
                relay.stdin.write(frame)
                await asyncio.wait_for(relay.stdin.drain(), timeout=1.0)
                if song_frame:
                    track.played_bytes += len(frame)
                deadline += self.FRAME_SECONDS
                # Do not burst buffered audio after an event-loop or pipe stall.
                if deadline < loop.time() - self.FRAME_SECONDS:
                    deadline = loop.time()
                await asyncio.sleep(max(0, deadline - loop.time()))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[KookMusic] PCM relay stream failed (%s): %s", type(exc).__name__, _redact_signed_urls(str(exc)))
        finally:
            if relay is self._relay:
                self._relay_failed = True
                track = self._song
                if track and not track.done.is_set():
                    track.exit_code = 1
                    track.ready.set()
                    track.done.set()

    async def play(
        self,
        file_path: str,
        rtp_url: str = "",
        ssrc: int = 0,
        extra_headers: dict | None = None,
        start_seconds: float = 0.0,
    ) -> bool:
        await self.stop()
        if not self.is_relay_running or not _source_exists(file_path):
            logger.error("[KookMusic] Invalid relay/source: %s", _source_label(file_path))
            return False
        track = _PCMTrack()
        self._song = track
        self._current_file = file_path
        cmd = [
            self.ffmpeg_path, "-nostdin", "-nostats", "-loglevel", "warning",
            *_http_input_args(file_path, extra_headers, start_seconds),
            "-map", "0:a:0", "-filter:a", f"volume={self.volume}",
            "-acodec", "pcm_s16le", "-ac", "2", "-ar", str(self.SAMPLE_RATE),
            "-f", "s16le", "pipe:1",
        ]
        try:
            process = await self._spawn(
                cmd, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=65536,
            )
            track.process = process
            if self._song is not track:
                await self._terminate_process(process)
                return False
            track.stderr_task = asyncio.create_task(self._read_song_stderr(process.stderr))
            track.decoder_task = asyncio.create_task(self._decode_song(track))
            await asyncio.wait_for(track.ready.wait(), self.SOURCE_READY_TIMEOUT)
            failed_or_empty = track.done.is_set() and (
                not track.has_audio or track.exit_code != 0
            )
            if self._song is not track or failed_or_empty or not self.is_relay_running:
                if self._song is track:
                    await self.stop()
                return False
            logger.info("[KookMusic] PCM song ready: %s", _source_label(file_path))
            return True
        except asyncio.CancelledError:
            if self._song is track:
                await self.stop()
            raise
        except Exception as exc:
            logger.error("[KookMusic] PCM song failed (%s): %s", type(exc).__name__, _redact_signed_urls(str(exc)))
            if self._song is track:
                await self.stop()
            return False

    async def _decode_song(self, track):
        try:
            while self._song is track:
                try:
                    frame = await track.process.stdout.readexactly(self.FRAME_BYTES)
                except asyncio.IncompleteReadError as exc:
                    if exc.partial:
                        await track.frames.put(exc.partial.ljust(self.FRAME_BYTES, b"\0"))
                        track.has_audio = True
                        track.ready.set()
                    break
                await track.frames.put(frame)
                track.has_audio = True
                track.ready.set()
            track.exit_code = await track.process.wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            track.exit_code = 1
            logger.debug("[KookMusic] PCM decode failed: %s", _redact_signed_urls(str(exc)))
        finally:
            track.decoded = True
            track.ready.set()
            if not track.has_audio:
                track.done.set()

    async def stop(self):
        # Detach before any await, so no buffered old-song frames can be selected.
        track, self._song = self._song, None
        self._current_file = ""
        if track is None:
            return
        track.exit_code = None
        track.ready.set()
        track.done.set()
        await self._finish_cleanup(self._cleanup_track(track))

    async def _cleanup_track(self, track):
        await self._cancel_task(track.decoder_task)
        await self._cancel_task(track.stderr_task)
        await self._terminate_process(track.process)

    async def stop_relay(self):
        await self._finish_cleanup(self._cleanup_relay())

    async def _cleanup_relay(self):
        await self.stop()
        relay, self._relay = self._relay, None
        feed, self._feed_task = self._feed_task, None
        stderr, self._relay_stderr_task = self._relay_stderr_task, None
        await self._cancel_task(feed)
        await self._cancel_task(stderr)
        await self._terminate_process(relay)

    @staticmethod
    async def _finish_cleanup(awaitable):
        # Repeated skip/kick/unload cancellation must not interrupt process reaping.
        task = asyncio.create_task(awaitable)
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        result = task.result()
        if cancelled:
            raise asyncio.CancelledError
        return result

    @staticmethod
    async def _cancel_task(task):
        if task and task is not asyncio.current_task():
            if not task.done():
                task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    @staticmethod
    async def _terminate_process(process):
        if process is None:
            return
        try:
            if process.returncode is None:
                process.kill()
            # Draining stdout avoids Process.wait() hanging on a full decoder pipe.
            await process.communicate()
        except ProcessLookupError:
            pass
        except Exception as exc:
            logger.debug("[KookMusic] Process cleanup failed: %s", _redact_signed_urls(str(exc)))

    async def wait_until_done(self, timeout: float | None = None) -> bool:
        track = self._song
        if track is None:
            return False
        try:
            await asyncio.wait_for(track.done.wait(), timeout)
        except asyncio.TimeoutError:
            if self._song is track:
                await self.stop()
            return False
        return track.exit_code == 0 and self.is_relay_running

    async def _read_relay_stderr(self, stderr_stream):
        await self._read_log_stream(stderr_stream, "relay")

    async def _read_song_stderr(self, stderr_stream):
        await self._read_log_stream(stderr_stream, "song")

    @staticmethod
    async def _read_log_stream(stderr_stream, label):
        if stderr_stream is None:
            return
        try:
            async for line in stderr_stream:
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    logger.debug("[KookMusic] %s: %s", label, _redact_signed_urls(text))
        except Exception:
            pass


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
        logger.info("[KookMusic] 使用推流模式: relay (PCM 连续推流)")
        return RelayFFmpegPlayer(ffmpeg_path=ffmpeg_path, volume=volume)
