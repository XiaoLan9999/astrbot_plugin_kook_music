import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from test_main_bilibili_flow import _FakeEvent, main_module
from astrbot_plugin_kook_music import card_builder
from astrbot_plugin_kook_music.music.model import Song
from astrbot_plugin_kook_music.music.playback_time import parse_playback_time


class PlaybackTimeTests(unittest.TestCase):
    def test_accepted_formats(self):
        for text, seconds in (("90", 90), ("1:30", 90), ("1:02:30", 3750),
                              ("0", 0), ("90:00", 5400), ("1\uff1a30", 90)):
            self.assertEqual(parse_playback_time(text), seconds)

    def test_reject_ambiguous_and_nonfinite_times(self):
        for text in ("", "-1", "+30", "nan", "inf", "1.5", "1:60", "1:60:00",
                     "1::2", "1:2:3:4", "1e3", "99999999", "\uff11\uff12"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_playback_time(text)

    def test_cards_expose_same_bounded_seek_controls(self):
        for builder in (card_builder.build_now_playing_card, card_builder.build_bilibili_playing_card):
            card = builder(Song("one", duration=100000))
            groups = [module for module in card["modules"] if module["type"] == "action-group"]
            self.assertEqual(len(groups), 2)
            self.assertEqual([item["value"] for item in groups[-1]["elements"]],
                             ["kook_music_back_15", "kook_music_forward_30"])
            self.assertTrue(all(len(module["elements"]) <= 4 for module in groups))
            unknown = builder(Song("unknown"))
            self.assertEqual(sum(module["type"] == "action-group" for module in unknown["modules"]), 1)


class MainSeekTests(unittest.IsolatedAsyncioTestCase):
    def make_plugin(self):
        plugin = object.__new__(main_module.KookMusicPlugin)
        plugin._is_kook = lambda event: event.get_platform_name() == "kook"
        plugin._get_guild_id = lambda event: "guild"
        plugin._card_msg_ids = {"guild": ["card"]}
        plugin._retire_card_ids = lambda ids: None
        song = Song("one", requester_id="user", duration=100000)
        session = NS(playlist=[song], pending_skips=0, current_song=song,
                     playback_generation=2, text_channel_id="text",
                     voice_client=NS(token="token"))
        plugin.voice_manager = NS(sessions={"guild": session},
                                  control=AsyncMock(return_value=(True, "seek accepted")))
        event = _FakeEvent()
        event.get_platform_name = lambda: "kook"
        event.message_str = "#跳转 1:30"
        return plugin, session, event

    async def test_command_parses_and_uses_shared_permission_path(self):
        plugin, session, event = self.make_plugin()
        replies = [reply async for reply in plugin.on_seek(event)]
        self.assertIn("seek accepted", replies[0])
        plugin.voice_manager.control.assert_awaited_once_with(
            "guild", "seek", actor_id="user", is_admin=False, expected_session=session,
            position=None, target_seconds=90, relative_seconds=None,
            expected_song=session.current_song, expected_generation=2,
        )

    async def test_invalid_command_never_controls_playback(self):
        plugin, _, event = self.make_plugin()
        for text in ("#跳转", "#跳转 1:60", "#跳转 1 2", "#跳转 nan"):
            event.message_str = text
            replies = [reply async for reply in plugin.on_seek(event)]
            self.assertIn("用法", replies[0])
        plugin.voice_manager.control.assert_not_awaited()

    async def test_other_platform_remains_silent(self):
        plugin, _, event = self.make_plugin()
        event.get_platform_name = lambda: "qq"
        self.assertEqual([reply async for reply in plugin.on_seek(event)], [])
        plugin.voice_manager.control.assert_not_awaited()

    async def test_unprivileged_user_cannot_seek(self):
        plugin, _, event = self.make_plugin()
        event.get_sender_id = lambda: "outsider"
        with patch.object(main_module, "is_guild_admin", AsyncMock(return_value=False)):
            replies = [reply async for reply in plugin.on_seek(event)]
        self.assertIn("管理员", replies[0])
        plugin.voice_manager.control.assert_not_awaited()

    async def test_admin_lookup_passes_original_song_generation_for_recheck(self):
        plugin, session, event = self.make_plugin()
        song = session.current_song
        event.get_sender_id = lambda: "admin"

        async def changed(*args):
            session.playback_generation += 1
            return True

        with patch.object(main_module, "is_guild_admin", side_effect=changed):
            _ = [reply async for reply in plugin.on_seek(event)]
        options = plugin.voice_manager.control.await_args.kwargs
        self.assertIs(options["expected_song"], song)
        self.assertEqual(options["expected_generation"], 2)

    async def test_stale_queued_seek_button_is_rejected(self):
        plugin, session, _ = self.make_plugin()
        with patch.object(main_module, "send_text_message", AsyncMock()) as sender:
            await plugin._handle_button_click({
                "value": "kook_music_forward_30", "channel_id": "text", "msg_id": "card",
                "user_id": "user", "token": "token", "expected_song": session.current_song,
                "expected_generation": 1,
            })
        plugin.voice_manager.control.assert_not_awaited()
        self.assertIn("变化", sender.await_args.args[2])

    async def test_relative_button_uses_same_control(self):
        plugin, session, _ = self.make_plugin()
        with patch.object(main_module, "send_text_message", AsyncMock()):
            await plugin._handle_button_click({
                "value": "kook_music_back_15", "channel_id": "text", "msg_id": "card",
                "user_id": "user", "token": "token", "expected_song": session.current_song,
                "expected_generation": 2,
            })
        self.assertEqual(plugin.voice_manager.control.await_args.kwargs["relative_seconds"], -15)

    async def test_orphan_button_cannot_seek_current_song(self):
        plugin, _, _ = self.make_plugin()
        with patch.object(main_module, "send_text_message", AsyncMock()):
            await plugin._handle_button_click({
                "value": "kook_music_forward_30", "channel_id": "text", "msg_id": "orphan",
                "user_id": "user", "token": "token",
            })
        plugin.voice_manager.control.assert_not_awaited()

    async def test_click_received_during_cooldown_never_executes_after_queue_delay(self):
        plugin, session, _ = self.make_plugin()
        with patch.object(main_module, "send_text_message", AsyncMock()) as sender:
            await plugin._handle_button_click({
                "value": "kook_music_forward_30", "channel_id": "text", "msg_id": "card",
                "user_id": "user", "token": "token", "expected_song": session.current_song,
                "expected_generation": 2, "seek_blocked_on_receipt": True,
            })
        plugin.voice_manager.control.assert_not_awaited()
        self.assertIn("点击时", sender.await_args.args[2])
