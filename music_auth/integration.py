"""Bind account ownership to one actual KOOK adapter and explicit private-chat admins."""

import asyncio
import logging
import re

from .kook_dm import send_private_auth
from .types import AudioResult

logger = logging.getLogger("astrbot")


def _number(value, default, low, high):
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


class MusicAuthMixin:
    @staticmethod
    def _account_platform_id(platform):
        # AstrBot 4.28.1 KOOK meta().id can remain "kook" for every instance.
        return str(platform.config.get("id") or platform.meta().id)

    def _configure_music_auth(self):
        raw = self.config.get("music_auth_admin_ids", [])
        self._music_auth_admin_ids = (
            {
                str(value).strip()
                for value in raw
                if not isinstance(value, bool)
                and re.fullmatch(r"[0-9]{1,20}", str(value).strip())
            }
            if isinstance(raw, list)
            else set()
        )
        self._music_auth_enabled = self.config.get(
            "music_auth_enabled", True
        ) is True and bool(self._music_auth_admin_ids)
        self._music_auth = None
        self._music_auth_binding = None
        self._music_auth_start_task = None
        self._music_auth_lock = asyncio.Lock()
        self._music_auth_closed = False
        self._music_auth_failed = False

    def _account_platform(self):
        requested = str(self.config.get("music_auth_kook_bot_id", "") or "").strip()
        platforms = []
        for platform in self.context.platform_manager.platform_insts:
            try:
                if platform.meta().name != "kook":
                    continue
                if not platform.config.get("kook_bot_token"):
                    continue
                if requested and self._account_platform_id(platform) != requested:
                    continue
                platforms.append(platform)
            except (AttributeError, TypeError):
                continue
        return platforms[0] if len(platforms) == 1 else None

    async def _notify_music_auth(self, bot_id, user_id, text, qr_bytes=None):
        if self._music_auth_closed or user_id not in self._music_auth_admin_ids:
            raise RuntimeError("Private account recipient is not authorized")
        platform = self._account_platform()
        if platform is None or self._account_platform_id(platform) != str(bot_id):
            raise RuntimeError("Account notification adapter is unavailable")
        token = str(platform.config.get("kook_bot_token", "") or "").strip()
        if self._music_auth_binding != (str(bot_id), token):
            raise RuntimeError("Account notification adapter changed")
        return await send_private_auth(token, str(user_id), str(text), qr_bytes)

    async def _ensure_music_auth(self):
        if (
            not getattr(self, "_music_auth_enabled", False)
            or self._music_auth_closed
            or self._music_auth_failed
        ):
            return None
        async with self._music_auth_lock:
            if self._music_auth_closed:
                return None
            platform = self._account_platform()
            if platform is None:
                await self._stop_music_auth_instance()
                return None
            binding = (
                self._account_platform_id(platform),
                str(platform.config.get("kook_bot_token", "") or "").strip(),
            )
            if self._music_auth is not None and self._music_auth_binding == binding:
                return self._music_auth
            await self._stop_music_auth_instance()
            try:
                from .manager import AuthManager
                from .netease_backend import NeteaseBackend
                from .qq_backend import QQBackend

                manager = AuthManager(
                    credentials_dir=self.data_dir / "music_accounts",
                    backends={"qq": QQBackend(), "netease": NeteaseBackend()},
                    notify=self._notify_music_auth,
                    admin_ids=set(self._music_auth_admin_ids),
                    check_interval=_number(
                        self.config.get("music_auth_check_interval"), 1800, 300, 86400
                    ),
                    notice_cooldown=_number(
                        self.config.get("music_auth_notice_cooldown"),
                        21600,
                        300,
                        604800,
                    ),
                    login_timeout=_number(
                        self.config.get("music_auth_login_timeout"), 180, 30, 300
                    ),
                )
            except Exception as exc:
                self._music_auth_failed = True
                logger.warning(
                    "[KookMusic] 账号模块未启用，原解析链路保持不变 (%s)",
                    type(exc).__name__,
                )
                return None
            self._music_auth = manager
            self._music_auth_binding = binding

            async def start():
                try:
                    await manager.start(binding[0])
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "[KookMusic] 账号检查暂时失败 (%s)", type(exc).__name__
                    )

            self._music_auth_start_task = asyncio.create_task(start())
            return manager

    async def _stop_music_auth_instance(self):
        if self._music_auth_start_task is not None:
            self._music_auth_start_task.cancel()
            await asyncio.gather(self._music_auth_start_task, return_exceptions=True)
            self._music_auth_start_task = None
        if self._music_auth is not None:
            await self._music_auth.close()
        self._music_auth = None
        self._music_auth_binding = None

    async def _close_music_auth(self):
        self._music_auth_closed = True
        async with self._music_auth_lock:
            await self._stop_music_auth_instance()

    async def _resolve_account_audio(self, song):
        manager = await self._ensure_music_auth()
        if manager is None:
            return AudioResult("unavailable")
        return await manager.resolve_audio(song)

    async def _music_auth_command(self, event, command):
        if event.get_platform_name() != "kook":
            return None
        event.stop_event()
        if not event.is_private_chat():
            return "音乐账号操作只接受 KOOK 私聊；请勿在频道发送登录信息。"
        actor = str(event.get_sender_id() or "")
        if actor not in self._music_auth_admin_ids:
            return "你不在音乐账号管理员白名单中。"
        if not self._music_auth_enabled:
            return "音乐账号功能未启用。"
        platform = self._account_platform()
        if (
            platform is None
            or str(platform.meta().id) != str(event.get_platform_id())
            or getattr(platform, "client", None) is None
            or getattr(event, "client", None) is not getattr(platform, "client", None)
        ):
            return "请联系配置中指定的 KOOK 机器人；多机器人时需设置 music_auth_kook_bot_id。"
        manager = await self._ensure_music_auth()
        if manager is None:
            return "账号模块尚未就绪；请检查依赖、管理员配置和加密存储，不要删除已有凭据文件。"
        bot_id = self._account_platform_id(platform)
        if command == "音乐账号状态":
            return manager.status()
        text = event.message_str.strip()
        argument = (
            re.sub(r"^[#/]?" + re.escape(command) + r"(?:\s+|$)", "", text, count=1)
            .strip()
            .lower()
        )
        options = {
            "qq": ("qq", "qq"),
            "微信": ("qq", "wechat"),
            "wechat": ("qq", "wechat"),
            "wx": ("qq", "wechat"),
            "网易云": ("netease", "netease"),
            "netease": ("netease", "netease"),
            "网易云网页": ("netease", "netease_web"),
        }
        target = options.get(argument)
        if target is None:
            return "私聊命令：音乐登录 qq / 音乐登录 微信 / 音乐登录 网易云 / 音乐登录 网易云网页；音乐账号状态；取消音乐登录 qq|网易云；退出音乐账号 qq|网易云。无需发送密码或 Cookie。"
        provider, method = target
        if command == "音乐登录":
            _, detail = await manager.start_login(provider, method, bot_id, actor)
        elif command == "取消音乐登录":
            _, detail = await manager.cancel_login(provider, bot_id, actor)
        else:
            _, detail = await manager.logout(provider, bot_id, actor)
        return detail
