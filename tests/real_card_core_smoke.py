"""Offline card receipts and retirement through real AstrBot KOOK dispatch.

Run in a fresh process with the AstrBot checkout and plugins parent in PYTHONPATH.
Both cwd and ASTRBOT_ROOT must be an isolated directory named core-smoke. Socket
and DNS calls are blocked before importing AstrBot. No plugin initialization,
voice playback, account access, real HTTP request, or persistent ledger is used.
"""

import asyncio
import json
import os
import socket
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

if not os.environ.get("ASTRBOT_ROOT"):
    raise RuntimeError("Set ASTRBOT_ROOT to an isolated test directory")
ROOT = Path(os.environ["ASTRBOT_ROOT"]).resolve()
if ROOT != Path.cwd().resolve() or ROOT.name != "core-smoke":
    raise RuntimeError("Run with cwd and ASTRBOT_ROOT equal to dedicated core-smoke")
os.environ["ASTRBOT_DISABLE_METRICS"] = "1"


def deny_network(*_args, **_kwargs):
    raise AssertionError("Network is forbidden in card receipt real-core smoke")


class HangingRequest:
    def __init__(self, payload):
        self.payload = payload
        self.cancelled = False
        self.entered = asyncio.Event()

    async def __aenter__(self):
        self.entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def __aexit__(self, *_args):
        return False


class FakeHTTPSession:
    def __init__(self):
        self.calls = asyncio.Queue()
        self.requests = []

    def post(self, url, **kwargs):
        if not url.endswith("/message/create"):
            raise AssertionError("Only synthetic message/create is permitted")
        request = HangingRequest(kwargs["json"])
        self.requests.append(request)
        self.calls.put_nowait(request)
        return request


