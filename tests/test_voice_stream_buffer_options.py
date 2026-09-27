import unittest
from unittest.mock import AsyncMock

from test_voice_manager import AutoDirectPlayer, FakeVoiceClient, make_song
from astrbot_plugin_kook_music.kook_voice.ffmpeg_player import RelayFFmpegPlayer
from astrbot_plugin_kook_music.kook_voice.voice_manager import GuildSession, VoiceManager
from astrbot_plugin_kook_music.music.model import Song


class OptionRelay(AutoDirectPlayer, RelayFFmpegPlayer):
    def __init__(self):
        AutoDirectPlayer.__init__(self)
        self.options = []

    @property
    def is_relay_running(self):
        return True

    async def play(self, path, **kwargs):
        self.options.append(kwargs)
        self.played.append(path)
        return True


class StreamBufferOptionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_remote_relay_receives_buffer_configuration(self):
        manager = VoiceManager(
            stream_startup_buffer_seconds=12,
            stream_max_buffer_seconds=90,
            stream_rebuffer_seconds=4,
        )
        player = OptionRelay()
        voice = FakeVoiceClient()
        voice.refresh_rtp = AsyncMock(side_effect=AssertionError("no transport refresh"))
        remote = Song(id="remote", name="remote", stream_url="https://cdn.test/audio")
        local = make_song("local")
        session = GuildSession("guild", "voice", "text", voice, player, playlist=[remote, local])
        manager.sessions["guild"] = session
        await manager._playback_loop("guild")
        self.assertEqual(player.options[0], {
            "extra_headers": {},
            "startup_buffer_seconds": 12,
            "max_buffer_seconds": 90,
            "rebuffer_seconds": 4,
        })
        self.assertEqual(player.options[1], {"extra_headers": None})
        voice.refresh_rtp.assert_not_awaited()

    async def test_direct_mode_retains_legacy_arguments(self):
        manager = VoiceManager(streaming_mode="direct")
        manager.PLAYBACK_START_DELAY = 0
        player = AutoDirectPlayer()
        song = Song(id="stream", name="stream", stream_url="https://cdn.test/audio")
        session = GuildSession("guild", "voice", "text", FakeVoiceClient(), player, playlist=[song])
        manager.sessions["guild"] = session
        await manager._playback_loop("guild")
        self.assertEqual(player.played, ["https://cdn.test/audio"])


if __name__ == "__main__":
    unittest.main()
