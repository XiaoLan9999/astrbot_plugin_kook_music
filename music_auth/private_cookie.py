"""Consume account secrets before KOOK messages reach AstrBot's event bus."""

import asyncio
import html
import json
import re
import time
from types import SimpleNamespace

from ..kook_api import _get_session
from ..kook_events import enum_value, event_field
from .kook_dm import _post

_COMMAND = re.compile(
    r"^[#/]?(音乐Cookie|音乐验证|音乐登录|音乐账号状态|取消音乐登录|退出音乐账号)(?:\s+|$)",
    re.IGNORECASE,
)
_SECRET = re.compile(
    r"MUSIC(?:\\?_)+(?:U|A|R(?:\\?_)+T)\b|(?:\\?_){2}csrf\b|Netscape HTTP Cookie File",
    re.IGNORECASE,
)
_MSG_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
GUIDE = (
    "请先在网易云官网完成登录/安全验证，再私聊发送 #音乐Cookie 网易云，"
    "随后在接收窗口内发送一条完整 Cookie 文本。支持 Header String、JSON、Netscape，"
    "可包在代码块中。也可把 Cookie 放在命令后的下一行；不接受文件或下载链接。"
    "超出 KOOK 单条消息长度时，请只保留 MUSIC_U（可加 __csrf），不要分条发送。"
    "内容会经过 KOOK 私聊服务器，插件在进入 AstrBot 日志/其他插件前拦截；"
    "校验成功后会尝试撤回原消息，但不保证 KOOK 能删除用户消息。"
)


def _secret_text(text):
    if _SECRET.search(text):
        return True
    probe = text.lstrip(" \t\r\n\ufeff")
    if probe.startswith("```"):
        lines = probe.splitlines()
        probe = (
            "\n".join(lines[1:-1]).lstrip(" \t\r\n\ufeff") if len(lines) >= 3 else ""
        )
    if not probe.startswith(("[", "{")):
        return False
    if len(probe) > 64 * 1024:
        return True
    try:
        value = json.loads(probe)
    except (ValueError, RecursionError):
        return '"name"' in probe and '"value"' in probe
    if isinstance(value, dict) and isinstance(value.get("cookies"), list):
        value = value["cookies"]
    if isinstance(value, dict):
        value = [value]
    return isinstance(value, list) and any(
        isinstance(item, dict) and "name" in item and "value" in item for item in value
    )


