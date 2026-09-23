# ruff: noqa: E402
import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

PLUGINS_DIR = Path(__file__).resolve().parents[2]
if str(PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGINS_DIR))

from astrbot_plugin_kook_music.kook_voice.voice_manager import GuildSession, VoiceManager
from astrbot_plugin_kook_music.music.model import Song
from test_voice_disconnect import LocalVoiceClient
from test_voice_manager import BlockingDirectPlayer, FakeVoiceClient, wait_for


MODULE = "astrbot_plugin_kook_music.kook_voice.voice_manager"


class IdleRequestRaceTests(unittest.IsolatedAsyncioTestCase):
    def make_manager(self):
        manager = VoiceManager(auto_leave_timeout=20, streaming_mode="direct")
        session = GuildSession(
            "guild", "voice", "text", FakeVoiceClient("token"), BlockingDirectPlayer()
        )
        manager.sessions["guild"] = session
        session.idle_seconds = 10
        return manager, session

    async def test_request_reservation_suspends_idle_during_slow_download(self):
        manager, session = self.make_manager()
        with manager.request_activity("guild") as request:
            for _ in range(20):
                await manager._check_session_idle("guild", session)
            self.assertIs(manager.sessions["guild"], session)
            self.assertEqual(session.idle_seconds, 0)
            self.assertTrue(request.current)
        self.assertFalse(request.current)
        await manager._check_session_idle("guild", session)
        self.assertIs(manager.sessions["guild"], session)
        await manager._check_session_idle("guild", session)
        self.assertNotIn("guild", manager.sessions)

    async def test_nested_requests_do_not_release_each_others_reservation(self):
        manager, session = self.make_manager()
        with manager.request_activity("guild") as first:
            with manager.request_activity("guild") as second:
                self.assertTrue(first.current and second.current)
            self.assertFalse(second.current)
            for _ in range(3):
                await manager._check_session_idle("guild", session)
            self.assertIs(manager.sessions["guild"], session)
        self.assertEqual(manager._activity_requests, {})

    async def test_cancelled_request_releases_reservation(self):
        manager, session = self.make_manager()
        entered = asyncio.Event()

        async def parse():
            with manager.request_activity("guild"):
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(parse())
        await entered.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(manager._activity_requests, {})
        self.assertEqual(session.idle_seconds, 0)
        await manager.leave_all()

    async def test_idle_rechecks_after_waiting_for_guild_lock(self):
        manager, session = self.make_manager()
        lock = manager._guild_locks.setdefault("guild", asyncio.Lock())
        async with lock:
            check = asyncio.create_task(manager._check_session_idle("guild", session))
            await asyncio.sleep(0)
            session.playlist.append(Song(id="new", name="new", file_path="fake"))
        await check
        self.assertIs(manager.sessions["guild"], session)
        self.assertEqual(session.idle_seconds, 0)
        await manager.leave_all()

    async def test_stale_idle_snapshot_cannot_remove_new_session(self):
        manager, old = self.make_manager()
        replacement = GuildSession(
            "guild", "voice", "text", FakeVoiceClient("token"), BlockingDirectPlayer()
        )
        manager.sessions["guild"] = replacement
        await manager._check_session_idle("guild", old)
        self.assertIs(manager.sessions["guild"], replacement)
        self.assertEqual(replacement.idle_seconds, 0)
        await manager.leave_all()

    async def test_idle_connection_is_reused_without_own_exit_event(self):
        manager, session = self.make_manager()
        song = Song(id="new", name="new", stream_url="https://example.test/audio")
        with manager.request_activity("guild") as request:
            with patch.object(manager, "_start_playback_loop"):
                ok, _ = await manager.join_and_play(
                    "token", "guild", "voice", "text", song, request=request
                )
        self.assertTrue(ok)
        self.assertIs(manager.sessions["guild"], session)
        self.assertEqual(session.voice_client.disconnect_calls, 0)
        self.assertEqual(song.stream_url, "https://example.test/audio")
        await manager.leave_all()

    async def test_reuse_waits_for_old_eof_cleanup_before_starting_new_loop(self):
        manager, session = self.make_manager()
        manager.DIRECT_RECONNECT_DELAY = 0
        session.playlist = [Song(id="old", name="old", file_path="old")]
        callback_started = asyncio.Event()
        callback_stopped = asyncio.Event()

        async def finished(guild_id):
            callback_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                callback_stopped.set()

        manager.on_playback_finished = finished
        manager._start_playback_loop("guild")
        await wait_for(lambda: session.ffmpeg_player.is_playing)
        session.ffmpeg_player.finish()
        await callback_started.wait()
        old_task = manager._playback_tasks["guild"]
        self.assertTrue((await manager.join_and_play(
            "token", "guild", "voice", "text", Song(id="new", name="new", file_path="new"),
        ))[0])
        self.assertTrue(old_task.done())
        self.assertTrue(callback_stopped.is_set())
        await wait_for(lambda: session.ffmpeg_player.is_playing)
        self.assertEqual(session.current_song.id, "new")
        self.assertIs(manager.sessions["guild"], session)
        await manager.leave_all()

    async def test_join_waits_for_committed_idle_disconnect(self):
        manager, old = self.make_manager()
        disconnect_started = asyncio.Event()
        release_disconnect = asyncio.Event()

        async def disconnect():
            disconnect_started.set()
            await release_disconnect.wait()
            old.voice_client.is_alive = False

        old.voice_client.disconnect = disconnect
        idle_task = asyncio.create_task(manager._check_session_idle("guild", old))
        await disconnect_started.wait()
        with manager.request_activity("guild") as request:
            with (
                patch(f"{MODULE}.VoiceClient", side_effect=FakeVoiceClient),
                patch(f"{MODULE}.create_player", return_value=BlockingDirectPlayer()),
                patch.object(manager, "_start_playback_loop"),
                patch.object(manager, "_ensure_idle_check"),
            ):
                join = asyncio.create_task(manager.join_and_play(
                    "token", "guild", "voice", "text", Song(id="new", name="new"),
                    request=request,
                ))
                await asyncio.sleep(0)
                self.assertFalse(join.done())
                release_disconnect.set()
                await idle_task
                self.assertTrue((await join)[0])
                self.assertTrue(request.current)
        self.assertIsNot(manager.sessions["guild"], old)
        await manager.leave_all()

    async def test_real_kick_invalidates_downloading_request(self):
        manager, session = self.make_manager()
        client = LocalVoiceClient()
        session.voice_client = client
        manager.on_playback_finished = AsyncMock()
        with manager.request_activity("guild") as request:
            self.assertTrue(await manager.handle_voice_removed("voice", client))
            self.assertFalse(request.current)
            self.assertFalse((await manager.join_and_play(
                "token", "guild", "voice", "text", Song(id="late", name="late"),
                request=request,
            ))[0])
        self.assertTrue(client.remote_removed)
        self.assertNotIn("guild", manager.sessions)
        self.assertEqual(client.reconnect_calls, 0)

    async def test_explicit_leave_invalidates_request_but_new_request_is_valid(self):
        manager, _ = self.make_manager()
        with manager.request_activity("guild") as old:
            await manager.leave("guild")
            self.assertFalse(old.current)
            with manager.request_activity("guild") as new:
                self.assertTrue(new.current)
            self.assertFalse(new.current)

    async def test_foreign_or_wrong_guild_reservation_is_rejected(self):
        manager, _ = self.make_manager()
        other = VoiceManager()
        with other.request_activity("guild") as foreign:
            self.assertFalse((await manager.join_and_play(
                "token", "guild", "voice", "text", Song(id="x", name="x"),
                request=foreign,
            ))[0])
        with manager.request_activity("another") as mismatched:
            self.assertFalse((await manager.join_and_play(
                "token", "guild", "voice", "text", Song(id="x", name="x"),
                request=mismatched,
            ))[0])
        await manager.leave_all()

    async def test_late_previous_exit_waits_for_new_server_join_boundary(self):
        manager, session = self.make_manager()
        client = LocalVoiceClient()
        session.voice_client = client
        session.prior_departure_pending = True
        removed = asyncio.create_task(manager.handle_voice_removed("voice", client, 1500))
        await asyncio.sleep(0)
        self.assertFalse(removed.done())
        client.note_channel_joined("voice", 2000)
        self.assertFalse(await removed)
        self.assertFalse(client.remote_removed)
        self.assertIs(manager.sessions["guild"], session)
        await manager.leave_all()

    async def test_actual_new_exit_is_not_swallowed_by_prior_departure(self):
        manager, session = self.make_manager()
        client = LocalVoiceClient()
        session.voice_client = client
        session.prior_departure_pending = True
        removed = asyncio.create_task(manager.handle_voice_removed("voice", client, 2500))
        await asyncio.sleep(0)
        client.note_channel_joined("voice", 2000)
        self.assertTrue(await removed)
        self.assertTrue(client.remote_removed)
        self.assertNotIn("guild", manager.sessions)
        self.assertEqual(client.reconnect_calls, 0)

    async def test_unknown_exit_without_server_join_boundary_fails_closed(self):
        manager, session = self.make_manager()
        manager.PRIOR_EXIT_JOIN_GRACE = 0
        client = LocalVoiceClient()
        session.voice_client = client
        session.prior_departure_pending = True
        self.assertTrue(await manager.handle_voice_removed("voice", client, 2500))
        self.assertTrue(client.remote_removed)
        self.assertNotIn("guild", manager.sessions)

    async def test_deferred_exit_cannot_remove_replacement_session(self):
        manager, old = self.make_manager()
        client = LocalVoiceClient()
        old.voice_client = client
        old.prior_departure_pending = True
        removed = asyncio.create_task(manager.handle_voice_removed("voice", client, 2500))
        await asyncio.sleep(0)
        replacement = GuildSession(
            "guild", "voice", "text", FakeVoiceClient("token"), BlockingDirectPlayer()
        )
        manager.sessions["guild"] = replacement
        self.assertFalse(await removed)
        self.assertIs(manager.sessions["guild"], replacement)
        await client.disconnect()
        await manager.leave_all()


if __name__ == "__main__":
    unittest.main()
