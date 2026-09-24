"""Offline authentication integration smoke using actual AstrBot core classes.

Run in a fresh process with the AstrBot checkout and plugins parent in PYTHONPATH.
ASTRBOT_ROOT and cwd must point to the same isolated test directory. Never run in
the production AstrBot directory. This does not initialize the plugin, install
FFmpeg, open credentials, create QR codes, or connect to any service.
"""

import asyncio
import importlib
import json
import os
import socket
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

if not os.environ.get("ASTRBOT_ROOT"):
    raise RuntimeError("Set ASTRBOT_ROOT to an isolated test directory")
ROOT = Path(os.environ["ASTRBOT_ROOT"]).resolve()
if ROOT != Path.cwd().resolve():
    raise RuntimeError("Run with cwd equal to the isolated ASTRBOT_ROOT")
if ROOT.name != "core-smoke":
    raise RuntimeError("Use a dedicated core-smoke directory, never production")
os.environ["ASTRBOT_DISABLE_METRICS"] = "1"

from astrbot.api.message_components import Plain  # noqa: E402
from astrbot.api.platform import (  # noqa: E402
    AstrBotMessage,
    AstrMessageEvent,
    MessageMember,
    MessageType,
    PlatformMetadata,
)
from astrbot.api.star import Star  # noqa: E402
from astrbot.core.config import VERSION, AstrBotConfig  # noqa: E402
from astrbot.core.pipeline.context_utils import call_handler  # noqa: E402
from astrbot.core.platform.sources.kook.kook_config import KookConfig  # noqa: E402
from astrbot.core.platform.sources.kook.kook_event import KookEvent  # noqa: E402
from astrbot.core.star.filter.command import CommandFilter  # noqa: E402
from astrbot.core.star.star import star_map  # noqa: E402
from astrbot.core.star.star_handler import star_handlers_registry  # noqa: E402


def deny_network(*_args, **_kwargs):
    raise AssertionError("Network is forbidden in authentication real-core smoke")


class FakeKookPlatform:
    def __init__(self, configured_id, token):
        self.config = {"id": configured_id, "kook_bot_token": token, "enable": True}
        self.kook_config = KookConfig.from_dict(self.config)
        self.client = SimpleNamespace(send_text=AsyncMock(), upload_asset=AsyncMock())

    def meta(self):
        return PlatformMetadata(
            "kook", "Offline real KookConfig semantics", self.kook_config.id
        )


def event_for(adapter, text, *, user="123", private=True, platform="kook", client=None):
    message = AstrBotMessage()
    message.type = MessageType.FRIEND_MESSAGE if private else MessageType.GROUP_MESSAGE
    message.message_id = "synthetic-message"
    message.self_id = "synthetic-bot-user"
    message.session_id = user if private else "synthetic-channel"
    message.group_id = "" if private else "synthetic-channel"
    message.sender = MessageMember(user_id=user, nickname="Synthetic Administrator")
    message.message = [Plain(text)]
    message.message_str = text
    message.raw_message = {
        "extra": {"guild_id": "synthetic-guild"} if not private else {}
    }
    metadata = PlatformMetadata(
        name=platform, description="Offline smoke", id=adapter.meta().id
    )
    if platform == "kook":
        event = KookEvent(
            text, message, metadata, message.session_id, client or adapter.client
        )
    else:
        event = AstrMessageEvent(text, message, metadata, message.session_id)
    # WakingStage removes the wake prefix before CommandFilter runs.
    event.is_at_or_wake_command = True
    return event


