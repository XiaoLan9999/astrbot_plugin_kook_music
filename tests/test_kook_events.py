import asyncio
from enum import Enum
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

from test_main_bilibili_flow import main_module
from astrbot_plugin_kook_music.kook_events import KookEventBridge
from astrbot_plugin_kook_music.kook_voice.voice_manager import GuildSession, VoiceManager
from test_voice_manager import BlockingDirectPlayer, FakeVoiceClient, make_song, wait_for


def platform(token="test-token", bot_id="bot", name="kook"):
    return NS(
        meta=lambda: NS(name=name),
        config={"kook_bot_token": token},
        client=NS(event_callback=AsyncMock(), bot_id=bot_id),
    )


def system_event(kind="message_btn_click", **body):
    return {"type": 255, "target_id": "not-the-text-channel", "extra": {
        "type": kind, "body": body,
    }}


def click(value="kook_music_next", user_id="requester", **kwargs):
    return system_event(value=value, target_id="text", msg_id="card", user_id=user_id, **kwargs)


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_kook_clients_only_and_idempotent_sync(self):
        handler = AsyncMock()
        bridge = KookEventBridge(handler)
        one, two, qq = platform(), platform("other-token"), platform(name="qq")
        originals = [p.client.event_callback for p in (one, two, qq)]
        bridge.sync([one, two, qq])
        first = one.client.event_callback
        bridge.sync([one, two, qq])
        self.assertIs(one.client.event_callback, first)
        self.assertIs(qq.client.event_callback, originals[2])
        for p in (one, two, qq):
            await p.client.event_callback(click())
        self.assertEqual(handler.await_count, 2)
        for callback in originals:
            callback.assert_awaited_once()
        bridge.close()
        for p, original in zip((one, two, qq), originals):
            self.assertIs(p.client.event_callback, original)

    async def test_replaced_client_and_removed_platform_restore_old_callback(self):
        handler = AsyncMock()
        bridge = KookEventBridge(handler)
        p = platform()
        old_client, old_callback = p.client, p.client.event_callback
        bridge.sync([p])
        old_wrapper = old_client.event_callback
        p.client = platform().client
        original = p.client.event_callback
        bridge.sync([p])
        self.assertIs(old_client.event_callback, old_callback)
        await old_wrapper(click())
        handler.assert_not_awaited()
        await p.client.event_callback(click())
        handler.assert_awaited_once()
        bridge.sync([])
        self.assertIs(p.client.event_callback, original)

    async def test_foreign_wrapper_and_hot_reload_do_not_duplicate_actions(self):
        handler = AsyncMock()
        bridge = KookEventBridge(handler)
        p = platform()
        original = p.client.event_callback
        bridge.sync([p])
        old_wrapper = p.client.event_callback

        async def foreign_wrapper(event):
            await old_wrapper(event)

        p.client.event_callback = foreign_wrapper
        bridge.sync([p])
        await p.client.event_callback(click())
        handler.assert_awaited_once()
        original.assert_awaited_once()
        bridge.close()
        self.assertIs(p.client.event_callback, foreign_wrapper)
        await p.client.event_callback(click())
        handler.assert_awaited_once()
        new_handler = AsyncMock()
        replacement = KookEventBridge(new_handler)
        replacement.sync([p])
        await p.client.event_callback(click())
        new_handler.assert_awaited_once()
        handler.assert_awaited_once()
        self.assertEqual(original.await_count, 3)
        replacement.close()

    async def test_internal_failure_still_forwards_original_event(self):
        bridge = KookEventBridge(AsyncMock(side_effect=ValueError("bad event")))
        p = platform()
        original = p.client.event_callback
        bridge.sync([p])
        event = click()
        await p.client.event_callback(event)
        original.assert_awaited_once_with(event)
        bridge.close()


class PluginEventTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.platforms = []
        context = NS(platform_manager=NS(platform_insts=self.platforms))
        self.plugin = main_module.KookMusicPlugin(context, {"custom_ffmpeg_path": "ffmpeg"})
        self.plugin.KOOK_ADAPTER_SYNC_INTERVAL = 0.01
        self.manager = NS(
            sessions={},
            skip=AsyncMock(return_value=(True, "next")),
            toggle_loop=AsyncMock(return_value=(True, "loop")),
            clear_playlist=AsyncMock(return_value=(True, "clear")),
            handle_voice_removed=AsyncMock(return_value=True),
            leave_all=AsyncMock(),
        )
        self.plugin.voice_manager = self.manager
        self.manager.iter_voice_sessions = lambda: tuple(self.manager.sessions.values())
        song = make_song("current")
        song.requester_id = "requester"
        self.manager.sessions["guild"] = NS(
            text_channel_id="text", voice_channel_id="voice",
            voice_client=NS(token="test-token"),
            playlist=[song], pending_skips=0,
        )

        async def control(guild_id, action, **kwargs):
            handlers = {"next": self.manager.skip, "loop": self.manager.toggle_loop, "clear": self.manager.clear_playlist}
            return await handlers[action](guild_id)

        self.manager.control = AsyncMock(side_effect=control)
        self.plugin._card_msg_ids = {"guild": ["card"]}
        self.sender = AsyncMock(return_value="reply")
        self.send_patch = patch.object(main_module, "send_text_message", self.sender)
        self.send_patch.start()
        await self.plugin.initialize()

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.send_patch.stop()

    async def drain_buttons(self):
        await asyncio.wait_for(self.plugin._button_click_queue.join(), 1)

    async def drain_exits(self):
        tasks = list(self.plugin._voice_exit_tasks)
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks), 1)

    async def test_cold_start_then_platform_creation_recovers_all_buttons(self):
        await asyncio.sleep(0.02)
        p = platform()
        original = p.client.event_callback
        self.platforms.append(p)
        for _ in range(30):
            if p.client.event_callback is not original:
                break
            await asyncio.sleep(0.01)
        self.assertIsNot(p.client.event_callback, original)
        self.assertEqual(self.plugin._kook_token, "test-token")
        for value in ("kook_music_next", "kook_music_clear", "kook_music_loop"):
            await p.client.event_callback(click(value))
        await self.drain_buttons()
        for action in (self.manager.skip, self.manager.clear_playlist, self.manager.toggle_loop):
            action.assert_awaited_once_with("guild")
        self.assertEqual(original.await_count, 3)
        self.assertEqual(self.sender.await_count, 3)

    async def test_command_installs_listener_before_first_card(self):
        p = platform()
        original = p.client.event_callback
        self.platforms.append(p)
        self.assertFalse(self.plugin._is_kook(NS(get_platform_name=lambda: "qq")))
        self.assertIs(p.client.event_callback, original)
        self.assertTrue(self.plugin._is_kook(NS(get_platform_name=lambda: "kook")))
        self.assertIsNot(p.client.event_callback, original)

    async def test_dict_typed_enum_and_raw_gateway_event_shapes(self):
        class MessageType(Enum):
            SYSTEM = 255

        class ExtraType(Enum):
            CLICK = "message_btn_click"

        p = platform()
        self.platforms.append(p)
        self.plugin._patch_kook_adapter()
        shapes = [
            click(),
            NS(type=MessageType.SYSTEM, extra=NS(type=ExtraType.CLICK, body=click()["extra"]["body"])),
            NS(type=255, extra=NS(type="message_btn_click", body=NS(**click()["extra"]["body"]))),
            {"s": 0, "d": click()},
        ]
        for event in shapes:
            await p.client.event_callback(event)
        await self.drain_buttons()
        self.assertEqual(self.manager.skip.await_count, 4)

    async def test_untracked_queue_card_routes_by_text_channel(self):
        event = system_event(value="kook_music_clear", target_id="text", msg_id="queue-card", user_id="requester")
        await self.plugin._handle_kook_system_event(platform().client, event, "test-token")
        await self.drain_buttons()
        self.manager.clear_playlist.assert_awaited_once_with("guild")

    async def test_button_missing_channel_uses_known_card_not_outer_target(self):
        event = system_event(value="kook_music_next", msg_id="card", user_id="requester")
        await self.plugin._handle_kook_system_event(platform().client, event, "test-token")
        await self.drain_buttons()
        self.manager.skip.assert_awaited_once_with("guild")
        self.assertEqual(self.sender.await_args.args[:2], ("test-token", "text"))

    async def test_foreign_bot_channel_unknown_card_and_non_plugin_events_ignored(self):
        events = [
            (click(), "other-token"),
            (system_event(value="kook_music_next", msg_id="card", target_id="other-channel"), "test-token"),
            (system_event(value="kook_music_next", msg_id="unknown"), "test-token"),
            (click("some_other_plugin"), "test-token"),
            ({"type": 1, "extra": click()["extra"]}, "test-token"),
            (click(), ""),
        ]
        for event, token in events:
            await self.plugin._handle_kook_system_event(platform().client, event, token)
        await self.drain_buttons()
        self.manager.skip.assert_not_awaited()
        self.sender.assert_not_awaited()

    async def test_rapid_next_clicks_each_execute_even_if_reply_fails(self):
        self.sender.side_effect = [OSError("offline"), "ok", "ok"]
        for _ in range(3):
            await self.plugin._handle_kook_system_event(platform().client, click(), "test-token")
        await self.drain_buttons()
        self.assertEqual(self.manager.skip.await_count, 3)

    async def test_bot_exit_uses_matching_channel_token_and_timestamp(self):
        event = system_event("exited_channel", user_id="bot", channel_id="voice", exited_at=123000)
        await self.plugin._handle_kook_system_event(platform().client, event, "test-token")
        await self.drain_exits()
        self.manager.handle_voice_removed.assert_awaited_once_with(
            "voice", expected_voice_client=self.manager.sessions["guild"].voice_client,
            occurred_at=123000,
        )

    async def test_exit_routes_to_pending_join_before_session_is_published(self):
        pending = self.manager.sessions.pop("guild")
        self.manager.iter_voice_sessions = lambda: (pending,)
        event = system_event("exited_channel", user_id="bot", channel_id="voice", exited_at=456000)
        await self.plugin._handle_kook_system_event(platform().client, event, "test-token")
        await self.drain_exits()
        self.manager.handle_voice_removed.assert_awaited_once_with(
            "voice", expected_voice_client=pending.voice_client, occurred_at=456000,
        )

    async def test_other_user_other_channel_other_bot_and_unknown_identity_exits_ignored(self):
        for bot_id, user_id, channel_id, token in [
            ("bot", "listener", "voice", "test-token"),
            ("bot", "bot", "other-voice", "test-token"),
            ("bot", "bot", "voice", "other-token"),
            ("", "", "voice", "test-token"),
        ]:
            event = system_event("exited_channel", user_id=user_id, channel_id=channel_id)
            await self.plugin._handle_kook_system_event(platform(bot_id=bot_id).client, event, token)
        await self.drain_exits()
        self.manager.handle_voice_removed.assert_not_awaited()

    async def test_matching_join_records_server_time_for_exit_ordering(self):
        voice_client = self.manager.sessions["guild"].voice_client
        voice_client.note_channel_joined = Mock()
        event = system_event("joined_channel", user_id="bot", channel_id="voice", joined_at=123000)
        await self.plugin._handle_kook_system_event(platform().client, event, "test-token")
        voice_client.note_channel_joined.assert_called_once_with("voice", 123000)
        self.manager.handle_voice_removed.assert_not_awaited()

    async def test_wrong_bot_or_channel_join_cannot_change_server_time(self):
        voice_client = self.manager.sessions["guild"].voice_client
        voice_client.note_channel_joined = Mock()
        for user_id, channel_id, token in [
            ("listener", "voice", "test-token"),
            ("bot", "other-voice", "test-token"),
            ("bot", "voice", "other-token"),
        ]:
            event = system_event("joined_channel", user_id=user_id, channel_id=channel_id, joined_at=123000)
            await self.plugin._handle_kook_system_event(platform().client, event, token)
        voice_client.note_channel_joined.assert_not_called()

    async def test_exit_cleanup_does_not_block_gateway_or_button_reply(self):
        release = asyncio.Event()
        async def slow_cleanup(*_a, **_kw):
            await release.wait()

        self.manager.handle_voice_removed.side_effect = slow_cleanup
        p = platform()
        original = p.client.event_callback
        self.platforms.append(p)
        self.plugin._patch_kook_adapter()
        event = system_event("exited_channel", user_id="bot", channel_id="voice")
        await asyncio.wait_for(p.client.event_callback(event), 0.2)
        original.assert_awaited_once_with(event)
        self.assertTrue(self.plugin._voice_exit_tasks)
        release.set()
        await self.drain_exits()

    async def start_real_manager(self):
        manager = VoiceManager(streaming_mode="direct")
        manager.PLAYBACK_START_DELAY = 0
        manager.DIRECT_RECONNECT_DELAY = 0
        manager.on_playback_finished = AsyncMock()
        player = BlockingDirectPlayer()
        session = GuildSession(
            guild_id="guild", voice_channel_id="voice", text_channel_id="text",
            voice_client=FakeVoiceClient("test-token"), ffmpeg_player=player,
            playlist=[make_song(name) for name in ("point-a", "list-1", "list-2", "point-b")],
        )
        for song in session.playlist:
            song.requester_id = "requester"
        manager.sessions["guild"] = session
        self.plugin.voice_manager = manager
        p = platform()
        self.platforms.append(p)
        self.plugin._patch_kook_adapter()
        manager._start_playback_loop("guild")
        await wait_for(lambda: player.played == ["point-a"])
        return manager, session, player, p.client

    async def test_gateway_buttons_drive_real_mixed_queue_and_rapid_skip(self):
        manager, session, player, client = await self.start_real_manager()
        await client.event_callback(click("kook_music_loop"))
        await self.drain_buttons()
        self.assertEqual(session.loop_mode, 1)
        await client.event_callback(click())
        await self.drain_buttons()
        await wait_for(lambda: player.played[-1] == "list-1")
        await client.event_callback(click())
        await client.event_callback(click())
        await self.drain_buttons()
        await wait_for(lambda: player.played[-1] == "point-b")
        self.assertNotIn("list-2", player.played)
        await client.event_callback(click("kook_music_clear"))
        await self.drain_buttons()
        self.assertEqual([s.name for s in session.playlist], ["point-b"])
        self.assertTrue(player.is_playing)
        await client.event_callback(click())
        await self.drain_buttons()
        await wait_for(lambda: not session.playlist and not session.is_playing)
        self.assertFalse(player.is_playing)

    async def test_gateway_clear_preserves_current_then_bot_exit_stops_real_playback(self):
        manager, session, player, client = await self.start_real_manager()
        await client.event_callback(click("kook_music_clear"))
        await self.drain_buttons()
        self.assertEqual([s.name for s in session.playlist], ["point-a"])
        self.assertTrue(player.is_playing)
        await client.event_callback(system_event("exited_channel", user_id="bot", channel_id="voice"))
        await self.drain_exits()
        self.assertNotIn("guild", manager.sessions)
        self.assertEqual(session.playlist, [])
        self.assertFalse(player.is_playing)
        self.assertFalse(session.voice_client.is_alive)
        self.assertNotIn("guild", manager._playback_tasks)
        manager.on_playback_finished.assert_awaited_once_with("guild")

    async def test_kick_cleanup_waits_for_inflight_card_send_and_deletes_late_card(self):
        sending = asyncio.Event()
        release = asyncio.Event()

        async def delayed_card(*_args):
            sending.set()
            await release.wait()
            return "late-card"

        deleted = []

        async def delete_cards(msg_ids):
            deleted.extend(msg_ids)
            return []

        self.plugin._delete_card_messages = delete_cards
        with patch.object(main_module, "send_card_message", side_effect=delayed_card):
            send = asyncio.create_task(self.plugin._send_card("text", "guild", {}))
            await asyncio.wait_for(sending.wait(), 1)
            self.manager.sessions.pop("guild")
            cleanup = asyncio.create_task(self.plugin._delete_card("guild"))
            await asyncio.sleep(0)
            release.set()
            await asyncio.wait_for(asyncio.gather(send, cleanup), 1)
        self.assertIn("late-card", deleted)
        self.assertNotIn("guild", self.plugin._card_msg_ids)

    async def test_stale_song_callback_cannot_send_after_session_replaced(self):
        old_session = self.manager.sessions.pop("guild")
        with patch.object(main_module, "send_card_message", new_callable=AsyncMock) as sender:
            result = await self.plugin._send_card(
                "text", "guild", {}, expected_session=old_session,
                expected_song=make_song("old"),
            )
        self.assertIsNone(result)
        sender.assert_not_awaited()

    async def test_late_finish_callback_does_not_delete_new_active_queue_card(self):
        self.manager.sessions["guild"].playlist = [make_song("new")]
        self.plugin._delete_card_messages = AsyncMock(return_value=[])
        await self.plugin._delete_card("guild")
        self.plugin._delete_card_messages.assert_not_awaited()
        self.assertEqual(self.plugin._card_msg_ids["guild"], ["card"])


if __name__ == "__main__":
    unittest.main()
