"""
多服务器语音会话管理器。
管理多个 Guild 的语音连接、播放队列和生命周期。
"""
import asyncio
import copy
import logging
import math
import random
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Union

import aiohttp

from ..music.model import Song
from .ffmpeg_player import (
    DirectFFmpegPlayer,
    RelayFFmpegPlayer,
    create_player,
)
from .voice_client import VoiceClient

logger = logging.getLogger("astrbot")


@dataclass
class SongPrefetch:
    original: Song
    candidate: Song
    task: asyncio.Task | None = field(default=None, repr=False)
    discarded: bool = False


@dataclass(eq=False)
class RequestActivity:
    manager: "VoiceManager" = field(repr=False)
    guild_id: str

    @property
    def current(self) -> bool:
        return (
            not self.manager._closing
            and self in self.manager._activity_requests.get(self.guild_id, ())
        )


@dataclass
class GuildSession:
    """单个服务器的语音会话"""
    guild_id: str
    voice_channel_id: str
    text_channel_id: str
    voice_client: VoiceClient
    ffmpeg_player: Union[DirectFFmpegPlayer, RelayFFmpegPlayer]
    playlist: list[Song] = field(default_factory=list)
    loop_mode: int = 0  # 0=关闭 1=单曲 2=列表 3=随机
    is_playing: bool = False
    idle_seconds: int = 0
    needs_relay_refresh: bool = False
    needs_direct_refresh: bool = False
    pending_skips: int = 0
    playback_retry_count: int = 0
    playback_started_at: float = 0.0
    playback_offset_seconds: float = 0.0
    preparation_task: asyncio.Task | None = field(default=None, repr=False)
    prefetch: SongPrefetch | None = field(default=None, repr=False)
    prefetch_tasks: set[asyncio.Task] = field(default_factory=set, repr=False)
    notification_tasks: set[asyncio.Task] = field(default_factory=set, repr=False)
    removal_requested: bool = False
    playback_generation: int = 0
    prior_departure_pending: bool = False
    initialization_done: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    @property
    def current_song(self) -> Song | None:
        return self.playlist[0] if self.playlist else None

    @property
    def loop_mode_name(self) -> str:
        return {0: "关闭", 1: "单曲循环", 2: "列表循环", 3: "随机播放"}.get(
            self.loop_mode, "未知"
        )


