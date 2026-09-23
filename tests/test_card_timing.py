import asyncio
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from test_main_bilibili_flow import main_module
from astrbot_plugin_kook_music.music.model import Song


class CardTimingTests(unittest.IsolatedAsyncioTestCase):
    def make_plugin(self):
        plugin = object.__new__(main_module.KookMusicPlugin)
        plugin._kook_token = "synthetic-token"
        plugin._card_msg_ids = {}
        plugin._card_locks = {}
        plugin._delete_card_messages = AsyncMock(return_value=[])
        song = Song(id="one", name="One", duration=100000)
        session = NS(current_song=song, is_playing=True, playback_started_at=100.0, playback_offset_seconds=5.0)
        plugin.voice_manager = NS(sessions={"guild": session})
        return plugin, session, song

    async def test_countdown_uses_remaining_time_after_old_card_delete(self):
        plugin, session, song = self.make_plugin()
        clock = [100.0]
        plugin._card_msg_ids = {"guild": ["old"]}

        async def delayed_delete(_ids):
            clock[0] = 112.25
            return []

        plugin._delete_card_messages.side_effect = delayed_delete
        card = {"modules": [{"type": "countdown", "startTime": 1, "endTime": 100001}]}
        sender = AsyncMock(return_value="new")
        fake_time = NS(monotonic=lambda: clock[0], time=lambda: 1000.0)
        with patch.object(main_module, "send_card_message", sender), patch.object(main_module, "time", fake_time):
            await plugin._send_card("text", "guild", card, session, song)
        countdown = sender.await_args.args[2]["modules"][0]
        self.assertEqual(countdown["endTime"] - countdown["startTime"], 82750)
        self.assertEqual(card["modules"][0]["endTime"], 100001)

    async def test_stale_inflight_card_is_deleted_after_its_id_arrives(self):
        plugin, session, song = self.make_plugin()
        started = asyncio.Event()
        release = asyncio.Event()

        async def send(*_args):
            started.set()
            await release.wait()
            return "late"

        with patch.object(main_module, "send_card_message", side_effect=send):
            task = asyncio.create_task(plugin._send_card("text", "guild", {}, session, song))
            await started.wait()
            session.current_song = Song(id="two", name="Two")
            release.set()
            self.assertIsNone(await task)
        plugin._delete_card_messages.assert_awaited_with(["late"])
        self.assertNotIn("guild", plugin._card_msg_ids)

    async def test_cancellation_recovers_http_result_and_cleans_accepted_card(self):
        plugin, session, song = self.make_plugin()
        started = asyncio.Event()
        release = asyncio.Event()
        request_cancelled = asyncio.Event()

        async def send(*_args):
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                request_cancelled.set()
                raise
            return "accepted-before-cancel"

        with patch.object(main_module, "send_card_message", side_effect=send):
            task = asyncio.create_task(plugin._send_card("text", "guild", {}, session, song))
            await started.wait()
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(request_cancelled.is_set())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
        plugin._delete_card_messages.assert_awaited_with(["accepted-before-cancel"])
        self.assertNotIn("guild", plugin._card_msg_ids)

    async def test_late_card_delete_failure_remains_tracked(self):
        plugin, session, song = self.make_plugin()

        async def send(*_args):
            session.is_playing = False
            return "late"

        plugin._delete_card_messages.side_effect = [[], ["late"]]
        with patch.object(main_module, "send_card_message", side_effect=send):
            self.assertIsNone(await plugin._send_card("text", "guild", {}, session, song))
        self.assertEqual(plugin._card_msg_ids["guild"], ["late"])

    async def test_cancelled_old_card_delete_does_not_lose_tracked_ids(self):
        plugin, session, song = self.make_plugin()
        plugin._card_msg_ids = {"guild": ["old"]}
        started = asyncio.Event()

        async def delete(_ids):
            started.set()
            await asyncio.Event().wait()

        plugin._delete_card_messages.side_effect = delete
        task = asyncio.create_task(plugin._send_card("text", "guild", {}, session, song))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(plugin._card_msg_ids["guild"], ["old"])


if __name__ == "__main__":
    unittest.main()
