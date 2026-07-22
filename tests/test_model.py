import sys
import unittest
from pathlib import Path


PLUGINS_DIR = Path(__file__).resolve().parents[2]
if str(PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGINS_DIR))

from astrbot_plugin_kook_music.music.model import Song


class SongPlaybackTests(unittest.TestCase):
    def test_playback_source_supports_local_and_stream_inputs(self):
        local_song = Song(
            id="local",
            file_path="cache/local.m4a",
            stream_url="https://cdn.example.test/fallback.m4s",
        )
        stream_song = Song(
            id="stream",
            stream_url="https://cdn.example.test/audio.m4s",
        )

        self.assertEqual(local_song.playback_source, "cache/local.m4a")
        self.assertEqual(
            stream_song.playback_source,
            "https://cdn.example.test/audio.m4s",
        )

    def test_duration_str_uses_hours_for_long_media(self):
        song = Song(id="long", duration=((3 * 60 + 4) * 60 + 5) * 1000)

        self.assertEqual(song.duration_str, "3:04:05")

    def test_duration_str_keeps_compact_format_below_one_hour(self):
        song = Song(id="short", duration=(4 * 60 + 5) * 1000)

        self.assertEqual(song.duration_str, "4:05")


if __name__ == "__main__":
    unittest.main()