async def smoke():
    checks = 0

    def check(condition, label):
        nonlocal checks
        if not condition:
            raise AssertionError(label)
        checks += 1

    module = importlib.import_module("astrbot_plugin_kook_music.main")
    klass = module.KookMusicPlugin
    check(issubclass(klass, Star), "actual Star subclass")
    check(star_map[module.__name__].star_cls_type is klass, "actual Star registration")
    expected = {
        "音乐登录": "on_music_login",
        "音乐账号状态": "on_music_account_status",
        "取消音乐登录": "on_music_login_cancel",
        "退出音乐账号": "on_music_account_logout",
    }
    handlers = {}
    for metadata in star_handlers_registry.get_handlers_by_module_name(module.__name__):
        for command_filter in metadata.event_filters:
            if (
                isinstance(command_filter, CommandFilter)
                and command_filter.command_name in expected
            ):
                command = command_filter.command_name
                check(command not in handlers, "command registered only once")
                check(
                    metadata.handler_name == expected[command],
                    "decorator handler mapping",
                )
                check(
                    command_filter.handler_params == {},
                    "handler uses the raw remaining text",
                )
                handlers[command] = (metadata, command_filter)
    check(set(handlers) == set(expected), "four account command decorators registered")

    schema = json.loads(
        Path(module.__file__).with_name("_conf_schema.json").read_text(encoding="utf-8")
    )
    with tempfile.TemporaryDirectory(prefix="auth-schema-", dir=ROOT) as directory:
        defaults = AstrBotConfig(str(Path(directory) / "config.json"), schema=schema)
        check(defaults["music_auth_admin_ids"] == [], "empty admin whitelist default")
        check(
            defaults["music_auth_kook_bot_id"] == "",
            "explicit bot binding optional only for one adapter",
        )
        check(defaults["music_auth_check_interval"] == 1800, "real schema check period")
        check(
            defaults["music_auth_notice_cooldown"] == 21600,
            "real schema notice dedup period",
        )
        check(defaults["music_auth_login_timeout"] == 180, "real schema QR deadline")
        check(defaults["music_auth_cookie_timeout"] == 180, "real schema private import deadline")
        check(
            not any(
                "cookie" in key.lower()
                for key in defaults
                if key.startswith("music_auth") and key != "music_auth_cookie_timeout"
            ),
            "no raw cookie config field",
        )

    client = SimpleNamespace(send_text=AsyncMock(), upload_asset=AsyncMock())
    metadata = PlatformMetadata(
        name="kook", description="Offline smoke", id="auth-smoke-bot"
    )
    adapter = SimpleNamespace(
        meta=lambda: metadata,
        client=client,
        config={"kook_bot_token": "synthetic-only-token"},
    )
    context = SimpleNamespace(
        platform_manager=SimpleNamespace(platform_insts=[adapter])
    )
    plugin = klass(
        context,
        {"music_auth_admin_ids": ["123"], "music_auth_kook_bot_id": metadata.id},
    )
    check(
        plugin.data_dir.resolve().is_relative_to(ROOT), "constructor data dir isolated"
    )
    check(
        not (plugin.data_dir / "music_accounts").exists(),
        "constructor did not read account storage",
    )
    check(plugin._music_auth_enabled, "explicit whitelist enables account module")
    check(
        plugin.searcher.account_resolver == plugin._resolve_account_audio,
        "actual searcher callback wired",
    )
    check(plugin._adapter_sync_task is None, "initialize was not called")
    manager = SimpleNamespace(
        start_login=AsyncMock(return_value=(True, "synthetic login started")),
        cancel_login=AsyncMock(return_value=(True, "synthetic login cancelled")),
        logout=AsyncMock(return_value=(True, "synthetic account logged out")),
        status=Mock(return_value="synthetic safe status"),
        close=AsyncMock(),
    )
    plugin._ensure_music_auth = AsyncMock(return_value=manager)

    async def dispatch(command, event):
        handler, command_filter = handlers[command]
        check(command_filter.filter(event, {}), "actual CommandFilter accepts command")
        check(
            event.get_extra("parsed_params") == {},
            "actual parser leaves no unexpected kwargs",
        )
        transport = getattr(getattr(event, "client", None), "send_text", None)
        before = transport.await_count if transport is not None else 0
        bound_handler = getattr(plugin, handler.handler_name)
        async for _ in call_handler(
            event, bound_handler, **event.get_extra("parsed_params")
        ):
            pass
        sent = transport.await_args_list[before:] if transport is not None else []
        for call in sent:
            check(
                isinstance(call.args[1], str),
                "actual KookEvent sends plain response text",
            )
            check(
                call.args[2] == event.message_obj.type,
                "actual KookEvent retains DM/group transport type",
            )
            check(
                event._has_send_oper,
                "real event super send records successful operation",
            )
        return sent

    for argument, provider, method in (
        ("QQ", "qq", "qq"),
        ("微信", "qq", "wechat"),
        ("网易云", "netease", "netease"),
    ):
        event = event_for(adapter, "音乐登录 " + argument)
        check(
            isinstance(event, KookEvent) and event.is_private_chat(),
            "real KOOK private event",
        )
        check(event.get_platform_id() == metadata.id, "real instance accessor")
        check(event.get_sender_id() == "123", "real sender accessor")
        results = await dispatch("音乐登录", event)
        check(
            len(results) == 1 and event.is_stopped(),
            "command stops later pipeline and replies",
        )
        manager.start_login.assert_awaited_with(provider, method, metadata.id, "123")
        check(True, "backend login mode mapping")
    for command, argument, name, provider in (
        ("取消音乐登录", "qq", "cancel_login", "qq"),
        ("退出音乐账号", "网易云", "logout", "netease"),
    ):
        event = event_for(adapter, command + " " + argument)
        check(len(await dispatch(command, event)) == 1, "cancel/logout reply")
        getattr(manager, name).assert_awaited_with(provider, metadata.id, "123")
        check(event.is_stopped(), "cancel/logout does not call LLM")
    check(
        len(await dispatch("音乐账号状态", event_for(adapter, "音乐账号状态"))) == 1,
        "safe account status reply",
    )
    manager.status.assert_called_once()

    def counts():
        return (
            manager.start_login.await_count,
            manager.cancel_login.await_count,
            manager.logout.await_count,
            manager.status.call_count,
        )

    for command in expected:
        text = command + (" qq" if command != "音乐账号状态" else "")
        baseline = counts()
        for blocked in (
            event_for(adapter, text, private=False),
            event_for(adapter, text, user="456"),
            event_for(
                adapter,
                text,
                client=SimpleNamespace(send_text=AsyncMock(), upload_asset=AsyncMock()),
            ),
        ):
            results = await dispatch(command, blocked)
            check(
                len(results) == 1 and blocked.is_stopped(),
                "group/foreign admin/stale client blocked",
            )
            check(counts() == baseline, "blocked command never touched account manager")
        foreign = event_for(adapter, text, platform="aiocqhttp")
        check(await dispatch(command, foreign) == [], "non-KOOK silent")
        check(
            not foreign.is_stopped(),
            "non-KOOK does not consume other platform commands",
        )
        check(counts() == baseline, "non-KOOK never touched account manager")

    cold = event_for(adapter, "音乐登录 qq")
    cold.is_at_or_wake_command = False
    check(
        not handlers["音乐登录"][1].filter(cold, {}), "real wake-command gate respected"
    )
    before = manager.start_login.await_count
    invalid = event_for(adapter, "音乐登录 not-a-provider")
    check(
        len(await dispatch("音乐登录", invalid)) == 1, "unknown login method shows help"
    )
    check(manager.start_login.await_count == before, "unknown method never forwarded")

    second = SimpleNamespace(
        meta=lambda: PlatformMetadata("kook", "other", "other-bot"),
        client=SimpleNamespace(send_text=AsyncMock(), upload_asset=AsyncMock()),
        config={"kook_bot_token": "other-token"},
    )
    context.platform_manager.platform_insts.append(second)
    check(
        plugin._account_platform() is adapter,
        "explicit selection remains correct with two bots",
    )
    event = event_for(second, "音乐登录 qq")
    baseline = counts()
    check(
        len(await dispatch("音乐登录", event)) == 1, "different real instance refused"
    )
    check(counts() == baseline, "different instance did not access accounts")
    plugin.config["music_auth_kook_bot_id"] = ""
    check(plugin._account_platform() is None, "ambiguous selection rejected")
    check(
        len(await dispatch("音乐登录", event_for(adapter, "音乐登录 qq"))) == 1,
        "ambiguous command rejected",
    )
    check(counts() == baseline, "ambiguous instance did not access accounts")
    plugin.config["music_auth_kook_bot_id"] = metadata.id

    # Real 4.28.1 KookConfig ignores configured id, so all events can say "kook".
    selected = FakeKookPlatform("configured-one", "synthetic-selected-token")
    unselected = FakeKookPlatform("configured-two", "synthetic-other-token")
    check(
        selected.kook_config.id == "kook",
        "actual KookConfig.from_dict ignores configured id",
    )
    check(
        unselected.meta().id == selected.meta().id,
        "two real KookConfig instances share metadata id",
    )
    check(
        selected.config["id"] != unselected.config["id"],
        "configuration instance IDs remain distinct",
    )
    context.platform_manager.platform_insts = [unselected, selected]
    plugin.config["music_auth_kook_bot_id"] = "configured-one"
    check(
        plugin._account_platform() is selected,
        "configuration ID selects correct platform despite metadata collision",
    )
    manager.start = AsyncMock()
    with patch(
        "astrbot_plugin_kook_music.music_auth.manager.AuthManager", return_value=manager
    ):
        check(
            await klass._ensure_music_auth(plugin) is manager,
            "real manager startup uses configured ID",
        )
        await asyncio.sleep(0)
    manager.start.assert_awaited_once_with("configured-one")
    check(
        plugin._music_auth_binding == ("configured-one", "synthetic-selected-token"),
        "manager binding combines config ID and its token",
    )
    for argument, provider, method in (
        ("QQ", "qq", "qq"),
        ("微信", "qq", "wechat"),
        ("网易云", "netease", "netease"),
    ):
        selected_event = event_for(selected, "音乐登录 " + argument)
        check(
            selected_event.get_platform_id() == "kook",
            "real KOOK event metadata ID differs from config ID",
        )
        check(
            len(await dispatch("音乐登录", selected_event)) == 1,
            "selected client delivers login response",
        )
        manager.start_login.assert_awaited_with(
            provider, method, "configured-one", "123"
        )
        check(True, "manager command receives config ID not shared metadata ID")
    baseline = counts()
    for command in expected:
        text = command + (" qq" if command != "音乐账号状态" else "")
        check(
            len(await dispatch(command, event_for(unselected, text))) == 1,
            "same metadata ID but other client rejected",
        )
        check(counts() == baseline, "unselected client never reaches manager")
    with patch(
        "astrbot_plugin_kook_music.music_auth.integration.send_private_auth",
        new_callable=AsyncMock,
    ) as private_send:
        await plugin._notify_music_auth("configured-one", "123", "synthetic notice")
        private_send.assert_awaited_once_with(
            "synthetic-selected-token", "123", "synthetic notice", None
        )
        check(True, "proactive notice uses selected platform token")
        for wrong_id in ("configured-two", "kook"):
            try:
                await plugin._notify_music_auth(wrong_id, "123", "must not send")
            except RuntimeError:
                check(
                    True,
                    "unselected config ID or shared metadata ID cannot route notices",
                )
            else:
                raise AssertionError("wrong bot ID received an account notice")
        check(private_send.await_count == 1, "no notice sent through unselected token")
    await plugin._music_auth_start_task
    plugin._music_auth_start_task = None
    context.platform_manager.platform_insts = [adapter, second]
    plugin.config["music_auth_kook_bot_id"] = metadata.id

    plugin._music_auth = manager
    plugin._music_auth_binding = (metadata.id, "synthetic-only-token")
    plugin._music_auth_start_task = asyncio.create_task(asyncio.sleep(3600))
    start_task = plugin._music_auth_start_task
    plugin._adapter_sync_task = asyncio.create_task(asyncio.sleep(3600))
    sync_task = plugin._adapter_sync_task
    plugin._button_handler_task = asyncio.create_task(asyncio.sleep(3600))
    button_task = plugin._button_handler_task
    for component in (
        plugin.searcher,
        plugin.downloader,
        plugin.bilibili,
        plugin.playlist_importer,
    ):
        component.close = AsyncMock(wraps=component.close)
    plugin.voice_manager.leave_all = AsyncMock(wraps=plugin.voice_manager.leave_all)
    await plugin.terminate()
    check(
        plugin._music_auth_closed and plugin._music_auth is None,
        "actual terminate closes login subsystem",
    )
    manager.close.assert_awaited_once()
    check(
        start_task.cancelled() and sync_task.cancelled() and button_task.cancelled(),
        "actual terminate cancels background tasks",
    )
    for component in (
        plugin.searcher,
        plugin.downloader,
        plugin.bilibili,
        plugin.playlist_importer,
    ):
        component.close.assert_awaited_once()
        check(True, "real resource close awaited")
    plugin.voice_manager.leave_all.assert_awaited_once()
    check(
        client.send_text.await_count > 0,
        "stopped events still delivered replies through real KookEvent",
    )
    client.upload_asset.assert_not_awaited()
    check(
        not (plugin.data_dir / "music_accounts").exists(),
        "no account files created during smoke",
    )
    print(
        f"PASS: {checks} authentication real-core checks; AstrBot {VERSION}; no network, credentials, QR, voice, or initialization"
    )


async def guarded_smoke():
    with (
        patch.object(socket.socket, "connect", deny_network),
        patch.object(socket.socket, "connect_ex", deny_network),
        patch.object(socket, "getaddrinfo", deny_network),
    ):
        await smoke()


if __name__ == "__main__":
    asyncio.run(guarded_smoke())