class PrivateCookieIntake:
    def __init__(self, owner, *, clock=time.monotonic):
        self.owner = owner
        self.clock = clock
        self.windows = {}
        self.tasks = set()
        self.seen = {}
        self.rates = {}
        self.generation = 0
        self.closed = False
        self.signature = None
        self.import_owner = None
        self.task_owners = {}
        self.busy_tasks = set()

    def _operation_owner(self):
        for task, actor in self.task_owners.items():
            if not task.done() and not task.cancelling():
                return actor
        if self.import_owner is not None:
            return self.import_owner[0]
        now = self.clock()
        for (_, actor), (expires, generation) in self.windows.items():
            if expires >= now and generation == self.generation:
                return actor
        return None

    def _task_done(self, task):
        self.tasks.discard(task)
        self.task_owners.pop(task, None)
        self.busy_tasks.discard(task)

    def refresh(self):
        platform = self.owner._account_platform()
        configured = self.owner.config.get("music_auth_admin_ids", [])
        signature = (
            id(getattr(platform, "client", None)),
            getattr(platform, "config", {}).get("id", ""),
            getattr(platform, "config", {}).get("kook_bot_token", ""),
            self.owner.config.get("music_auth_enabled", True),
            repr(configured),
            self.owner.config.get("music_auth_kook_bot_id", ""),
            self.owner.config.get("music_auth_cookie_timeout", 180),
        )
        if self.signature is not None and signature != self.signature:
            self.revoke(include_busy=True)
        self.signature = signature

    def revoke(self, *, include_busy=False):
        self.generation += 1
        self.windows.clear()
        current = asyncio.current_task()
        for task in tuple(self.tasks):
            if task is not current and (include_busy or task not in self.busy_tasks):
                task.cancel()

    async def close(self):
        self.closed = True
        self.revoke(include_busy=True)
        await asyncio.gather(*tuple(self.tasks), return_exceptions=True)

    def _eligible(self, client, token, actor):
        owner = self.owner
        if self.closed or owner._music_auth_closed:
            return None
        platform = owner._account_platform()
        if (
            platform is None
            or getattr(platform, "client", None) is not client
            or token != str(platform.config.get("kook_bot_token", "") or "").strip()
            or not token
            or getattr(getattr(client, "config", None), "token", token) != token
            or owner.config.get("music_auth_enabled", True) is not True
            or actor not in owner._music_auth_admin_ids
            or actor
            not in {
                str(value).strip()
                for value in owner.config.get("music_auth_admin_ids", [])
                if not isinstance(value, bool)
            }
        ):
            return None
        return platform

    def intercept(self, client, event, token):
        if event_field(event, "type") is None and event_field(event, "s") == 0:
            event = event_field(event, "d")
        if enum_value(event_field(event, "type")) == 255:
            return False
        raw = event_field(event, "content", "")
        markdown = event_field(event_field(event, "extra"), "kmarkdown")
        raw_markdown = event_field(markdown, "raw_content", "")
        candidates = [
            value.strip()
            for value in (raw_markdown, raw)
            if isinstance(value, str) and value.strip()
        ]
        if enum_value(event_field(event, "type")) == 9:
            candidates = [html.unescape(value) for value in candidates]
        text = next(
            (
                value
                for value in candidates
                if _COMMAND.match(value) or _secret_text(value)
            ),
            candidates[0] if candidates else "",
        )
        actor = str(event_field(event, "author_id", "") or "")
        key = (id(client), actor)
        command = _COMMAND.match(text)
        if key in self.windows and self.windows[key][0] < self.clock():
            self.windows.pop(key)
        private = enum_value(event_field(event, "channel_type")) == "PERSON"
        pending = private and key in self.windows
        sensitive = _secret_text(text)
        if not (command or pending or sensitive):
            return False
        # Once recognized, every failure stays consumed; never call the adapter.
        try:
            self.refresh()
            if not private:
                return True
            platform = self._eligible(client, token, actor)
            if (
                platform is None
                or not actor.isdigit()
                or actor == str(getattr(client, "bot_id", ""))
            ):
                return True
            message_id = str(event_field(event, "msg_id", "") or "")
            if not _MSG_ID.fullmatch(message_id):
                return True
            now = self.clock()
            self.seen = {k: t for k, t in self.seen.items() if now - t < 600}
            replay_key = (id(client), message_id)
            if replay_key in self.seen:
                return True
            if len(self.seen) >= 512:
                self.seen.pop(next(iter(self.seen)))
            self.seen[replay_key] = now
            recent = [t for t in self.rates.get(key, ()) if now - t < 60]
            if len(recent) >= 8:
                self.windows.pop(key, None)
                return True
            self.rates[key] = recent + [now]
            if len(self.tasks) >= 8:
                self.windows.pop(key, None)
                return True
            active_owner = self._operation_owner()
            other_import = active_owner is not None and active_owner != actor
            window = (
                self.windows.get(key)
                if command and command[1] == "音乐账号状态"
                else self.windows.pop(key, None)
            )
            if command and command[1] != "音乐账号状态" and not other_import:
                self.revoke()
            generation = self.generation
            task = asyncio.create_task(
                self._run(
                    client,
                    token,
                    actor,
                    platform,
                    message_id,
                    text,
                    command,
                    window,
                    generation,
                    other_import,
                )
            )
            self.tasks.add(task)
            if other_import:
                self.busy_tasks.add(task)
            if not other_import and (
                (not command and window is not None)
                or command
                and command[1] != "音乐账号状态"
            ):
                self.task_owners[task] = actor
            task.add_done_callback(self._task_done)
        except Exception:
            self.revoke()
        return True

    async def _run(
        self,
        client,
        token,
        actor,
        platform,
        message_id,
        text,
        command,
        window,
        generation,
        busy=False,
    ):
        try:
            async with asyncio.timeout(60):
                if busy and not (command and command[1] == "音乐账号状态"):
                    await self.owner._ensure_music_auth()
                    bot_id = self.owner._account_platform_id(platform)
                    if self._eligible(
                        client, token, actor
                    ) and self.owner._manual_auth_allowed(bot_id, actor):
                        await self.owner._notify_music_auth(
                            bot_id, actor, "其他管理员正在处理账号，请稍后重试。"
                        )
                    return
                await self._process(
                    client,
                    token,
                    actor,
                    platform,
                    message_id,
                    text,
                    command,
                    window,
                    generation,
                    busy,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Exception messages may contain submitted secrets; never log them.
            if generation == self.generation and self._eligible(client, token, actor):
                try:
                    await self.owner._notify_music_auth(
                        self.owner._account_platform_id(platform),
                        actor,
                        "网易云手动接入处理异常（PRIVATE_IMPORT）。请先发送 #音乐账号状态 确认结果，再决定是否重试。",
                    )
                except Exception:
                    pass

    async def _process(
        self,
        client,
        token,
        actor,
        platform,
        message_id,
        text,
        command,
        window,
        generation,
        busy=False,
    ):
        owner = self.owner
        manager = await owner._ensure_music_auth()
        bot_id = owner._account_platform_id(platform)

        def current():
            return (
                generation == self.generation
                and self._eligible(client, token, actor) is platform
                and owner._music_auth is manager
                and owner._manual_auth_allowed(bot_id, actor)
            )

        if manager is None or not current():
            return
        if busy and not (command and command[1] == "音乐账号状态"):
            await owner._notify_music_auth(
                bot_id, actor, "其他管理员正在校验账号，请稍后重试。"
            )
            return
        if command:
            name = command[1]
            if name.lower() == "音乐cookie":
                name = "音乐Cookie"
            argument = text[command.end() :].strip()
            if name in {"音乐Cookie", "音乐验证"}:
                match = re.match(
                    r"(?:网易云|netease)(?:\s+|$)", argument, re.IGNORECASE
                )
                if not match:
                    await owner._notify_music_auth(bot_id, actor, GUIDE)
                    return
                ok, _ = await manager.prepare_manual_handoff(bot_id, actor)
                if not current():
                    return
                if not ok:
                    await owner._notify_music_auth(
                        bot_id, actor, "其他管理员正在处理账号登录，请稍后重试。"
                    )
                    return
                payload = argument[match.end() :].strip()
                if not payload:
                    try:
                        ttl = max(
                            30,
                            min(
                                600,
                                int(owner.config.get("music_auth_cookie_timeout", 180)),
                            ),
                        )
                    except (TypeError, ValueError, OverflowError):
                        ttl = 180
                    self.windows[(id(client), actor)] = (self.clock() + ttl, generation)
                    await owner._notify_music_auth(
                        bot_id, actor, f"已开启 {ttl} 秒一次性接收窗口。\n" + GUIDE
                    )
                    return
                text = payload
            else:
                event = SimpleNamespace(
                    get_platform_name=lambda: "kook",
                    stop_event=lambda: None,
                    is_private_chat=lambda: True,
                    get_sender_id=lambda: actor,
                    get_platform_id=lambda: platform.meta().id,
                    client=client,
                    message_str=text,
                )
                reply = await owner._music_auth_command(event, name, intake=True)
                if reply and current():
                    await owner._notify_music_auth(bot_id, actor, reply)
                return
        elif window is None or window[0] < self.clock() or window[1] != generation:
            await owner._notify_music_auth(
                bot_id,
                actor,
                "未开启窗口或窗口已过期；请先发送 #音乐Cookie 网易云，再发送完整 Cookie。",
            )
            return
        if not current():
            return
        operation = (actor, object())
        self.import_owner = operation
        try:
            ok, detail = await owner._import_netease_cookie(
                bot_id, actor, text, authorized=current
            )
        finally:
            if self.import_owner is operation:
                self.import_owner = None
        if not current():
            return
        if ok:
            self.revoke()
            try:
                await owner._notify_music_auth(bot_id, actor, detail)
            except Exception:
                pass
            try:
                session = await _get_session()
                await _post(
                    session, token, "direct-message/delete", json={"msg_id": message_id}
                )
            except Exception:
                pass
        else:
            await owner._notify_music_auth(bot_id, actor, detail)