class VoiceManager:
    """语音会话管理器"""

    # KOOK API
    JOINED_CHANNEL_URL = (
        "https://www.kookapp.cn/api/v3/channel-user/get-joined-channel"
    )

    # FFmpeg 播放超时额外缓冲（秒）
    PLAYBACK_TIMEOUT_BUFFER = 30
    PLAYBACK_START_DELAY = 0.0
    DIRECT_RECONNECT_DELAY = 0.5
    PLAYBACK_RETRY_DELAY = 5.0
    MAX_PLAYBACK_RETRIES = 3
    PRIOR_EXIT_JOIN_GRACE = 1.0
    STREAM_RESUME_MAX_ATTEMPTS = 1
    STREAM_RESUME_REWIND_SECONDS = 5.0
    _STREAM_RESUME_OFFSET_KEY = "_kook_music_stream_resume_offset"
    _STREAM_RESUME_ATTEMPTS_KEY = "_kook_music_stream_resume_attempts"
    _STREAM_NOTIFIED_KEY = "_kook_music_stream_notified"

    def __init__(
        self,
        max_sessions: int = 5,
        auto_leave_timeout: int = 300,
        volume: float = 0.15,
        ffmpeg_path: str = "ffmpeg",
        streaming_mode: str = "relay",
        max_queue_size: int = 50,
        prefetch_next: bool = True,
    ):
        self.max_sessions = max_sessions
        self.auto_leave_timeout = auto_leave_timeout
        self.volume = volume
        self.ffmpeg_path = ffmpeg_path
        self.streaming_mode = streaming_mode  # "direct" or "relay"
        self.max_queue_size = max_queue_size
        self.prefetch_next = prefetch_next
        self.sessions: dict[str, GuildSession] = {}
        self._pending_sessions: dict[str, GuildSession] = {}
        self._initialization_tasks: dict[str, asyncio.Task] = {}
        self._playback_tasks: dict[str, asyncio.Task] = {}
        self._retry_tasks: dict[str, asyncio.Task] = {}
        self._guild_locks: dict[str, asyncio.Lock] = {}
        self._activity_requests: dict[str, set[RequestActivity]] = {}
        self._recent_departures: dict[str, tuple[str, str, float]] = {}
        self._session_creation_lock = asyncio.Lock()
        self._idle_task: asyncio.Task | None = None
        self._closing = False
        self.on_playback_finished: callable = None  # 回调：播放全部完成时调用
        self.on_song_started: callable = None  # 回调：新歌曲开始播放时调用 (guild_id, song)
        self.on_download_song: callable = None  # 回调：下载歌曲 async (Song) -> Song

    def get_session(self, guild_id: str) -> GuildSession | None:
        return self.sessions.get(guild_id)

    @contextmanager
    def request_activity(self, guild_id: str):
        """解析到入队期间暂停空闲退出；停止/被踢会使迟到结果失效。"""
        request = RequestActivity(self, guild_id)
        self._activity_requests.setdefault(guild_id, set()).add(request)
        session = self.sessions.get(guild_id)
        if session:
            session.idle_seconds = 0
        try:
            yield request
        finally:
            requests = self._activity_requests.get(guild_id)
            if requests is not None:
                requests.discard(request)
                if not requests:
                    self._activity_requests.pop(guild_id, None)
            session = self.sessions.get(guild_id)
            if session:
                session.idle_seconds = 0

    def _invalidate_requests(self, guild_id: str):
        self._activity_requests.pop(guild_id, None)

    @staticmethod
    def control_song(session: GuildSession) -> Song | None:
        """已排队跳过的歌曲不再授予下一次控制权限。"""
        index = session.pending_skips
        if 0 <= index < len(session.playlist):
            return session.playlist[index]
        return None

    async def control(
        self,
        guild_id: str,
        action: str,
        *,
        actor_id: str,
        is_admin: bool = False,
        expected_session: GuildSession | None = None,
        position: int | None = None,
        destination: int | None = None,
    ) -> tuple[bool, str]:
        """在执行前原子检查当前点歌者，供按钮及文字命令共用。"""
        lock = self._guild_locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            session = self.sessions.get(guild_id)
            if not session:
                return False, "Bot 不在语音频道中"
            if expected_session is not None and session is not expected_session:
                return False, "播放会话已变化，请重新操作"
            actor_id = str(actor_id or "").strip()
            if not actor_id:
                return False, "无法确认操作用户，已拒绝操作"
            queue_edit = action in {"move", "remove", "reorder"}
            if session.pending_skips > 0 and (queue_edit or action == "clear"):
                return False, "正在切换歌曲，请稍后再调整队列"
            if queue_edit:
                error = self._queue_position_error(session, position)
                if error:
                    return False, error
                if action == "reorder":
                    error = self._queue_position_error(session, destination)
                    if error:
                        return False, error
                song = session.playlist[position - 1]
            else:
                song = self.control_song(session)
            requester_id = str(song.requester_id or "").strip() if song else ""
            if is_admin is not True and (
                not requester_id or actor_id != requester_id
            ):
                return False, (
                    "只有该歌曲的点歌者或管理员可以调整这首待播歌曲"
                    if queue_edit else "只有当前歌曲的点歌者或管理员可以调整播放"
                )

            # 下列入口在首个 await 前完成状态变更；鉴权到提交之间不能让出执行权。
            if action == "next":
                return await self.skip(guild_id)
            if action == "loop":
                return await self.toggle_loop(guild_id)
            if action == "clear":
                return await self.clear_playlist(guild_id)
            if action == "stop":
                return await self._stop_playback(session)
            if action == "move":
                return await self.move_to_next(guild_id, position)
            if action == "remove":
                return await self.remove_queued_song(guild_id, position)
            if action == "reorder":
                return await self.reorder_queued_song(guild_id, position, destination)
            if action == "leave":
                await self._cleanup_session(guild_id, expected_session=session)
                return True, "已退出语音频道"
            return False, "未知的播放操作"

    def iter_voice_sessions(self) -> tuple[GuildSession, ...]:
        """包括仍在连接或启动中继的会话，供网关事件按 Token/频道路由。"""
        combined = dict(self._pending_sessions)
        combined.update(self.sessions)
        return tuple(combined.values())

    async def get_user_voice_channel(
        self, token: str, guild_id: str, user_id: str
    ) -> str | None:
        """通过 KOOK API 获取用户所在语音频道 ID"""
        headers = {"Authorization": f"Bot {token}"}
        params = {"guild_id": guild_id, "user_id": user_id}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self.JOINED_CHANNEL_URL,
                    headers=headers,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        logger.warning(f"[KookMusic] 获取用户频道失败: HTTP {resp.status}")
                        return None
                    data = await resp.json()
                    items = data.get("data", {}).get("items", [])
                    if items:
                        return items[0].get("id")
                    return None
        except Exception as e:
            logger.error(f"[KookMusic] 获取用户语音频道异常: {e}")
            return None

    async def join_and_play(
        self,
        token: str,
        guild_id: str,
        voice_channel_id: str,
        text_channel_id: str,
        song: Song,
        *,
        request: RequestActivity | None = None,
    ) -> tuple[bool, str]:
        """
        加入语音频道并将歌曲加入队列。
        歌曲的下载和播放由播放循环统一管理。

        Returns:
            (成功与否, 消息)
            消息以 "QUEUED:" 开头表示歌曲已入队等待播放，
            否则表示立即开始播放。
        """
        return await self.join_and_play_many(
            token,
            guild_id,
            voice_channel_id,
            text_channel_id,
            [song],
            request=request,
        )

    async def join_and_play_many(
        self,
        token: str,
        guild_id: str,
        voice_channel_id: str,
        text_channel_id: str,
        songs: list[Song],
        *,
        request: RequestActivity | None = None,
    ) -> tuple[bool, str]:
        """原子地加入一批歌曲；容量不足时整批拒绝，不产生部分入队。"""
        if not songs:
            return False, "没有可加入的歌曲"
        if len(songs) > self.max_queue_size:
            return False, f"所选歌曲超过播放队列上限（最大 {self.max_queue_size} 首）"

        lock = self._guild_locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            if self._closing:
                return False, "插件正在卸载，无法开始播放"
            if request is not None and (
                request.manager is not self or request.guild_id != guild_id or not request.current
            ):
                return False, "本次点歌已被停止或退出操作取消，请重新点歌"
            session = self.sessions.get(guild_id)

            if session:
                await self._settle_idle_playback(session)
                available = self.max_queue_size - len(session.playlist)
                if len(songs) > available:
                    return False, f"播放队列容量不足，目前最多还能加入 {available} 首"
                for queued_song in songs:
                    if queued_song.stream_url and (session.is_playing or session.playlist):
                        # 并发首播请求可能在解析流地址期间被另一首歌抢先建会话。
                        # 临时签名 URL 不能带入等待队列，轮到时再 fresh 解析。
                        queued_song.stream_url = ""
                        queued_song.extra_headers = {}
                start_pos = len(session.playlist) + 1
                session.playlist.extend(songs)
                session.idle_seconds = 0
                if not session.is_playing:
                    self._start_playback_loop(guild_id)
                    return True, f"开始播放，共加入 {len(songs)} 首"
                self._sync_prefetch(session)
                if len(songs) == 1:
                    return True, (
                        f"QUEUED:已添加到队列第 {start_pos} 位："
                        f"{songs[0].display_name}"
                    )
                return True, (
                    f"QUEUED:已添加 {len(songs)} 首到队列第 "
                    f"{start_pos}-{start_pos + len(songs) - 1} 位"
                )

            return await self._create_session_with_songs(
                token,
                guild_id,
                voice_channel_id,
                text_channel_id,
                songs,
            )

    async def _create_session_with_songs(
        self,
        token: str,
        guild_id: str,
        voice_channel_id: str,
        text_channel_id: str,
        songs: list[Song],
    ) -> tuple[bool, str]:
        """串行创建新会话，并在初始化完全成功后才对外发布。"""
        async with self._session_creation_lock:
            if self._closing:
                return False, "插件正在卸载，无法开始播放"
            if len(self.sessions) >= self.max_sessions:
                return False, f"播放槽位已满（最大 {self.max_sessions}）"

            voice_client = VoiceClient(token)
            voice_client.on_remote_removed = (
                lambda removed_client: self.handle_voice_removed(
                    voice_channel_id, expected_voice_client=removed_client
                )
            )
            ffmpeg_player = create_player(
                mode=self.streaming_mode,
                ffmpeg_path=self.ffmpeg_path,
                volume=self.volume,
            )
            session = GuildSession(
                guild_id=guild_id,
                voice_channel_id=voice_channel_id,
                text_channel_id=text_channel_id,
                voice_client=voice_client,
                ffmpeg_player=ffmpeg_player,
                playlist=list(songs),
            )
            now = time.monotonic()
            self._recent_departures = {
                key: value for key, value in self._recent_departures.items()
                if value[2] > now
            }
            departure = self._recent_departures.get(guild_id)
            session.prior_departure_pending = bool(
                departure and departure[0] == token and departure[1] == voice_channel_id
            )
            self._pending_sessions[guild_id] = session
            task = asyncio.create_task(self._initialize_voice_session(session))
            self._initialization_tasks[guild_id] = task
            try:
                return await task
            except asyncio.CancelledError:
                if getattr(voice_client, "remote_removed", False):
                    return False, "机器人已被移出语音频道，播放已结束"
                raise
            finally:
                try:
                    # 在首次执行前被取消的初始化任务不会进入它自身的 finally。
                    if task.cancelled():
                        await self._stop_session_resources(session)
                finally:
                    if self._pending_sessions.get(guild_id) is session:
                        self._pending_sessions.pop(guild_id)
                    if self._initialization_tasks.get(guild_id) is task:
                        self._initialization_tasks.pop(guild_id)
                    session.initialization_done.set()

    async def _initialize_voice_session(self, session: GuildSession) -> tuple[bool, str]:
        voice_client = session.voice_client
        ffmpeg_player = session.ffmpeg_player
        removed_message = "机器人已被移出语音频道，播放已结束"
        published = False
        try:
            connected = await voice_client.connect(session.voice_channel_id)
            if not connected:
                if getattr(voice_client, "remote_removed", False):
                    return False, removed_message
                return False, "无法连接语音频道"
            if isinstance(ffmpeg_player, RelayFFmpegPlayer):
                relay_ok = await ffmpeg_player.start_relay(
                    voice_client.rtp_url, voice_client.ssrc
                )
                if not relay_ok:
                    logger.error("[KookMusic] UDP 中继启动失败，回退到 direct 模式")
                    ffmpeg_player = DirectFFmpegPlayer(
                        ffmpeg_path=self.ffmpeg_path, volume=self.volume
                    )
                    session.ffmpeg_player = ffmpeg_player

            if getattr(voice_client, "remote_removed", False):
                return False, removed_message
            if self._closing:
                return False, "插件正在卸载，无法开始播放"

            self.sessions[session.guild_id] = session
            self._start_playback_loop(session.guild_id)
            self._ensure_idle_check()
            published = True
            return True, f"已加入语音频道，开始播放，共加入 {len(session.playlist)} 首"
        except asyncio.CancelledError:
            if getattr(voice_client, "remote_removed", False):
                return False, removed_message
            raise
        finally:
            if not published:
                await self._stop_session_resources(session)

    def _start_playback_loop(self, guild_id: str):
        """启动或重启播放循环"""
        retry_task = self._retry_tasks.pop(guild_id, None)
        if (
            retry_task
            and retry_task is not asyncio.current_task()
            and not retry_task.done()
        ):
            retry_task.cancel()
        # 取消旧任务（如果存在且未完成）
        old_task = self._playback_tasks.get(guild_id)
        if old_task and not old_task.done():
            old_task.cancel()
        task = asyncio.create_task(self._playback_loop(guild_id))
        self._playback_tasks[guild_id] = task

    async def _settle_idle_playback(self, session: GuildSession):
        if session.is_playing or session.playlist:
            return
        task = self._playback_tasks.get(session.guild_id)
        if task is not None and task is not asyncio.current_task() and not task.done():
            # Natural EOF may still be stopping the relay or deleting its card.
            # Finish that generation before reusing the same player/session.
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def add_song(self, guild_id: str, song: Song) -> tuple[bool, str]:
        """添加歌曲到队列"""
        lock = self._guild_locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            session = self.sessions.get(guild_id)
            if not session:
                return False, "Bot 不在语音频道中"
            if self._closing:
                return False, "插件正在卸载，无法开始播放"
            await self._settle_idle_playback(session)
            if len(session.playlist) >= self.max_queue_size:
                return False, f"播放队列已满（最大 {self.max_queue_size} 首）"
            if song.stream_url and (session.is_playing or session.playlist):
                song.stream_url = ""
                song.extra_headers = {}
            session.playlist.append(song)
            pos = len(session.playlist)
            session.idle_seconds = 0
            if not session.is_playing:
                self._start_playback_loop(guild_id)
            else:
                self._sync_prefetch(session)
            return True, f"已添加到队列第 {pos} 位"

    async def skip(self, guild_id: str) -> tuple[bool, str]:
        """跳过当前歌曲"""
        session = self.sessions.get(guild_id)
        if not session:
            return False, "Bot 不在语音频道中"
        if not session.playlist:
            return False, "播放队列为空"
        if session.pending_skips >= len(session.playlist):
            return False, "没有更多可跳过的歌曲"
        session.pending_skips += 1
        if session.pending_skips > 1:
            self._cancel_prefetch(session)
        if not session.is_playing:
            self._start_playback_loop(guild_id)
        preparation_task = session.preparation_task
        if preparation_task and not preparation_task.done():
            preparation_task.cancel()
        await session.ffmpeg_player.stop()
        return True, "已跳过一首歌曲"

    async def move_to_next(self, guild_id: str, position: int) -> tuple[bool, str]:
        """将播放队列中的指定序号移动到下一首播放。

        position 使用播放队列展示的 1-based 序号，1 代表当前播放中的歌曲。
        """
        session = self.sessions.get(guild_id)
        if not session:
            return False, "Bot 不在语音频道中"
        if session.pending_skips > 0:
            return False, "正在切换歌曲，请稍后再调整队列"
        if not isinstance(position, int) or isinstance(position, bool):
            return False, "请提供有效的歌曲序号"
        if not session.playlist:
            return False, "播放队列为空"
        if position < 1 or position > len(session.playlist):
            return False, f"请输入 1-{len(session.playlist)} 之间的序号"
        if position == 1:
            return False, "第 1 首正在播放，不能插队到下一首"
        if position == 2:
            return True, f"第 2 首已经是下一首：{session.playlist[1].display_name}"

        song = session.playlist.pop(position - 1)
        session.playlist.insert(1, song)
        self._sync_prefetch(session)
        return True, f"已将第 {position} 首插队到下一首：{song.display_name}"

    @staticmethod
    def _queue_position_error(session: GuildSession, position: int | None) -> str:
        if not isinstance(position, int) or isinstance(position, bool):
            return "请提供有效的歌曲序号"
        if not session.playlist:
            return "播放队列为空"
        if position < 1 or position > len(session.playlist):
            return f"请输入 1-{len(session.playlist)} 之间的序号"
        if position == 1:
            return "第 1 首正在播放或准备中，不能删除或移动"
        return ""

    async def remove_queued_song(self, guild_id: str, position: int) -> tuple[bool, str]:
        """Remove one pending item without interrupting the current audio."""
        session = self.sessions.get(guild_id)
        if not session:
            return False, "Bot 不在语音频道中"
        if session.pending_skips > 0:
            return False, "正在切换歌曲，请稍后再调整队列"
        error = self._queue_position_error(session, position)
        if error:
            return False, error
        song = session.playlist.pop(position - 1)
        self._sync_prefetch(session)
        # Repeated queue entries can share one Song or cache file. Its surviving
        # owner, especially the current player, must keep that resource alive.
        if not any(queued is song for queued in session.playlist):
            if song.file_path and any(
                queued.file_path == song.file_path for queued in session.playlist
            ):
                song.file_path = ""
            self._cleanup_song_file(song)
        return True, f"已删除第 {position} 首待播歌曲：{song.display_name}"

    async def reorder_queued_song(
        self, guild_id: str, position: int, destination: int,
    ) -> tuple[bool, str]:
        """Move a pending item to its final 1-based queue position."""
        session = self.sessions.get(guild_id)
        if not session:
            return False, "Bot 不在语音频道中"
        if session.pending_skips > 0:
            return False, "正在切换歌曲，请稍后再调整队列"
        for value in (position, destination):
            error = self._queue_position_error(session, value)
            if error:
                return False, error
        if position == destination:
            return True, f"第 {position} 首已在目标位置：{session.playlist[position - 1].display_name}"
        song = session.playlist.pop(position - 1)
        session.playlist.insert(destination - 1, song)
        self._sync_prefetch(session)
        return True, f"已将第 {position} 首移动到第 {destination} 位：{song.display_name}"

    async def clear_playlist(self, guild_id: str) -> tuple[bool, str]:
        """清空播放队列（保留当前播放）"""
        session = self.sessions.get(guild_id)
        if not session:
            return False, "Bot 不在语音频道中"
        self._cancel_prefetch(session)
        if len(session.playlist) > 1:
            current = session.playlist[0]
            removed = session.playlist[1:]
            session.playlist.clear()
            session.playlist.append(current)
            for song in removed:
                self._cleanup_song_file(song)
        session.pending_skips = min(session.pending_skips, len(session.playlist))
        return True, "已清空队列"

    async def _stop_playback(self, session: GuildSession) -> tuple[bool, str]:
        """持有 guild 锁停止当前代播放，但不退出语音频道。"""
        guild_id = session.guild_id
        self._invalidate_requests(guild_id)
        session.playback_generation += 1
        abandoned_songs = list(session.playlist)
        session.playlist.clear()
        session.pending_skips = 0
        session.is_playing = False
        session.idle_seconds = 0
        self._cancel_prefetch(session)
        preparation_task = session.preparation_task
        tasks = list(dict.fromkeys(
            task for task in (
                self._playback_tasks.pop(guild_id, None),
                self._retry_tasks.pop(guild_id, None),
                preparation_task,
                *session.prefetch_tasks,
                *session.notification_tasks,
            )
            if task is not None and task is not asyncio.current_task()
        ))
        for task in tasks:
            if not task.done():
                task.cancel()
        try:
            await session.ffmpeg_player.stop()
        finally:
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            # A cancellation-resistant startup may have begun output after the
            # first stop. The guild lock prevents new playback until this pass.
            await session.ffmpeg_player.stop()
            if isinstance(session.ffmpeg_player, RelayFFmpegPlayer):
                # Keep the established RTP clock alive with silence. Recreating
                # a Producer after every stop can leave KOOK's mixer on the old one.
                session.needs_relay_refresh = not session.ffmpeg_player.is_relay_running
                logger.info("[KookMusic] Playback stopped; silent relay retained=%s", not session.needs_relay_refresh)
            else:
                session.needs_direct_refresh = True
            if preparation_task is not None:
                self._cleanup_unused_preparation(session, preparation_task)
            for song in abandoned_songs:
                self._cleanup_song_file(song)
            session.preparation_task = None
            session.prefetch_tasks.clear()
            session.notification_tasks.clear()
            session.playback_retry_count = 0
            session.playback_started_at = 0.0
            session.playback_offset_seconds = 0.0
            session.is_playing = False
            session.idle_seconds = 0
        if self.on_playback_finished:
            try:
                result = self.on_playback_finished(guild_id)
                if asyncio.isfuture(result) or asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                logger.debug(f"[KookMusic] 停止播放回调异常: {exc}")
        self._ensure_idle_check()
        return True, "已停止播放并清空队列，保留语音连接"

    async def toggle_loop(self, guild_id: str) -> tuple[bool, str]:
        """切换循环模式"""
        session = self.sessions.get(guild_id)
        if not session:
            return False, "Bot 不在语音频道中"
        session.loop_mode = (session.loop_mode + 1) % 4
        self._sync_prefetch(session)
        return True, f"循环模式: {session.loop_mode_name}"

    async def leave(self, guild_id: str) -> tuple[bool, str]:
        """退出语音频道"""
        lock = self._guild_locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            session = self.sessions.get(guild_id)
            if not session:
                return False, "Bot 不在语音频道中"
            await self._cleanup_session(guild_id)
            return True, "已退出语音频道"

    async def leave_all(self):
        """退出所有语音频道（插件卸载时调用）"""
        self._closing = True
        self._activity_requests.clear()
        initialization_tasks = list(self._initialization_tasks.values())
        for task in initialization_tasks:
            if not task.done():
                task.cancel()
        if initialization_tasks:
            await asyncio.gather(*initialization_tasks, return_exceptions=True)
        guild_ids = list(self.sessions.keys())
        for gid in guild_ids:
            await self.leave(gid)
        self._recent_departures.clear()
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
            try:
                await self._idle_task
            except (asyncio.CancelledError, Exception):
                pass
        self._idle_task = None

    async def handle_voice_removed(
        self,
        channel_id: str,
        expected_voice_client: VoiceClient | None = None,
        occurred_at: float | None = None,
    ) -> bool:
        """处理已确认属于机器人的远端退出；旧连接通知不得关闭新会话。"""
        for session in self.iter_voice_sessions():
            guild_id = session.guild_id
            if session.voice_channel_id != str(channel_id):
                continue
            client = session.voice_client
            if expected_voice_client is not None and client is not expected_voice_client:
                continue
            if (
                session.prior_departure_pending
                and occurred_at is not None
                and getattr(client, "_awaiting_join_event", False)
                and not getattr(client, "remote_removed", False)
            ):
                # A prior intentional disconnect can reach the gateway after
                # the next voice handshake. Wait for a server timestamp boundary
                # instead of treating the first exit as an unconditional grace.
                deadline = time.monotonic() + self.PRIOR_EXIT_JOIN_GRACE
                while (
                    getattr(client, "_awaiting_join_event", False)
                    and not getattr(client, "remote_removed", False)
                    and time.monotonic() < deadline
                ):
                    if not any(current is session for current in self.iter_voice_sessions()):
                        return False
                    await asyncio.sleep(0.02)
                if not any(current is session for current in self.iter_voice_sessions()):
                    return False
            if not getattr(client, "remote_removed", False):
                consume_exit = getattr(client, "consume_expected_exit", None)
                if consume_exit and consume_exit(channel_id, occurred_at):
                    logger.debug("[KookMusic] 忽略正常语音重连引起的退出事件")
                    continue
            if (
                self._pending_sessions.get(guild_id) is session
                and self.sessions.get(guild_id) is not session
            ):
                if session.removal_requested:
                    return False
                session.removal_requested = True
                self._invalidate_requests(guild_id)
                mark_removed = getattr(client, "mark_remote_removed", None)
                if mark_removed:
                    mark_removed()
                initialization_task = self._initialization_tasks.get(guild_id)
                if (
                    initialization_task
                    and initialization_task is not asyncio.current_task()
                    and not initialization_task.done()
                ):
                    initialization_task.cancel()
                    await asyncio.gather(initialization_task, return_exceptions=True)
                if initialization_task is not asyncio.current_task():
                    await session.initialization_done.wait()
                    lock = self._guild_locks.setdefault(guild_id, asyncio.Lock())
                    async with lock:
                        # 旧初始化的完成通知不能删除随后新点歌创建的卡片。
                        if guild_id not in self.sessions and guild_id not in self._pending_sessions:
                            await self._notify_voice_removed(guild_id)
                else:
                    await self._notify_voice_removed(guild_id)
                return True
            lock = self._guild_locks.setdefault(guild_id, asyncio.Lock())
            async with lock:
                if self.sessions.get(guild_id) is not session:
                    continue
                # 先使重连失效，再取消正在播放、准备或等待重试的任务。
                mark_removed = getattr(client, "mark_remote_removed", None)
                if mark_removed:
                    mark_removed()
                await self._cleanup_session(guild_id, expected_session=session)
                await self._notify_voice_removed(guild_id)
                return True
        return False

    async def _notify_voice_removed(self, guild_id: str):
        if self.on_playback_finished:
            try:
                result = self.on_playback_finished(guild_id)
                if asyncio.isfuture(result) or asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                logger.debug(f"[KookMusic] 远端退出完成回调异常: {exc}")
        logger.info(f"[KookMusic] 机器人已被移出语音频道，播放已结束: {guild_id}")

    def get_playlist_text(self, guild_id: str) -> str:
        """获取播放队列文本"""
        session = self.sessions.get(guild_id)
        if not session or not session.playlist:
            return "播放队列为空"
        lines = ["**播放队列：**"]
        for i, song in enumerate(session.playlist):
            prefix = "▶ " if i == 0 else f"{i + 1}. "
            lines.append(f"{prefix}{song.display_name}")
        lines.append(f"\n共 {len(session.playlist)} 首 | 循环: {session.loop_mode_name}")
        return "\n".join(lines)

    def _ensure_idle_check(self):
        """确保空闲检查循环已启动"""
        if self.auto_leave_timeout <= 0:
            return
        if self._idle_task is None or self._idle_task.done():
            self._idle_task = asyncio.create_task(self._idle_check_loop())

    async def _idle_check_loop(self):
        """空闲检查循环：定期检查所有会话，空闲超时后自动退出语音频道"""
        try:
            empty_count = 0  # 无会话的连续计数
            while True:
                await asyncio.sleep(10)  # 每 10 秒检查一次

                if not self.sessions:
                    empty_count += 1
                    if empty_count >= 6:  # 连续 60 秒无会话，停止循环
                        logger.debug("[KookMusic] 无活跃会话，空闲检查停止")
                        break
                    continue
                empty_count = 0

                for guild_id, session in list(self.sessions.items()):
                    await self._check_session_idle(guild_id, session)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[KookMusic] 空闲检查异常: {e}")

    async def _check_session_idle(self, guild_id: str, expected_session: GuildSession):
        lock = self._guild_locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            session = self.sessions.get(guild_id)
            if session is not expected_session or self._closing:
                return
            if session.is_playing or session.playlist or self._activity_requests.get(guild_id):
                session.idle_seconds = 0
                return
            session.idle_seconds += 10
            if self.auto_leave_timeout <= 0 or session.idle_seconds < self.auto_leave_timeout:
                return
            logger.info(
                f"[KookMusic] 空闲超时 ({self.auto_leave_timeout}s)，自动退出: {guild_id}"
            )
            # The same lock covers teardown and subsequent joins. Activity that
            # begins after teardown committed remains valid for the next session.
            await self._cleanup_session(
                guild_id, expected_session=session, invalidate_requests=False
            )
            if self.on_playback_finished:
                try:
                    result = self.on_playback_finished(guild_id)
                    if asyncio.isfuture(result) or asyncio.iscoroutine(result):
                        await result
                except Exception:
                    pass

    async def _ensure_voice_alive(self, session: GuildSession) -> bool:
        """确保语音连接仍然存活，否则尝试重连"""
        if getattr(session.voice_client, "remote_removed", False):
            return False
        if session.voice_client.is_alive:
            return True

        logger.warning("[KookMusic] 语音连接已断开，尝试重连...")
        ok = await session.voice_client.reconnect(session.voice_channel_id)
        if not ok:
            logger.error("[KookMusic] 语音重连失败")
            return False

        logger.info("[KookMusic] 语音重连成功")

        # relay 模式：重连后 RTP 地址已改变，必须重建中继进程
        player = session.ffmpeg_player
        if isinstance(player, RelayFFmpegPlayer):
            logger.info("[KookMusic] 重建 UDP 中继进程（RTP 地址已更新）...")
            await player.stop_relay()
            relay_ok = await player.start_relay(
                session.voice_client.rtp_url,
                session.voice_client.ssrc,
            )
            if not relay_ok:
                logger.error("[KookMusic] 中继进程重建失败")
                return False
            session.needs_relay_refresh = False

        return True

    async def _prepare_relay_for_playback(self, session: GuildSession) -> bool:
        """Reuse a healthy silent relay; rebuild a broken transport by rejoining."""
        if getattr(session.voice_client, "remote_removed", False):
            return False
        player = session.ffmpeg_player
        if not isinstance(player, RelayFFmpegPlayer):
            return True
        if player.is_relay_running and not session.needs_relay_refresh:
            return True

        logger.info("[KookMusic] UDP 中继不可复用，重新连接语音并重建传输...")
        await player.stop_relay()
        if not await session.voice_client.reconnect(session.voice_channel_id):
            logger.error("[KookMusic] 语音重连失败")
            return False
        if getattr(session.voice_client, "remote_removed", False):
            return False

        relay_ok = await player.start_relay(
            session.voice_client.rtp_url,
            session.voice_client.ssrc,
        )
        if not relay_ok:
            logger.error("[KookMusic] 中继进程重建失败")
            return False

        session.needs_relay_refresh = False
        return True

    def _cancel_prefetch(self, session: GuildSession):
        state = session.prefetch
        if state is None:
            return
        session.prefetch = None
        state.discarded = True
        task = state.task
        if task is not None and not task.done():
            task.cancel()
            return
        result = None
        if task is not None and not task.cancelled():
            try:
                result = task.result()
            except Exception:
                pass
        self._cleanup_song_file(state.candidate)
        if result is not None and result is not state.candidate:
            self._cleanup_song_file(result)

    def _sync_prefetch(self, session: GuildSession):
        target = None
        if (
            self.prefetch_next
            and not self._closing
            and self.sessions.get(session.guild_id) is session
            and session.is_playing
            and session.loop_mode in (0, 2)
            and session.pending_skips <= 1
            and len(session.playlist) > 1
            and self.on_download_song
        ):
            next_song = session.playlist[1]
            if next_song.platform != "bilibili" and not next_song.playback_source:
                target = next_song
        if session.prefetch and session.prefetch.original is target:
            return
        self._cancel_prefetch(session)
        if (
            target is None
            or not session.ffmpeg_player.is_playing
            or session.prefetch_tasks
        ):
            return

        # Keep background mutation and abandoned downloads out of the live queue.
        state = SongPrefetch(target, copy.deepcopy(target))
        session.prefetch = state
        task = asyncio.create_task(self._prefetch_song(session, state))
        state.task = task
        session.prefetch_tasks.add(task)

        def completed(done_task):
            session.prefetch_tasks.discard(done_task)
            if not done_task.cancelled():
                done_task.exception()
            # A replaced target waits for cancellation to finish before another
            # download can start; at most one background prepare per session.
            if session.prefetch is None and session.ffmpeg_player.is_playing:
                self._sync_prefetch(session)

        task.add_done_callback(completed)

    async def _prefetch_song(self, session: GuildSession, state: SongPrefetch):
        result = None
        retained = False
        try:
            logger.info(f"[KookMusic] 后台预准备下一首: {state.original.name}")
            result = await self.on_download_song(state.candidate)
            if result is not None:
                result.requester_id = state.original.requester_id
                result.requester_name = state.original.requester_name
                result.provider_data = {
                    **copy.deepcopy(state.original.provider_data),
                    **result.provider_data,
                }
            retained = (
                not state.discarded
                and self.sessions.get(session.guild_id) is session
                and result is not None
                and bool(result.playback_source)
            )
            if state.discarded or self.sessions.get(session.guild_id) is not session:
                return None
            status = "就绪" if retained else "未取得可播放音频"
            logger.info(f"[KookMusic] 下一首预准备{status}: {state.original.name}")
            return result
        finally:
            if not retained:
                self._cleanup_song_file(state.candidate)
                if result is not None and result is not state.candidate:
                    self._cleanup_song_file(result)
            elif (
                result is not state.candidate
                and state.candidate.file_path != result.file_path
            ):
                self._cleanup_song_file(state.candidate)

    def _take_prefetch(self, session: GuildSession, song: Song):
        state = session.prefetch
        if state is not None and state.original is song:
            session.prefetch = None
            return state.task
        self._cancel_prefetch(session)
        return None

    def _cleanup_unused_preparation(self, session: GuildSession, task: asyncio.Task):
        if not task.done() or task.cancelled():
            return
        try:
            result = task.result()
        except Exception:
            return
        if result is not None and not any(song is result for song in session.playlist):
            self._cleanup_song_file(result)

    def _schedule_song_notification(self, session: GuildSession, song: Song):
        if not self.on_song_started or song.provider_data.get(self._STREAM_NOTIFIED_KEY):
            return
        song.provider_data[self._STREAM_NOTIFIED_KEY] = True
        queue_size = len(session.playlist)
        loop_name = session.loop_mode_name

        async def notify():
            try:
                if (
                    self.sessions.get(session.guild_id) is not session
                    or session.current_song is not song
                    or session.pending_skips > 0
                ):
                    return
                result = self.on_song_started(
                    session.guild_id, song, queue_size, loop_name
                )
                if asyncio.isfuture(result) or asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                logger.debug(f"[KookMusic] 歌曲开始回调异常: {exc}")

        task = asyncio.create_task(notify())
        session.notification_tasks.add(task)
        task.add_done_callback(session.notification_tasks.discard)

    async def _playback_loop(self, guild_id: str):
        """播放循环：依次播放队列中的歌曲

        根据推流模式执行不同的播放策略：
        - relay 模式：常驻中继进程持续推流，歌曲间无缝切换
        - direct 模式：每首歌独立推流，歌曲间隙重建 RTP Transport
        """
        session = self.sessions.get(guild_id)
        if not session:
            return

        player = session.ffmpeg_player
        is_relay = isinstance(player, RelayFFmpegPlayer)
        retry_needed = False
        generation = session.playback_generation

        def is_current():
            return (
                self.sessions.get(guild_id) is session
                and session.playback_generation == generation
            )

        session.is_playing = True
        try:
            while is_current():
                if not session.playlist or getattr(
                    session.voice_client, "remote_removed", False
                ):
                    break

                # 连续点击切歌时，每次点击都对应丢弃一首。尚未开始下载/播放的
                # 歌曲直接跳过，避免多个快速点击最终只生效一次。
                if session.pending_skips > 0:
                    self._consume_pending_skip(session)
                    continue

                song = session.playlist[0]

                # ---- 延迟准备：在播放前才下载歌曲或解析临时流地址 ----
                if not song.playback_source:
                    if self.on_download_song:
                        preparing_session = session
                        preparation_task = self._take_prefetch(session, song)
                        preparation_label = "接管下一首预准备" if preparation_task else "开始准备队列歌曲"
                        if preparation_task is None:
                            preparation_task = asyncio.create_task(
                                self.on_download_song(song)
                            )
                        preparing_session.preparation_task = preparation_task
                        try:
                            logger.info(f"[KookMusic] {preparation_label}: {song.name}")
                            updated_song = await preparation_task
                            # 重新检查 session 是否仍然有效
                            if not is_current():
                                break
                            session = self.sessions[guild_id]
                            if session.pending_skips > 0:
                                if updated_song and updated_song.playback_source:
                                    song = updated_song
                                    if session.playlist and session.playlist[0] is not song:
                                        session.playlist[0] = song
                                self._consume_pending_skip(session)
                                continue
                            # 更新队列中的歌曲对象
                            if updated_song and updated_song.playback_source:
                                if session.playlist and session.playlist[0] is song:
                                    session.playlist[0] = updated_song
                                    song = updated_song
                            else:
                                logger.error(f"[KookMusic] 音频准备失败: {song.name}")
                                self._drop_failed_current(session, song)
                                continue
                        except asyncio.CancelledError:
                            current_session = self.sessions.get(guild_id)
                            if (
                                current_session is preparing_session
                                and current_session.playback_generation == generation
                                and current_session.pending_skips > 0
                            ):
                                self._consume_pending_skip(current_session)
                                continue
                            raise
                        except Exception as e:
                            logger.error(f"[KookMusic] 音频准备异常: {song.name}: {e}")
                            if is_current():
                                self._drop_failed_current(session, song)
                            continue
                        finally:
                            if preparing_session.preparation_task is preparation_task:
                                preparing_session.preparation_task = None
                            self._cleanup_unused_preparation(
                                preparing_session, preparation_task
                            )
                    else:
                        logger.warning(
                            f"[KookMusic] 歌曲无播放源且无准备回调: {song.name}"
                        )
                        self._drop_failed_current(session, song)
                        continue

                if not is_relay and session.needs_direct_refresh:
                    if not is_current() or getattr(session.voice_client, "remote_removed", False):
                        break
                    logger.info("[KookMusic] Direct 模式: 重连语音频道获取新 RTP...")
                    await session.voice_client.disconnect()
                    await asyncio.sleep(self.DIRECT_RECONNECT_DELAY)
                    if not is_current() or getattr(session.voice_client, "remote_removed", False):
                        break
                    reconnected = await session.voice_client.connect(
                        session.voice_channel_id
                    )
                    if not is_current():
                        break
                    if not reconnected:
                        logger.error("[KookMusic] 语音重连失败，停止播放")
                        retry_needed = True
                        break
                    session.needs_direct_refresh = False
                    logger.info(
                        f"[KookMusic] RTP 已更新: "
                        f"{session.voice_client.rtp_url}, "
                        f"SSRC: {session.voice_client.ssrc}"
                    )

                # 确保语音连接存活（每首歌播放前都检查）
                if not await self._ensure_voice_alive(session):
                    logger.error("[KookMusic] 语音连接不可用，停止播放")
                    retry_needed = True
                    break
                if is_relay and not await self._prepare_relay_for_playback(session):
                    logger.error("[KookMusic] 中继恢复失败，停止播放")
                    retry_needed = True
                    break
                if not is_current():
                    break
                if session.pending_skips > 0:
                    self._consume_pending_skip(session)
                    continue

                logger.info(f"[KookMusic] 开始播放: {song.display_name}")
                if self.PLAYBACK_START_DELAY > 0:
                    await asyncio.sleep(self.PLAYBACK_START_DELAY)
                if not is_current():
                    break
                if session.pending_skips > 0:
                    self._consume_pending_skip(session)
                    continue

                playback_source = song.playback_source
                stream_headers = song.extra_headers if song.stream_url else None
                try:
                    resume_offset = max(
                        0.0,
                        float(song.provider_data.get(self._STREAM_RESUME_OFFSET_KEY, 0)),
                    )
                except (TypeError, ValueError, OverflowError):
                    resume_offset = 0.0
                try:
                    resume_attempts = max(
                        0,
                        int(song.provider_data.get(self._STREAM_RESUME_ATTEMPTS_KEY, 0)),
                    )
                except (TypeError, ValueError, OverflowError):
                    resume_attempts = 0
                play_options = {"extra_headers": stream_headers}
                if resume_offset > 0:
                    play_options["start_seconds"] = resume_offset
                if is_relay:
                    # ---- relay 模式：直接播放，不需要 RTP 参数 ----
                    ok = await player.play(
                        playback_source,
                        **play_options,
                    )
                else:
                    # ---- direct 模式：每首歌传入 RTP 参数 ----
                    ok = await player.play(
                        playback_source,
                        session.voice_client.rtp_url,
                        session.voice_client.ssrc,
                        **play_options,
                    )
                    # Direct 的 Transport 在一次播放尝试后即视为已使用；即使
                    # 启动阶段被切歌终止，下一首也必须刷新 RTP，避免无声。
                    session.needs_direct_refresh = True

                # skip 可能发生在最后一次检查之后、FFmpeg 启动 await 期间。
                # 启动返回后再检查一次，避免“停止了空播放器，随后反而开始播”。
                if not is_current():
                    await player.stop()
                    break
                if session.pending_skips > 0:
                    if ok:
                        await player.stop()
                    self._consume_pending_skip(session)
                    continue

                if not ok:
                    if (
                        song.stream_url
                        and resume_attempts < self.STREAM_RESUME_MAX_ATTEMPTS
                    ):
                        song.stream_url = ""
                        song.extra_headers = {}
                        song.provider_data[self._STREAM_RESUME_ATTEMPTS_KEY] = (
                            resume_attempts + 1
                        )
                        logger.warning(
                            f"[KookMusic] B站流启动失败，fresh 解析后重试: {song.name}"
                        )
                        continue
                    logger.error(f"[KookMusic] 播放失败: {song.name}")
                    self._clear_stream_resume_state(song)
                    if self.sessions.get(guild_id) is session:
                        self._drop_failed_current(session, song)
                    continue
                session.playback_retry_count = 0
                segment_started_at = time.monotonic()
                session.playback_started_at = segment_started_at
                session.playback_offset_seconds = resume_offset

                self._sync_prefetch(session)
                self._schedule_song_notification(session, song)
                # Give background work a turn, not an HTTP round trip. A slow
                # card upload must never delay observing playback completion.
                await asyncio.sleep(0)

                # 计算播放超时：歌曲时长(ms->s) + 缓冲时间
                if song.duration > 0:
                    remaining_seconds = max(
                        0.0,
                        (song.duration / 1000) - resume_offset,
                    )
                    timeout = remaining_seconds + self.PLAYBACK_TIMEOUT_BUFFER
                    logger.debug(
                        f"[KookMusic] 播放超时设定: {timeout:.0f}s "
                        f"(时长={song.duration}ms)"
                    )
                else:
                    # 未知时长不能用固定分钟数截断；远程输入由 FFmpeg 的
                    # rw_timeout 负责处理卡死，用户切歌/退出仍会立即 kill。
                    timeout = None
                    logger.debug("[KookMusic] 歌曲时长未知，等待 FFmpeg 自然结束")

                # 等待播放完成（带超时保护）
                playback_completed = await player.wait_until_done(timeout=timeout)
                played_seconds = getattr(player, "played_seconds", None)
                if (
                    isinstance(played_seconds, (int, float))
                    and not isinstance(played_seconds, bool)
                    and math.isfinite(played_seconds)
                    and played_seconds >= 0
                ):
                    segment_elapsed = float(played_seconds)
                else:
                    segment_elapsed = max(0.0, time.monotonic() - segment_started_at)

                # 确保歌曲进程已清理（stop 是幂等的，重复调用无副作用）
                await player.stop()
                was_stream = bool(song.stream_url)
                if was_stream:
                    # CDN 签名地址只对本次播放有效。循环重播或列表再次轮到时
                    # 必须重新解析，不能复用可能已经过期的地址。
                    song.stream_url = ""
                    song.extra_headers = {}

                # 重新检查 session 有效性
                if not is_current():
                    break
                session = self.sessions[guild_id]
                if (
                    not playback_completed
                    and session.pending_skips <= 0
                    and was_stream
                    and resume_attempts < self.STREAM_RESUME_MAX_ATTEMPTS
                ):
                    next_offset = max(
                        0.0,
                        resume_offset
                        + segment_elapsed
                        - self.STREAM_RESUME_REWIND_SECONDS,
                    )
                    duration_seconds = song.duration / 1000 if song.duration > 0 else 0
                    if duration_seconds <= 0 or next_offset < duration_seconds - 1:
                        song.provider_data[self._STREAM_RESUME_OFFSET_KEY] = next_offset
                        song.provider_data[self._STREAM_RESUME_ATTEMPTS_KEY] = (
                            resume_attempts + 1
                        )
                        logger.warning(
                            f"[KookMusic] B站流异常中断，将 fresh 解析并从 "
                            f"{next_offset:.0f}s 附近续播: {song.name}"
                        )
                        continue
                if not playback_completed and session.pending_skips <= 0:
                    logger.warning(
                        f"[KookMusic] 歌曲未正常播放完毕，继续后续队列: {song.name}"
                    )
                self._clear_stream_resume_state(song)
                # 处理循环模式
                if not is_current():
                    break
                session = self.sessions[guild_id]
                if session.pending_skips > 0:
                    self._consume_pending_skip(session)
                elif session.loop_mode == 0:
                    finished_song = session.playlist.pop(0)
                    # 清理已播放完毕的歌曲缓存文件
                    self._cleanup_song_file(finished_song)
                elif session.loop_mode == 1:
                    pass  # 不弹出，继续播放同一首
                elif session.loop_mode == 2:
                    session.playlist.append(session.playlist.pop(0))
                elif session.loop_mode == 3:
                    finished_song = session.playlist.pop(0)
                    self._cleanup_song_file(finished_song)
                    if session.playlist:
                        random.shuffle(session.playlist)

                session.idle_seconds = 0

        except asyncio.CancelledError:
            logger.info(f"[KookMusic] 播放循环被取消: {guild_id}")
        except Exception as e:
            logger.error(f"[KookMusic] 播放循环异常: {e}")
            retry_needed = True
        finally:
            session.is_playing = False
            self._cancel_prefetch(session)
            # 确保当前歌曲已停止（不停中继进程，由 _cleanup_session 处理）
            try:
                if player.is_playing:
                    await player.stop()
            except Exception:
                pass
            # 仅更新属于本次循环的 session（避免污染新创建的 session）
            current_session = self.sessions.get(guild_id)
            if current_session is session:
                current_session.is_playing = False
            if (
                retry_needed
                and is_current()
                and current_session.playlist
                and not getattr(current_session.voice_client, "remote_removed", False)
            ):
                if current_session.playback_retry_count < self.MAX_PLAYBACK_RETRIES:
                    current_session.playback_retry_count += 1
                    self._schedule_playback_retry(guild_id, current_session)
                else:
                    logger.error(
                        f"[KookMusic] 连续 {self.MAX_PLAYBACK_RETRIES} 次恢复播放失败，"
                        "清空队列并结束会话"
                    )
                    for queued_song in current_session.playlist:
                        self._cleanup_song_file(queued_song)
                    current_session.playlist.clear()
                    current_session.pending_skips = 0
            # 播放队列为空时通知外部（但不自动退出，空闲检查循环会处理退出）
            current_session = self.sessions.get(guild_id)
            queue_empty = is_current() and not current_session.playlist
            if queue_empty and isinstance(current_session.ffmpeg_player, RelayFFmpegPlayer):
                # A silent relay is intentionally retained until idle disconnect.
                current_session.needs_relay_refresh = not current_session.ffmpeg_player.is_relay_running
                logger.info("[KookMusic] Queue idle; silent relay retained=%s", not current_session.needs_relay_refresh)
            if queue_empty and self.on_playback_finished:
                try:
                    result = self.on_playback_finished(guild_id)
                    if asyncio.isfuture(result) or asyncio.iscoroutine(result):
                        await result
                except Exception as e:
                    logger.debug(f"[KookMusic] 播放完成回调异常: {e}")

    def _schedule_playback_retry(
        self, guild_id: str, expected_session: GuildSession
    ):
        old_task = self._retry_tasks.pop(guild_id, None)
        if old_task and not old_task.done():
            old_task.cancel()

        async def retry_later():
            try:
                delay = self.PLAYBACK_RETRY_DELAY * max(
                    1, expected_session.playback_retry_count
                )
                await asyncio.sleep(delay)
                current = self.sessions.get(guild_id)
                if (
                    current is expected_session
                    and current.playlist
                    and not current.is_playing
                    and not getattr(current.voice_client, "remote_removed", False)
                ):
                    self._start_playback_loop(guild_id)
            except asyncio.CancelledError:
                pass

        self._retry_tasks[guild_id] = asyncio.create_task(retry_later())

    def _consume_pending_skip(self, session: GuildSession) -> bool:
        """消费一次排队的切歌请求，并删除对应队首歌曲。"""
        if session.pending_skips <= 0 or not session.playlist:
            return False
        skipped_song = session.playlist.pop(0)
        if session.prefetch and session.prefetch.original is skipped_song:
            self._cancel_prefetch(session)
        session.pending_skips -= 1
        session.idle_seconds = 0
        self._cleanup_song_file(skipped_song)
        return True

    def _drop_failed_current(self, session: GuildSession, song: Song) -> bool:
        """移除失败的队首；若切歌请求正等待，则同时只消费这一次请求。"""
        if not session.playlist or session.playlist[0] is not song:
            return False
        if session.pending_skips > 0:
            return self._consume_pending_skip(session)
        failed_song = session.playlist.pop(0)
        self._cleanup_song_file(failed_song)
        session.idle_seconds = 0
        return True

    @staticmethod
    def _clear_stream_resume_state(song: Song):
        song.provider_data.pop(VoiceManager._STREAM_RESUME_OFFSET_KEY, None)
        song.provider_data.pop(VoiceManager._STREAM_RESUME_ATTEMPTS_KEY, None)
        song.provider_data.pop(VoiceManager._STREAM_NOTIFIED_KEY, None)

    @staticmethod
    def _cleanup_song_file(song: Song):
        """清理单首歌曲的本地缓存与当前临时流。"""
        if song.file_path:
            try:
                import os
                if os.path.exists(song.file_path):
                    os.unlink(song.file_path)
            except Exception:
                pass
            song.file_path = ""
        song.stream_url = ""
        song.extra_headers = {}
        VoiceManager._clear_stream_resume_state(song)

    async def _cleanup_session(
        self, guild_id: str, expected_session: GuildSession | None = None,
        *, invalidate_requests: bool = True,
    ):
        """清理会话"""
        session = self.sessions.get(guild_id)
        if not session or (
            expected_session is not None and session is not expected_session
        ):
            return
        # 在首个 await 前移除会话，阻止旧播放循环重试或污染新会话。
        self.sessions.pop(guild_id)
        client = session.voice_client
        if getattr(client, "token", "") and not getattr(client, "remote_removed", False):
            self._recent_departures[guild_id] = (
                client.token, session.voice_channel_id, time.monotonic() + 15.0
            )
        if invalidate_requests:
            self._invalidate_requests(guild_id)
        session.playback_generation += 1
        self._cancel_prefetch(session)
        retry_task = self._retry_tasks.pop(guild_id, None)
        task = self._playback_tasks.pop(guild_id, None)
        preparation_task = session.preparation_task
        pending_tasks = list(dict.fromkeys(
            pending for pending in (
                retry_task, task, preparation_task,
                *session.prefetch_tasks, *session.notification_tasks,
            )
            if pending is not None and pending is not asyncio.current_task()
        ))
        for pending in pending_tasks:
            if not pending.done():
                pending.cancel()
        # Stop audible output before waiting for downloader cancellation or a
        # shielded card request to return its ID for stale-card cleanup.
        try:
            await self._stop_session_resources(session)
        finally:
            for pending in pending_tasks:
                try:
                    await pending
                except (asyncio.CancelledError, Exception):
                    pass
            if preparation_task is not None:
                self._cleanup_unused_preparation(session, preparation_task)
            session.prefetch_tasks.clear()
            session.notification_tasks.clear()
        session.preparation_task = None
        session.is_playing = False
        session.pending_skips = 0
        session.playback_retry_count = 0
        session.playback_started_at = 0.0
        session.playback_offset_seconds = 0.0

        logger.info(f"[KookMusic] 会话已清理: {guild_id}")

    async def _stop_session_resources(self, session: GuildSession):
        player = session.ffmpeg_player
        try:
            if isinstance(player, RelayFFmpegPlayer):
                await player.stop_relay()
            else:
                await player.stop()
        finally:
            try:
                await session.voice_client.disconnect()
            finally:
                for song in session.playlist:
                    self._cleanup_song_file(song)
                session.playlist.clear()
