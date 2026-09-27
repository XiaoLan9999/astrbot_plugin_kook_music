import asyncio
import unittest
from unittest.mock import patch

from test_relay_audio import Process

# isort: split

from astrbot_plugin_kook_music.kook_voice import ffmpeg_player as module
from astrbot_plugin_kook_music.kook_voice.ffmpeg_player import RelayFFmpegPlayer


async def until(predicate, timeout=1):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.001)


class StreamBufferTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.player = RelayFFmpegPlayer()
        self.player.FRAME_SECONDS = 0.002
        self.player.WARMUP_SECONDS = 0.004
        self.player.SOURCE_READY_TIMEOUT = 0.5
        self.encoder = Process()
        self.decoder = Process(eof=False)
        self.tasks = []

        async def spawn(*command, **_kwargs):
            return self.encoder if "pipe:0" in command else self.decoder

        replacement = patch.object(module.asyncio, "create_subprocess_exec", spawn)
        replacement.start()
        self.addCleanup(replacement.stop)
        self.assertTrue(
            await self.player.start_relay("rtp://127.0.0.1:5000?rtcpport=5001", 42)
        )

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.player.stop_relay()

    def feed(self, count, value=1, *, eof=False):
        self.decoder.stdout.feed_data(bytes([value]) * self.player.FRAME_BYTES * count)
        if eof:
            self.decoder.stdout.feed_eof()

    def play(self, **kwargs):
        task = asyncio.create_task(self.player.play(__file__, **kwargs))
        self.tasks.append(task)
        return task

    def audio(self):
        return [frame for frame in self.encoder.stdin.frames if any(frame)]

    async def start_and_underflow(self, *, refill=0.06):
        self.feed(2)
        self.assertTrue(
            await self.play(
                startup_buffer_seconds=0.04,
                max_buffer_seconds=0.5,
                rebuffer_seconds=refill,
            )
        )
        await until(
            lambda: self.player.played_seconds >= 0.04 and self.player.is_buffering
        )
        return self.player._song

    async def test_default_local_track_still_has_25_frames_and_immediate_ready(self):
        self.feed(1)
        self.assertTrue(await self.play())
        self.assertEqual(self.player._song.frames.maxsize, 25)
        self.assertFalse(self.player._song.controlled_buffering)
        self.assertFalse(self.player.is_buffering)
        self.decoder.stdout.feed_eof()
        self.assertTrue(await self.player.wait_until_done(1))
        self.assertAlmostEqual(self.player.played_seconds, 0.02)

    async def test_startup_waits_for_media_frame_target_while_encoder_sends_silence(
        self,
    ):
        self.feed(4)
        task = self.play(
            startup_buffer_seconds=0.1, max_buffer_seconds=0.2, rebuffer_seconds=0.06
        )
        await until(lambda: self.player.buffered_seconds == 0.08)
        await asyncio.sleep(0.02)
        self.assertFalse(task.done())
        self.assertTrue(self.player.is_buffering)
        self.assertEqual(self.player.played_seconds, 0)
        self.assertFalse(self.audio())
        self.feed(1)
        self.assertTrue(await task)
        await until(lambda: self.player.played_seconds >= 0.1)
        self.assertEqual(len(self.audio()), 5)

    async def test_buffer_seconds_use_pcm_media_duration_not_accelerated_feed_clock(
        self,
    ):
        self.player.FRAME_SECONDS = 0.0005
        self.feed(4)
        task = self.play(startup_buffer_seconds=0.1, max_buffer_seconds=0.5)
        await until(lambda: self.player.buffered_seconds == 0.08)
        self.assertEqual(self.player._song.startup_frames, 5)
        self.assertEqual(self.player._song.frames.maxsize, 25)
        self.assertFalse(task.done())
        self.feed(1, eof=True)
        self.assertTrue(await task)
        self.assertTrue(await self.player.wait_until_done(1))
        self.assertAlmostEqual(self.player.played_seconds, 0.1)

    async def test_short_source_eof_releases_less_than_startup_target(self):
        self.feed(3, eof=True)
        self.assertTrue(
            await self.play(
                startup_buffer_seconds=15, max_buffer_seconds=120, rebuffer_seconds=5
            )
        )
        self.assertTrue(await self.player.wait_until_done(1))
        self.assertEqual(len(self.audio()), 3)
        self.assertAlmostEqual(self.player.played_seconds, 0.06)

    async def test_partial_short_eof_frame_is_padded_and_drained(self):
        self.decoder.stdout.feed_data(bytes([3]) * (self.player.FRAME_BYTES + 100))
        self.decoder.stdout.feed_eof()
        self.assertTrue(
            await self.play(startup_buffer_seconds=15, max_buffer_seconds=120)
        )
        self.assertTrue(await self.player.wait_until_done(1))
        self.assertEqual(len(self.audio()), 2)
        self.assertEqual(
            self.audio()[-1], bytes([3]) * 100 + bytes(self.player.FRAME_BYTES - 100)
        )

    async def test_decoder_alone_sets_ready_without_running_feed_task(self):
        await self.player._cancel_task(self.player._feed_task)
        self.player._feed_task = None
        self.player._relay_failed = False
        self.feed(3, eof=True)
        self.assertTrue(
            await self.play(startup_buffer_seconds=0.04, max_buffer_seconds=0.5)
        )
        self.assertEqual(self.player._song.frames.qsize(), 3)
        self.assertFalse(self.player.is_buffering)

    async def test_underflow_waits_for_complete_refill_instead_of_dribbling_audio(self):
        await self.start_and_underflow()
        self.assertEqual(len(self.audio()), 2)
        self.feed(1, value=2)
        await until(lambda: self.player.buffered_seconds == 0.02)
        await asyncio.sleep(0.012)
        self.assertEqual(len(self.audio()), 2)
        self.feed(1, value=2)
        await until(lambda: self.player.buffered_seconds == 0.04)
        await asyncio.sleep(0.012)
        self.assertEqual(len(self.audio()), 2)
        self.feed(1, value=2, eof=True)
        self.assertTrue(await self.player.wait_until_done(1))
        self.assertEqual([frame[0] for frame in self.audio()], [1, 1, 2, 2, 2])
        self.assertAlmostEqual(self.player.played_seconds, 0.1)

    async def test_eof_during_refill_drains_remaining_below_threshold(self):
        await self.start_and_underflow(refill=0.2)
        self.feed(2, value=2, eof=True)
        self.assertTrue(await self.player.wait_until_done(1))
        self.assertEqual([frame[0] for frame in self.audio()], [1, 1, 2, 2])
        self.assertFalse(self.player.is_buffering)

    async def test_empty_eof_during_refill_finishes_without_hanging(self):
        await self.start_and_underflow(refill=0.2)
        self.decoder.stdout.feed_eof()
        self.assertTrue(await self.player.wait_until_done(1))
        self.assertAlmostEqual(self.player.played_seconds, 0.04)

    async def test_refill_silence_does_not_increment_played_media_time(self):
        await self.start_and_underflow()
        self.assertAlmostEqual(self.player.played_seconds, 0.04)
        before = len(self.encoder.stdin.frames)
        await asyncio.sleep(0.025)
        self.assertGreater(len(self.encoder.stdin.frames), before + 1)
        self.assertTrue(
            all(not any(frame) for frame in self.encoder.stdin.frames[before:])
        )
        self.assertAlmostEqual(self.player.played_seconds, 0.04)

    async def test_cumulative_rebuffer_wait_is_excluded_from_playback_timeout(self):
        await self.start_and_underflow(refill=0.04)
        started = asyncio.get_running_loop().time()
        waiter = asyncio.create_task(self.player.wait_until_done(0.12))
        self.tasks.append(waiter)
        await asyncio.sleep(0.1)
        self.feed(2)
        await until(
            lambda: self.player.played_seconds >= 0.08 and self.player.is_buffering
        )
        await asyncio.sleep(0.1)
        self.feed(2, eof=True)
        self.assertTrue(await waiter)
        self.assertGreaterEqual(asyncio.get_running_loop().time() - started, 0.2)
        self.assertAlmostEqual(self.player.played_seconds, 0.12)

    async def test_unknown_duration_stall_is_bounded_and_keeps_resume_counter(self):
        self.player.SOURCE_READY_TIMEOUT = 0.04
        track = await self.start_and_underflow(refill=0.2)
        self.assertFalse(await asyncio.wait_for(self.player.wait_until_done(), 0.3))
        self.assertAlmostEqual(self.player.played_seconds, 0.04)
        self.assertTrue(track.done.is_set())
        self.assertFalse(self.player.is_buffering)
        self.assertTrue(self.player.is_relay_running)
        self.assertTrue(self.decoder.cleaned)
        self.assertEqual(track.frames.qsize(), 0)

    async def test_drip_frames_do_not_reset_a_single_stall_deadline(self):
        self.player.SOURCE_READY_TIMEOUT = 0.07
        track = await self.start_and_underflow(refill=0.4)
        started = asyncio.get_running_loop().time()

        async def drip():
            while self.decoder.returncode is None:
                await asyncio.sleep(0.015)
                if self.decoder.returncode is None:
                    self.feed(1)

        producer = asyncio.create_task(drip())
        self.tasks.append(producer)
        self.assertFalse(await asyncio.wait_for(self.player.wait_until_done(), 0.3))
        self.assertLess(asyncio.get_running_loop().time() - started, 0.15)
        self.assertAlmostEqual(self.player.played_seconds, 0.04)
        self.assertEqual(track.frames.qsize(), 0)

    async def test_each_successful_refill_gets_its_own_stall_window(self):
        self.player.SOURCE_READY_TIMEOUT = 0.08
        await self.start_and_underflow(refill=0.04)
        for emitted in (0.08, 0.12):
            await asyncio.sleep(0.045)
            self.feed(2)
            await until(
                lambda: (
                    self.player.played_seconds >= emitted and self.player.is_buffering
                )
            )
        self.decoder.stdout.feed_eof()
        self.assertTrue(await self.player.wait_until_done(1))

    async def test_startup_timeout_releases_decoder_and_queued_audio(self):
        self.player.SOURCE_READY_TIMEOUT = 0.025
        self.feed(2)
        task = self.play(startup_buffer_seconds=0.2, max_buffer_seconds=0.5)
        await until(lambda: self.player._song is not None)
        track = self.player._song
        self.assertFalse(await task)
        self.assertTrue(self.decoder.cleaned)
        self.assertEqual(track.frames.qsize(), 0)
        self.assertTrue(self.player.is_relay_running)
        self.assertFalse(self.audio())

    async def test_nonbuffering_playback_timeout_still_enforces_finite_budget(self):
        self.feed(100)
        self.assertTrue(
            await self.play(
                startup_buffer_seconds=0.04, max_buffer_seconds=1, rebuffer_seconds=0.04
            )
        )
        self.assertFalse(await self.player.wait_until_done(0.005))
        self.assertTrue(self.decoder.cleaned)
        self.assertTrue(self.player.is_relay_running)
        self.assertGreater(self.player.played_seconds, 0)

    async def test_stop_releases_large_queue_and_retains_encoder_silence(self):
        self.encoder.stdin.block = True
        await self.encoder.stdin.blocked.wait()
        self.feed(30)
        self.assertTrue(
            await self.play(
                startup_buffer_seconds=0.04,
                max_buffer_seconds=0.2,
                rebuffer_seconds=0.06,
            )
        )
        track = self.player._song
        await until(lambda: track.frames.qsize() == 10)
        self.assertFalse(track.decoder_task.done())
        await self.player.stop()
        self.assertEqual(track.frames.qsize(), 0)
        self.assertTrue(track.decoder_task.done())
        self.assertTrue(self.decoder.cleaned)
        self.assertTrue(self.player.is_relay_running)
        before = len(self.encoder.stdin.frames)
        self.encoder.stdin.release.set()
        await asyncio.sleep(0.01)
        self.assertTrue(
            all(not any(frame) for frame in self.encoder.stdin.frames[before:])
        )

    async def test_cancel_startup_wait_cleans_process_and_buffer(self):
        self.feed(2)
        task = self.play(startup_buffer_seconds=0.2, max_buffer_seconds=1)
        await until(lambda: self.player.buffered_seconds == 0.04)
        track = self.player._song
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.decoder.cleaned)
        self.assertEqual(track.frames.qsize(), 0)
        self.assertTrue(self.player.is_relay_running)

    async def test_cancel_buffered_completion_wait_cleans_process_and_buffer(self):
        track = await self.start_and_underflow()
        waiter = asyncio.create_task(self.player.wait_until_done())
        self.tasks.append(waiter)
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertTrue(self.decoder.cleaned)
        self.assertEqual(track.frames.qsize(), 0)
        self.assertFalse(self.player.is_playing)
        self.assertTrue(self.player.is_relay_running)

    async def test_failed_eof_before_startup_drains_available_pcm_then_reports_failure(
        self,
    ):
        self.feed(2, eof=True)
        self.decoder.returncode = 1
        self.assertTrue(
            await self.play(startup_buffer_seconds=0.2, max_buffer_seconds=1)
        )
        self.assertFalse(await self.player.wait_until_done(1))
        self.assertTrue(self.decoder.cleaned)
        self.assertEqual(len(self.audio()), 2)
        self.assertAlmostEqual(self.player.played_seconds, 0.04)

    async def test_failed_eof_without_audio_still_fails_immediately(self):
        self.decoder.stdout.feed_eof()
        self.decoder.returncode = 1
        self.assertFalse(
            await self.play(startup_buffer_seconds=15, max_buffer_seconds=120)
        )
        self.assertFalse(self.audio())
        self.assertTrue(self.decoder.cleaned)

    async def test_nonzero_eof_retains_buffer_until_all_pcm_is_emitted(self):
        self.encoder.stdin.block = True
        await self.encoder.stdin.blocked.wait()
        self.feed(20, value=4, eof=True)
        self.decoder.returncode = 1
        self.assertTrue(
            await self.play(
                startup_buffer_seconds=0.04,
                max_buffer_seconds=120,
                rebuffer_seconds=5,
            )
        )
        track = self.player._song
        await until(lambda: track.decoded)
        self.assertEqual(track.frames.qsize(), 20)
        self.assertTrue(track.failed_eof)
        self.assertFalse(track.done.is_set())
        waiter = asyncio.create_task(self.player.wait_until_done(1))
        self.tasks.append(waiter)
        await asyncio.sleep(0.015)
        self.assertFalse(waiter.done())
        self.encoder.stdin.release.set()
        self.assertFalse(await waiter)
        self.assertEqual([frame[0] for frame in self.audio()], [4] * 20)
        self.assertAlmostEqual(self.player.played_seconds, 0.4)
        self.assertEqual(track.frames.qsize(), 0)
        self.assertTrue(self.decoder.cleaned)
        self.assertTrue(self.player.is_relay_running)

    async def test_failed_eof_during_rebuffer_drains_below_refill_target(self):
        await self.start_and_underflow(refill=0.2)
        self.feed(2, value=2, eof=True)
        self.decoder.returncode = 1
        self.assertFalse(await self.player.wait_until_done(1))
        self.assertEqual([frame[0] for frame in self.audio()], [1, 1, 2, 2])
        self.assertAlmostEqual(self.player.played_seconds, 0.08)

    async def test_failed_short_clip_already_emitted_before_ready_still_preserves_seek_time(
        self,
    ):
        async def finished_before_ready(track):
            track.has_audio = True
            track.failed_eof = True
            track.played_bytes = self.player.FRAME_BYTES
            track.exit_code = 1
            track.decoded = True
            track.ready.set()
            track.done.set()

        with patch.object(self.player, "_decode_song", finished_before_ready):
            self.assertTrue(
                await self.play(startup_buffer_seconds=15, max_buffer_seconds=120)
            )
        self.assertFalse(await self.player.wait_until_done(1))
        self.assertAlmostEqual(self.player.played_seconds, 0.02)
        self.assertTrue(self.decoder.cleaned)

    async def test_failed_decoder_after_playback_preserves_emitted_seconds(self):
        await self.start_and_underflow()
        self.decoder.returncode = 1
        self.decoder.stdout.feed_eof()
        self.assertFalse(await self.player.wait_until_done(1))
        self.assertAlmostEqual(self.player.played_seconds, 0.04)
        self.assertTrue(self.decoder.cleaned)

    async def test_decoder_exception_log_contains_no_signed_url_or_exception_text(self):
        self.decoder.stdout.set_exception(
            ValueError("https://example.invalid/audio?secret=synthetic-private")
        )
        with self.assertLogs("astrbot", level="DEBUG") as captured:
            self.assertFalse(
                await self.play(startup_buffer_seconds=0.2, max_buffer_seconds=1)
            )
        text = "\n".join(captured.output)
        self.assertNotIn("synthetic-private", text)
        self.assertNotIn("https://example.invalid", text)
        self.assertIn("ValueError", text)

    async def test_broken_encoder_cleans_buffered_decoder(self):
        track = await self.start_and_underflow()
        self.encoder.stdin.fail = True
        self.assertFalse(await asyncio.wait_for(self.player.wait_until_done(), 0.3))
        self.assertTrue(self.decoder.cleaned)
        self.assertEqual(track.frames.qsize(), 0)

    async def test_capacity_and_threshold_parameters_are_clamped_safely(self):
        self.assertEqual(
            self.player._buffer_settings(15, 120, 5), (6000, 750, 250, True)
        )
        self.assertEqual(
            self.player._buffer_settings(99999, 99999, 99999),
            (15000, 15000, 15000, True),
        )
        self.assertEqual(self.player._buffer_settings(15, 0.5, 5), (25, 25, 25, True))
        self.assertEqual(self.player._buffer_settings(-1, 0, -5), (1, 1, 0, False))
        for value in (float("nan"), float("inf"), None, "invalid", True):
            self.assertEqual(
                self.player._buffer_settings(value, value, value), (25, 1, 0, False)
            )

    async def test_maximum_buffer_allocates_bounded_queue_not_unlimited_queue(self):
        task = self.play(
            startup_buffer_seconds=300, max_buffer_seconds=99999, rebuffer_seconds=5
        )
        await until(lambda: self.player._song is not None)
        self.assertEqual(self.player._song.frames.maxsize, 15000)
        self.assertLessEqual(
            self.player._song.frames.maxsize * self.player.FRAME_BYTES, 57_600_000
        )
        await self.player.stop()
        self.assertFalse(await task)

    async def test_startup_stall_before_spawn_returns_reaps_late_decoder(self):
        self.player.SOURCE_READY_TIMEOUT = 0.025
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_spawn(*_args, **_kwargs):
            entered.set()
            await release.wait()
            return self.decoder

        with patch.object(module.asyncio, "create_subprocess_exec", slow_spawn):
            task = self.play(startup_buffer_seconds=0.2, max_buffer_seconds=1)
            await entered.wait()
            track = self.player._song
            await until(lambda: track.done.is_set())
            release.set()
            self.assertFalse(await task)
        self.assertTrue(self.decoder.cleaned)
        self.assertEqual(self.decoder.returncode, -9)
        self.assertIsNone(self.player._song)
        self.assertTrue(self.player.is_relay_running)

    async def test_buffer_observation_properties_are_read_only(self):
        self.assertEqual(self.player.buffered_seconds, 0)
        self.assertFalse(self.player.is_buffering)
        with self.assertRaises(AttributeError):
            self.player.buffered_seconds = 5
        with self.assertRaises(AttributeError):
            self.player.is_buffering = True


if __name__ == "__main__":
    unittest.main()
