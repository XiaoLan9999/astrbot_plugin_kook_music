import asyncio
import math
import sys
import unittest
from pathlib import Path


PLUGINS_DIR = Path(__file__).resolve().parents[2]
if str(PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGINS_DIR))

from astrbot_plugin_kook_music.kook_voice.ffmpeg_player import (  # noqa: E402
    DirectFFmpegPlayer,
    RelayFFmpegPlayer,
)
from astrbot_plugin_kook_music.kook_voice.voice_manager import GuildSession, VoiceManager  # noqa: E402
from astrbot_plugin_kook_music.music.model import Song  # noqa: E402


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


class Voice:
    token = "token"
    is_alive = True
    remote_removed = False
    rtp_url = "rtp://127.0.0.1:1"
    ssrc = 1234

    def __init__(self):
        self.connections = 0

    async def connect(self, channel):
        self.connections += 1
        return True

    async def reconnect(self, channel):
        return await self.connect(channel)

    async def disconnect(self):
        pass


class Relay(RelayFFmpegPlayer):
    def __init__(self):
        self.played = []
        self._playing = False
        self.done = asyncio.Event()
        self.elapsed = 0.0
        self.stops = 0
        self.relay_stops = 0
        self.fail_next = False

    @property
    def is_relay_running(self):
        return True

    @property
    def is_playing(self):
        return self._playing

    @property
    def played_seconds(self):
        return self.elapsed

    async def play(self, source, **options):
        self.played.append((source, options))
        self._playing = not self.fail_next
        self.fail_next = False
        self.elapsed = 0.0
        self.done = asyncio.Event()
        return self._playing

    async def wait_until_done(self, timeout=None):
        await self.done.wait()
        return True

    async def stop(self):
        self.stops += 1
        self._playing = False
        self.done.set()

    async def stop_relay(self):
        self.relay_stops += 1
        await self.stop()


class Direct(DirectFFmpegPlayer):
    def __init__(self):
        self._playing = False
        self.played = []
        self.done = asyncio.Event()

    @property
    def is_playing(self):
        return self._playing

    async def play(self, source, rtp, ssrc, **options):
        self.played.append((source, options))
        self._playing = True
        self.done = asyncio.Event()
        return True

    async def wait_until_done(self, timeout=None):
        await self.done.wait()
        return True

    async def stop(self):
        self._playing = False
        self.done.set()


class SeekPlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def make_manager(self, *, stream=False, direct=False, loop=0):
        manager = VoiceManager(auto_leave_timeout=0, prefetch_next=False)
        manager.DIRECT_RECONNECT_DELAY = 0
        player = Direct() if direct else Relay()
        first = Song(
            id="first", name="first", requester_id="owner", duration=120_000,
            file_path="" if stream else "first.m4a",
            stream_url="https://cdn.example.test/audio.m4s" if stream else "",
            platform="bilibili" if stream else "netease",
            extra_headers={"Referer": "https://www.bilibili.com/"} if stream else {},
        )
        second = Song(id="second", name="second", requester_id="other", file_path="second.m4a")
        session = GuildSession(
            "guild", "voice", "text", Voice(), player,
            playlist=[first, second], loop_mode=loop,
        )
        manager.sessions["guild"] = session
        notifications = []
        manager.on_song_started = lambda *args: notifications.append(
            (args[1], session.playback_offset_seconds)
        )
        manager._start_playback_loop("guild")
        self.addAsyncCleanup(manager.leave_all)
        await until(lambda: session.playback_ready and notifications)
        return manager, session, player, first, second, notifications

    async def seek(self, manager, **options):
        return await manager.control("guild", "seek", actor_id="owner", **options)

    async def test_absolute_seek_preserves_queue_loop_local_cache_and_relay(self):
        for loop in range(4):
            with self.subTest(loop=loop):
                manager, session, player, first, second, notices = await self.make_manager(loop=loop)
                original_task = manager._playback_tasks["guild"]
                ok, message = await self.seek(manager, target_seconds=30)
                self.assertTrue(ok, message)
                await until(lambda: session.playback_ready and len(notices) == 2)
                self.assertTrue(original_task.done())
                self.assertEqual(session.playlist, [first, second])
                self.assertEqual(session.loop_mode, loop)
                self.assertEqual(first.file_path, "first.m4a")
                self.assertEqual(player.played[-1][1]["start_seconds"], 30)
                self.assertEqual(session.playback_offset_seconds, 30)
                self.assertEqual(notices[-1], (first, 30))
                self.assertEqual(player.relay_stops, 0)
                self.assertEqual(session.voice_client.connections, 0)

    async def test_relative_seek_uses_actual_pcm_progress_and_current_offset(self):
        manager, session, player, *_ = await self.make_manager()
        session.playback_offset_seconds = 20
        player.elapsed = 10
        ok, message = await self.seek(manager, relative_seconds=-15)
        self.assertTrue(ok, message)
        await until(lambda: session.playback_ready)
        self.assertEqual(player.played[-1][1]["start_seconds"], 15)

    async def test_rewind_near_start_clamps_zero_but_forward_past_end_rejects(self):
        manager, session, player, *_ = await self.make_manager()
        player.elapsed = 3
        self.assertFalse((await self.seek(manager, relative_seconds=150))[0])
        self.assertEqual(session.seek_cooldown_until, 0)
        self.assertTrue((await self.seek(manager, relative_seconds=-15))[0])
        await until(lambda: session.playback_ready)
        self.assertEqual(player.played[-1][1].get("start_seconds", 0), 0)

    async def test_direct_seek_refreshes_transport_and_applies_offset(self):
        manager, session, player, *_ = await self.make_manager(direct=True)
        ok, message = await self.seek(manager, target_seconds=45)
        self.assertTrue(ok, message)
        await until(lambda: session.playback_ready)
        self.assertEqual(player.played[-1][1]["start_seconds"], 45)
        self.assertEqual(session.voice_client.connections, 1)

    async def test_stream_seek_resets_old_recovery_budget_and_forwards_headers(self):
        manager, session, player, first, *_ = await self.make_manager(stream=True)
        first.provider_data[manager._STREAM_RESUME_ATTEMPTS_KEY] = 1
        first.provider_data[manager._STREAM_RESUME_OFFSET_KEY] = 50
        ok, message = await self.seek(manager, target_seconds=10)
        self.assertTrue(ok, message)
        await until(lambda: session.playback_ready)
        self.assertEqual(player.played[-1][1]["start_seconds"], 10)
        self.assertEqual(player.played[-1][1]["extra_headers"], first.extra_headers)
        self.assertNotIn(manager._STREAM_RESUME_ATTEMPTS_KEY, first.provider_data)

    async def test_failed_stream_start_refreshes_at_requested_target_not_zero(self):
        manager, session, player, first, *_ = await self.make_manager(stream=True)
        refreshed = []

        async def prepare(song):
            refreshed.append(song)
            song.stream_url = "https://cdn.example.test/fresh.m4s"
            return song

        manager.on_download_song = prepare
        player.fail_next = True
        ok, message = await self.seek(manager, target_seconds=25)
        self.assertTrue(ok, message)
        await until(lambda: session.playback_ready)
        self.assertEqual(refreshed, [first])
        self.assertEqual(player.played[-1][1]["start_seconds"], 25)
        self.assertEqual(session.playlist[0], first)

    async def test_cooldown_is_shared_by_actors_and_only_affects_seek(self):
        manager, session, player, *_ = await self.make_manager()
        self.assertTrue((await self.seek(manager, target_seconds=30))[0])
        await until(lambda: session.playback_ready)
        generation = session.playback_generation
        ok, message = await manager.control(
            "guild", "seek", actor_id="admin", is_admin=True, target_seconds=40,
        )
        self.assertFalse(ok)
        self.assertIn("冷却", message)
        self.assertEqual(session.playback_generation, generation)
        self.assertEqual(len(player.played), 2)
        self.assertTrue((await manager.control("guild", "next", actor_id="owner"))[0])
        await until(lambda: session.current_song and session.current_song.id == "second")

    async def test_cooldown_expires_and_zero_target_restarts_from_beginning(self):
        manager, session, player, *_ = await self.make_manager()
        self.assertTrue((await self.seek(manager, target_seconds=30))[0])
        await until(lambda: session.playback_ready)
        session.seek_cooldown_until = 0
        self.assertTrue((await self.seek(manager, target_seconds=0))[0])
        await until(lambda: session.playback_ready)
        self.assertEqual(player.played[-1][1].get("start_seconds", 0), 0)

    async def test_unauthorized_and_stale_ownership_never_consume_cooldown(self):
        manager, session, player, first, second, _ = await self.make_manager()
        for options in (
            {"actor_id": "stranger"},
            {"actor_id": "owner", "expected_song": second},
            {"actor_id": "owner", "expected_generation": session.playback_generation + 1},
        ):
            ok, _ = await manager.control("guild", "seek", target_seconds=10, **options)
            self.assertFalse(ok)
        self.assertEqual(session.seek_cooldown_until, 0)
        self.assertEqual(player.played[0][0], first.file_path)
        self.assertEqual(len(player.played), 1)

    async def test_invalid_targets_do_not_stop_output_or_consume_cooldown(self):
        manager, session, player, *_ = await self.make_manager()
        for target in (-1, 120, 999, 10 ** 1000, math.inf, math.nan, -math.inf, True, "20"):
            with self.subTest(target=target):
                self.assertFalse((await self.seek(manager, target_seconds=target))[0])
        self.assertFalse((await self.seek(manager))[0])
        self.assertFalse((await self.seek(manager, target_seconds=20, relative_seconds=1))[0])
        self.assertEqual(session.seek_cooldown_until, 0)
        self.assertEqual(player.stops, 0)

    async def test_unknown_duration_preparing_switching_and_stopped_are_rejected(self):
        manager, session, player, first, *_ = await self.make_manager()
        first.duration = 0
        self.assertFalse((await self.seek(manager, target_seconds=1))[0])
        first.duration = 120_000
        session.playback_ready = False
        self.assertFalse((await self.seek(manager, target_seconds=1))[0])
        session.playback_ready = True
        session.pending_skips = 1
        self.assertFalse((await self.seek(manager, target_seconds=1))[0])
        session.pending_skips = 0
        session.is_playing = False
        self.assertFalse((await self.seek(manager, target_seconds=1))[0])
        self.assertEqual(session.seek_cooldown_until, 0)
        self.assertEqual(player.stops, 0)

    async def test_simultaneous_seeks_commit_only_once(self):
        manager, session, player, first, *_ = await self.make_manager()
        generation = session.playback_generation
        results = await asyncio.gather(*(
            self.seek(
                manager, target_seconds=target, expected_song=first,
                expected_generation=generation,
            )
            for target in (10, 20, 30)
        ))
        self.assertEqual(sum(result[0] for result in results), 1)
        await until(lambda: session.playback_ready)
        self.assertEqual(len(player.played), 2)

    async def test_stop_after_seek_prevents_late_restart_and_next_request_works(self):
        manager, session, player, *_ = await self.make_manager()
        self.assertTrue((await self.seek(manager, target_seconds=20))[0])
        self.assertTrue((await manager.control("guild", "stop", actor_id="owner"))[0])
        await asyncio.sleep(0)
        self.assertFalse(player.is_playing)
        self.assertFalse(session.playback_ready)
        self.assertEqual(session.playlist, [])
        song = Song(id="new", requester_id="owner", file_path="new.m4a", duration=90_000)
        self.assertTrue((await manager.add_song("guild", song))[0])
        await until(lambda: session.playback_ready)
        self.assertEqual(player.played[-1][0], "new.m4a")

    async def test_kicked_during_seek_teardown_cannot_restart_decoder(self):
        manager, session, player, *_ = await self.make_manager()
        original_stop = player.stop

        async def removed_stop():
            session.voice_client.remote_removed = True
            await original_stop()

        player.stop = removed_stop
        ok, _ = await self.seek(manager, target_seconds=10)
        self.assertFalse(ok)
        await asyncio.sleep(0)
        self.assertEqual(len(player.played), 1)
        self.assertFalse(player.is_playing)
        self.assertFalse(session.playback_ready)

    async def test_old_same_song_notification_is_invalid_after_generation_changes(self):
        manager, session, _, song, *_ = await self.make_manager()
        notices = []
        manager.on_song_started = lambda *args: notices.append(args)
        song.provider_data.pop(manager._STREAM_NOTIFIED_KEY, None)
        manager._schedule_song_notification(session, song)
        session.playback_generation += 1
        await asyncio.sleep(0)
        self.assertEqual(notices, [])

    async def test_seek_audio_does_not_wait_for_old_card_receipt_cleanup(self):
        manager, session, player, song, *_ = await self.make_manager()
        await until(lambda: not session.notification_tasks)
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def held_card_receipt(*args):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()

        manager.on_song_started = held_card_receipt
        song.provider_data.pop(manager._STREAM_NOTIFIED_KEY, None)
        manager._schedule_song_notification(session, song)
        await entered.wait()
        old_notification = next(iter(session.notification_tasks))
        new_notices = []
        manager.on_song_started = lambda *args: new_notices.append(
            session.playback_offset_seconds
        )
        try:
            ok, message = await asyncio.wait_for(
                self.seek(manager, target_seconds=35), timeout=1,
            )
            self.assertTrue(ok, message)
            await until(lambda: session.playback_ready and new_notices)
            self.assertTrue(cancelled.is_set())
            self.assertFalse(old_notification.done())
            self.assertIn(old_notification, session.notification_tasks)
            self.assertEqual(player.played[-1][1]["start_seconds"], 35)
            self.assertEqual(new_notices, [35])
        finally:
            release.set()
            await old_notification
        await until(lambda: not session.notification_tasks)

    async def test_stream_failure_after_manual_seek_resumes_from_new_progress(self):
        manager, session, player, song, *_ = await self.make_manager(stream=True)
        resume = asyncio.Event()
        original_wait = player.wait_until_done

        async def wait_until_done(timeout=None):
            if len(player.played) == 2:
                await resume.wait()
                player.elapsed = 12
                return False
            return await original_wait(timeout)

        async def prepare(current):
            current.stream_url = "https://cdn.example.test/fresh.m4s"
            return current

        player.wait_until_done = wait_until_done
        manager.on_download_song = prepare
        self.assertTrue((await self.seek(manager, target_seconds=30))[0])
        await until(lambda: session.playback_ready)
        resume.set()
        await until(lambda: len(player.played) == 3 and session.playback_ready)
        self.assertEqual(player.played[-1][1]["start_seconds"], 37)
        self.assertIs(session.current_song, song)

    async def test_cancelled_command_finishes_decoder_swap_before_unlocking(self):
        manager, session, player, first, second, _ = await self.make_manager()
        entered, release = asyncio.Event(), asyncio.Event()
        original_stop = player.stop

        async def held_stop():
            entered.set()
            await release.wait()
            await original_stop()

        player.stop = held_stop
        command = asyncio.create_task(self.seek(manager, target_seconds=35))
        await entered.wait()
        command.cancel()
        await asyncio.sleep(0)
        command.cancel()
        await asyncio.sleep(0)
        self.assertFalse(command.done())
        self.assertTrue(manager._guild_locks["guild"].locked())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await command
        await until(lambda: session.playback_ready)
        self.assertEqual(player.played[-1][1]["start_seconds"], 35)
        self.assertEqual(session.playlist, [first, second])
        self.assertFalse(session.seek_in_progress)

    async def test_unload_during_cancelled_seek_does_not_restart(self):
        manager, session, player, *_ = await self.make_manager()
        entered, release = asyncio.Event(), asyncio.Event()
        original_stop = player.stop

        async def held_stop():
            entered.set()
            await release.wait()
            await original_stop()

        player.stop = held_stop
        command = asyncio.create_task(self.seek(manager, target_seconds=35))
        await entered.wait()
        command.cancel()
        closing = asyncio.create_task(manager.leave_all())
        await asyncio.sleep(0)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await command
        await closing
        self.assertEqual(len(player.played), 1)
        self.assertFalse(player.is_playing)
        self.assertEqual(manager.sessions, {})


if __name__ == "__main__":
    unittest.main()
