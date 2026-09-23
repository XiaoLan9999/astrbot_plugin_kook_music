# ruff: noqa: E402
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

PLUGINS_DIR = Path(__file__).resolve().parents[2]
if str(PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGINS_DIR))

from astrbot_plugin_kook_music.kook_voice.voice_manager import GuildSession, VoiceManager
from astrbot_plugin_kook_music.music.model import Song
from test_voice_disconnect import BlockingRelayPlayer
from test_voice_manager import BlockingDirectPlayer, FakeVoiceClient, wait_for


MODULE = "astrbot_plugin_kook_music.kook_voice.voice_manager"


class StopPlaybackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.managers = []
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    async def asyncTearDown(self):
        for manager in self.managers:
            await manager.leave_all()
        self.temp.cleanup()

    def song(self, name, owner="owner", ready=True):
        song = Song(id=name, name=name, requester_id=owner)
        if ready:
            path = self.root / f"{name}.mp3"
            path.write_bytes(b"synthetic audio")
            song.file_path = str(path)
        return song

    def manager(self, songs, player=None):
        manager = VoiceManager(auto_leave_timeout=0, streaming_mode="direct")
        manager.DIRECT_RECONNECT_DELAY = 0
        session = GuildSession(
            "guild", "voice", "text", FakeVoiceClient("token"),
            player or BlockingDirectPlayer(), playlist=songs,
        )
        manager.sessions["guild"] = session
        manager.on_playback_finished = AsyncMock()
        self.managers.append(manager)
        return manager, session

    async def test_stop_clears_current_and_queue_but_keeps_voice_connection(self):
        songs = [self.song("current"), self.song("queued", owner="other")]
        paths = [Path(song.file_path) for song in songs]
        manager, session = self.manager(songs)
        session.loop_mode = 2
        manager._start_playback_loop("guild")
        await wait_for(lambda: session.ffmpeg_player.is_playing)
        ok, _ = await manager.control("guild", "stop", actor_id="owner")
        self.assertTrue(ok)
        self.assertIs(manager.sessions["guild"], session)
        self.assertEqual(session.playlist, [])
        self.assertEqual(session.loop_mode, 2)
        self.assertFalse(session.ffmpeg_player.is_playing)
        self.assertFalse(session.is_playing)
        self.assertEqual(session.voice_client.disconnect_calls, 0)
        self.assertTrue(session.voice_client.is_alive)
        self.assertTrue(session.needs_direct_refresh)
        self.assertTrue(all(not path.exists() for path in paths))
        manager.on_playback_finished.assert_awaited_once_with("guild")

    async def test_stop_permission_and_expected_session_are_checked_atomically(self):
        manager, session = self.manager([self.song("current")])
        for actor in ("", "other"):
            self.assertFalse((await manager.control("guild", "stop", actor_id=actor))[0])
        self.assertFalse((await manager.control(
            "guild", "stop", actor_id="owner", expected_session=object(),
        ))[0])
        self.assertEqual(len(session.playlist), 1)
        self.assertTrue((await manager.control(
            "guild", "stop", actor_id="admin", is_admin=True,
        ))[0])

    async def test_repeated_stop_does_not_grant_old_owner_control_over_new_song(self):
        manager, session = self.manager([self.song("current")])
        self.assertTrue((await manager.control("guild", "stop", actor_id="owner"))[0])
        self.assertFalse((await manager.control("guild", "stop", actor_id="owner"))[0])
        self.assertTrue((await manager.control(
            "guild", "stop", actor_id="admin", is_admin=True,
        ))[0])
        await manager.join_and_play("token", "guild", "voice", "text", self.song("new", "other"))
        self.assertFalse((await manager.control("guild", "stop", actor_id="owner"))[0])
        self.assertEqual(session.current_song.id, "new")

    async def test_stop_invalidates_slow_frontend_download(self):
        manager, session = self.manager([self.song("current")])
        with manager.request_activity("guild") as request:
            self.assertTrue((await manager.control("guild", "stop", actor_id="owner"))[0])
            self.assertFalse(request.current)
            self.assertFalse((await manager.join_and_play(
                "token", "guild", "voice", "text", self.song("late"), request=request,
            ))[0])
        self.assertEqual(session.playlist, [])

    async def test_stop_cancels_preparation_and_cleans_late_download(self):
        manager, session = self.manager([self.song("current", ready=False)])
        started = asyncio.Event()
        late_path = self.root / "late-preparation.mp3"

        async def prepare(song):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                late_path.write_bytes(b"late synthetic audio")
                return Song(id=song.id, name=song.name, file_path=str(late_path))

        manager.on_download_song = prepare
        manager._start_playback_loop("guild")
        await started.wait()
        self.assertTrue((await manager.control("guild", "stop", actor_id="owner"))[0])
        self.assertEqual(session.ffmpeg_player.played, [])
        self.assertFalse(late_path.exists())
        self.assertIsNone(session.preparation_task)
        self.assertEqual(session.playlist, [])
        manager.on_playback_finished.assert_awaited_once_with("guild")

    async def test_stop_cancels_background_prefetch_and_late_file(self):
        manager, session = self.manager([self.song("current"), self.song("next", ready=False)])
        started = asyncio.Event()
        late_path = self.root / "late-prefetch.mp3"

        async def prepare(song):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                late_path.write_bytes(b"late synthetic audio")
                song.file_path = str(late_path)
                return song

        manager.on_download_song = prepare
        manager._start_playback_loop("guild")
        await started.wait()
        self.assertTrue((await manager.control("guild", "stop", actor_id="owner"))[0])
        self.assertEqual(len(session.ffmpeg_player.played), 1)
        self.assertFalse(late_path.exists())
        self.assertIsNone(session.prefetch)
        self.assertEqual(session.prefetch_tasks, set())

    async def test_stop_cleans_completed_prefetch_file(self):
        manager, session = self.manager([self.song("current"), self.song("next", ready=False)])
        path = self.root / "prepared.mp3"

        async def prepare(song):
            path.write_bytes(b"prepared")
            song.file_path = str(path)
            return song

        manager.on_download_song = prepare
        manager._start_playback_loop("guild")
        await wait_for(lambda: session.prefetch and session.prefetch.task.done())
        await manager.control("guild", "stop", actor_id="owner")
        self.assertFalse(path.exists())

    async def test_cancellation_resistant_player_start_cannot_survive_stop(self):
        entered = asyncio.Event()

        class LatePlayer(BlockingDirectPlayer):
            async def play(self, *args, **kwargs):
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    return await super().play(*args, **kwargs)

        manager, session = self.manager([self.song("current")], LatePlayer())
        manager._start_playback_loop("guild")
        await entered.wait()
        self.assertTrue((await manager.control("guild", "stop", actor_id="owner"))[0])
        self.assertFalse(session.ffmpeg_player.is_playing)
        self.assertEqual(session.playlist, [])
        self.assertNotIn("guild", manager._retry_tasks)

    async def test_stop_cancels_retry_without_disconnect(self):
        manager, session = self.manager([self.song("current")])
        session.playback_retry_count = 1
        manager.PLAYBACK_RETRY_DELAY = 60
        manager._schedule_playback_retry("guild", session)
        retry = manager._retry_tasks["guild"]
        await manager.control("guild", "stop", actor_id="owner")
        self.assertTrue(retry.done())
        self.assertEqual(session.playback_retry_count, 0)
        self.assertEqual(session.voice_client.disconnect_calls, 0)
        self.assertNotIn("guild", manager._retry_tasks)

    async def test_stop_cancels_pending_song_notification(self):
        manager, session = self.manager([self.song("current")])
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def notify(*args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        manager.on_song_started = notify
        manager._start_playback_loop("guild")
        await entered.wait()
        await manager.control("guild", "stop", actor_id="owner")
        self.assertTrue(cancelled.is_set())
        self.assertEqual(session.notification_tasks, set())

    async def test_stop_relay_stops_audio_and_marks_refresh_without_leaving(self):
        player = BlockingRelayPlayer()
        manager, session = self.manager([self.song("current")], player)
        manager._start_playback_loop("guild")
        await player.started.wait()
        await manager.control("guild", "stop", actor_id="owner")
        self.assertFalse(player.is_relay_running)
        self.assertTrue(session.needs_relay_refresh)
        self.assertEqual(session.voice_client.disconnect_calls, 0)

    async def test_stop_then_new_song_reuses_session_and_plays_normally(self):
        manager, session = self.manager([self.song("current")])
        manager._start_playback_loop("guild")
        await wait_for(lambda: session.ffmpeg_player.is_playing)
        await manager.control("guild", "stop", actor_id="owner")
        await manager.join_and_play("token", "guild", "voice", "text", self.song("new", "other"))
        await wait_for(lambda: len(session.ffmpeg_player.played) == 2)
        self.assertIs(manager.sessions["guild"], session)
        self.assertEqual(session.current_song.id, "new")
        self.assertTrue(session.ffmpeg_player.is_playing)
        self.assertEqual(session.voice_client.connect_calls, 1)

    async def test_stop_waits_for_inflight_initial_join(self):
        manager = VoiceManager(auto_leave_timeout=0, streaming_mode="direct")
        self.managers.append(manager)
        entered = asyncio.Event()
        release = asyncio.Event()
        client = FakeVoiceClient("token")

        async def connect(channel_id):
            entered.set()
            await release.wait()
            return True

        client.connect = connect
        with (
            patch(f"{MODULE}.VoiceClient", return_value=client),
            patch(f"{MODULE}.create_player", return_value=BlockingDirectPlayer()),
            patch.object(manager, "_start_playback_loop"),
        ):
            join = asyncio.create_task(manager.join_and_play(
                "token", "guild", "voice", "text", self.song("first"),
            ))
            await entered.wait()
            stop = asyncio.create_task(manager.control("guild", "stop", actor_id="owner"))
            await asyncio.sleep(0)
            self.assertFalse(stop.done())
            release.set()
            self.assertTrue((await join)[0])
            self.assertTrue((await stop)[0])
        self.assertEqual(manager.sessions["guild"].playlist, [])
        self.assertEqual(client.disconnect_calls, 0)

    async def test_clear_keeps_current_and_next_keeps_original_skip_semantics(self):
        manager, session = self.manager([self.song("current"), self.song("next")])
        manager._start_playback_loop("guild")
        await wait_for(lambda: session.ffmpeg_player.is_playing)
        self.assertTrue((await manager.control("guild", "clear", actor_id="owner"))[0])
        self.assertEqual(session.current_song.id, "current")
        self.assertTrue(session.ffmpeg_player.is_playing)
        self.assertTrue((await manager.control("guild", "next", actor_id="owner"))[0])
        await wait_for(lambda: not session.is_playing)
        self.assertEqual(session.playlist, [])


if __name__ == "__main__":
    unittest.main()
