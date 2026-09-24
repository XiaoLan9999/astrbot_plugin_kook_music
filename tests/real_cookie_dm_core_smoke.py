"""Offline private-cookie intake smoke against real AstrBot KOOK boundaries.

Run in a fresh process with the AstrBot checkout and plugins parent in PYTHONPATH.
Both cwd and ASTRBOT_ROOT must be the dedicated core-smoke directory. Imports,
real KOOK event parsing, client dispatch, adapter conversion, queueing and event
logging run with socket/DNS disabled. Account validation and outbound messages
are synthetic test doubles; no account storage, QR codes or live services are used.
"""

import asyncio
import json
import logging
import os
import socket
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

if not os.environ.get("ASTRBOT_ROOT"):
    raise RuntimeError("Set ASTRBOT_ROOT to an isolated test directory")
ROOT = Path(os.environ["ASTRBOT_ROOT"]).resolve()
if ROOT != Path.cwd().resolve() or ROOT.name != "core-smoke":
    raise RuntimeError("Run with cwd and ASTRBOT_ROOT equal to dedicated core-smoke")
os.environ["ASTRBOT_DISABLE_METRICS"] = "1"


def deny_network(*_args, **_kwargs):
    raise AssertionError("Network is forbidden in private-cookie real-core smoke")


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


