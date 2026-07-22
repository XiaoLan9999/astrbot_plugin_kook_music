import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


PLUGINS_DIR = Path(__file__).resolve().parents[2]
if str(PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGINS_DIR))

from astrbot_plugin_kook_music.kook_voice import ffmpeg_player as ffmpeg_module
from astrbot_plugin_kook_music.kook_voice.ffmpeg_player import (
    DirectFFmpegPlayer,
    RelayFFmpegPlayer,
)


class _FakeStream:
    def __init__(self, payload=b""):
        self.payload = payload

    async def read(self):
        return self.payload

    def __aiter__(self):
        self._lines = iter(self.payload.splitlines(keepends=True))
        return self

    async def __anext__(self):
        try:
            return next(self._lines)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _FakeProcess:
    def __init__(self, pid=4321, returncode=None, stderr=None):
        self.pid = pid
        self.returncode = returncode
        self.stdout = None
        self.stderr = stderr

    async def wait(self):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def kill(self):
        self.returncode = -9


def _render_log_calls(mocked_logger_method):
    return "\n".join(
        " ".join(str(value) for value in logged_call.args)
        for logged_call in mocked_logger_method.call_args_list
    )


class FFmpegHttpInputTests(unittest.IsolatedAsyncioTestCase):
    stream_url = (
        "https://cdn.example.test/audio.m4s?token=secret-value&expires=999999"
    )
    headers = {
        "Referer": "https://www.bilibili.com/",
        "User-Agent": "test-agent",
    }

    def assert_http_input_options(self, command):
        input_index = command.index("-i")
        self.assertEqual(command[input_index + 1], self.stream_url)

        headers_index = command.index("-headers")
        self.assertLess(headers_index, input_index)
        header_block = command[headers_index + 1]
        self.assertIn("Referer: https://www.bilibili.com/", header_block)
        self.assertIn("User-Agent: test-agent", header_block)

        for option in ("-reconnect", "-reconnect_streamed", "-reconnect_delay_max"):
            self.assertLess(command.index(option), input_index)

    async def test_direct_resume_seek_is_applied_before_http_input(self):
        process = _FakeProcess()
        player = DirectFFmpegPlayer()
        with (
            patch.object(
                ffmpeg_module.asyncio,
                "create_subprocess_exec",
                AsyncMock(return_value=process),
            ) as spawn,
            patch.object(ffmpeg_module.asyncio, "sleep", AsyncMock()),
        ):
            started = await player.play(
                self.stream_url,
                "rtp://127.0.0.1:5000?rtcpport=5001",
                1234,
                extra_headers=self.headers,
                start_seconds=7195.25,
            )

        self.assertTrue(started)
        command = list(spawn.await_args.args)
        self.assertLess(command.index("-ss"), command.index("-i"))
        self.assertEqual(command[command.index("-ss") + 1], "7195.250")
        await player.stop()

    async def test_direct_player_accepts_https_and_redacts_signed_query_from_log(self):
        process = _FakeProcess()
        player = DirectFFmpegPlayer()

        with (
            patch.object(
                ffmpeg_module.asyncio,
                "create_subprocess_exec",
                AsyncMock(return_value=process),
            ) as spawn,
            patch.object(ffmpeg_module.asyncio, "sleep", AsyncMock()),
            patch.object(ffmpeg_module.logger, "info") as info_log,
        ):
            started = await player.play(
                self.stream_url,
                "rtp://127.0.0.1:5000?rtcpport=5001",
                1234,
                extra_headers=self.headers,
            )

        self.assertTrue(started)
        command = list(spawn.await_args.args)
        self.assert_http_input_options(command)
        self.assertNotIn("secret-value", _render_log_calls(info_log))
        await player.stop()

    async def test_relay_player_accepts_https_and_redacts_signed_query_from_log(self):
        process = _FakeProcess()
        player = RelayFFmpegPlayer()
        player._relay = _FakeProcess(pid=1111)
        player._udp_port = 54321

        with (
            patch.object(
                ffmpeg_module.asyncio,
                "create_subprocess_exec",
                AsyncMock(return_value=process),
            ) as spawn,
            patch.object(ffmpeg_module.asyncio, "sleep", AsyncMock()),
            patch.object(ffmpeg_module.logger, "info") as info_log,
        ):
            started = await player.play(
                self.stream_url,
                extra_headers=self.headers,
            )

        self.assertTrue(started)
        command = list(spawn.await_args.args)
        self.assert_http_input_options(command)
        self.assertNotIn("secret-value", _render_log_calls(info_log))
        await player.stop()

    async def test_direct_player_redacts_signed_query_from_failure_log(self):
        stderr = _FakeStream(
            f"Server returned 403 for {self.stream_url}".encode("utf-8")
        )
        process = _FakeProcess(returncode=1, stderr=stderr)
        player = DirectFFmpegPlayer()

        with (
            patch.object(
                ffmpeg_module.asyncio,
                "create_subprocess_exec",
                AsyncMock(return_value=process),
            ),
            patch.object(ffmpeg_module.asyncio, "sleep", AsyncMock()),
            patch.object(ffmpeg_module.logger, "error") as error_log,
        ):
            started = await player.play(
                self.stream_url,
                "rtp://127.0.0.1:5000?rtcpport=5001",
                1234,
                extra_headers=self.headers,
            )

        self.assertFalse(started)
        self.assertNotIn("secret-value", _render_log_calls(error_log))

    async def test_relay_player_redacts_signed_query_from_failure_log(self):
        stderr = _FakeStream(
            f"Server returned 403 for {self.stream_url}".encode("utf-8")
        )
        process = _FakeProcess(returncode=1, stderr=stderr)
        player = RelayFFmpegPlayer()
        player._relay = _FakeProcess(pid=1111)
        player._udp_port = 54321

        with (
            patch.object(
                ffmpeg_module.asyncio,
                "create_subprocess_exec",
                AsyncMock(return_value=process),
            ),
            patch.object(ffmpeg_module.asyncio, "sleep", AsyncMock()),
            patch.object(ffmpeg_module.logger, "error") as error_log,
        ):
            started = await player.play(
                self.stream_url,
                extra_headers=self.headers,
            )

        self.assertFalse(started)
        self.assertNotIn("secret-value", _render_log_calls(error_log))

    async def test_background_stderr_readers_redact_signed_query(self):
        payload = f"Network error at {self.stream_url}\n".encode("utf-8")
        direct = DirectFFmpegPlayer()
        relay = RelayFFmpegPlayer()

        with patch.object(ffmpeg_module.logger, "debug") as debug_log:
            await direct._read_stderr(_FakeStream(payload))
            await relay._read_song_stderr(_FakeStream(payload))

        rendered = _render_log_calls(debug_log)
        self.assertNotIn("secret-value", rendered)
        self.assertNotIn("expires=999999", rendered)


if __name__ == "__main__":
    unittest.main()
