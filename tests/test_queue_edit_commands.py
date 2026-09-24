import asyncio
import unittest

import test_main_permissions as permissions
from astrbot_plugin_kook_music import card_builder
from astrbot_plugin_kook_music.music.model import Song
from test_main_bilibili_flow import _FakeEvent


class QueueCommandTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = permissions.PlaybackPermissionTests.asyncSetUp
    asyncTearDown = permissions.PlaybackPermissionTests.asyncTearDown

    def event(self, text, actor="bob", platform="kook"):
        event = _FakeEvent()
        event.message_str = text
        event.get_sender_id = lambda: actor
        event.get_platform_name = lambda: platform
        self.plugin._get_guild_id = lambda _event: "guild"
        return event

    async def call(self, handler, text, actor="bob", platform="kook"):
        return [r async for r in handler(self.event(text, actor, platform))]

    async def test_pending_owner_can_remove_without_current_song_permission(self):
        reply = await self.call(self.plugin.on_queue_remove, "#队列删除 2")
        self.assertIn("✅", str(reply))
        self.assertEqual([s.name for s in self.session.playlist], ["first", "third"])
        self.auth.assert_not_awaited()
        self.player.stop.assert_not_awaited()

    async def test_current_owner_cannot_remove_someone_elses_pending_song(self):
        reply = await self.call(self.plugin.on_queue_remove, "队列删除 2", "alice")
        self.assertIn("❌", str(reply))
        self.assertEqual(len(self.session.playlist), 3)
        self.auth.assert_awaited_once()

    async def test_reorder_to_final_position_without_stopping_audio(self):
        reply = await self.call(self.plugin.on_queue_reorder, "/队列移动 2 3")
        self.assertIn("✅", str(reply))
        self.assertEqual(
            [s.name for s in self.session.playlist], ["first", "third", "second"]
        )
        self.player.stop.assert_not_awaited()

    async def test_admin_can_edit_others_and_top_alias_preserves_current(self):
        self.auth.return_value = True
        reply = await self.call(self.plugin.on_queue_top, "队列置顶 3", "admin")
        self.assertIn("✅", str(reply))
        self.assertEqual(
            [s.name for s in self.session.playlist], ["first", "third", "second"]
        )

    async def test_current_track_and_invalid_arguments_rejected(self):
        for text in (
            "队列删除 1",
            "队列删除 999",
            "队列删除 -1",
            "队列删除 2 3",
            "队列删除 " + "9" * 1000,
        ):
            await self.call(self.plugin.on_queue_remove, text)
        self.assertEqual(len(self.session.playlist), 3)
        self.player.stop.assert_not_awaited()

    async def test_edit_rechecks_target_owner_after_waiting_for_lock(self):
        lock = self.manager._guild_locks.setdefault("guild", asyncio.Lock())
        await lock.acquire()
        task = asyncio.create_task(self.call(self.plugin.on_queue_remove, "队列删除 2"))
        await asyncio.sleep(0)
        self.session.playlist[1].requester_id = "carol"
        lock.release()
        self.assertIn("❌", str(await task))
        self.assertEqual(len(self.session.playlist), 3)

    async def test_new_commands_are_silent_on_other_platforms(self):
        for handler, text in (
            (self.plugin.on_queue_remove, "队列删除 2"),
            (self.plugin.on_queue_top, "队列置顶 3"),
            (self.plugin.on_queue_reorder, "队列移动 2 3"),
        ):
            self.assertEqual(await self.call(handler, text, platform="qq"), [])
        self.auth.assert_not_awaited()

    async def test_queue_command_validates_page(self):
        reply = await self.call(self.plugin.on_playlist, "#歌单 0")
        self.assertIn("页码", str(reply))
        reply = await self.call(self.plugin.on_playlist, "歌单 9")
        self.assertIn("页码", str(reply))


class QueuePaginationTests(unittest.TestCase):
    def test_page_two_retains_global_indices_and_no_current_marker(self):
        songs = [Song(id=str(i), name=f"track-{i}") for i in range(1, 206)]
        card = card_builder.build_queue_card(songs, page=2)
        rendered = str(card)
        self.assertIn("**101.** track-101", rendered)
        self.assertIn("**200.** track-200", rendered)
        self.assertNotIn("track-201", rendered)
        self.assertNotIn("▶", rendered)
        self.assertLessEqual(len(card["modules"]), 50)

    def test_final_page_can_reach_two_thousandth_song(self):
        songs = [Song(id=str(i), name=f"track-{i}") for i in range(1, 2001)]
        rendered = str(card_builder.build_queue_card(songs, page=20))
        self.assertIn("**2000.** track-2000", rendered)
        self.assertIn("第 20/20 页", rendered)


if __name__ == "__main__":
    unittest.main()
