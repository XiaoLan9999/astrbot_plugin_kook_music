import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from test_main_bilibili_flow import _FakeEvent, main_module
import test_main_permissions as permission_fixtures
from test_voice_manager import BlockingDirectPlayer, FakeVoiceClient
from astrbot_plugin_kook_music import card_builder
from astrbot_plugin_kook_music.kook_voice.voice_manager import GuildSession, VoiceManager
from astrbot_plugin_kook_music.music.bilibili import BilibiliCollection
from astrbot_plugin_kook_music.music.model import Song


async def collect(generator):
    return [result async for result in generator]


class MainActivityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        main_module._active_music_request.set(None)
        main_module._pending_playlist_ranges.clear()
        main_module._playlist_import_requests.clear()
        main_module._bilibili_play_requests.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.plugin = object.__new__(main_module.KookMusicPlugin)
        self.manager = VoiceManager(auto_leave_timeout=20, streaming_mode="direct")
        self.session = GuildSession(
            "guild", "voice", "text", FakeVoiceClient("token"), BlockingDirectPlayer(),
        )
        self.session.idle_seconds = 10
        self.manager.sessions["guild"] = self.session
        self.manager._start_playback_loop = lambda _guild: None
        self.plugin.voice_manager = self.manager
        self.plugin._kook_token = "token"
        self.plugin.default_platform = "netease"
        self.plugin.search_limit = 1
        self.plugin.max_queue_size = 200
        self.plugin.playlist_range_timeout = 1
        self.plugin.bili_stream_threshold_minutes = 15
        self.plugin.data_dir = Path(self.temp.name)
        self.plugin._is_kook = lambda event: event.get_platform_name() == "kook"
        self.plugin._get_guild_id = lambda _event: "guild"
        self.plugin._get_channel_id = lambda _event: "text"
        self.plugin._delete_messages = AsyncMock()
        self.plugin._delete_card = AsyncMock()
        self.plugin.searcher = NS(search=AsyncMock(), fetch_audio_url=AsyncMock())
        self.plugin.downloader = NS(download=AsyncMock())
        self.plugin.bilibili = NS(resolve_input=AsyncMock())
        self.plugin.playlist_importer = NS(resolve_playlist_input=AsyncMock())
        self.event = _FakeEvent()
        self.event.get_platform_name = lambda: "kook"
        self.sender = patch.object(main_module, "send_text_message", AsyncMock(return_value="progress"))
        self.cards = patch.object(main_module, "send_card_message", AsyncMock(return_value="card"))
        self.sender.start()
        self.cards.start()

    async def asyncTearDown(self):
        await self.manager.leave_all()
        self.sender.stop()
        self.cards.stop()
        self.temp.cleanup()
        main_module._active_music_request.set(None)
        main_module._pending_playlist_ranges.clear()
        main_module._playlist_import_requests.clear()
        main_module._bilibili_play_requests.clear()

    def ready_song(self, name="song", platform="netease"):
        path = Path(self.temp.name) / f"{name}.mp3"
        path.write_bytes(b"synthetic")
        return Song(id=name, name=name, platform=platform, audio_url="https://example.test/audio", file_path=str(path))

    async def hold_stage_and_check_idle(self, public_method, attribute, method, result):
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked(*args, **kwargs):
            self.assertIsNotNone(self.plugin._play_request("guild"))
            self.assertTrue(self.plugin._play_request("guild").current)
            entered.set()
            await release.wait()
            return result

        setattr(attribute, method, AsyncMock(side_effect=blocked))
        task = asyncio.create_task(collect(public_method(self.event)))
        await asyncio.wait_for(entered.wait(), 1)
        for _ in range(4):
            await self.manager._check_session_idle("guild", self.session)
        self.assertIs(self.manager.sessions["guild"], self.session)
        self.assertEqual(self.session.idle_seconds, 0)
        self.assertEqual(self.session.voice_client.disconnect_calls, 0)
        release.set()
        await asyncio.wait_for(task, 1)
        self.assertEqual(self.manager._activity_requests, {})
        self.assertIsNone(self.plugin._play_request("guild"))

    async def test_public_song_entry_reserves_before_search(self):
        self.event.message_str = "点歌 test"
        await self.hold_stage_and_check_idle(
            self.plugin.on_play_music, self.plugin.searcher, "search", [],
        )

    async def test_public_video_entry_reserves_before_link_resolution(self):
        self.event.message_str = "播放 BV1ANQqBTEVU"
        await self.hold_stage_and_check_idle(
            self.plugin.on_play_video, self.plugin.bilibili, "resolve_input", "",
        )

    async def test_public_playlist_entry_reserves_before_link_resolution(self):
        self.event.message_str = "导入歌单 123"
        await self.hold_stage_and_check_idle(
            self.plugin.on_import_playlist, self.plugin.playlist_importer,
            "resolve_playlist_input", ("", ""),
        )

    async def test_public_request_cancellation_releases_reservation(self):
        self.event.message_str = "播放 BV1ANQqBTEVU"
        entered = asyncio.Event()

        async def resolve(*args):
            entered.set()
            await asyncio.Event().wait()

        self.plugin.bilibili.resolve_input.side_effect = resolve
        task = asyncio.create_task(collect(self.plugin.on_play_video(self.event)))
        await entered.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.manager._activity_requests, {})
        self.assertIsNone(self.plugin._play_request("guild"))

    async def test_public_request_exception_releases_reservation(self):
        self.event.message_str = "播放 BV1ANQqBTEVU"
        self.plugin.bilibili.resolve_input.side_effect = RuntimeError("synthetic parse error")
        with self.assertRaisesRegex(RuntimeError, "synthetic"):
            await collect(self.plugin.on_play_video(self.event))
        self.assertEqual(self.manager._activity_requests, {})
        self.assertIsNone(self.plugin._play_request("guild"))

    async def test_early_generator_close_releases_reservation_and_context(self):
        for command, public_method in (
            ("点歌", self.plugin.on_play_music),
            ("播放", self.plugin.on_play_video),
            ("导入歌单", self.plugin.on_import_playlist),
        ):
            with self.subTest(command=command):
                self.event.message_str = command
                iterator = public_method(self.event)
                result = await anext(iterator)
                self.assertIn("用法", result)
                await iterator.aclose()
                self.assertEqual(self.manager._activity_requests, {})
                self.assertIsNone(self.plugin._play_request("guild"))

    async def test_yield_does_not_publish_request_context_to_caller(self):
        self.event.message_str = "播放"
        iterator = self.plugin.on_play_video(self.event)
        try:
            await anext(iterator)
            self.assertIsNone(self.plugin._play_request("guild"))
        finally:
            await iterator.aclose()

    async def test_generator_can_resume_and_close_in_a_different_task(self):
        self.event.message_str = "导入歌单"
        iterator = self.plugin.on_import_playlist(self.event)
        try:
            result = await asyncio.create_task(anext(iterator))
            self.assertIn("用法", result)
            with self.assertRaises(StopAsyncIteration):
                await asyncio.create_task(anext(iterator))
            self.assertEqual(self.manager._activity_requests, {})
        finally:
            await iterator.aclose()

    async def test_stop_during_public_song_download_rejects_late_result(self):
        self.event.message_str = "点歌 test"
        song = self.ready_song()
        path = Path(song.file_path)
        self.plugin.searcher.search.return_value = [song]
        entered, release = asyncio.Event(), asyncio.Event()

        async def download(value):
            entered.set()
            await release.wait()
            return value

        self.plugin.downloader.download.side_effect = download
        task = asyncio.create_task(collect(self.plugin.on_play_music(self.event)))
        await entered.wait()
        self.assertTrue((await self.manager.control(
            "guild", "stop", actor_id="admin", is_admin=True,
        ))[0])
        release.set()
        await task
        self.assertEqual(self.session.playlist, [])
        self.assertFalse(path.exists())
        self.assertTrue(any("已因停止" in str(reply) for reply in self.event.sent))
        self.assertEqual(self.manager._activity_requests, {})

    async def test_stopped_request_is_rejected_before_fetch_or_download(self):
        song = self.ready_song()
        path = Path(song.file_path)

        async def handler(event):
            await self.manager.control("guild", "stop", actor_id="admin", is_admin=True)
            await self.plugin._play_song(event, song, "guild")
            if False:
                yield None

        await collect(self.plugin._run_music_request(self.event, handler))
        self.plugin.searcher.fetch_audio_url.assert_not_awaited()
        self.plugin.downloader.download.assert_not_awaited()
        self.assertFalse(path.exists())
        self.assertEqual(self.session.playlist, [])

    async def test_song_join_receives_active_request(self):
        self.event.message_str = "点歌 test"
        song = self.ready_song()
        self.plugin.searcher.search.return_value = [song]
        self.plugin.downloader.download.side_effect = lambda value: value
        actual = self.manager.join_and_play
        seen = []

        async def join(*args, **kwargs):
            seen.append(kwargs["request"].current)
            return await actual(*args, **kwargs)

        self.manager.join_and_play = AsyncMock(side_effect=join)
        await collect(self.plugin.on_play_music(self.event))
        self.assertEqual(seen, [True])
        self.assertIs(self.session.current_song, song)

    async def test_playlist_join_receives_active_request(self):
        self.event.message_str = "导入歌单 123"
        song = self.ready_song()
        self.plugin.playlist_importer.resolve_playlist_input.return_value = ("netease", "123")
        self.plugin.playlist_importer.import_netease_playlist = AsyncMock(return_value=[song])
        self.plugin.playlist_importer.enrich_netease_songs = AsyncMock()
        actual = self.manager.join_and_play_many
        seen = []

        async def join(*args, **kwargs):
            seen.append(kwargs["request"].current)
            return await actual(*args, **kwargs)

        self.manager.join_and_play_many = AsyncMock(side_effect=join)
        await collect(self.plugin.on_import_playlist(self.event))
        self.assertEqual(seen, [True])

    async def test_bilibili_collection_join_receives_active_request(self):
        self.event.message_str = "播放 BV1ANQqBTEVU"
        song = self.ready_song(platform="bilibili")
        collection = BilibiliCollection(id="BV1ANQqBTEVU", title="parts", kind="分P视频", songs=[song])
        self.plugin.bilibili.resolve_input.return_value = "BV1ANQqBTEVU"
        self.plugin.bilibili.extract_collection = AsyncMock(return_value=collection)
        self.plugin.bilibili.materialize_collection_songs = AsyncMock(return_value=[song])
        actual = self.manager.join_and_play_many
        seen = []

        async def join(*args, **kwargs):
            seen.append(kwargs["request"].current)
            return await actual(*args, **kwargs)

        self.manager.join_and_play_many = AsyncMock(side_effect=join)
        await collect(self.plugin.on_play_video(self.event))
        self.assertEqual(seen, [True])

    async def test_non_kook_public_commands_do_not_create_activity(self):
        self.event.get_platform_name = lambda: "qq"
        for method in (self.plugin.on_play_music, self.plugin.on_play_video, self.plugin.on_import_playlist):
            self.assertEqual(await collect(method(self.event)), [])
        self.assertEqual(self.manager._activity_requests, {})


class MainStopPermissionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = permission_fixtures.PlaybackPermissionTests.asyncSetUp
    asyncTearDown = permission_fixtures.PlaybackPermissionTests.asyncTearDown
    click = permission_fixtures.PlaybackPermissionTests.click

    async def test_outsider_stop_button_does_not_stop_or_clear(self):
        await self.click(user="outsider", action="stop")
        self.assertEqual(len(self.session.playlist), 3)
        self.player.stop.assert_not_awaited()
        self.auth.assert_awaited_once_with("test-token", "guild", "outsider")

    async def test_requester_stop_button_uses_same_authorization_path(self):
        await self.click(user="alice", action="stop")
        self.assertEqual(self.session.playlist, [])
        self.assertIs(self.manager.sessions["guild"], self.session)
        self.player.stop.assert_awaited()
        self.auth.assert_not_awaited()

    async def test_verified_administrator_stop_button(self):
        self.auth.return_value = True
        await self.click(user="admin", action="stop")
        self.assertEqual(self.session.playlist, [])
        self.auth.assert_awaited_once_with("test-token", "guild", "admin")

    async def test_stop_rechecks_requester_after_waiting_for_guild_lock(self):
        entered = asyncio.Event()
        actual = self.manager.control

        async def control(*args, **kwargs):
            entered.set()
            return await actual(*args, **kwargs)

        self.manager.control = AsyncMock(side_effect=control)
        lock = self.manager._guild_locks.setdefault("guild", asyncio.Lock())
        async with lock:
            click_task = asyncio.create_task(self.click(user="alice", action="stop"))
            await entered.wait()
            self.session.playlist.pop(0)
        await click_task
        self.assertEqual(self.session.current_song.requester_id, "bob")
        self.assertEqual(len(self.session.playlist), 2)
        self.player.stop.assert_not_awaited()

    async def test_stop_command_denies_outsider_and_accepts_requester(self):
        self.plugin._get_guild_id = lambda _event: "guild"
        event = _FakeEvent()
        event.get_platform_name = lambda: "kook"
        event.get_sender_id = lambda: "outsider"
        result = await collect(self.plugin.on_stop(event))
        self.assertTrue(any("管理员" in str(reply) for reply in result))
        self.assertEqual(len(self.session.playlist), 3)
        event.get_sender_id = lambda: "alice"
        result = await collect(self.plugin.on_stop(event))
        self.assertTrue(any("停止" in str(reply) for reply in result))
        self.assertEqual(self.session.playlist, [])

    async def test_stop_command_silent_on_other_platform(self):
        event = _FakeEvent()
        event.get_platform_name = lambda: "qq"
        self.assertEqual(await collect(self.plugin.on_stop(event)), [])
        self.assertEqual(len(self.session.playlist), 3)

    async def test_stop_button_does_not_change_clear_or_next_meaning(self):
        await self.click(user="alice", action="clear")
        self.assertEqual([song.id for song in self.session.playlist], ["first"])
        self.player.stop.assert_not_awaited()
        await self.click(user="alice", action="next")
        self.assertEqual(self.session.pending_skips, 1)
        self.player.stop.assert_awaited_once()


class StopCardSchemaTests(unittest.TestCase):
    def test_both_playing_cards_have_four_valid_control_buttons(self):
        song = Song(id="test", name="test", platform="bilibili", requester_name="owner")
        for builder in (card_builder.build_now_playing_card, card_builder.build_bilibili_playing_card):
            with self.subTest(builder=builder.__name__):
                card = builder(song, 2, "关闭")
                groups = [module for module in card["modules"] if module["type"] == "action-group"]
                self.assertEqual(len(groups), 1)
                buttons = groups[0]["elements"]
                self.assertLessEqual(len(buttons), 4)
                self.assertEqual([button["value"] for button in buttons], [
                    "kook_music_next", "kook_music_loop", "kook_music_clear", "kook_music_stop",
                ])
                for button in buttons:
                    self.assertEqual(button["type"], "button")
                    self.assertEqual(button["click"], "return-val")
                    self.assertEqual(button["text"]["type"], "plain-text")
                    self.assertIn(button["theme"], ("primary", "warning", "danger"))


if __name__ == "__main__":
    unittest.main()
