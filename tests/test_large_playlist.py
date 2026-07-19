import sys
import unittest
from pathlib import Path


PLUGINS_DIR = Path(__file__).resolve().parents[2]
if str(PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGINS_DIR))

from astrbot_plugin_kook_music import card_builder
from astrbot_plugin_kook_music.music.model import Song
from astrbot_plugin_kook_music.music.playlist_import import PlaylistImporter


class LargePlaylistTests(unittest.TestCase):
    def test_merge_restores_1200_song_order_and_placeholders(self):
        track_ids = [str(index) for index in range(1, 1201)]
        partial = [
            Song(id=str(index), name=f"song-{index}", platform="netease")
            for index in range(1, 201)
        ]

        merged = PlaylistImporter._merge_playlist_order(
            track_ids, partial, "user", "name"
        )

        self.assertEqual(len(merged), 1200)
        self.assertEqual([song.id for song in merged], track_ids)
        self.assertEqual(merged[0].name, "song-1")
        self.assertEqual(merged[1199].name, "未知歌曲")
        self.assertEqual(merged[1199].requester_id, "user")

    def test_queue_card_for_2000_songs_stays_under_module_limit(self):
        playlist = [
            Song(id=str(index), name=f"song-{index}")
            for index in range(1, 2001)
        ]

        card = card_builder.build_queue_card(playlist)

        self.assertLessEqual(len(card["modules"]), 50)
        rendered = str(card)
        self.assertIn("仅展示前 100 首", rendered)
        self.assertIn("共 **2000** 首", rendered)

    def test_user_example_url_keeps_playlist_id_and_ignores_extra_query(self):
        url = (
            "https://music.163.com/playlist?id=14301996301&"
            "uct2=U2FsdGVkX18cfz7SvI0N73lr3R5z9mtJ6ajcXTeaMa4="
        )
        self.assertEqual(
            PlaylistImporter.parse_playlist_input(url),
            ("netease", "14301996301"),
        )

    def test_import_result_card_displays_music_platform(self):
        card = card_builder.build_import_result_card(
            total=12,
            playlist_id="123",
            queue_size=14,
            platform="qq",
        )

        self.assertIn("QQ音乐", str(card))


if __name__ == "__main__":
    unittest.main()