async def smoke():
    from astrbot import logger
    from astrbot.core.config import VERSION
    from astrbot.core.event_bus import EventBus
    from astrbot.core.platform.sources.kook.kook_adapter import KookPlatformAdapter
    from astrbot.core.platform.sources.kook.kook_client import KookClient
    from astrbot.core.platform.sources.kook.kook_event import KookEvent
    from astrbot.core.platform.sources.kook.kook_types import (
        KookMessageEventData,
        KookWebsocketEvent,
    )
    from astrbot.core.utils.metrics import Metric
    from astrbot_plugin_kook_music.kook_events import KookEventBridge
    from astrbot_plugin_kook_music.main import KookMusicPlugin
    from astrbot_plugin_kook_music.music_auth import integration, private_cookie

    checks = 0

    def check(condition, label):
        nonlocal checks
        if not condition:
            raise AssertionError(label)
        checks += 1

    check(VERSION == "4.28.1", "smoke uses the intended real AstrBot version")
    check(Metric._is_disabled(), "metrics disabled before importing core")
    queue = asyncio.Queue()
    adapters = [
        KookPlatformAdapter(
            {
                "id": configured_id,
                "enable": True,
                "kook_bot_token": "synthetic-token-" + configured_id,
            },
            {},
            queue,
        )
        for configured_id in ("cookie-smoke-main", "cookie-smoke-other")
    ]
    primary, other = adapters
    for index, adapter in enumerate(adapters):
        adapter.client._bot_id = str(900001 + index)
        check(isinstance(adapter.client, KookClient), "real KOOK client instance")
    check(
        primary.meta().id == other.meta().id == "kook",
        "actual KOOK metadata collision cannot serve as account binding",
    )

    plugin = object.__new__(KookMusicPlugin)
    plugin.config = {
        "music_auth_enabled": True,
        "music_auth_admin_ids": ["123"],
        "music_auth_kook_bot_id": primary.config["id"],
    }
    plugin.context = SimpleNamespace(
        platform_manager=SimpleNamespace(platform_insts=adapters)
    )
    plugin._configure_music_auth()
    now = [1000.0]
    plugin._music_cookie_intake.clock = lambda: now[0]
    imports = []

    async def import_credentials(provider, credential, bot_id, actor, *, authorized):
        check(authorized() is True, "real mixin supplies fresh authorization")
        imports.append((provider, credential, bot_id, actor))
        return True, "Synthetic account accepted; no persistent storage used."

    manager = SimpleNamespace(
        import_credentials=AsyncMock(side_effect=import_credentials),
        prepare_manual_handoff=AsyncMock(return_value=(True, "synthetic handoff")),
        start_login=AsyncMock(return_value=(True, "synthetic login started")),
        cancel_login=AsyncMock(return_value=(True, "synthetic login cancelled")),
        logout=AsyncMock(return_value=(True, "synthetic account removed")),
        status=Mock(return_value="synthetic account status"),
        close=AsyncMock(),
    )
    plugin._music_auth = manager
    plugin._music_auth_binding = (
        primary.config["id"],
        primary.config["kook_bot_token"],
    )
    plugin._ensure_music_auth = AsyncMock(return_value=manager)
    originals = []
    for adapter in adapters:
        callback = adapter.client.event_callback
        spy = AsyncMock(wraps=callback)
        adapter.client.event_callback = spy
        originals.append(spy)
    on_system_event = AsyncMock()
    bridge = KookEventBridge(on_system_event, plugin._intercept_music_auth)
    bridge.sync(adapters)
    retained_callback = primary.client.event_callback
    bus = EventBus(queue, {}, SimpleNamespace())
    capture = Capture()
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(capture)
    sequence = [0]

    def event_for(text, *, actor="123", private=True, raw_content=None):
        sequence[0] += 1
        wire = {
            "s": 0,
            "sn": sequence[0],
            "d": {
                "channel_type": "PERSON" if private else "GROUP",
                "type": 9,
                "target_id": "900001" if private else "synthetic-channel",
                "author_id": actor,
                "content": text,
                "msg_id": "cookie-smoke-" + str(sequence[0]),
                "msg_timestamp": 1000 + sequence[0],
                "nonce": "synthetic-nonce",
                "from_type": 1,
                "extra": {
                    "type": 9,
                    "guild_id": None if private else "synthetic-guild",
                    "kmarkdown": {
                        "raw_content": text if raw_content is None else raw_content,
                        "mention_part": [],
                        "mention_role_part": [],
                    },
                },
            },
        }
        event = KookWebsocketEvent.from_json(json.dumps(wire))
        check(isinstance(event, KookWebsocketEvent), "real websocket model parsed")
        check(isinstance(event.data, KookMessageEventData), "real callback data model")
        return event

    async def settle():
        while plugin._music_cookie_intake.tasks:
            await asyncio.wait_for(
                asyncio.gather(*tuple(plugin._music_cookie_intake.tasks)), 2
            )
        await asyncio.sleep(0)

    async def dispatch(text, *, adapter=primary, wait=True, **kwargs):
        event = event_for(text, **kwargs)
        await adapter.client._handle_signal(event)
        check(adapter.client.last_sn == event.sn, "real client updates sequence number")
        if wait:
            await settle()
        return event

    async def consumed(text, *, adapter=primary, **kwargs):
        callback_count = sum(item.await_count for item in originals)
        system_count = on_system_event.await_count
        await dispatch(text, adapter=adapter, **kwargs)
        check(queue.empty(), "private account input never reaches event bus queue")
        check(
            sum(item.await_count for item in originals) == callback_count,
            "private account input never reaches original adapter callback",
        )
        check(
            on_system_event.await_count == system_count,
            "private account input does not reach other bridge handlers",
        )

    async def normal(text, *, adapter=primary, **kwargs):
        before = sum(item.await_count for item in originals)
        await dispatch(text, adapter=adapter, **kwargs)
        check(queue.qsize() == 1, "ordinary message reaches real adapter queue")
        event = queue.get_nowait()
        check(isinstance(event, KookEvent), "real adapter creates real KookEvent")
        check(event.message_str == text, "ordinary message content preserved")
        check(
            sum(item.await_count for item in originals) == before + 1,
            "ordinary message invokes original callback exactly once",
        )
        bus._print_event(event, "synthetic-config")
        check(
            any(text in message for message in capture.messages),
            "ordinary message reaches actual EventBus logging boundary",
        )
        return event

    markers = (
        "synthetic-header-secret-only",
        "synthetic-json-secret-only",
        "synthetic-netscape-secret-only",
        "synthetic-blocked-secret-only",
        "synthetic-raw-secret-only",
        "synthetic-closing-secret-only",
    )
    private_send = AsyncMock(return_value=("synthetic-message-id", None))
    deletion = AsyncMock(return_value={})
    try:
        with (
            patch.object(integration, "send_private_auth", private_send),
            patch.object(
                private_cookie, "_get_session", AsyncMock(return_value=object())
            ),
            patch.object(private_cookie, "_post", deletion),
        ):
            await normal("ordinary synthetic direct message")
            await normal("ordinary synthetic alternate-instance message", adapter=other)

            formats = (
                ("Header String", f"MUSIC_U={markers[0]}; __csrf=synthetic-csrf"),
                (
                    "JSON",
                    "```json\n"
                    + json.dumps(
                        [
                            {
                                "name": "MUSIC_U",
                                "value": markers[1],
                                "domain": ".music.163.com",
                                "path": "/",
                            },
                            {
                                "name": "__csrf",
                                "value": "synthetic-csrf",
                                "domain": ".music.163.com",
                                "path": "/",
                            },
                        ]
                    )
                    + "\n```",
                ),
                (
                    "Netscape",
                    "```\n# Netscape HTTP Cookie File\n"
                    f".music.163.com\tTRUE\t/\tTRUE\t0\tMUSIC_U\t{markers[2]}\n"
                    ".music.163.com\tTRUE\t/\tTRUE\t0\t__csrf\tsynthetic-csrf\n```",
                ),
            )
            for index, (label, payload) in enumerate(formats):
                now[0] += 61
                before = len(imports)
                if index == 0:
                    await consumed("#音乐Cookie 网易云\n" + payload)
                else:
                    await consumed("#音乐Cookie 网易云")
                    check(
                        bool(plugin._music_cookie_intake.windows),
                        label + " window opens",
                    )
                    if index == 1:
                        windows = dict(plugin._music_cookie_intake.windows)
                        replies = private_send.await_count
                        group_event = await normal(
                            "#点歌 synthetic-group-song", private=False
                        )
                        check(
                            not group_event.is_private_chat(),
                            "actual adapter preserves group scope during private intake",
                        )
                        check(
                            plugin._music_cookie_intake.windows == windows,
                            "ordinary group song request preserves private intake window",
                        )
                        check(
                            private_send.await_count == replies,
                            "ordinary group request does not trigger account replies",
                        )
                        check(
                            len(imports) == before,
                            "ordinary group request does not start account validation",
                        )
                    await consumed(payload)
                check(len(imports) == before + 1, label + " reaches account validator")
                check(
                    imports[-1]
                    == (
                        "netease",
                        {
                            "cookies": {
                                "MUSIC_U": markers[index],
                                "__csrf": "synthetic-csrf",
                            }
                        },
                        primary.config["id"],
                        "123",
                    ),
                    label + " preserves identity and first-party cookie values",
                )
                check(
                    not plugin._music_cookie_intake.windows, label + " consumes window"
                )
                check(
                    deletion.await_count == index + 1,
                    label + " attempts private recall",
                )

            now[0] += 61
            for command, method, args in (
                (
                    "#音乐登录 QQ",
                    "start_login",
                    ("qq", "qq", primary.config["id"], "123"),
                ),
                (
                    "#取消音乐登录 网易云",
                    "cancel_login",
                    ("netease", primary.config["id"], "123"),
                ),
                (
                    "#退出音乐账号 网易云",
                    "logout",
                    ("netease", primary.config["id"], "123"),
                ),
            ):
                await consumed(command)
                check(
                    getattr(manager, method).await_args.args == args,
                    "account command is privately routed before event bus",
                )
            await consumed("#音乐账号状态")
            check(
                manager.status.call_count == 1, "status command uses private response"
            )

            blocked_payload = "#音乐Cookie 网易云\nMUSIC_U=" + markers[3]
            for kwargs in (
                {"private": False},
                {"actor": "456"},
                {"adapter": other},
                {"actor": primary.client.bot_id},
            ):
                before = len(imports)
                replies = private_send.await_count
                await consumed(blocked_payload, **kwargs)
                check(
                    len(imports) == before, "untrusted scope cannot import credentials"
                )
                check(
                    private_send.await_count == replies,
                    "untrusted scope gets no account reply",
                )

            now[0] += 61
            await consumed("MUSIC_U=" + markers[3])
            check(
                len(imports) == 3, "unsolicited secret without a window is not imported"
            )
            await consumed("#音乐Cookie 网易云")
            now[0] += 181
            await consumed("MUSIC_U=" + markers[3])
            check(len(imports) == 3, "expired window cannot import credentials")

            now[0] += 61
            await consumed("#音乐Cookie 网易云")
            plugin.config["music_auth_admin_ids"] = []
            await consumed("MUSIC_U=" + markers[3])
            check(len(imports) == 3, "live whitelist revocation blocks imports")
            plugin.config["music_auth_admin_ids"] = ["123"]
            plugin._music_cookie_intake.revoke()

            now[0] += 61
            plugin.config["music_auth_enabled"] = False
            replies = private_send.await_count
            await consumed(blocked_payload)
            check(len(imports) == 3, "disabled account feature cannot import")
            check(
                private_send.await_count == replies, "disabled feature does not reply"
            )
            plugin.config["music_auth_enabled"] = True

            await consumed('#音乐Cookie 网易云\n{"MUSIC_U":"' + markers[3])
            check(len(imports) == 3, "malformed JSON does not reach validator")
            check(
                "COOKIE_FORMAT" in private_send.await_args.args[2],
                "malformed secret input produces only a safe fixed error",
            )

            manager.import_credentials.side_effect = RuntimeError(markers[3])
            await consumed(blocked_payload)
            check(len(imports) == 3, "validator exception cannot report success")
            check(
                "PRIVATE_IMPORT" in private_send.await_args.args[2],
                "validator exception is replaced with a safe fixed error",
            )
            manager.import_credentials.side_effect = import_credentials

            now[0] += 61
            await consumed(
                "safe rendered placeholder", raw_content="MUSIC_U=" + markers[4]
            )
            check(len(imports) == 3, "raw markdown secret cannot bypass interception")

            for call in private_send.await_args_list:
                check(
                    call.args[0] == primary.config["kook_bot_token"],
                    "private replies use bound bot",
                )
                check(
                    call.args[1] == "123", "private replies target whitelist owner only"
                )
                check(
                    not any(marker in str(call) for marker in markers),
                    "private reply does not echo credential",
                )
            check(
                not any(
                    marker in message
                    for marker in markers
                    for message in capture.messages
                ),
                "no credential marker reaches core or plugin logs",
            )

            now[0] += 61
            entered, cancelled = asyncio.Event(), asyncio.Event()

            async def blocked_import(*args, **kwargs):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

            manager.import_credentials.side_effect = blocked_import
            await dispatch("#音乐Cookie 网易云\nMUSIC_U=" + markers[5], wait=False)
            await asyncio.wait_for(entered.wait(), 2)
            replies = private_send.await_count
            await asyncio.wait_for(plugin._close_music_auth(), 2)
            check(
                cancelled.is_set(),
                "unload cancels in-flight private credential validation",
            )
            check(
                not plugin._music_cookie_intake.tasks, "unload leaves no intake tasks"
            )
            check(
                not plugin._music_cookie_intake.windows,
                "unload revokes receive windows",
            )
            check(plugin._music_auth is None, "unload clears manager binding")
            check(manager.close.await_count == 1, "unload closes manager once")
            check(
                private_send.await_count == replies,
                "unload cannot send stale success reply",
            )
            check(queue.empty(), "closing operation never enters core queue")
            await consumed("MUSIC_U=" + markers[5])
            check(
                private_send.await_count == replies,
                "closed intake consumes secrets without creating new replies",
            )

            bridge.close()
            check(
                primary.client.event_callback is originals[0],
                "unload restores original callback",
            )
            await normal("ordinary synthetic message after bridge unload")
            before = originals[0].await_count
            event = event_for("ordinary retained wrapper passthrough")
            await retained_callback(event.data)
            check(
                originals[0].await_count == before + 1,
                "retained closed wrapper only passes through",
            )
            queued = queue.get_nowait()
            check(
                isinstance(queued, KookEvent),
                "closed wrapper still preserves core event type",
            )
            check(queue.empty(), "no extra events left in real core queue")
    finally:
        bridge.close()
        await plugin._close_music_auth()
        for adapter in adapters:
            await adapter.client.close()
        logger.removeHandler(capture)
        logger.setLevel(old_level)

    print(
        f"PASS: {checks} private-cookie real-core checks; AstrBot {VERSION}; "
        "real KOOK websocket/client/adapter/queue/log boundaries; "
        "synthetic account validator only; no network or account storage"
    )


async def guarded_smoke():
    with (
        patch.object(socket.socket, "connect", deny_network),
        patch.object(socket.socket, "connect_ex", deny_network),
        patch.object(socket, "getaddrinfo", deny_network),
        patch.object(socket, "gethostbyname", deny_network),
        patch.object(socket, "gethostbyname_ex", deny_network),
    ):
        await smoke()


if __name__ == "__main__":
    asyncio.run(guarded_smoke())
