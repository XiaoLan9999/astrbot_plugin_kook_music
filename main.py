"""
AstrBot KOOK 语音点歌插件。

整合 astrbot_plugin_music 的免登录搜索/下载能力与 KO-ON-Bot 的 KOOK 语音推流能力，
实现在 KOOK 语音频道中自动加入并播放音乐。
"""

import asyncio
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.message.components import Json, Plain

from . import card_builder
from .kook_api import (
    send_card_message,
    send_text_message,
    delete_message,
    close_shared_session,
)
from .kook_voice.voice_manager import VoiceManager
from .kook_voice.ffmpeg_installer import check_and_install_ffmpeg
from .music.downloader import MusicDownloader
from .music.model import Song
from .music.searcher import MusicSearcher
from .music.bilibili import BilibiliCollection, BilibiliExtractor
from .music.playlist_import import PlaylistImporter
from .playlist_range import looks_like_playlist_range, validate_playlist_range

# 用户选歌等待映射：session_key -> (songs, future, [msg_ids_to_delete])
_pending_selections: dict[str, tuple[list[Song], asyncio.Future, list[str]]] = {}
_pending_selections_lock = asyncio.Lock()

# 大型歌单区间选择：session_key -> (完整歌单, 等待区间结果的 future)
_pending_playlist_ranges: dict[str, tuple[list[Song], asyncio.Future]] = {}
_pending_playlist_ranges_lock = asyncio.Lock()
_playlist_import_requests: dict[str, object] = {}
_bilibili_play_requests: dict[str, object] = {}
_playlist_request_commit_lock = asyncio.Lock()

# 已知的平台标识
_KNOWN_PLATFORMS = {"netease", "qq", "kugou", "kuwo", "migu", "baidu", "bilibili"}

# 按钮点击回调队列（由 monkey-patch 写入，由 on_message 消费）
_button_click_queue: asyncio.Queue | None = None