async def smoke():
    from astrbot.core.config import VERSION
    from astrbot.core.platform.sources.kook.kook_adapter import KookPlatformAdapter
    from astrbot.core.platform.sources.kook.kook_client import KookClient
    from astrbot.core.platform.sources.kook.kook_event import KookEvent
    from astrbot.core.platform.sources.kook.kook_types import (
        KookMessageEventData,
        KookWebsocketEvent,
    )
    from astrbot.core.utils.metrics import Metric
    from astrbot_plugin_kook_music import card_builder, kook_api
    from astrbot_plugin_kook_music.card_ledger import CardLedger
    from astrbot_plugin_kook_music.kook_events import KookEventBridge
    from astrbot_plugin_kook_music.main import KookMusicPlugin
    from astrbot_plugin_kook_music.music.model import Song

    checks = 0

    def check(condition, label):
        nonlocal checks
        if not condition:
            raise AssertionError(label)
        checks += 1

    check(VERSION == "4.28.1", "intended real AstrBot version")
    check(Metric._is_disabled(), "metrics disabled before importing core")
    queue = asyncio.Queue()
    adapter = KookPlatformAdapter(
        {"id": "card-smoke", "enable": True, "kook_bot_token": "synthetic-card-token"},
        {},
        queue,
    )
    adapter.client._bot_id = "900001"
    check(isinstance(adapter.client, KookClient), "real KOOK client")
    token, bot_id = adapter.config["kook_bot_token"], adapter.client.bot_id
    original = AsyncMock(wraps=adapter.client.event_callback)
    adapter.client.event_callback = original
    plugin = object.__new__(KookMusicPlugin)
    plugin.config = {"music_auth_enabled": False, "music_auth_admin_ids": []}
    plugin.context = SimpleNamespace(
        platform_manager=SimpleNamespace(platform_insts=[adapter])
    )
    plugin._configure_music_auth()
    plugin._kook_token = token
    plugin._card_ledger = CardLedger(None)
    plugin._card_locks = {}
    plugin._card_msg_ids = {}
    plugin._card_reconcile_needed = set()
    plugin._card_cleanup_wake = asyncio.Event()
    plugin._card_cleanup_closing = False
    plugin._card_cleanup_task = None
    plugin._button_click_queue = asyncio.Queue()
    plugin._voice_exit_tasks = set()
    plugin.voice_manager = SimpleNamespace(sessions={}, control=AsyncMock())
    bridge = KookEventBridge(
        plugin._handle_kook_system_event, plugin._intercept_gateway_event
    )
    bridge.sync([adapter])
    sequence = 0
    active_tasks = []
    fake_http = FakeHTTPSession()

    def event_for(
        content,
        *,
        message_type=10,
        actor=None,
        nonce="",
        channel="GROUP",
        target="synthetic-channel",
        message_id="synthetic-receipt",
        system=None,
    ):
        nonlocal sequence
        sequence += 1
        extra = {
            "type": message_type,
            "guild_id": "synthetic-guild" if channel == "GROUP" else None,
        }
        if message_type == 9:
            extra["kmarkdown"] = {
                "raw_content": content,
                "mention_part": [],
                "mention_role_part": [],
            }
        elif message_type == 255:
            extra = {
                "type": "deleted_message",
                "body": system or {"msg_id": message_id},
            }
        wire = {
            "s": 0,
            "sn": sequence,
            "d": {
                "channel_type": channel,
                "type": message_type,
                "target_id": target,
                "author_id": bot_id if actor is None else actor,
                "content": content,
                "msg_id": message_id,
                "msg_timestamp": 1000 + sequence,
                "nonce": nonce,
                "from_type": 1,
                "extra": extra,
            },
        }
        event = KookWebsocketEvent.from_json(json.dumps(wire))
        check(isinstance(event, KookWebsocketEvent), "real websocket parser")
        check(isinstance(event.data, KookMessageEventData), "real gateway data model")
        return event

    async def dispatch(event):
        await adapter.client._handle_signal(event)
        check(adapter.client.last_sn == event.sn, "real client sequence advanced")

    async def next_request():
        request = await asyncio.wait_for(fake_http.calls.get(), 2)
        await asyncio.wait_for(request.entered.wait(), 2)
        check(
            isinstance(request.payload.get("nonce"), str),
            "HTTP request carries correlation nonce",
        )
        check(bool(request.payload["nonce"]), "correlation nonce is nonempty")
        check(request.payload["type"] == 10, "HTTP request is a card")
        return request

    async def acknowledge(request, message_id, **kwargs):
        await dispatch(
            event_for(
                request.payload["content"],
                nonce=request.payload["nonce"],
                message_id=message_id,
                **kwargs,
            )
        )

    async def send_via_gateway(coroutine, message_id):
        task = asyncio.create_task(coroutine)
        active_tasks.append(task)
        request = await next_request()
        await acknowledge(request, message_id)
        result = await asyncio.wait_for(task, 2)
        check(result == message_id, "gateway receipt returns the actual message ID")
        check(request.cancelled, "gateway receipt cancels hanging HTTP request")
        check(
            not kook_api._pending_card_receipts,
            "completed attempt releases receipt registry",
        )
        return result

    try:
        with patch.object(kook_api, "_get_session", AsyncMock(return_value=fake_http)):
            ordinary = "ordinary synthetic group message"
            await dispatch(
                event_for(
                    ordinary, message_type=9, actor="123", message_id="normal-group"
                )
            )
            check(
                queue.qsize() == 1, "ordinary group message reaches real adapter queue"
            )
            queued = queue.get_nowait()
            check(
                isinstance(queued, KookEvent),
                "ordinary message retains real KookEvent type",
            )
            check(queued.message_str == ordinary, "ordinary content preserved")
            before = original.await_count
            await dispatch(
                event_for(
                    "MUSIC_U=synthetic-private-only",
                    message_type=9,
                    actor="123",
                    channel="PERSON",
                    target=bot_id,
                    message_id="private-secret",
                )
            )
            check(
                original.await_count == before,
                "private cookie does not reach original adapter",
            )
            check(queue.empty(), "private cookie never enters AstrBot event queue")
            await dispatch(
                event_for(
                    "ordinary synthetic private message",
                    message_type=9,
                    actor="123",
                    channel="PERSON",
                    target=bot_id,
                    message_id="private-normal",
                )
            )
            check(queue.qsize() == 1, "ordinary private message is not swallowed")
            check(
                queue.get_nowait().is_private_chat(),
                "real private-message classification preserved",
            )

            song = Song(
                id="synthetic-song",
                name="MUSIC_U synthetic title is not a credential",
                requester_id="123",
            )
            card = card_builder.build_now_playing_card(song)
            direct_task = asyncio.create_task(
                kook_api.send_card_message(token, "synthetic-channel", card)
            )
            active_tasks.append(direct_task)
            direct_request = await next_request()
            await acknowledge(
                direct_request, "wrong-channel-receipt", target="another-channel"
            )
            await asyncio.sleep(0)
            check(not direct_task.done(), "wrong channel cannot satisfy card receipt")
            await acknowledge(
                direct_request,
                "private-receipt",
                channel="PERSON",
                target="synthetic-channel",
            )
            await asyncio.sleep(0)
            check(
                not direct_task.done(), "private echo cannot satisfy group-card receipt"
            )
            await acknowledge(direct_request, "actual-direct-receipt")
            check(
                await asyncio.wait_for(direct_task, 2) == "actual-direct-receipt",
                "real gateway delivery confirms API result",
            )
            check(
                direct_request.cancelled,
                "API HTTP cancellation after real gateway echo",
            )
            check(
                queue.empty(),
                "self card with cookie-like title is not exposed to other plugins",
            )
            check(
                not kook_api._pending_card_receipts, "direct attempt registry cleaned"
            )

            await send_via_gateway(
                plugin._send_card("synthetic-channel", "synthetic-guild", card),
                "old-playing-card",
            )
            record = plugin._card_ledger.records["old-playing-card"]
            check(
                record["guild_id"] == "synthetic-guild",
                "main sends register correct guild",
            )
            check(
                record["channel_id"] == "synthetic-channel",
                "main sends register correct channel",
            )
            check(
                record["token_hash"] == CardLedger.token_hash(token),
                "ledger records only token hash",
            )
            check(not record["pending"], "current playing card is active")
            check(
                plugin._card_msg_ids["synthetic-guild"] == ["old-playing-card"],
                "main retains confirmed ID",
            )
            check(plugin._card_ledger.path is None, "smoke ledger is in-memory only")

            session = SimpleNamespace(
                playlist=[song],
                current_song=song,
                pending_skips=0,
                voice_client=SimpleNamespace(token=token),
                text_channel_id="synthetic-channel",
            )
            plugin.voice_manager.sessions["synthetic-guild"] = session
            control_entered, control_release = asyncio.Event(), asyncio.Event()

            async def controlled_skip(guild, action, **kwargs):
                check(
                    guild == "synthetic-guild" and action == "next",
                    "skip reaches manager with correct scope",
                )
                check(
                    kwargs["actor_id"] == "123", "skip retains requester authorization"
                )
                check(
                    kwargs["expected_session"] is session,
                    "skip pins the existing session",
                )
                control_entered.set()
                await control_release.wait()
                return True, "synthetic next"

            plugin.voice_manager.control = AsyncMock(side_effect=controlled_skip)
            control_task = asyncio.create_task(
                plugin._control_playback("synthetic-guild", "123", "next")
            )
            active_tasks.append(control_task)
            await asyncio.wait_for(control_entered.wait(), 2)
            with patch.object(
                plugin,
                "_delete_card_messages",
                AsyncMock(return_value=["old-playing-card"]),
            ):
                await send_via_gateway(
                    plugin._send_card("synthetic-channel", "synthetic-guild", card),
                    "new-playing-card",
                )
            check(
                not plugin._card_ledger.records["new-playing-card"]["pending"],
                "replacement card is active before skip returns",
            )
            control_release.set()
            check(
                (await asyncio.wait_for(control_task, 2))[0],
                "skip finishes successfully",
            )
            check(
                plugin._card_ledger.records["old-playing-card"]["pending"],
                "skip retires its old-card snapshot",
            )
            check(
                not plugin._card_ledger.records["new-playing-card"]["pending"],
                "late skip retirement never marks replacement card pending",
            )

            event = event_for(
                "",
                message_type=255,
                message_id="deleted-old-event",
                system={"msg_id": "old-playing-card"},
            )
            await dispatch(event)
            check(
                "old-playing-card" not in plugin._card_ledger.records,
                "real deleted_message receipt clears ledger",
            )
            check(
                plugin._card_msg_ids["synthetic-guild"] == ["new-playing-card"],
                "deletion receipt preserves replacement IDs",
            )
            wrong = event_for(
                "",
                message_type=255,
                message_id="wrong-delete-event",
                system={"msg_id": "new-playing-card"},
            )
            await plugin._handle_kook_system_event(
                adapter.client, wrong.data, "different-synthetic-token"
            )
            check(
                "new-playing-card" in plugin._card_ledger.records,
                "foreign-token deletion cannot remove ledger entry",
            )
            await dispatch(wrong)
            check(
                not plugin._card_ledger.records,
                "matching deletion receipt clears final tracked card",
            )
            check(
                not plugin._card_msg_ids.get("synthetic-guild"),
                "matching receipt clears active ID map",
            )
            check(
                queue.empty(),
                "system receipts do not create user-facing message events",
            )
            check(
                all(request.cancelled for request in fake_http.requests),
                "all fake hanging HTTP operations were cancelled",
            )
            check(
                len(fake_http.requests) == 3,
                "no ambiguous POST retry creates duplicate cards",
            )
    finally:
        for task in active_tasks:
            if not task.done():
                task.cancel()
        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)
        bridge.close()
        await plugin._close_music_auth()
        await adapter.client.close()

    check(
        not kook_api._pending_card_receipts, "no receipt registry remains at shutdown"
    )
    print(
        f"PASS: {checks} card real-core checks; AstrBot {VERSION}; "
        "real KOOK websocket/client/bridge with synthetic HTTP and memory ledger; "
        "no network, account storage, voice startup or plugin initialization"
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
