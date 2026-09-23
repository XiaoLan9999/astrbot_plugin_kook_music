import asyncio
import sys
import tempfile
import unittest
from pathlib import Path


TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from test_voice_manager import BlockingDirectPlayer, FakeVoiceClient, wait_for
from astrbot_plugin_kook_music.kook_voice.voice_manager import GuildSession, VoiceManager
from astrbot_plugin_kook_music.music.model import Song


class PrefetchPlaybackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.managers = []
        self.files = []

    async def asyncTearDown(self):
        for manager in self.managers:
            await manager.leave_all()
        self.temp.cleanup()

    def song(self, name, ready=False, platform="qq"):
        song = Song(
            id=name, name=name, platform=platform,
            requester_id=f"requester-{name}", requester_name=f"user-{name}",
            provider_data={"nested": {"version": "original"}},
        )
        if ready:
            self.downloaded(song)
        return song

    def downloaded(self, song):
        path = self.root / f"{len(self.files)}-{song.id}.mp3"
        path.write_bytes(b"synthetic audio")
        self.files.append(path)
        song.file_path = str(path)
        return song

    def manager(self, songs, *, enabled=True, loop_mode=0, player=None):
        manager = VoiceManager(
            auto_leave_timeout=0, streaming_mode="direct", prefetch_next=enabled,
        )
        manager.DIRECT_RECONNECT_DELAY = 0
        player = player or BlockingDirectPlayer()
        session = GuildSession(
            guild_id="guild", voice_channel_id="voice", text_channel_id="text",
            voice_client=FakeVoiceClient(), ffmpeg_player=player,
            playlist=list(songs), loop_mode=loop_mode,
        )
        manager.sessions["guild"] = session
        self.managers.append(manager)
        return manager, session, player

    async def start(self, manager, player):
        manager._start_playback_loop("guild")
        await wait_for(lambda: len(player.played) == 1)

    async def test_only_next_song_is_prepared_and_reused(self):
        songs = [self.song("first", True), self.song("next"), self.song("third")]
        manager, session, player = self.manager(songs)
        calls = []

        async def prepare(song):
            calls.append(song.id)
            song.provider_data["nested"]["version"] = "prepared"
            return self.downloaded(song)

        manager.on_download_song = prepare
        await self.start(manager, player)
        await wait_for(lambda: session.prefetch and session.prefetch.task.done())
        self.assertEqual(calls, ["next"])
        self.assertEqual(songs[1].file_path, "")
        self.assertEqual(songs[1].provider_data["nested"]["version"], "original")
        prepared_path = session.prefetch.task.result().file_path
        player.finish()
        await wait_for(lambda: len(player.played) == 2)
        self.assertEqual(player.played[1], prepared_path)
        self.assertEqual(calls.count("next"), 1)
        self.assertEqual(session.current_song.requester_id, "requester-next")
        self.assertEqual(session.current_song.provider_data["nested"]["version"], "prepared")

    async def test_inflight_prefetch_is_claimed_without_duplicate_download(self):
        manager, session, player = self.manager([
            self.song("first", True), self.song("next"),
        ])
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def prepare(song):
            calls.append(song.id)
            started.set()
            await release.wait()
            return self.downloaded(song)

        manager.on_download_song = prepare
        await self.start(manager, player)
        await started.wait()
        prefetch_task = session.prefetch.task
        player.finish()
        await wait_for(lambda: session.preparation_task is prefetch_task)
        self.assertEqual(calls, ["next"])
        release.set()
        await wait_for(lambda: len(player.played) == 2)
        self.assertEqual(calls, ["next"])

    async def test_failed_prefetch_is_not_repeated_when_it_reaches_head(self):
        for raises in (False, True):
            with self.subTest(raises=raises):
                manager, session, player = self.manager([
                    self.song("first", True), self.song("bad"), self.song("good", True),
                ])
                calls = []

                async def prepare(song):
                    calls.append(song.id)
                    if raises:
                        raise RuntimeError("synthetic resolver timeout")
                    song.unplayable_reason = "unavailable"
                    return song

                manager.on_download_song = prepare
                await self.start(manager, player)
                await wait_for(lambda: session.prefetch and session.prefetch.task.done())
                player.finish()
                await wait_for(lambda: len(player.played) == 2)
                self.assertEqual(calls, ["bad"])
                self.assertEqual(session.current_song.id, "good")

    async def test_append_during_playback_starts_prefetch(self):
        for batch in (False, True):
            with self.subTest(batch=batch):
                manager, session, player = self.manager([self.song("first", True)])
                calls = []

                async def prepare(song):
                    calls.append(song.id)
                    return self.downloaded(song)

                manager.on_download_song = prepare
                await self.start(manager, player)
                if batch:
                    await manager.join_and_play_many("token", "guild", "voice", "text", [
                        self.song("added"), self.song("third"),
                    ])
                else:
                    await manager.add_song("guild", self.song("added"))
                await wait_for(lambda: session.prefetch and session.prefetch.task.done())
                self.assertEqual(calls, ["added"])

    async def test_disabled_and_bilibili_and_unpredictable_loops_do_not_prefetch(self):
        cases = [(False, "qq", 0), (True, "bilibili", 0),
                 (True, "qq", 1), (True, "qq", 3)]
        for enabled, platform, loop_mode in cases:
            with self.subTest(enabled=enabled, platform=platform, loop=loop_mode):
                manager, session, player = self.manager([
                    self.song("first", True), self.song("next", platform=platform),
                ], enabled=enabled, loop_mode=loop_mode)
                calls = []

                async def prepare(song):
                    calls.append(song.id)
                    return self.downloaded(song)

                manager.on_download_song = prepare
                await self.start(manager, player)
                await asyncio.sleep(0)
                self.assertIsNone(session.prefetch)
                self.assertEqual(calls, [])

    async def test_list_loop_can_prefetch_and_single_loop_cancels_it(self):
        manager, session, player = self.manager([
            self.song("first", True), self.song("next"),
        ])
        calls = []

        async def prepare(song):
            calls.append(song.id)
            return self.downloaded(song)

        manager.on_download_song = prepare
        await self.start(manager, player)
        await wait_for(lambda: session.prefetch and session.prefetch.task.done())
        first_path = Path(session.prefetch.task.result().file_path)
        await manager.toggle_loop("guild")
        self.assertEqual(session.loop_mode, 1)
        self.assertIsNone(session.prefetch)
        self.assertFalse(first_path.exists())
        await manager.toggle_loop("guild")
        await wait_for(lambda: session.prefetch and session.prefetch.task.done())
        self.assertEqual(session.loop_mode, 2)
        self.assertEqual(calls, ["next", "next"])

    async def test_clear_removes_completed_prefetch_but_preserves_current(self):
        first = self.song("first", True)
        manager, session, player = self.manager([first, self.song("next")])

        async def prepare(song):
            return self.downloaded(song)

        manager.on_download_song = prepare
        await self.start(manager, player)
        await wait_for(lambda: session.prefetch and session.prefetch.task.done())
        cached = Path(session.prefetch.task.result().file_path)
        await manager.clear_playlist("guild")
        self.assertIsNone(session.prefetch)
        self.assertEqual(session.playlist, [first])
        self.assertFalse(cached.exists())
        self.assertTrue(Path(first.file_path).exists())

    async def test_clear_cleans_download_returned_after_cancellation(self):
        manager, session, player = self.manager([
            self.song("first", True), self.song("next"),
        ])
        started, cancelled, release = (asyncio.Event() for _ in range(3))
        produced = []

        async def prepare(song):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            result = self.downloaded(song)
            produced.append(Path(result.file_path))
            return result

        manager.on_download_song = prepare
        await self.start(manager, player)
        await started.wait()
        task = session.prefetch.task
        await manager.clear_playlist("guild")
        await cancelled.wait()
        release.set()
        await task
        await asyncio.sleep(0)
        self.assertFalse(produced[0].exists())
        self.assertFalse(session.prefetch_tasks)
        self.assertEqual(len(session.playlist), 1)

    async def test_move_waits_for_cancelled_prefetch_before_starting_new_one(self):
        manager, session, player = self.manager([
            self.song("first", True), self.song("old-next"), self.song("new-next"),
        ])
        started, cancelled, release = (asyncio.Event() for _ in range(3))
        calls, produced = [], []
        running, peak = 0, 0

        async def prepare(song):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            calls.append(song.id)
            try:
                if song.id == "old-next":
                    started.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        cancelled.set()
                        await release.wait()
                result = self.downloaded(song)
                produced.append((song.id, Path(result.file_path)))
                return result
            finally:
                running -= 1

        manager.on_download_song = prepare
        await self.start(manager, player)
        await started.wait()
        await manager.move_to_next("guild", 3)
        await cancelled.wait()
        self.assertEqual(calls, ["old-next"])
        release.set()
        await wait_for(lambda: session.prefetch and session.prefetch.task.done())
        self.assertEqual(calls, ["old-next", "new-next"])
        self.assertEqual(peak, 1)
        self.assertFalse(produced[0][1].exists())
        self.assertTrue(produced[1][1].exists())

    async def test_single_skip_reuses_prefetch_and_double_skip_discards_it(self):
        for skip_count in (1, 2):
            with self.subTest(skip_count=skip_count):
                manager, session, player = self.manager([
                    self.song("first", True), self.song("next"), self.song("third", True),
                ])
                calls = []

                async def prepare(song):
                    calls.append(song.id)
                    return self.downloaded(song)

                manager.on_download_song = prepare
                await self.start(manager, player)
                await wait_for(lambda: session.prefetch and session.prefetch.task.done())
                prepared_path = Path(session.prefetch.task.result().file_path)
                for _ in range(skip_count):
                    await manager.skip("guild")
                await wait_for(lambda: len(player.played) == 2)
                self.assertEqual(session.pending_skips, 0)
                self.assertEqual(calls, ["next"])
                self.assertEqual(session.current_song.id, "next" if skip_count == 1 else "third")
                self.assertEqual(prepared_path.exists(), skip_count == 1)

    async def test_new_song_result_retains_requester_and_provider_metadata(self):
        next_song = self.song("next")
        manager, session, player = self.manager([self.song("first", True), next_song])

        async def prepare(song):
            return self.downloaded(Song(
                id=song.id, name="resolved", platform="qq", provider_data={"resolved": True},
            ))

        manager.on_download_song = prepare
        await self.start(manager, player)
        await wait_for(lambda: session.prefetch and session.prefetch.task.done())
        player.finish()
        await wait_for(lambda: len(player.played) == 2)
        current = session.current_song
        self.assertEqual(current.requester_id, next_song.requester_id)
        self.assertEqual(current.requester_name, next_song.requester_name)
        self.assertEqual(current.provider_data["nested"], {"version": "original"})
        self.assertTrue(current.provider_data["resolved"])
        denied, _ = await manager.control("guild", "next", actor_id="requester-first")
        self.assertFalse(denied)

    async def test_leave_kick_and_unload_cancel_and_drain_prefetch(self):
        for action in ("leave", "kick", "unload"):
            with self.subTest(action=action):
                manager, session, player = self.manager([
                    self.song("first", True), self.song("next"),
                ])
                started, cancelled = asyncio.Event(), asyncio.Event()

                async def prepare(song):
                    self.downloaded(song)
                    started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cancelled.set()

                manager.on_download_song = prepare
                await self.start(manager, player)
                await started.wait()
                task = session.prefetch.task
                partial = Path(session.prefetch.candidate.file_path)
                if action == "leave":
                    await manager.leave("guild")
                elif action == "kick":
                    await manager.handle_voice_removed("voice", session.voice_client)
                else:
                    await manager.leave_all()
                self.assertTrue(cancelled.is_set())
                self.assertTrue(task.done())
                self.assertFalse(partial.exists())
                self.assertFalse(session.prefetch_tasks)
                self.assertNotIn("guild", manager.sessions)

    async def test_late_old_result_cannot_pollute_replacement_session(self):
        manager, old, player = self.manager([self.song("first", True), self.song("next")])
        started, cancelled, release = (asyncio.Event() for _ in range(3))
        produced = []

        async def prepare(song):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            result = self.downloaded(song)
            produced.append(Path(result.file_path))
            return result

        manager.on_download_song = prepare
        await self.start(manager, player)
        await started.wait()
        cleanup = asyncio.create_task(manager._cleanup_session("guild", old))
        await cancelled.wait()
        replacement = GuildSession(
            "guild", "other-voice", "other-text", FakeVoiceClient(), BlockingDirectPlayer(),
            playlist=[self.song("replacement", True)],
        )
        manager.sessions["guild"] = replacement
        release.set()
        await cleanup
        self.assertIs(manager.sessions["guild"], replacement)
        self.assertEqual(replacement.current_song.id, "replacement")
        self.assertFalse(produced[0].exists())

    async def test_slow_cards_do_not_block_next_song_and_cleanup_drains_tasks(self):
        manager, session, player = self.manager([
            self.song("first", True), self.song("next", True),
        ])
        started, cancelled = [], []

        async def notify(guild, song, size, loop):
            started.append(song.id)
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(song.id)

        manager.on_song_started = notify
        await self.start(manager, player)
        await wait_for(lambda: started == ["first"])
        first_started_at = session.playback_started_at
        player.finish()
        await wait_for(lambda: len(player.played) == 2)
        await wait_for(lambda: started == ["first", "next"])
        self.assertEqual(cancelled, [])
        self.assertEqual(len(session.notification_tasks), 2)
        self.assertGreaterEqual(session.playback_started_at, first_started_at)
        await manager.leave("guild")
        self.assertCountEqual(cancelled, ["first", "next"])
        self.assertFalse(session.notification_tasks)
        self.assertEqual(session.playback_started_at, 0)

    async def test_completed_prefetch_is_cleaned_if_waiter_is_cancelled_before_claim(self):
        manager, session, player = self.manager([
            self.song("first", True), self.song("next"),
        ])
        started, release = asyncio.Event(), asyncio.Event()
        produced = []

        async def prepare(song):
            started.set()
            await release.wait()
            result = self.downloaded(song)
            produced.append(Path(result.file_path))
            return result

        manager.on_download_song = prepare
        await self.start(manager, player)
        await started.wait()
        preparation = session.prefetch.task
        playback = manager._playback_tasks["guild"]
        preparation.add_done_callback(lambda _task: playback.cancel())
        player.finish()
        await wait_for(lambda: session.preparation_task is preparation)
        release.set()
        await playback
        self.assertTrue(preparation.done())
        self.assertFalse(produced[0].exists())
        self.assertEqual(session.current_song.id, "next")
        self.assertEqual(session.current_song.file_path, "")

    async def test_output_stops_before_slow_card_cancellation_finishes(self):
        manager, session, player = self.manager([self.song("first", True)])
        started, cancelling, release = (asyncio.Event() for _ in range(3))

        async def notify(*_args):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelling.set()
                await release.wait()

        manager.on_song_started = notify
        await self.start(manager, player)
        await started.wait()
        leaving = asyncio.create_task(manager.leave("guild"))
        await cancelling.wait()
        self.assertFalse(leaving.done())
        self.assertFalse(player.is_playing)
        self.assertFalse(session.voice_client.is_alive)
        release.set()
        await leaving
        self.assertFalse(session.notification_tasks)

    async def test_stream_resume_uses_sent_audio_not_network_stall_time(self):
        class InterruptedPlayer(BlockingDirectPlayer):
            played_seconds = 20.0

            async def wait_until_done(self, timeout=None):
                if len(self.played) == 1:
                    return False
                return await super().wait_until_done(timeout)

        song = self.song("long-video", platform="bilibili")
        song.duration = 600_000
        song.stream_url = "https://media.example.test/first.m4s"
        manager, session, player = self.manager([song], player=InterruptedPlayer())

        async def prepare(queued_song):
            queued_song.stream_url = "https://media.example.test/resumed.m4s"
            return queued_song

        manager.on_download_song = prepare
        manager._start_playback_loop("guild")
        await wait_for(lambda: len(player.played) == 2)
        self.assertEqual(player.play_offsets, [0.0, 15.0])
        self.assertEqual(session.playback_offset_seconds, 15.0)

    async def test_playback_retry_cancels_prefetch_without_restarting_it(self):
        class FailingPlayer(BlockingDirectPlayer):
            async def wait_until_done(self, timeout=None):
                raise RuntimeError("synthetic player failure")

        manager, session, player = self.manager([
            self.song("first", True), self.song("next"),
        ], player=FailingPlayer())
        manager.PLAYBACK_RETRY_DELAY = 60
        calls, produced = [], []

        async def prepare(song):
            calls.append(song.id)
            self.downloaded(song)
            produced.append(Path(song.file_path))
            await asyncio.Event().wait()

        manager.on_download_song = prepare
        manager._start_playback_loop("guild")
        await manager._playback_tasks["guild"]
        await wait_for(lambda: not session.prefetch_tasks)
        self.assertEqual(calls, ["next"])
        self.assertFalse(produced[0].exists())
        self.assertIsNone(session.prefetch)
        self.assertFalse(session.is_playing)
        self.assertIn("guild", manager._retry_tasks)

    async def test_prefetch_does_not_grant_skip_rights_to_previous_requester(self):
        manager, session, player = self.manager([
            self.song("first", True), self.song("next"), self.song("third", True),
        ])

        async def prepare(song):
            return self.downloaded(Song(id=song.id, name=song.name, platform="qq"))

        manager.on_download_song = prepare
        await self.start(manager, player)
        await wait_for(lambda: session.prefetch and session.prefetch.task.done())
        allowed, _ = await manager.control("guild", "next", actor_id="requester-first")
        denied, _ = await manager.control("guild", "next", actor_id="requester-first")
        self.assertTrue(allowed)
        self.assertFalse(denied)
        await wait_for(lambda: len(player.played) == 2)
        self.assertEqual(session.current_song.id, "next")
        self.assertEqual(session.current_song.requester_id, "requester-next")


if __name__ == "__main__":
    unittest.main()
