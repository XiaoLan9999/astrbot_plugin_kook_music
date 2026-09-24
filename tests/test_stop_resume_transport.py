import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from astrbot_plugin_kook_music.kook_voice.ffmpeg_player import RelayFFmpegPlayer
from astrbot_plugin_kook_music.kook_voice.voice_manager import (
    GuildSession,
    VoiceManager,
)
from astrbot_plugin_kook_music.music.model import Song
from test_voice_manager import BlockingDirectPlayer, FakeVoiceClient, wait_for


class RelayStub(BlockingDirectPlayer, RelayFFmpegPlayer):
    def __init__(self):
        BlockingDirectPlayer.__init__(self)
        self.running = True
        self.starts = []
        self.relay_stops = 0

    @property
    def is_relay_running(self):
        return self.running

    async def play(self, file_path, rtp_url="", ssrc=0, **kwargs):
        return await BlockingDirectPlayer.play(self, file_path, rtp_url, ssrc, **kwargs)

    async def stop_relay(self):
        self.relay_stops += 1
        await self.stop()
        self.running = False

    async def start_relay(self, rtp_url, ssrc):
        self.starts.append((rtp_url, ssrc))
        self.running = True
        return True


class StopResumeTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = VoiceManager(auto_leave_timeout=0)
        self.player = RelayStub()
        self.voice = FakeVoiceClient("synthetic")
        self.voice.refresh_rtp = AsyncMock(
            side_effect=AssertionError("Healthy relay must not refresh RTP")
        )
        self.voice.reconnect = AsyncMock(
            side_effect=AssertionError("Healthy relay must not reconnect")
        )
        self.session = GuildSession("guild", "voice", "text", self.voice, self.player)
        self.manager.sessions["guild"] = self.session
        self.manager.on_song_started = AsyncMock()

    async def asyncTearDown(self):
        await self.manager.leave_all()
        self.temp.cleanup()

    def song(self, label):
        path = Path(self.temp.name) / (label + ".mp3")
        path.write_bytes(b"synthetic-only")
        return Song(id=label, name=label, file_path=str(path), requester_id="owner")

    async def play(self, label):
        result = await self.manager.join_and_play(
            "synthetic", "guild", "voice", "text", self.song(label)
        )
        self.assertTrue(result[0])
        await wait_for(lambda: self.player.is_playing)

    async def stop(self):
        self.assertTrue(
            (await self.manager.control("guild", "stop", actor_id="owner"))[0]
        )
        self.assertEqual(self.session.playlist, [])
        self.assertFalse(self.player.is_playing)

    async def test_stop_new_song_keeps_same_relay_and_rtp(self):
        original = (self.voice.rtp_url, self.voice.ssrc)
        await self.play("first")
        await self.stop()
        self.assertTrue(self.player.is_relay_running)
        self.assertFalse(self.session.needs_relay_refresh)
        await self.play("second")
        self.assertEqual(self.session.current_song.id, "second")
        self.assertEqual(len(self.player.played), 2)
        self.assertEqual((self.voice.rtp_url, self.voice.ssrc), original)
        self.assertEqual(self.player.relay_stops, 0)
        self.voice.refresh_rtp.assert_not_awaited()
        self.voice.reconnect.assert_not_awaited()

    async def test_repeated_stop_restart_does_not_accumulate_encoders(self):
        for index in range(5):
            await self.play(str(index))
            await self.stop()
        self.assertEqual(self.player.starts, [])
        self.assertEqual(self.player.relay_stops, 0)
        self.assertEqual(len(self.player.played), 5)

    async def test_natural_eof_then_new_request_reuses_transport(self):
        await self.play("first")
        self.player._done.set()
        await wait_for(
            lambda: not self.session.playlist and not self.session.is_playing
        )
        self.assertTrue(self.player.is_relay_running)
        self.assertFalse(self.session.needs_relay_refresh)
        await self.play("second")
        self.assertEqual(len(self.player.played), 2)
        self.voice.refresh_rtp.assert_not_awaited()
        self.voice.reconnect.assert_not_awaited()

    async def test_idle_disconnect_still_reaps_silent_relay(self):
        await self.play("first")
        await self.stop()
        self.manager.auto_leave_timeout = 10
        await self.manager._check_session_idle("guild", self.session)
        self.assertNotIn("guild", self.manager.sessions)
        self.assertFalse(self.player.is_relay_running)
        self.assertEqual(self.voice.disconnect_calls, 1)

    async def test_damaged_relay_rejoins_instead_of_inplace_rtp_refresh(self):
        self.player.running = False
        self.session.needs_relay_refresh = True

        async def rejoin(_channel):
            self.voice.rtp_url = "rtp://new-transport"
            self.voice.ssrc = 9876
            self.voice.is_alive = True
            return True

        self.voice.reconnect.side_effect = rejoin
        await self.play("recovered")
        self.voice.reconnect.assert_awaited_once_with("voice")
        self.voice.refresh_rtp.assert_not_awaited()
        self.assertEqual(self.player.starts, [("rtp://new-transport", 9876)])
        self.assertFalse(self.session.needs_relay_refresh)

    async def test_dead_voice_recovery_does_not_rebuild_twice(self):
        self.voice.is_alive = False
        self.player.running = False
        self.session.needs_relay_refresh = True

        async def rejoin(_channel):
            self.voice.is_alive = True
            return True

        self.voice.reconnect.side_effect = rejoin
        await self.play("recovered")
        self.voice.reconnect.assert_awaited_once()
        self.assertEqual(len(self.player.starts), 1)
        self.voice.refresh_rtp.assert_not_awaited()

    async def test_rejoin_failure_never_announces_playing_song(self):
        self.player.running = False
        self.voice.reconnect.side_effect = None
        self.voice.reconnect.return_value = False
        self.manager.MAX_PLAYBACK_RETRIES = 0
        self.assertTrue(
            (
                await self.manager.join_and_play(
                    "synthetic", "guild", "voice", "text", self.song("failed")
                )
            )[0]
        )
        await wait_for(
            lambda: (
                self.voice.reconnect.await_count == 1 and not self.session.is_playing
            )
        )
        self.assertFalse(self.player.played)
        self.manager.on_song_started.assert_not_awaited()

    async def test_remote_kick_never_reconnects_silent_relay(self):
        self.voice.remote_removed = True
        self.assertFalse(await self.manager._prepare_relay_for_playback(self.session))
        self.voice.reconnect.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
