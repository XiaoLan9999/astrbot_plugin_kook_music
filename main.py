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
from .music.bilibili import BilibiliExtractor
from .music.playlist_import import PlaylistImporter

# 用户选歌等待映射：session_key -> (songs, future, [msg_ids_to_delete])
_pending_selections: dict[str, tuple[list[Song], asyncio.Future, list[str]]] = {}
_pending_selections_lock = asyncio.Lock()

# 已知的平台标识
_KNOWN_PLATFORMS = {"netease", "qq", "kugou", "kuwo", "migu", "baidu", "bilibili"}

# 按钮点击回调队列（由 monkey-patch 写入，由 on_message 消费）
_button_click_queue: asyncio.Queue | None = None


class KookMusicPlugin(Star):
    """KOOK 语音点歌插件

    在 KOOK 平台实现语音频道点歌：
    - 点歌 <歌名> [平台] — 搜索并播放
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
        self.max_queue_size = self.config.get("max_queue_size", 50)
        self.search_limit = self.config.get("search_limit", 5)
        self.auto_leave_timeout = self.config.get("auto_leave_timeout", 300)
        self.max_sessions = self.config.get("max_sessions", 5)
        self.volume = self._parse_volume(self.config.get("volume", "0.15"))
        self.streaming_mode = self.config.get("streaming_mode", "relay")

        # 核心组件
        self.data_dir = Path("data/plugin_data/astrbot_plugin_kook_music")
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.searcher = MusicSearcher()
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
        global _button_click_queue, _pending_selections
        _button_click_queue = asyncio.Queue()
        # 重置全局状态（支持热重载）
        _pending_selections.clear()

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
                "用法：点歌 <歌名/网易云单曲链接/网易云电台节目链接> [平台]\n"
                "例如：点歌 青花瓷\n"
                "例如：点歌 https://music.163.com/#/song?id=xxxx\n"
                "例如：点歌 https://music.163.com/#/program?id=xxxx\n"
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
        session_key = f"{event.get_sender_id()}_{guild_id}"
        future = asyncio.get_running_loop().create_future()
        async with _pending_selections_lock:
            _pending_selections[session_key] = (songs, future, search_msg_ids)

        try:
            selected: Song = await asyncio.wait_for(future, timeout=30)
            await self._play_song(event, selected, guild_id)
        except asyncio.TimeoutError:
            async with _pending_selections_lock:
                entry = _pending_selections.pop(session_key, None)
            if entry:
                await self._delete_messages(entry[2])
            yield event.plain_result("⏰ 选歌超时，已取消")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """处理用户选歌回复"""
        if not self._is_kook(event):
            return

        text = event.message_str.strip()
        guild_id = self._get_guild_id(event)
        session_key = f"{event.get_sender_id()}_{guild_id}"

        if session_key not in _pending_selections:
            return

        if not text.isdigit():
            return

        async with _pending_selections_lock:
            entry = _pending_selections.get(session_key)
            if not entry:
                return
            songs, future, msg_ids = entry
            idx = int(text) - 1

            if 0 <= idx < len(songs):
                _pending_selections.pop(session_key, None)
                if not future.done():
                    future.set_result(songs[idx])
                # 清理搜索相关消息
                asyncio.create_task(self._delete_messages(msg_ids))
                # 同时删除用户选择的数字消息
                user_msg_id = event.message_obj.message_id
                if user_msg_id and self._kook_token:
                    asyncio.create_task(delete_message(self._kook_token, user_msg_id))
                event.stop_event()
            else:
                yield event.plain_result(f"❌ 请输入 1-{len(songs)} 的数字")
                # 不需要重新放回，因为没有 pop

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

    @filter.command("播放b站")
    async def on_play_bilibili(self, event: AstrMessageEvent):
        """播放b站 <关键词/BV号/链接> — 播放B站视频音频"""
        if not self._is_kook(event):
            return

        if not self._kook_token:
            self._kook_token = self._find_kook_token()
            if not self._kook_token:
                yield event.plain_result("❌ 未找到 KOOK Token")
                return

        raw_text = event.message_str.strip()
        for prefix in ("播放b站", "/播放b站", "播放B站", "/播放B站"):
            if raw_text.startswith(prefix):
                raw_text = raw_text[len(prefix):].strip()
                break

        if not raw_text:
            yield event.plain_result(
                "用法：播放b站 <关键词/BV号/链接>\n"
                "例如：播放b站 海阔天空\n"
                "例如：播放b站 BV1qa411e7Fi\n"
                "例如：播放b站 https://www.bilibili.com/video/BV1kT4y1X7UW?p=4"
            )
            return

        guild_id = self._get_guild_id(event)
        if not guild_id:
            yield event.plain_result("❌ 无法获取服务器信息")
            return

        await self._play_bilibili(event, raw_text, guild_id)

    @filter.command("导入歌单")
    async def on_import_playlist(self, event: AstrMessageEvent):
        """导入歌单 <歌单ID或链接> — 导入网易云歌单/电台到播放队列"""
        if not self._is_kook(event):
            return

        if not self._kook_token:
            self._kook_token = self._find_kook_token()
            if not self._kook_token:
                yield event.plain_result("❌ 未找到 KOOK Token")
                return

        raw_text = event.message_str.strip()
        for prefix in ("导入歌单", "/导入歌单"):
            if raw_text.startswith(prefix):
                raw_text = raw_text[len(prefix):].strip()
                break

        if not raw_text:
            yield event.plain_result(
                "用法：导入歌单 <歌单ID或链接>\n"
                "支持：普通歌单、电台(djradio)\n"
                "例如：导入歌单 977171340\n"
                "例如：导入歌单 https://music.163.com/#/playlist?id=977171340\n"
                "例如：导入歌单 https://music.163.com/#/djradio?id=972583481"
            )
            return

        guild_id = self._get_guild_id(event)
        if not guild_id:
            yield event.plain_result("❌ 无法获取服务器信息")
            return

        channel_id = self._get_channel_id(event)

        # 解析歌单链接/ID（自动识别普通歌单 vs 电台）
        import_type, target_id = PlaylistImporter.parse_playlist_input(raw_text)
        if not target_id:
            yield event.plain_result("❌ 无法识别歌单/电台 ID，请检查输入")
            return

        # 发送进度提示
        type_label = "电台" if import_type == "djradio" else "歌单"
        await event.send(event.plain_result(f"📥 正在导入{type_label} {target_id}，请稍候..."))

        # 根据类型选择导入方法
        requester_id = event.get_sender_id()
        requester_name = event.get_sender_name()

        if import_type == "djradio":
            songs = await self.playlist_importer.import_netease_djradio(
                target_id, requester_id, requester_name
            )
        else:
            songs = await self.playlist_importer.import_netease_playlist(
                target_id, requester_id, requester_name
            )

        if not songs:
            yield event.plain_result(f"❌ {type_label} {target_id} 导入失败或内容为空")
            return

        # 确保 Bot 在语音频道中
        existing_session = self.voice_manager.get_session(guild_id)
        idle_existing_session = (
            existing_session
            and not existing_session.is_playing
            and not existing_session.playlist
        )
        started_with_first_song = False
        if not existing_session or idle_existing_session:
            # 获取用户所在语音频道
            if existing_session:
                voice_channel_id = existing_session.voice_channel_id
            else:
                user_id = event.get_sender_id()
                voice_channel_id = await self.voice_manager.get_user_voice_channel(
                    self._kook_token, guild_id, user_id
                )
            if not voice_channel_id:
                yield event.plain_result("❌ 请先加入一个语音频道再导入歌单")
                return

            # 用第一首歌创建会话
            first_song = songs[0]
            ok, msg = await self.voice_manager.join_and_play(
                self._kook_token, guild_id, voice_channel_id, channel_id, first_song
            )
            if not ok:
                yield event.plain_result(f"❌ {msg}")
                return
            # 剩余歌曲加入队列
            remaining = songs[1:]
            started_with_first_song = True
        else:
            remaining = songs

        # 批量加入队列
        added = 0
        session = self.voice_manager.get_session(guild_id)
        if session:
            for song in remaining:
                if len(session.playlist) >= self.max_queue_size:
                    break
                session.playlist.append(song)
                added += 1
            # 如果队列之前为空且不在播放，启动播放循环
            if not started_with_first_song and not session.is_playing and session.playlist:
                self.voice_manager._start_playback_loop(guild_id)

        total_added = added + (1 if not existing_session or idle_existing_session else 0)
        queue_size = len(session.playlist) if session else total_added

        # 发送导入结果卡片
        result_card = card_builder.build_import_result_card(
            total=total_added,
            playlist_id=target_id,
            queue_size=queue_size,
            requester_name=requester_name,
        )
        if self._kook_token and channel_id:
            await send_card_message(self._kook_token, channel_id, result_card)
        else:
            yield event.chain_result([Json(data=result_card)])

    # ========== 内部方法 ==========

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
            logger.error(f"[KookMusic] 无法获取音频链接: {song.name}")
            return song

        # 下载到本地
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
        is_currently_playing = existing_session and existing_session.is_playing

        if is_currently_playing:
            # ---- 已在播放：仅入队，不下载 ----
            # 但仍然需要先获取音频 URL（用于元数据展示）
            if not song.audio_url:
                song = await self.searcher.fetch_audio_url(song)
            if not song.audio_url:
                await event.send(
                    event.plain_result(f"❌ 无法获取音频链接：{song.name}")
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
                        event.plain_result(f"❌ 无法获取音频链接：{song.name}")
                    )
                    return

                await event.send(
                    event.plain_result(f"⏬ 正在下载：{song.display_name}...")
                )
                song = await self.downloader.download(song)

            if not song.file_path:
                await event.send(
                    event.plain_result(f"❌ 下载失败：{song.name}")
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

    async def _play_bilibili(
        self, event: AstrMessageEvent, keyword: str, guild_id: str
    ):
        """B站视频点歌流程"""
        channel_id = self._get_channel_id(event)

        # 解析提示
        await event.send(event.plain_result(f"🔍 正在解析B站视频：{keyword}..."))

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
