import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

PLUGINS_DIR = Path(__file__).resolve().parents[2]
if str(PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGINS_DIR))

from astrbot_plugin_kook_music import card_builder
from astrbot_plugin_kook_music.music.model import Song
from astrbot_plugin_kook_music.music.playlist_import import PlaylistImporter
from astrbot_plugin_kook_music.music.searcher import MusicSearcher
from test_playlist_platforms import FakeResponse, FakeSession


TITLE = "Test Song (DJ Remix)"


def track(**overrides):
    return {
        "id": 1234567,
        "mid": "ExampleDJMid",
        "name": "Test Song",
        "title": TITLE,
        "subtitle": "",
        "singer": [{"name": "Artist"}],
        "file": {"media_mid": "ExampleDJMedia"},
        "album": {"mid": "ExampleAlbum"},
        "interval": 434,
        **overrides,
    }


class QQTitleTests(unittest.TestCase):
    def test_full_title_does_not_change_audio_identity_or_duration(self):
        song = MusicSearcher._parse_qq_track(track(), require_free=False)
        self.assertEqual(song.name, TITLE)
        self.assertEqual(song.id, "ExampleDJMid")
        self.assertEqual(song.provider_data["media_mid"], "ExampleDJMedia")
        self.assertEqual(song.duration_str, "7:14")

    def test_empty_or_malformed_title_falls_back_to_name(self):
        for value in (None, "", "   ", 42, [], {}):
            with self.subTest(value=value):
                song = MusicSearcher._parse_qq_track(track(title=value), require_free=False)
                self.assertEqual(song.name, "Test Song")

    def test_legacy_songname_and_unknown_fallback(self):
        for source, expected in [
            ({"songmid": "MID", "songname": TITLE}, TITLE),
            ({"mid": "MID", "name": " ", "songname": TITLE}, TITLE),
            ({"mid": "MID"}, "未知歌曲"),
        ]:
            with self.subTest(source=source):
                self.assertEqual(MusicSearcher._parse_qq_track(source, require_free=False).name, expected)

    def test_no_duplicate_version_or_unrelated_subtitle_is_appended(self):
        song = MusicSearcher._parse_qq_track(track(subtitle="DJ Remix"), require_free=False)
        self.assertEqual(song.name, TITLE)
        song = MusicSearcher._parse_qq_track(track(title="Test Song", subtitle="Film theme song"), require_free=False)
        self.assertEqual(song.name, "Test Song")

    def test_official_title_is_kept_verbatim_apart_from_outer_whitespace(self):
        title = "Test Song（Live / 2024 Remaster）"
        song = MusicSearcher._parse_qq_track(track(title=f" {title} "), require_free=False)
        self.assertEqual(song.name, title)

    def test_search_results_include_version(self):
        result = {"req_0": {"code": 0, "data": {"body": {"song": {"list": [track()]}}}}}
        songs = MusicSearcher._parse_qq_search_songs(result, 5)
        self.assertEqual(songs[0].name, TITLE)

    def test_unavailable_playlist_entry_keeps_version(self):
        song = PlaylistImporter._qq_unavailable_placeholder(track(mid="", file={}), 1)
        self.assertEqual(song.name, TITLE)
        self.assertEqual(PlaylistImporter._qq_unavailable_placeholder({}, 1).name, "已下架歌曲")

    def test_playing_audio_queue_and_search_cards_use_the_full_title(self):
        song = MusicSearcher._parse_qq_track(track(), require_free=False)
        song.audio_url = "https://isure.stream.qqmusic.qq.com/example.mp3"
        playing = card_builder.build_now_playing_card(song)
        self.assertIn(TITLE, playing["modules"][0]["text"]["content"])
        audio = next(module for module in playing["modules"] if module["type"] == "audio")
        self.assertEqual(audio["title"], f"{TITLE} - Artist")
        queued = card_builder.build_queued_card(song)
        self.assertIn(TITLE, queued["modules"][0]["text"]["content"])
        search = card_builder.build_search_result_card([song])
        self.assertIn(TITLE, json.dumps(search, ensure_ascii=False))


class QQTitleFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_official_song_detail_preserves_the_full_title(self):
        searcher = MusicSearcher()
        session = FakeSession(post_responses=[FakeResponse(data={"req_0": {"data": {"track_info": track()}}})])
        searcher._get_session = AsyncMock(return_value=session)
        searcher._fill_qq_audio_url = AsyncMock()
        song = await searcher._fetch_qq_song_by_id("ExampleDJMid")
        self.assertEqual(song.name, TITLE)
        searcher._fill_qq_audio_url.assert_awaited_once_with(session, song)

    async def test_playlist_import_preserves_the_full_title(self):
        importer = PlaylistImporter()
        session = FakeSession(get_responses=[FakeResponse(data={
            "code": 0, "cdlist": [{"disstid": "12345", "songlist": [track()]}],
        })])
        importer._get_session = AsyncMock(return_value=session)
        songs = await importer.import_qq_playlist("12345")
        self.assertEqual(songs[0].name, TITLE)
        self.assertEqual(songs[0].id, "ExampleDJMid")

    async def test_aggregator_audio_fallback_cannot_erase_official_edition(self):
        searcher = MusicSearcher()
        direct = MusicSearcher._parse_qq_track(track(), require_free=False)
        fallback = Song(id=direct.id, name="Test Song", platform="qq", audio_url="https://isure.stream.qqmusic.qq.com/test.mp3")
        searcher._fetch_qq_song_by_id = AsyncMock(return_value=direct)
        searcher._search_via_aggregator = AsyncMock(return_value=[fallback])
        result = await searcher.fetch_song_by_id("qq", direct.id)
        self.assertIs(result, fallback)
        self.assertEqual(result.name, TITLE)
        self.assertTrue(result.audio_url)

    async def test_meting_audio_fallback_cannot_erase_official_edition(self):
        searcher = MusicSearcher()
        direct = MusicSearcher._parse_qq_track(track(), require_free=False)
        fallback = Song(id=direct.id, name="Test Song", platform="qq", audio_url="https://isure.stream.qqmusic.qq.com/test.mp3")
        searcher._fetch_qq_song_by_id = AsyncMock(return_value=direct)
        searcher._search_via_aggregator = AsyncMock(return_value=[])
        searcher._fetch_meting_song_by_id = AsyncMock(return_value=fallback)
        result = await searcher.fetch_song_by_id("qq", direct.id)
        self.assertIs(result, fallback)
        self.assertEqual(result.name, TITLE)


if __name__ == "__main__":
    unittest.main()