class KookMusicPlugin(Star):
    """KOOK 语音点歌插件

    在 KOOK 平台实现语音频道点歌：
    - 点歌 <歌名> [平台] — 搜索并播放
    - 播放 <关键词/BV号/链接> — 播放 B站视频、多P或收藏夹
    - 下一首 — 跳过当前
    - 歌单 — 查看队列
    - 循环模式 — 切换循环
    - 清空歌单 — 清空队列
    - 退出语音 — 退出频道
    """

    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config or {}

        # 配置项
        self.default_platform = self.config.get("default_platform", "netease")
        self.max_queue_size = self._parse_int_range(
            self.config.get("max_queue_size", 50), 50, 10, 2000
        )
        self.playlist_range_timeout = self._parse_int_range(
            self.config.get("playlist_range_timeout", 60), 60, 10, 300
        )
        self.search_limit = self.config.get("search_limit", 5)
        self.auto_leave_timeout = self.config.get("auto_leave_timeout", 300)
        self.max_sessions = self.config.get("max_sessions", 5)
        self.volume = self._parse_volume(self.config.get("volume", "0.15"))
        self.streaming_mode = self.config.get("streaming_mode", "relay")

        # 核心组件
        self.data_dir = Path("data/plugin_data/astrbot_plugin_kook_music")
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.searcher = MusicSearcher(
            qq_vip_resolver_url=self.config.get(
                "qq_vip_resolver_url",
                MusicSearcher.DEFAULT_QQ_VIP_RESOLVER_URL,
            ),
            qq_vip_resolver_quality=self.config.get(
                "qq_vip_resolver_quality",
                "320",
            ),
        )
        self.downloader = MusicDownloader(self.data_dir / "songs")
        self.bilibili = BilibiliExtractor()
        self.playlist_importer = PlaylistImporter()
        self.voice_manager = VoiceManager(
            max_sessions=self.max_sessions,
            auto_leave_timeout=self.auto_leave_timeout,
            volume=self.volume,
            streaming_mode=self.streaming_mode,
            max_queue_size=self.max_queue_size,
        )

        self.custom_ffmpeg_path = self.config.get("custom_ffmpeg_path", "").strip()

        # KOOK Bot Token
        self._kook_token: str = self.config.get("kook_token", "").strip()

        # 已发送的卡片消息 ID 映射：guild_id -> msg_id（用于播放结束后清理）
        self._card_msg_ids: dict[str, list[str]] = {}

        # 后台任务引用（用于 terminate 时取消）
        self._button_handler_task: asyncio.Task | None = None

        # 保存 KOOK 客户端回调，插件热重载/卸载时恢复，避免重复包裹回调。
        self._patched_kook_client = None
        self._original_kook_callback = None
        self._installed_kook_callback = None

    async def initialize(self):
        """插件初始化"""
        global _button_click_queue, _pending_selections, _pending_playlist_ranges
        _button_click_queue = asyncio.Queue()
        # 重置全局状态（支持热重载）
        _pending_selections.clear()
        _pending_playlist_ranges.clear()
        _playlist_import_requests.clear()
        _bilibili_play_requests.clear()

        self._kook_token = self._find_kook_token()
        if self._kook_token:
            logger.info("[KookMusic] 已从 AstrBot 配置中获取 KOOK Token")
        else:
            logger.warning(
                "[KookMusic] 未找到 KOOK Token，请确保已配置 KOOK 平台适配器"
            )

        # 自动检查和安装 FFmpeg（如果未手动指定）
        if self.custom_ffmpeg_path:
            logger.info(f"[KookMusic] 使用自定义 FFmpeg 路径: {self.custom_ffmpeg_path}")
            self.voice_manager.ffmpeg_path = self.custom_ffmpeg_path
            self.bilibili.set_ffmpeg_path(self.custom_ffmpeg_path)
        else:
            ffmpeg_path = await check_and_install_ffmpeg(self.data_dir)
            self.voice_manager.ffmpeg_path = ffmpeg_path
            self.bilibili.set_ffmpeg_path(ffmpeg_path)

        # 加载 B站 Cookie
        bili_cookie = self.config.get("bili_cookie", "").strip()
        if bili_cookie:
            # 优先使用配置中填写的 SESSDATA
            self.bilibili.set_cookie(bili_cookie, self.data_dir)
        else:
            # 回退：检查手动放置的 cookies 文件
            bili_cookies_file = self.data_dir / "bili_cookies.txt"
            if bili_cookies_file.exists():
                self.bilibili.set_cookies_file(str(bili_cookies_file))

        # 注入 KOOK 按钮点击事件处理
        self._patch_kook_adapter()

        # 注册播放管理器回调（只注册一次，避免多服务器场景下被覆盖）
        self.voice_manager.on_playback_finished = lambda gid: asyncio.create_task(
            self._on_playback_finished(gid)
        )
        self.voice_manager.on_song_started = lambda gid, song, qsize, lmode: asyncio.create_task(
            self._on_song_started(gid, song, qsize, lmode)
        )
        self.voice_manager.on_download_song = self._download_song

        # 启动按钮点击处理循环（保存引用以便 terminate 时取消）
        self._button_handler_task = asyncio.create_task(self._button_click_handler_loop())

    async def terminate(self):
        """插件卸载清理"""
        logger.info("[KookMusic] 正在清理资源...")
        self._restore_kook_adapter()
        for _, future, _ in _pending_selections.values():
            if not future.done():
                future.cancel()
        _pending_selections.clear()
        for _, future in _pending_playlist_ranges.values():
            if not future.done():
                future.cancel()
        _pending_playlist_ranges.clear()
        _playlist_import_requests.clear()
        _bilibili_play_requests.clear()
        # 取消按钮处理循环
        if self._button_handler_task and not self._button_handler_task.done():
            self._button_handler_task.cancel()
            try:
                await self._button_handler_task
            except (asyncio.CancelledError, Exception):
                pass
            self._button_handler_task = None
        await self.voice_manager.leave_all()
        await self.searcher.close()
        await self.downloader.close()
        await self.bilibili.close()
        await self.playlist_importer.close()
        await close_shared_session()
        logger.info("[KookMusic] 清理完成")

    def _patch_kook_adapter(self):
        """Monkey-patch KOOK 适配器以拦截按钮点击系统事件。

        关键：必须 patch platform.client.event_callback 而不是 platform._on_received，
        因为 KookClient 在构造时通过 KookClient(config, self._on_received) 保存了
        _on_received 的绑定方法引用到 self.event_callback，之后一直调用
        self.event_callback(data)。替换 platform._on_received 不会影响已经被复制走的引用。
        """
        try:
            for platform in self.context.platform_manager.platform_insts:
                if platform.meta().name != "kook":
                    continue

                # 获取实际被调用的回调引用
                if not hasattr(platform, "client") or not hasattr(platform.client, "event_callback"):
                    logger.warning("[KookMusic] KOOK 适配器结构不符合预期，跳过 patch")
                    continue

                original_callback = platform.client.event_callback

                async def patched_callback(event, _orig=original_callback):
                    # 检查是否为按钮点击事件
                    try:
                        raw_event_type = getattr(event, "type", None)
                        event_type = getattr(raw_event_type, "value", raw_event_type)
                        extra = getattr(event, "extra", None)
                        raw_extra_type = getattr(extra, "type", None)
                        extra_type = getattr(raw_extra_type, "value", raw_extra_type)
                        if event_type == 255 and extra_type == "message_btn_click":
                            body = getattr(extra, "body", None)
                            if isinstance(body, dict):
                                value = body.get("value", "")
                                if value.startswith("kook_music_"):
                                    user_id = body.get("user_id", "")
                                    msg_id = body.get("msg_id", "")
                                    # KOOK 的按钮事件 body 不包含 guild_id。target_id
                                    # 才是卡片所在的文字频道，guild_id 稍后由消息/会话映射解析。
                                    channel_id = body.get("target_id", "") or getattr(
                                        event, "target_id", ""
                                    )
                                    logger.info(
                                        f"[KookMusic] 收到按钮点击: value={value}, "
                                        f"user={user_id}, channel={channel_id}, msg={msg_id}"
                                    )
                                    if _button_click_queue:
                                        await _button_click_queue.put({
                                            "value": value,
                                            "user_id": user_id,
                                            "msg_id": msg_id,
                                            "channel_id": channel_id,
                                        })
                    except Exception as e:
                        logger.debug(f"[KookMusic] 按钮点击检查异常: {e}")

                    # 其他事件照常处理
                    await _orig(event)

                # patch 实际被调用的回调
                platform.client.event_callback = patched_callback
                self._patched_kook_client = platform.client
                self._original_kook_callback = original_callback
                self._installed_kook_callback = patched_callback
                logger.info("[KookMusic] 已注入 KOOK 按钮点击事件处理 (patch event_callback)")
                return

            logger.warning("[KookMusic] 未找到 KOOK 平台适配器，按钮功能不可用")
        except Exception as e:
            logger.warning(f"[KookMusic] 注入按钮事件处理失败: {e}")

    def _restore_kook_adapter(self):
        """仅在回调仍由本插件持有时恢复，避免覆盖其他插件后续的 patch。"""
        client = self._patched_kook_client
        if (
            client is not None
            and self._original_kook_callback is not None
            and client.event_callback is self._installed_kook_callback
        ):
            client.event_callback = self._original_kook_callback
        self._patched_kook_client = None
        self._original_kook_callback = None
        self._installed_kook_callback = None

    def _resolve_button_guild_id(self, msg_id: str, channel_id: str) -> str:
        """从播放卡片消息或活跃文字频道反查服务器 ID。"""
        if msg_id:
            for guild_id, msg_ids in self._card_msg_ids.items():
                if msg_id in msg_ids:
                    return guild_id

        if channel_id:
            matches = [
                guild_id
                for guild_id, session in self.voice_manager.sessions.items()
                if session.text_channel_id == channel_id
            ]
            if len(matches) == 1:
                return matches[0]
        return ""

    async def _button_click_handler_loop(self):
        """后台循环处理按钮点击事件"""
        while True:
            try:
                if not _button_click_queue:
                    await asyncio.sleep(1)
                    continue

                click_data = await _button_click_queue.get()
                value = click_data.get("value", "")
                channel_id = click_data.get("channel_id", "")
                msg_id = click_data.get("msg_id", "")
                guild_id = self._resolve_button_guild_id(msg_id, channel_id)

                if not guild_id:
                    logger.warning(
                        f"[KookMusic] 无法定位按钮所属服务器: msg={msg_id}, "
                        f"channel={channel_id}, value={value}"
                    )
                    continue

                if value == "kook_music_next":
                    ok, msg = await self.voice_manager.skip(guild_id)
                    reply = f"{'✅' if ok else '❌'} {msg}"
                elif value == "kook_music_loop":
                    ok, msg = await self.voice_manager.toggle_loop(guild_id)
                    reply = f"🔁 {msg}"
                elif value == "kook_music_clear":
                    ok, msg = await self.voice_manager.clear_playlist(guild_id)
                    reply = f"{'✅' if ok else '❌'} {msg}"
                else:
                    continue

                # 使用 KOOK API 直接回复到频道
                if self._kook_token and channel_id:
                    await send_text_message(
                        self._kook_token, channel_id, reply
                    )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[KookMusic] 按钮处理异常: {e}")
                await asyncio.sleep(1)

    def _find_kook_token(self) -> str:
        """获取 KOOK Token（优先插件配置，其次从平台适配器实例获取）"""
        # 1. 优先使用插件配置中手动填写的 token
        if self._kook_token:
            return self._kook_token

        # 2. 从 AstrBot 平台管理器中获取 KOOK 适配器的 token
        try:
            for platform in self.context.platform_manager.platform_insts:
                if platform.meta().name != "kook":
                    continue
                token = platform.config.get("kook_bot_token", "")
                if token:
                    logger.info("[KookMusic] 已从 KOOK 平台适配器实例中获取 Token")
                    return token
        except Exception as e:
            logger.debug(f"[KookMusic] 从平台适配器获取 KOOK Token 异常: {e}")

        return ""

    @staticmethod
    def _parse_volume(raw) -> float:
        """安全解析音量值，异常时回退为 0.15"""
        try:
            v = float(raw)
            if v < 0.01:
                v = 0.01
            elif v > 1.0:
                v = 1.0
            return v
        except (ValueError, TypeError):
            logger.warning(f"[KookMusic] 无效音量值 '{raw}'，使用默认 0.15")
            return 0.15

    @staticmethod
    def _parse_int_range(raw, default: int, minimum: int, maximum: int) -> int:
        try:
            return max(minimum, min(maximum, int(raw)))
        except (TypeError, ValueError):
            return default

    def _is_kook(self, event: AstrMessageEvent) -> bool:
        """检查是否为 KOOK 平台"""
        return event.get_platform_name() == "kook"

    def _get_guild_id(self, event: AstrMessageEvent) -> str:
        """从事件中提取 guild_id"""
        try:
            raw = event.message_obj.raw_message
            if isinstance(raw, dict):
                return raw.get("extra", {}).get("guild_id", "") or ""
        except Exception:
            pass
        return ""

    def _get_channel_id(self, event: AstrMessageEvent) -> str:
        """获取当前频道 ID"""
        return event.message_obj.group_id or event.message_obj.session_id or ""

    # ========== 命令注册 ==========

    @filter.command("点歌")
    async def on_play_music(self, event: AstrMessageEvent):
        """点歌 <歌名> [平台] — 在KOOK语音频道播放音乐"""
        if not self._is_kook(event):
            return

        if not self._kook_token:
            self._kook_token = self._find_kook_token()
            if not self._kook_token:
                yield event.plain_result(
                    "❌ 未找到 KOOK Token，请先在设置中配置 KOOK 平台适配器或手动填写 Token"
                )
                return

        # 从完整消息中提取命令后的内容（保留空格）
        raw_text = event.message_str.strip()
        # 移除命令前缀 "点歌"
        for prefix in ("点歌", "/点歌"):
            if raw_text.startswith(prefix):
                raw_text = raw_text[len(prefix):].strip()
                break

        # 解析参数：歌名可含空格，平台名在末尾（如果匹配已知平台）
        if not raw_text:
            yield event.plain_result(
                "用法：点歌 <歌名/单曲链接> [平台]\n"
                "例如：点歌 青花瓷\n"
                "例如：点歌 https://music.163.com/#/song?id=xxxx\n"
                "例如：点歌 https://music.163.com/#/program?id=xxxx\n"
                "例如：点歌 https://y.qq.com/n/ryqq/songDetail/xxxx\n"
                "例如：点歌 https://www.kugou.com/song/#hash=xxxx\n"
                "指定平台：点歌 青花瓷 qq\n"
                "支持平台：netease / qq / kugou / kuwo / migu / baidu"
            )
            return

        keyword = raw_text
        platform = self.default_platform

        # 检查最后一个"词"是否为平台名
        parts = raw_text.rsplit(maxsplit=1)
        if len(parts) == 2:
            resolved = MusicSearcher.resolve_platform(parts[1])
            if resolved in _KNOWN_PLATFORMS:
                platform = resolved
                keyword = parts[0]

        guild_id = self._get_guild_id(event)
        if not guild_id:
            yield event.plain_result("❌ 无法获取服务器信息")
            return

        channel_id = self._get_channel_id(event)

        # ---- 网易云单曲 / 电台节目链接：直接解析播放，不进入搜索流程 ----
        direct_type, direct_id = PlaylistImporter.parse_direct_song_input(raw_text)
        if direct_id:
            msg_ids: list[str] = []
            type_label = "电台节目" if direct_type == "program" else "网易云单曲"
            if self._kook_token and channel_id:
                msg_id = await send_text_message(
                    self._kook_token,
                    channel_id,
                    f"🔗 正在解析{type_label}链接：{direct_id}..."
                )
                if msg_id:
                    msg_ids.append(msg_id)
            else:
                yield event.plain_result(f"🔗 正在解析{type_label}链接：{direct_id}...")

            requester_id = event.get_sender_id()
            requester_name = event.get_sender_name()
            if direct_type == "program":
                song = await self.playlist_importer.fetch_netease_program(
                    direct_id, requester_id, requester_name
                )
            else:
                song = await self.playlist_importer.fetch_netease_song(
                    direct_id, requester_id, requester_name
                )

            await self._delete_messages(msg_ids)
            if not song:
                yield event.plain_result(f"❌ 无法解析{type_label}：{direct_id}")
                return

            await self._play_song(event, song, guild_id)
            return

        # ---- QQ音乐 / 酷狗单曲链接：按平台稳定 ID 解析 ----
        direct_platform = MusicSearcher.detect_direct_platform(raw_text)
        if direct_platform:
            msg_ids: list[str] = []
            platform_label = "QQ音乐" if direct_platform == "qq" else "酷狗音乐"
            if self._kook_token and channel_id:
                msg_id = await send_text_message(
                    self._kook_token,
                    channel_id,
                    f"🔗 正在解析{platform_label}单曲链接...",
                )
                if msg_id:
                    msg_ids.append(msg_id)
            else:
                yield event.plain_result(f"🔗 正在解析{platform_label}单曲链接...")

            song = await self.searcher.fetch_direct_song(raw_text)
            await self._delete_messages(msg_ids)
            if not song:
                yield event.plain_result(
                    f"❌ 无法从该链接取得{platform_label}歌曲 ID 或歌曲信息"
                )
                return

            await self._play_song(event, song, guild_id)
            return

        # ---- 其他平台：原有搜索流程 ----
        search_msg_ids: list[str] = []
        if self._kook_token and channel_id:
            msg_id = await send_text_message(
                self._kook_token, channel_id,
                f"🔍 正在搜索：{keyword} ({platform})..."
            )
            if msg_id:
                search_msg_ids.append(msg_id)
        else:
            yield event.plain_result(f"🔍 正在搜索：{keyword} ({platform})...")

        # 搜索
        songs = await self.searcher.search(keyword, platform, self.search_limit)
        if not songs:
            await self._delete_messages(search_msg_ids)
            yield event.plain_result(f"❌ 未找到歌曲：{keyword}")
            return

        if self.search_limit == 1 or len(songs) == 1:
            await self._delete_messages(search_msg_ids)
            await self._play_song(event, songs[0], guild_id)
            return

        # 发送搜索结果卡片（通过 KOOK API 以获取 msg_id）
        card_data = card_builder.build_search_result_card(songs, keyword)
        if self._kook_token and channel_id:
            card_msg_id = await send_card_message(
                self._kook_token, channel_id, card_data
            )
            if card_msg_id:
                search_msg_ids.append(card_msg_id)
            else:
                yield event.chain_result([Json(data=card_data)])
        else:
            yield event.chain_result([Json(data=card_data)])

        # 等待用户选择
        session_key = self._search_interaction_key(event, guild_id)
        future = asyncio.get_running_loop().create_future()
        old_entry = None
        async with _pending_selections_lock:
            old_entry = _pending_selections.pop(session_key, None)
            _pending_selections[session_key] = (songs, future, search_msg_ids)
        if old_entry:
            _, old_future, old_msg_ids = old_entry
            if not old_future.done():
                old_future.set_result(None)
            await self._delete_messages(old_msg_ids)

        try:
            selected: Song | None = await asyncio.wait_for(future, timeout=30)
            if selected is None:
                yield event.plain_result("ℹ️ 本次选歌已由新的搜索请求替换")
                return
            await self._play_song(event, selected, guild_id)
        except asyncio.TimeoutError:
            own_msg_ids: list[str] = []
            async with _pending_selections_lock:
                entry = _pending_selections.get(session_key)
                if entry and entry[1] is future:
                    _pending_selections.pop(session_key, None)
                    own_msg_ids = entry[2]
            await self._delete_messages(own_msg_ids)
            yield event.plain_result("⏰ 选歌超时，已取消")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """处理搜索选歌和大型歌单区间回复。"""
        if not self._is_kook(event):
            return

        text = event.message_str.strip()
        guild_id = self._get_guild_id(event)
        selection_key = self._search_interaction_key(event, guild_id)
        range_key = self._playlist_interaction_key(event, guild_id)

        # 纯数字优先交给搜索选歌，避免与大型歌单区间等待互相抢消息。
        if text.isdigit() and selection_key in _pending_selections:
            selection_error = None
            async with _pending_selections_lock:
                entry = _pending_selections.get(selection_key)
                if not entry:
                    return
                songs, future, msg_ids = entry
                idx = int(text) - 1

                if 0 <= idx < len(songs):
                    _pending_selections.pop(selection_key, None)
                    if not future.done():
                        future.set_result(songs[idx])
                    asyncio.create_task(self._delete_messages(msg_ids))
                    user_msg_id = event.message_obj.message_id
                    if user_msg_id and self._kook_token:
                        asyncio.create_task(delete_message(self._kook_token, user_msg_id))
                    event.stop_event()
                else:
                    selection_error = f"❌ 请输入 1-{len(songs)} 的数字"
            if selection_error:
                yield event.plain_result(selection_error)
            return

        # 普通聊天和其他命令不应在等待区间时被误报为格式错误。
        if range_key not in _pending_playlist_ranges:
            return
        if not looks_like_playlist_range(text):
            return
        async for result in self._handle_playlist_range_reply(
            event, range_key, guild_id, text
        ):
            yield result

    @filter.command("下一首")
    async def on_skip(self, event: AstrMessageEvent):
        """跳过当前歌曲"""
        if not self._is_kook(event):
            return
        guild_id = self._get_guild_id(event)
        ok, msg = await self.voice_manager.skip(guild_id)
        yield event.plain_result(f"{'✅' if ok else '❌'} {msg}")

    @filter.command("歌单")
    async def on_playlist(self, event: AstrMessageEvent):
        """查看播放队列"""
        if not self._is_kook(event):
            return
        guild_id = self._get_guild_id(event)
        session = self.voice_manager.get_session(guild_id)
        if not session:
            yield event.plain_result("📋 当前没有播放队列")
            return
        card_data = card_builder.build_queue_card(
            session.playlist, session.loop_mode_name
        )
        yield event.chain_result([Json(data=card_data)])

    @filter.command("队列插队")
    async def on_queue_jump(self, event: AstrMessageEvent):
        """队列插队 <序号> — 将指定歌曲插到下一首播放"""
        if not self._is_kook(event):
            return

        guild_id = self._get_guild_id(event)
        if not guild_id:
            yield event.plain_result("❌ 无法获取服务器信息")
            return

        raw_text = event.message_str.strip()
        for prefix in ("队列插队", "/队列插队"):
            if raw_text.startswith(prefix):
                raw_text = raw_text[len(prefix):].strip()
                break

        if not raw_text:
            yield event.plain_result("用法：队列插队 <序号>\n例如：队列插队 10")
            return

        parts = raw_text.split()
        if len(parts) != 1 or not parts[0].isdigit():
            yield event.plain_result("❌ 序号必须是播放队列中的数字，例如：队列插队 10")
            return

        ok, msg = await self.voice_manager.move_to_next(guild_id, int(parts[0]))
        yield event.plain_result(f"{'✅' if ok else '❌'} {msg}")

    @filter.command("循环模式")
    async def on_loop(self, event: AstrMessageEvent):
        """切换循环模式: 关闭 → 单曲循环 → 列表循环 → 随机播放"""
        if not self._is_kook(event):
            return
        guild_id = self._get_guild_id(event)
        ok, msg = await self.voice_manager.toggle_loop(guild_id)
        yield event.plain_result(f"🔁 {msg}")

    @filter.command("清空歌单")
    async def on_clear(self, event: AstrMessageEvent):
        """清空播放队列（保留当前播放中的歌曲）"""
        if not self._is_kook(event):
            return
        guild_id = self._get_guild_id(event)
        ok, msg = await self.voice_manager.clear_playlist(guild_id)
        yield event.plain_result(f"{'✅' if ok else '❌'} {msg}")

    @filter.command("退出语音")
    async def on_leave(self, event: AstrMessageEvent):
        """退出语音频道并清空队列"""
        if not self._is_kook(event):
            return
        guild_id = self._get_guild_id(event)
        await self._delete_card(guild_id)
        ok, msg = await self.voice_manager.leave(guild_id)
        yield event.plain_result(f"{'✅' if ok else '❌'} {msg}")

    @filter.command("播放")
    async def on_play_video(self, event: AstrMessageEvent):
        """播放 <关键词/BV号/视频或收藏夹链接> — 播放视频音频"""
        if not self._is_kook(event):
            return

        if not self._kook_token:
            self._kook_token = self._find_kook_token()
            if not self._kook_token:
                yield event.plain_result("❌ 未找到 KOOK Token")
                return

        raw_text = event.message_str.strip()
        for prefix in ("#播放", "/播放", "播放"):
            if raw_text.startswith(prefix):
                raw_text = raw_text[len(prefix):].strip()
                break
        for legacy_prefix in ("b站", "B站"):
            if raw_text.startswith(legacy_prefix):
                raw_text = raw_text[len(legacy_prefix):].strip()
                break

        if not raw_text:
            yield event.plain_result(
                "用法：播放 <关键词/BV号/视频链接/收藏夹链接>\n"
                "例如：播放 海阔天空\n"
                "例如：播放 BV1dujdzrEA4\n"
                "例如：播放 https://www.bilibili.com/video/BV1dujdzrEA4?p=2\n"
                "例如：播放 https://space.bilibili.com/84912/favlist?fid=213003412"
            )
            return

        guild_id = self._get_guild_id(event)
        if not guild_id:
            yield event.plain_result("❌ 无法获取服务器信息")
            return

        interaction_key = self._playlist_interaction_key(event, guild_id)
        request_marker = object()
        async with _playlist_request_commit_lock:
            async with _pending_playlist_ranges_lock:
                old_bili_marker = _bilibili_play_requests.get(interaction_key)
                _bilibili_play_requests[interaction_key] = request_marker
                if (
                    old_bili_marker
                    and _playlist_import_requests.get(interaction_key)
                    is old_bili_marker
                ):
                    old_entry = _pending_playlist_ranges.pop(interaction_key, None)
                    if old_entry and not old_entry[1].done():
                        old_entry[1].cancel()
                    _playlist_import_requests.pop(interaction_key, None)

        try:
            await event.send(event.plain_result(
                f"🔍 正在解析视频内容：{raw_text}..."
            ))
            resolved_text = await self.bilibili.resolve_input(raw_text)
            if not resolved_text:
                await event.send(event.plain_result(
                    "❌ B站短链展开失败或跳转目标不受支持"
                ))
                return
            raw_text = resolved_text
            collection = await self.bilibili.extract_collection(raw_text)
            if collection:
                await self._play_bilibili_collection(
                    event,
                    collection,
                    guild_id,
                    interaction_key,
                    request_marker,
                )
                return
            if self.bilibili.parse_favorite_input(raw_text):
                await event.send(event.plain_result(
                    "❌ 无法读取 B站收藏夹；请确认收藏夹公开，"
                    "私密收藏夹需要在插件配置中填写有效的 bili_cookie"
                ))
                return
            await self._play_bilibili(event, raw_text, guild_id)
        finally:
            async with _playlist_request_commit_lock:
                if _bilibili_play_requests.get(interaction_key) is request_marker:
                    _bilibili_play_requests.pop(interaction_key, None)

    @filter.command("导入歌单")
    async def on_import_playlist(self, event: AstrMessageEvent):
        """导入歌单 <歌单ID或链接> — 导入多平台歌单到播放队列"""
        if not self._is_kook(event):
            return

        if not self._kook_token:
            self._kook_token = self._find_kook_token()
            if not self._kook_token:
                yield event.plain_result("❌ 未找到 KOOK Token")
                return

        raw_text = event.message_str.strip()
        for prefix in ("导入歌单", "/导入歌单", "#导入歌单"):
            if raw_text.startswith(prefix):
                raw_text = raw_text[len(prefix):].strip()
                break

        if not raw_text:
            yield event.plain_result(
                "用法：导入歌单 <歌单ID或链接>\n"
                "支持：网易云、QQ音乐、酷狗音乐歌单及网易云电台\n"
                "裸数字默认作为网易云歌单 ID；QQ/酷狗数字 ID 请写平台名。\n"
                "例如：导入歌单 977171340\n"
                "例如：导入歌单 qq 7706179315\n"
                "例如：导入歌单 酷狗 6319673\n"
                "也可以直接粘贴各平台的官方歌单链接。"
            )
            return

        guild_id = self._get_guild_id(event)
        if not guild_id:
            yield event.plain_result("❌ 无法获取服务器信息")
            return

        channel_id = self._get_channel_id(event)
        requester_id = event.get_sender_id()
        requester_name = event.get_sender_name()
        interaction_key = self._playlist_interaction_key(event, guild_id)
        request_marker = object()
        async with _playlist_request_commit_lock:
            async with _pending_playlist_ranges_lock:
                old_entry = _pending_playlist_ranges.pop(interaction_key, None)
                if old_entry and not old_entry[1].done():
                    old_entry[1].cancel()
                _playlist_import_requests[interaction_key] = request_marker

        # 官方链接自动识别平台；裸数字保持兼容，仍默认网易云。
        import_type, target_id = await self.playlist_importer.resolve_playlist_input(
            raw_text
        )
        if not target_id:
            self._finish_playlist_import_request(interaction_key, request_marker)
            yield event.plain_result(
                "❌ 无法识别歌单/电台 ID，请检查链接；"
                "QQ/酷狗数字 ID 需要同时写平台名"
            )
            return

        # 发送进度提示
        type_labels = {
            "netease": "网易云歌单",
            "djradio": "网易云电台",
            "qq": "QQ音乐歌单",
            "kugou": "酷狗音乐歌单",
        }
        type_label = type_labels.get(import_type, "歌单")
        await event.send(event.plain_result(f"📥 正在导入{type_label} {target_id}，请稍候..."))

        # 根据类型选择导入方法
        if import_type == "djradio":
            songs = await self.playlist_importer.import_netease_djradio(
                target_id, requester_id, requester_name
            )
        elif import_type == "netease":
            songs = await self.playlist_importer.import_netease_playlist(
                target_id,
                requester_id,
                requester_name,
                fill_durations=False,
            )
        elif import_type == "qq":
            songs = await self.playlist_importer.import_qq_playlist(
                target_id, requester_id, requester_name
            )
        elif import_type == "kugou":
            songs = await self.playlist_importer.import_kugou_playlist(
                target_id, requester_id, requester_name
            )
        else:
            songs = []

        if _playlist_import_requests.get(interaction_key) is not request_marker:
            yield event.plain_result("ℹ️ 本次导入已由更新的歌单请求替换")
            return

        if not songs:
            self._finish_playlist_import_request(interaction_key, request_marker)
            yield event.plain_result(f"❌ {type_label} {target_id} 导入失败或内容为空")
            return

        available_slots = self._available_queue_slots(guild_id)
        if available_slots <= 0:
            self._finish_playlist_import_request(interaction_key, request_marker)
            yield event.plain_result(
                f"❌ 当前播放队列已满（上限 {self.max_queue_size} 首），无法导入歌单"
            )
            return

        if len(songs) > available_slots:
            future = asyncio.get_running_loop().create_future()
            async with _pending_playlist_ranges_lock:
                request_replaced = (
                    _playlist_import_requests.get(interaction_key)
                    is not request_marker
                )
                if not request_replaced:
                    _pending_playlist_ranges[interaction_key] = (songs, future)
            if request_replaced:
                yield event.plain_result("ℹ️ 本次导入已由更新的歌单请求替换")
                return

            await event.send(event.plain_result(
                f"📚 {type_label}共有 {len(songs)} 首，当前队列最多还能加入 "
                f"{available_slots} 首。\n"
                f"请在 {self.playlist_range_timeout} 秒内输入要播放的歌曲区间，"
                f"例如 `1-{available_slots}`。\n"
                f"区间必须在 1-{len(songs)} 内，且歌曲数量不能超过 "
                f"{available_slots} 首。"
            ))

            try:
                songs = await asyncio.wait_for(
                    future, timeout=self.playlist_range_timeout
                )
            except asyncio.TimeoutError:
                self._finish_playlist_import_request(interaction_key, request_marker)
                yield event.plain_result("⏰ 歌单区间选择超时，本次导入已取消")
                return
            except asyncio.CancelledError:
                yield event.plain_result("ℹ️ 已由新的歌单导入请求替换本次区间选择")
                return
            finally:
                async with _pending_playlist_ranges_lock:
                    current = _pending_playlist_ranges.get(interaction_key)
                    if current and current[1] is future:
                        _pending_playlist_ranges.pop(interaction_key, None)

        # 区间确认后再次按实时队列检查，避免等待期间其他用户把队列填满。
        available_slots = self._available_queue_slots(guild_id)
        if len(songs) > available_slots:
            self._finish_playlist_import_request(interaction_key, request_marker)
            yield event.plain_result(
                f"❌ 等待期间队列发生变化，目前只能再加入 {available_slots} 首。"
                "请重新执行导入歌单并选择更短的区间。"
            )
            return

        if import_type == "netease":
            await self.playlist_importer.enrich_netease_songs(songs)

        # 整批原子入队：等待期间即便其他用户继续点歌，也不会出现只加入
        # 所选区间前半段的情况。容量不足则整批拒绝并要求重新选择。
        existing_session = self.voice_manager.get_session(guild_id)
        if existing_session:
            voice_channel_id = existing_session.voice_channel_id
        else:
            voice_channel_id = await self.voice_manager.get_user_voice_channel(
                self._kook_token, guild_id, event.get_sender_id()
            )
            if not voice_channel_id and not self.voice_manager.get_session(guild_id):
                self._finish_playlist_import_request(interaction_key, request_marker)
                yield event.plain_result("❌ 请先加入一个语音频道再导入歌单")
                return

        request_replaced = False
        async with _playlist_request_commit_lock:
            if _playlist_import_requests.get(interaction_key) is not request_marker:
                request_replaced = True
                ok, msg = False, "请求已被替换"
            else:
                ok, msg = await self.voice_manager.join_and_play_many(
                    self._kook_token,
                    guild_id,
                    voice_channel_id or "",
                    channel_id,
                    songs,
                )
        if request_replaced:
            yield event.plain_result("ℹ️ 本次导入已由更新的批量请求替换")
            return
        if not ok:
            self._finish_playlist_import_request(interaction_key, request_marker)
            yield event.plain_result(f"❌ {msg}，请重新导入并选择更短的区间")
            return

        total_added = len(songs)
        session = self.voice_manager.get_session(guild_id)
        queue_size = len(session.playlist) if session else total_added

        # 发送导入结果卡片
        result_card = card_builder.build_import_result_card(
            total=total_added,
            playlist_id=target_id,
            queue_size=queue_size,
            requester_name=requester_name,
            platform=import_type,
        )
        if self._kook_token and channel_id:
            await send_card_message(self._kook_token, channel_id, result_card)
        else:
            yield event.chain_result([Json(data=result_card)])
        self._finish_playlist_import_request(interaction_key, request_marker)

    # ========== 内部方法 ==========

    def _available_queue_slots(self, guild_id: str) -> int:
        """返回服务器播放队列按当前配置仍可加入的歌曲数。"""
        session = self.voice_manager.get_session(guild_id)
        used = len(session.playlist) if session else 0
        return max(0, self.max_queue_size - used)

    def _playlist_interaction_key(
        self, event: AstrMessageEvent, guild_id: str
    ) -> str:
        """区间等待按用户、服务器和文字频道隔离。"""
        return f"{event.get_sender_id()}_{guild_id}_{self._get_channel_id(event)}"

    def _search_interaction_key(
        self, event: AstrMessageEvent, guild_id: str
    ) -> str:
        """选歌等待按用户、服务器和文字频道隔离。"""
        return f"{event.get_sender_id()}_{guild_id}_{self._get_channel_id(event)}"

    @staticmethod
    def _finish_playlist_import_request(interaction_key: str, marker: object):
        if _playlist_import_requests.get(interaction_key) is marker:
            _playlist_import_requests.pop(interaction_key, None)

    async def _handle_playlist_range_reply(
        self,
        event: AstrMessageEvent,
        session_key: str,
        guild_id: str,
        text: str,
    ):
        """校验大型歌单的 1-based 闭区间；错误输入不会结束等待。"""
        error = None
        async with _pending_playlist_ranges_lock:
            entry = _pending_playlist_ranges.get(session_key)
            if not entry:
                return
            songs, future = entry

            available_slots = self._available_queue_slots(guild_id)
            selected_range, error = validate_playlist_range(
                text, len(songs), available_slots
            )
            if not error:
                start, end = selected_range
                _pending_playlist_ranges.pop(session_key, None)
                if not future.done():
                    future.set_result(songs[start - 1:end])

        if error:
            yield event.plain_result(f"❌ {error}")
            return

        user_msg_id = event.message_obj.message_id
        if user_msg_id and self._kook_token:
            asyncio.create_task(delete_message(self._kook_token, user_msg_id))
        event.stop_event()

    async def _send_card(self, channel_id: str, guild_id: str, card_data: dict) -> str | None:
        """发送卡片并记录 msg_id（先删旧卡片再发新卡片）"""
        # 先删除旧卡片；删除失败时保留 msg_id，下次继续补删，避免长队列播放时残留累积。
        old_msg_ids = self._card_msg_ids.pop(guild_id, [])
        failed_msg_ids = await self._delete_card_messages(old_msg_ids)

        # 发送新卡片
        msg_id = await send_card_message(self._kook_token, channel_id, card_data)
        tracked_msg_ids = failed_msg_ids
        if msg_id:
            tracked_msg_ids.append(msg_id)
        if tracked_msg_ids:
            self._card_msg_ids[guild_id] = tracked_msg_ids
        return msg_id

    async def _delete_card(self, guild_id: str):
        """删除已发送的卡片消息"""
        msg_ids = self._card_msg_ids.pop(guild_id, [])
        failed_msg_ids = await self._delete_card_messages(msg_ids)
        if failed_msg_ids:
            self._card_msg_ids[guild_id] = failed_msg_ids

    async def _delete_card_messages(self, msg_ids: list[str]) -> list[str]:
        """删除播放卡片消息，返回仍未删除成功的 msg_id。"""
        if not self._kook_token or not msg_ids:
            return []
        if isinstance(msg_ids, str):
            msg_ids = [msg_ids]

        unique_msg_ids: list[str] = []
        seen: set[str] = set()
        for msg_id in msg_ids:
            if not msg_id or msg_id in seen:
                continue
            seen.add(msg_id)
            unique_msg_ids.append(msg_id)

        results = await asyncio.gather(
            *(self._delete_message_with_retry(msg_id) for msg_id in unique_msg_ids)
        )
        failed_msg_ids = [
            msg_id for msg_id, ok in zip(unique_msg_ids, results) if not ok
        ]
        if failed_msg_ids:
            logger.warning(
                f"[KookMusic] 有 {len(failed_msg_ids)} 张播放卡片删除失败，已保留待下次重试"
            )
        return failed_msg_ids

    async def _delete_message_with_retry(self, msg_id: str, attempts: int = 3) -> bool:
        """删除 KOOK 消息，短重试以规避偶发接口失败。"""
        for attempt in range(attempts):
            if await delete_message(self._kook_token, msg_id):
                return True
            if attempt < attempts - 1:
                await asyncio.sleep(0.2 * (attempt + 1))
        return False

    async def _delete_messages(self, msg_ids: list[str]):
        """批量删除 KOOK 消息"""
        if not self._kook_token or not msg_ids:
            return
        for msg_id in msg_ids:
            if msg_id:
                await self._delete_message_with_retry(msg_id)

    async def _download_song(self, song: Song) -> Song:
        """下载歌曲回调：获取音频 URL 并下载到本地。

        由 VoiceManager 的播放循环在播放每首歌前调用。
        """
        # B站平台：使用 yt-dlp 下载
        if song.platform == "bilibili":
            if not song.file_path:
                song = await self.bilibili.download_audio(
                    song, self.data_dir / "songs"
                )
            return song

        # 其他平台：获取音频 URL 后 HTTP 下载
        if not song.audio_url:
            song = await self.searcher.fetch_audio_url(song)
        if not song.audio_url:
            logger.error(self._unplayable_message(song))
            return song

        # 下载到本地
        song = await self.downloader.download(song)
        if not song.file_path and song.id:
            # 大型队列中的直链可能在轮到播放前已经过期。清空旧 URL，
            # 按歌曲 ID 重新解析一次后重试下载。
            song.audio_url = ""
            song = await self.searcher.fetch_audio_url(song)
            if song.audio_url:
                song = await self.downloader.download(song)
        return song

    async def _play_song(
        self, event: AstrMessageEvent, song: Song, guild_id: str
    ):
        """完整播放流程：获取URL → 按需下载 → 加入频道 → 播放/入队

        如果当前没有歌曲在播放，立即下载并播放。
        如果已有歌曲在播放，仅加入队列（不下载），由播放循环在轮到时下载。
        """
        # 填充请求者信息
        song.requester_id = event.get_sender_id()
        song.requester_name = event.get_sender_name()

        # 检查是否已在播放
        existing_session = self.voice_manager.get_session(guild_id)
        has_active_queue = existing_session and (
            existing_session.is_playing or existing_session.playlist
        )

        if has_active_queue:
            # ---- 已在播放：仅入队，不下载 ----
            # 但仍然需要先获取音频 URL（用于元数据展示）
            if not song.audio_url:
                song = await self.searcher.fetch_audio_url(song)
            if not song.audio_url:
                await event.send(
                    event.plain_result(self._unplayable_message(song))
                )
                return
        else:
            # ---- 首次播放：立即获取URL并下载 ----
            if song.platform == "bilibili":
                # B站平台：使用 yt-dlp 下载音频
                await event.send(
                    event.plain_result(f"⏬ 正在下载B站音频：{song.display_name}...")
                )
                song = await self.bilibili.download_audio(
                    song, self.data_dir / "songs"
                )
            else:
                # 其他平台：HTTP 下载
                if not song.audio_url:
                    song = await self.searcher.fetch_audio_url(song)
                if not song.audio_url:
                    await event.send(
                        event.plain_result(self._unplayable_message(song))
                    )
                    return

                await event.send(
                    event.plain_result(f"⏬ 正在下载：{song.display_name}...")
                )
                song = await self.downloader.download(song)

            if not song.file_path:
                await event.send(
                    event.plain_result(self._unplayable_message(song, download=True))
                )
                return

        # 获取语音频道
        if existing_session:
            voice_channel_id = existing_session.voice_channel_id
        else:
            user_id = event.get_sender_id()
            voice_channel_id = await self.voice_manager.get_user_voice_channel(
                self._kook_token, guild_id, user_id
            )
            if not voice_channel_id:
                await event.send(
                    event.plain_result("❌ 请先加入一个语音频道再点歌")
                )
                self.voice_manager._cleanup_song_file(song)
                return

        text_channel_id = self._get_channel_id(event)

        # 加入频道并播放/入队
        ok, msg = await self.voice_manager.join_and_play(
            self._kook_token,
            guild_id,
            voice_channel_id,
            text_channel_id,
            song,
        )

        if ok:
            session = self.voice_manager.get_session(guild_id)
            queue_size = len(session.playlist) if session else 0
            loop_name = session.loop_mode_name if session else "关闭"

            if msg.startswith("QUEUED:"):
                card_data = card_builder.build_queued_card(
                    song, queue_size, loop_name
                )
                await event.send(event.chain_result([Json(data=card_data)]))
        else:
            await event.send(event.plain_result(f"❌ {msg}"))
            self.voice_manager._cleanup_song_file(song)

    @staticmethod
    def _unplayable_message(song: Song, download: bool = False) -> str:
        action = "下载失败" if download else "无法获取音频链接"
        message = f"❌ {action}：{song.name}"
        if song.unplayable_reason:
            message += f"\n原因：{song.unplayable_reason}"
        elif song.platform in {"qq", "kugou"}:
            message += "\n原因：该歌曲可能需要平台会员或受版权/地区限制"
        return message

    async def _play_bilibili(
        self, event: AstrMessageEvent, keyword: str, guild_id: str
    ):
        """B站视频点歌流程"""
        # 提取视频音频
        song = await self.bilibili.extract(keyword)
        if not song:
            await event.send(event.plain_result(
                f"❌ 无法解析B站视频：{keyword}\n"
                "可能原因：视频不存在、地区限制或需要登录\n"
                "提示：可在插件数据目录放置 bili_cookies.txt 解决鉴权问题"
            ))
            return

        # 填充请求者信息
        song.requester_id = event.get_sender_id()
        song.requester_name = event.get_sender_name()

        # 复用现有播放流程
        await self._play_song(event, song, guild_id)

    async def _play_bilibili_collection(
        self,
        event: AstrMessageEvent,
        collection: BilibiliCollection,
        guild_id: str,
        interaction_key: str,
        request_marker: object,
    ):
        """按歌单区间规则将 B站收藏夹或多P视频整批原子入队。"""
        channel_id = self._get_channel_id(event)
        requester_id = event.get_sender_id()
        requester_name = event.get_sender_name()

        request_replaced = False
        async with _playlist_request_commit_lock:
            if _bilibili_play_requests.get(interaction_key) is not request_marker:
                request_replaced = True
            else:
                async with _pending_playlist_ranges_lock:
                    old_entry = _pending_playlist_ranges.pop(interaction_key, None)
                    if old_entry and not old_entry[1].done():
                        old_entry[1].cancel()
                    _playlist_import_requests[interaction_key] = request_marker
        if request_replaced:
            await event.send(event.plain_result(
                "ℹ️ 本次添加已由更新的播放请求替换"
            ))
            return

        try:
            songs = list(collection.songs)
            if not songs:
                await event.send(event.plain_result(
                    f"❌ B站{collection.kind}为空或没有可读取的视频"
                ))
                return

            available_slots = self._available_queue_slots(guild_id)
            if available_slots <= 0:
                await event.send(event.plain_result(
                    f"❌ 当前播放队列已满（上限 {self.max_queue_size} 首），"
                    f"无法添加 B站{collection.kind}"
                ))
                return

            if len(songs) > available_slots:
                future = asyncio.get_running_loop().create_future()
                async with _pending_playlist_ranges_lock:
                    request_replaced = (
                        _playlist_import_requests.get(interaction_key)
                        is not request_marker
                    )
                    if not request_replaced:
                        _pending_playlist_ranges[interaction_key] = (songs, future)
                if request_replaced:
                    await event.send(event.plain_result(
                        "ℹ️ 本次添加已由更新的批量播放请求替换"
                    ))
                    return

                title = (
                    collection.title.replace("\r", " ").replace("\n", " ").strip()[:80]
                )
                await event.send(event.plain_result(
                    f"📚 B站{collection.kind}《{title}》共有 {len(songs)} 项，"
                    f"当前队列最多还能加入 {available_slots} 项。\n"
                    f"请在 {self.playlist_range_timeout} 秒内输入要播放的区间，"
                    f"例如 `1-{available_slots}`。\n"
                    f"区间必须在 1-{len(songs)} 内，且数量不能超过 "
                    f"{available_slots} 项。"
                ))

                try:
                    songs = await asyncio.wait_for(
                        future, timeout=self.playlist_range_timeout
                    )
                except asyncio.TimeoutError:
                    await event.send(event.plain_result(
                        "⏰ B站批量播放区间选择超时，本次添加已取消"
                    ))
                    return
                except asyncio.CancelledError:
                    if (
                        _playlist_import_requests.get(interaction_key)
                        is not request_marker
                    ):
                        await event.send(event.plain_result(
                            "ℹ️ 已由新的批量播放请求替换本次区间选择"
                        ))
                        return
                    raise
                finally:
                    async with _pending_playlist_ranges_lock:
                        current = _pending_playlist_ranges.get(interaction_key)
                        if current and current[1] is future:
                            _pending_playlist_ranges.pop(interaction_key, None)

            if _playlist_import_requests.get(interaction_key) is not request_marker:
                await event.send(event.plain_result(
                    "ℹ️ 本次添加已由更新的批量播放请求替换"
                ))
                return

            available_slots = self._available_queue_slots(guild_id)
            if len(songs) > available_slots:
                await event.send(event.plain_result(
                    f"❌ 等待期间队列发生变化，目前只能再加入 {available_slots} 项。"
                    "请重新执行播放命令并选择更短的区间。"
                ))
                return

            selected_count = len(songs)
            materialized = await self.bilibili.materialize_collection_songs(songs)
            if materialized is None:
                await event.send(event.plain_result(
                    f"❌ B站{collection.kind}内容读取失败，请稍后重试"
                ))
                return
            songs = materialized
            skipped = selected_count - len(songs)
            if not songs:
                await event.send(event.plain_result(
                    "❌ 所选区间没有可播放的视频，内容可能已失效或被删除"
                ))
                return

            if _playlist_import_requests.get(interaction_key) is not request_marker:
                await event.send(event.plain_result(
                    "ℹ️ 本次添加已由更新的批量播放请求替换"
                ))
                return

            for song in songs:
                song.requester_id = requester_id
                song.requester_name = requester_name

            available_slots = self._available_queue_slots(guild_id)
            if len(songs) > available_slots:
                await event.send(event.plain_result(
                    f"❌ 读取期间队列发生变化，目前只能再加入 {available_slots} 项。"
                    "请重新执行播放命令并选择更短的区间。"
                ))
                return

            existing_session = self.voice_manager.get_session(guild_id)
            if existing_session:
                voice_channel_id = existing_session.voice_channel_id
            else:
                voice_channel_id = await self.voice_manager.get_user_voice_channel(
                    self._kook_token, guild_id, requester_id
                )
                if not voice_channel_id and not self.voice_manager.get_session(guild_id):
                    await event.send(event.plain_result(
                        "❌ 请先加入一个语音频道再添加 B站内容"
                    ))
                    return

            request_replaced = False
            async with _playlist_request_commit_lock:
                if (
                    _bilibili_play_requests.get(interaction_key)
                    is not request_marker
                    or _playlist_import_requests.get(interaction_key)
                    is not request_marker
                ):
                    request_replaced = True
                    ok, msg = False, "请求已被替换"
                else:
                    ok, msg = await self.voice_manager.join_and_play_many(
                        self._kook_token,
                        guild_id,
                        voice_channel_id or "",
                        channel_id,
                        songs,
                    )
            if request_replaced:
                await event.send(event.plain_result(
                    "ℹ️ 本次添加已由更新的播放或歌单请求替换"
                ))
                return
            if not ok:
                await event.send(event.plain_result(
                    f"❌ {msg}，请重新执行播放命令并选择更短的区间"
                ))
                return

            session = self.voice_manager.get_session(guild_id)
            queue_size = len(session.playlist) if session else len(songs)
            result_card = card_builder.build_bilibili_collection_result_card(
                total=len(songs),
                collection_id=collection.id,
                collection_title=collection.title,
                collection_kind=collection.kind,
                queue_size=queue_size,
                requester_name=requester_name,
                skipped=skipped,
            )
            if self._kook_token and channel_id:
                await send_card_message(self._kook_token, channel_id, result_card)
            else:
                await event.send(event.chain_result([Json(data=result_card)]))
        finally:
            async with _playlist_request_commit_lock:
                self._finish_playlist_import_request(
                    interaction_key,
                    request_marker,
                )

    async def _on_playback_finished(self, guild_id: str):
        """播放队列全部完成的回调"""
        logger.info(f"[KookMusic] 队列播放完毕: {guild_id}")
        # 删除卡片消息防止刷屏
        await self._delete_card(guild_id)

    async def _on_song_started(
        self, guild_id: str, song: Song, queue_size: int, loop_mode: str
    ):
        """新歌曲开始播放的回调：删除旧卡片并发送新的「正在播放」卡片"""
        session = self.voice_manager.get_session(guild_id)
        if not session:
            return
        text_channel_id = session.text_channel_id
        if not text_channel_id or not self._kook_token:
            return

        # 根据平台选择不同的播放卡片
        if song.platform == "bilibili":
            now_card = card_builder.build_bilibili_playing_card(song, queue_size, loop_mode)
        else:
            now_card = card_builder.build_now_playing_card(song, queue_size, loop_mode)
        await self._send_card(text_channel_id, guild_id, now_card)
