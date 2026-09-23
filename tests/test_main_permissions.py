import asyncio
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from test_main_bilibili_flow import _FakeEvent, main_module
from test_kook_events import platform, system_event
from test_voice_manager import BlockingDirectPlayer, FakeVoiceClient, make_song
from astrbot_plugin_kook_music.kook_voice.voice_manager import GuildSession, VoiceManager


class PlaybackPermissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.platform = platform()
        context = NS(platform_manager=NS(platform_insts=[self.platform]))
        self.plugin = main_module.KookMusicPlugin(context, {"kook_token": "test-token", "custom_ffmpeg_path": "ffmpeg"})
        self.manager = VoiceManager(auto_leave_timeout=0, streaming_mode="direct")
        self.player = BlockingDirectPlayer()
        self.player.stop = AsyncMock(wraps=self.player.stop)
        songs = [make_song("first"), make_song("second"), make_song("third")]
        for song, owner in zip(songs, ("alice", "bob", "alice")):
            song.requester_id = owner
        self.session = GuildSession(
            "guild", "voice", "text", FakeVoiceClient("test-token"), self.player,
            playlist=songs, is_playing=True,
        )
        self.manager.sessions["guild"] = self.session
        self.plugin.voice_manager = self.manager
        self.plugin._card_msg_ids = {"guild": ["card"]}
        self.auth = AsyncMock(return_value=False)
        self.auth_patch = patch.object(main_module, "is_guild_admin", self.auth)
        self.auth_patch.start()
        self.sender = AsyncMock(return_value="reply")
        self.send_patch = patch.object(main_module, "send_text_message", self.sender)
        self.send_patch.start()
        self.plugin._patch_kook_adapter()
        self.worker = asyncio.create_task(self.plugin._button_click_handler_loop())

    async def asyncTearDown(self):
        self.plugin._restore_kook_adapter()
        self.worker.cancel()
        await asyncio.gather(self.worker, return_exceptions=True)
        await self.manager.leave_all()
        await self.plugin.searcher.close()
        await self.plugin.downloader.close()
        await self.plugin.bilibili.close()
        await self.plugin.playlist_importer.close()
        self.auth_patch.stop()
        self.send_patch.stop()

    async def click(self, user="alice", action="next", **extra):
        body = {"value": "kook_music_" + action, "target_id": "text", "msg_id": "card", **extra}
        if user is not None:
            body["user_id"] = user
        await self.platform.client.event_callback(system_event(**body))
        await asyncio.wait_for(self.plugin._button_click_queue.join(), 1)

    async def test_current_requester_controls_without_admin_lookup(self):
        await self.click(action="loop")
        self.assertEqual(self.session.loop_mode, 1)
        await self.click(action="clear")
        self.assertEqual(len(self.session.playlist), 1)
        await self.click(action="next")
        self.assertEqual(self.session.pending_skips, 1)
        self.auth.assert_not_awaited()

    async def test_other_user_cannot_use_any_button(self):
        for action in ("next", "loop", "clear"):
            await self.click(user="outsider", action=action)
        self.assertEqual(self.session.pending_skips, 0)
        self.assertEqual(self.session.loop_mode, 0)
        self.assertEqual(len(self.session.playlist), 3)
        self.player.stop.assert_not_awaited()
        self.assertEqual(self.auth.await_count, 3)
        self.auth.assert_awaited_with("test-token", "guild", "outsider")
        self.assertTrue(all("管理员" in call.args[2] for call in self.sender.await_args_list))

    async def test_missing_user_identity_is_rejected_without_rest_lookup(self):
        await self.click(user=None)
        self.assertEqual(self.session.pending_skips, 0)
        self.auth.assert_not_awaited()
        self.assertIn("无法识别", self.sender.await_args.args[2])

    async def test_forged_event_roles_do_not_grant_admin_access(self):
        await self.click(user="outsider", user_info={"roles": [999], "is_master": True}, is_admin=True)
        self.assertEqual(self.session.pending_skips, 0)
        self.auth.assert_awaited_once_with("test-token", "guild", "outsider")

    async def test_verified_admin_can_control_another_users_song(self):
        self.auth.return_value = True
        for action in ("loop", "clear", "next"):
            await self.click(user="admin", action=action)
        self.assertEqual(self.session.loop_mode, 1)
        self.assertEqual(len(self.session.playlist), 1)
        self.assertEqual(self.session.pending_skips, 1)
        self.assertEqual(self.auth.await_count, 3)

    async def test_admin_permission_is_rechecked_after_revocation(self):
        self.auth.side_effect = [True, False]
        await self.click(user="admin", action="loop")
        await self.click(user="admin", action="loop")
        self.assertEqual(self.session.loop_mode, 1)
        self.assertEqual(self.auth.await_count, 2)

    async def test_requester_cannot_rapid_skip_the_next_users_song(self):
        await self.click(user="alice")
        await self.click(user="alice")
        self.assertEqual(self.session.pending_skips, 1)
        self.player.stop.assert_awaited_once()
        self.auth.assert_awaited_once_with("test-token", "guild", "alice")
        await self.click(user="bob")
        self.assertEqual(self.session.pending_skips, 2)

    async def test_admin_can_rapid_skip_across_different_requesters(self):
        self.auth.return_value = True
        await self.click(user="admin")
        await self.click(user="admin")
        self.assertEqual(self.session.pending_skips, 2)

    async def test_old_song_requester_cannot_control_new_song_from_old_card(self):
        self.session.playlist.pop(0)
        await self.click(user="alice", action="clear")
        self.assertEqual(len(self.session.playlist), 2)
        self.assertEqual(self.session.playlist[0].requester_id, "bob")

    async def test_session_replaced_during_admin_lookup_is_not_modified(self):
        old = self.session
        replacement = GuildSession("guild", "voice", "text", old.voice_client, self.player, playlist=[make_song("new")])

        async def replace(*_args):
            self.manager.sessions["guild"] = replacement
            return True

        self.auth.side_effect = replace
        await self.click(user="admin", action="loop")
        self.assertEqual(replacement.loop_mode, 0)
        self.assertEqual(old.loop_mode, 0)
        self.assertIn("变化", self.sender.await_args.args[2])

    async def test_missing_requester_allows_only_verified_admin(self):
        self.session.playlist[0].requester_id = ""
        await self.click(user="alice", action="loop")
        self.assertEqual(self.session.loop_mode, 0)
        self.auth.return_value = True
        await self.click(user="admin", action="loop")
        self.assertEqual(self.session.loop_mode, 1)

    async def test_foreign_bot_callback_cannot_reach_authorization(self):
        event = system_event(value="kook_music_clear", user_id="alice", target_id="text", msg_id="card")
        await self.plugin._handle_kook_system_event(platform(token="other").client, event, "other")
        await asyncio.wait_for(self.plugin._button_click_queue.join(), 1)
        self.auth.assert_not_awaited()
        self.assertEqual(len(self.session.playlist), 3)
        self.sender.assert_not_awaited()

    async def test_text_commands_cannot_bypass_button_permissions(self):
        self.plugin._get_guild_id = lambda _event: "guild"
        event = _FakeEvent()
        event.get_platform_name = lambda: "kook"
        event.get_sender_id = lambda: "outsider"
        event.message_str = "队列插队 3"
        self.plugin._delete_card = AsyncMock()
        for method in (self.plugin.on_skip, self.plugin.on_loop, self.plugin.on_clear, self.plugin.on_leave, self.plugin.on_queue_jump):
            replies = [reply async for reply in method(event)]
            self.assertTrue(any("管理员" in str(reply) for reply in replies))
        self.assertEqual(self.session.loop_mode, 0)
        self.assertEqual(self.session.pending_skips, 0)
        self.assertEqual([song.name for song in self.session.playlist], ["first", "second", "third"])
        self.plugin._delete_card.assert_not_awaited()
        self.player.stop.assert_not_awaited()

    async def test_non_kook_commands_remain_silent(self):
        event = _FakeEvent()
        event.get_platform_name = lambda: "qq"
        for method in (self.plugin.on_skip, self.plugin.on_loop, self.plugin.on_clear, self.plugin.on_leave, self.plugin.on_queue_jump):
            self.assertEqual([reply async for reply in method(event)], [])
        self.auth.assert_not_awaited()

    async def test_authorized_leave_removes_card_only_after_stopping_session(self):
        self.plugin._get_guild_id = lambda _event: "guild"
        event = _FakeEvent()
        event.get_platform_name = lambda: "kook"
        event.get_sender_id = lambda: "alice"

        async def delete_card(guild_id):
            self.assertNotIn(guild_id, self.manager.sessions)

        self.plugin._delete_card = AsyncMock(side_effect=delete_card)
        replies = [reply async for reply in self.plugin.on_leave(event)]
        self.assertTrue(any("已退出" in str(reply) for reply in replies))
        self.plugin._delete_card.assert_awaited_once_with("guild")


if __name__ == "__main__":
    unittest.main()
