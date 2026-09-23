import asyncio
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_kook_music.kook_voice import ffmpeg_player as module
from astrbot_plugin_kook_music.kook_voice.ffmpeg_player import RelayFFmpegPlayer


class Writer:
    def __init__(self):
        self.frames = []
        self.fail = False
        self.block = False
        self.blocked = asyncio.Event()
        self.release = asyncio.Event()

    def write(self, frame):
        if self.fail:
            raise BrokenPipeError("encoder failed")
        self.frames.append(frame)

    async def drain(self):
        if self.block:
            self.blocked.set()
            await self.release.wait()


class Process:
    def __init__(self, payload=b"", eof=True):
        self.pid = 1234
        self.returncode = None
        self.stdin = Writer()
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(payload)
        if eof:
            self.stdout.feed_eof()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()
        self.cleaned = False

    async def wait(self):
        if self.returncode is None:
            if not self.stdout.at_eof():
                await asyncio.Future()
            self.returncode = 0
        return self.returncode

    def kill(self):
        self.returncode = -9
        if not self.stdout.at_eof():
            self.stdout.feed_eof()

    async def communicate(self):
        self.cleaned = True
        return await self.stdout.read(), await self.stderr.read()


class RelayAudioTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.player = RelayFFmpegPlayer(volume=0.25)
        self.player.WARMUP_SECONDS = 0.04
        self.player.FRAME_SECONDS = 0.005
        self.encoder = Process()
        self.decoder = Process(bytes([1]) * self.player.FRAME_BYTES * 10)
        self.commands = []

        async def spawn(*command, **kwargs):
            self.commands.append((command, kwargs))
            return self.encoder if "pipe:0" in command else self.decoder

        self.patch = patch.object(module.asyncio, "create_subprocess_exec", spawn)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.assertTrue(await self.player.start_relay("rtp://127.0.0.1:5000?rtcpport=5001", 42))

    async def asyncTearDown(self):
        await self.player.stop_relay()

    async def test_start_uses_only_silence_and_no_mpegts_probe(self):
        self.assertGreater(len(self.encoder.stdin.frames), 1)
        self.assertTrue(all(not any(frame) for frame in self.encoder.stdin.frames))
        command, kwargs = self.commands[0]
        self.assertNotIn("nobuffer", command)
        self.assertNotIn("mpegts", command)
        self.assertIn("s16le", command)
        self.assertEqual(command[command.index("-frame_duration") + 1], "20")
        self.assertEqual(kwargs["stdin"], asyncio.subprocess.PIPE)

    async def test_decoder_eof_does_not_end_playback_before_queued_audio(self):
        self.assertTrue(await self.player.play(__file__))
        await asyncio.sleep(0)
        self.assertEqual(self.decoder.returncode, 0)
        self.assertTrue(self.player.is_playing)
        self.assertFalse(self.player._song.done.is_set())
        self.assertTrue(await self.player.wait_until_done(timeout=1))
        audio = [frame for frame in self.encoder.stdin.frames if any(frame)]
        self.assertEqual(len(audio), 10)
        self.assertFalse(self.player.is_playing)

    async def test_played_seconds_excludes_warmup_and_decode_stall_silence(self):
        self.assertEqual(self.player.played_seconds, 0)
        self.decoder = Process(bytes([1]) * self.player.FRAME_BYTES * 2, eof=False)
        self.assertTrue(await self.player.play(__file__))
        while self.player.played_seconds < 0.04:
            await asyncio.sleep(0.001)
        await asyncio.sleep(0.02)
        self.assertAlmostEqual(self.player.played_seconds, 0.04)
        self.decoder.stdout.feed_data(bytes(self.player.FRAME_BYTES))
        self.decoder.stdout.feed_eof()
        self.assertTrue(await self.player.wait_until_done(timeout=1))
        self.assertAlmostEqual(self.player.played_seconds, 0.06)

    async def test_partial_final_frame_is_padded_once(self):
        self.decoder = Process(bytes([7]) * 100)
        self.assertTrue(await self.player.play(__file__))
        self.assertTrue(await self.player.wait_until_done(timeout=1))
        audio = [frame for frame in self.encoder.stdin.frames if any(frame)]
        self.assertEqual(audio, [bytes([7]) * 100 + bytes(self.player.FRAME_BYTES - 100)])

    async def test_short_clip_completed_before_ready_wait_resumes_is_success(self):
        async def completed_clip(track):
            track.has_audio = True
            track.played_bytes = self.player.FRAME_BYTES
            track.exit_code = 0
            track.decoded = True
            track.ready.set()
            track.done.set()

        with patch.object(self.player, "_decode_song", completed_clip):
            self.assertTrue(await self.player.play(__file__))
        self.assertTrue(await self.player.wait_until_done())

    async def test_decoder_buffer_is_bounded(self):
        self.decoder = Process(bytes([3]) * self.player.FRAME_BYTES * 100)
        self.assertTrue(await self.player.play(__file__))
        track = self.player._song
        await asyncio.sleep(0.01)
        self.assertLessEqual(track.frames.qsize(), 25)
        self.assertFalse(track.decoder_task.done())
        await self.player.stop()
        self.assertTrue(track.decoder_task.done())
        self.assertTrue(self.decoder.cleaned)

    async def test_stop_drops_buffer_and_retains_silent_relay(self):
        self.assertTrue(await self.player.play(__file__))
        await self.player.stop()
        count = len(self.encoder.stdin.frames)
        await asyncio.sleep(0.02)
        self.assertTrue(self.player.is_relay_running)
        self.assertTrue(self.decoder.cleaned)
        self.assertFalse(self.player.is_playing)
        self.assertTrue(all(not any(frame) for frame in self.encoder.stdin.frames[count:]))

    async def test_stop_cancels_source_preparation_without_leaking_process(self):
        self.decoder = Process(eof=False)
        task = asyncio.create_task(self.player.play(__file__))
        while self.player._song is None or self.player._song.decoder_task is None:
            await asyncio.sleep(0)
        await self.player.stop()
        self.assertFalse(await task)
        self.assertTrue(self.decoder.cleaned)
        self.assertEqual(self.decoder.returncode, -9)

    async def test_source_timeout_stops_decoder_but_preserves_relay(self):
        self.decoder = Process(eof=False)
        self.player.SOURCE_READY_TIMEOUT = 0.01
        self.assertFalse(await self.player.play(__file__))
        self.assertTrue(self.decoder.cleaned)
        self.assertTrue(self.player.is_relay_running)

    async def test_cancel_during_spawn_reaps_late_process(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed_spawn(*args, **kwargs):
            entered.set()
            await release.wait()
            return self.decoder

        with patch.object(module.asyncio, "create_subprocess_exec", delayed_spawn):
            task = asyncio.create_task(self.player.play(__file__))
            await entered.wait()
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(self.decoder.cleaned)
        self.assertFalse(self.player.is_playing)

    async def test_repeated_cancel_during_spawn_still_reaps_late_process(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed_spawn(*args, **kwargs):
            entered.set()
            await release.wait()
            return self.decoder

        with patch.object(module.asyncio, "create_subprocess_exec", delayed_spawn):
            task = asyncio.create_task(self.player.play(__file__))
            await entered.wait()
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(0)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(self.decoder.cleaned)
        self.assertFalse(self.player.is_playing)

    async def test_repeated_cancel_during_stop_relay_finishes_cleanup(self):
        self.assertTrue(await self.player.play(__file__))
        entered, release = asyncio.Event(), asyncio.Event()
        communicate = self.decoder.communicate

        async def delayed_cleanup():
            entered.set()
            await release.wait()
            return await communicate()

        self.decoder.communicate = delayed_cleanup
        task = asyncio.create_task(self.player.stop_relay())
        await entered.wait()
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.decoder.cleaned)
        self.assertTrue(self.encoder.cleaned)
        self.assertFalse(self.player.is_relay_running)

    async def test_stale_inflight_frame_cannot_reappear_after_next_song(self):
        self.assertTrue(await self.player.play(__file__))
        self.encoder.stdin.block = True
        await self.encoder.stdin.blocked.wait()
        await self.player.stop()
        self.decoder = Process(bytes([2]) * self.player.FRAME_BYTES * 2)
        self.assertTrue(await self.player.play(__file__))
        before_release = len(self.encoder.stdin.frames)
        self.encoder.stdin.release.set()
        self.assertTrue(await self.player.wait_until_done(timeout=1))
        after_release = self.encoder.stdin.frames[before_release:]
        self.assertTrue(any(frame[0] == 2 for frame in after_release))
        self.assertTrue(all(frame[0] != 1 for frame in after_release))

    async def test_encoder_broken_pipe_finishes_unknown_duration_wait(self):
        self.assertTrue(await self.player.play(__file__))
        self.encoder.stdin.fail = True
        self.assertFalse(await asyncio.wait_for(self.player.wait_until_done(), 0.3))
        self.assertFalse(self.player.is_relay_running)

    async def test_encoder_exit_finishes_unknown_duration_wait(self):
        self.assertTrue(await self.player.play(__file__))
        self.encoder.returncode = 1
        self.assertFalse(await asyncio.wait_for(self.player.wait_until_done(), 0.3))

    async def test_wait_timeout_cleans_track_without_stopping_encoder(self):
        self.assertTrue(await self.player.play(__file__))
        self.assertFalse(await self.player.wait_until_done(timeout=0.001))
        self.assertTrue(self.decoder.cleaned)
        self.assertTrue(self.player.is_relay_running)

    async def test_stop_relay_cleans_all_processes_and_tasks(self):
        self.assertTrue(await self.player.play(__file__))
        track = self.player._song
        feed = self.player._feed_task
        stderr = self.player._relay_stderr_task
        await self.player.stop_relay()
        self.assertTrue(self.decoder.cleaned)
        self.assertTrue(self.encoder.cleaned)
        for task in (feed, stderr, track.decoder_task, track.stderr_task):
            self.assertTrue(task.done())
        self.assertFalse(self.player.is_relay_running)

    async def test_empty_or_failed_source_is_not_reported_as_playing(self):
        for returncode in (0, 1):
            with self.subTest(returncode=returncode):
                self.decoder = Process()
                self.decoder.returncode = returncode
                self.assertFalse(await self.player.play(__file__))
                self.assertFalse(self.player.is_playing)

    async def test_song_decoder_preserves_http_seek_and_volume_without_re(self):
        self.assertTrue(await self.player.play(
            "https://cdn.example.test/audio?token=secret", start_seconds=120.5,
            extra_headers={"Referer": "https://www.bilibili.com/", "Cookie": "private"},
        ))
        command, _ = self.commands[-1]
        self.assertLess(command.index("-ss"), command.index("-i"))
        self.assertIn("120.500", command)
        self.assertIn("volume=0.25", command)
        self.assertNotIn("-re", command)
        self.assertNotIn("private", " ".join(command))


if __name__ == "__main__":
    unittest.main()
