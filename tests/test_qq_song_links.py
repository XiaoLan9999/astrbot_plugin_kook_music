import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

PLUGINS_DIR = Path(__file__).resolve().parents[2]
if str(PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGINS_DIR))

from astrbot_plugin_kook_music.music.model import Song
from astrbot_plugin_kook_music.music.playlist_import import PlaylistImporter
from astrbot_plugin_kook_music.music.searcher import MusicSearcher
from test_playlist_platforms import FakeResponse, FakeSession
from test_main_bilibili_flow import _FakeEvent, main_module


SHORT = "https://c6.y.qq.com/base/fcgi-bin/u?__=ExampleShare"
MID = "002ExampleSong"
NUMERIC_ID = "705000001"


class QQSongLinkParsingTests(unittest.TestCase):
    def test_kook_markdown_html_entities_and_wrappers_preserve_short_link(self):
        inputs = [
            SHORT,
            f"[{SHORT}]({SHORT})",
            f"**[{SHORT}]({SHORT})**",
            f"点歌&#x20;[{SHORT}]({SHORT})",
            f"[QQ音乐]({SHORT})",
            f"<{SHORT}>",
            f"`{SHORT}`",
            f"**{SHORT}**",
            SHORT.replace("__", r"\_\_"),
        ]
        for text in inputs:
            with self.subTest(text=text):
                self.assertEqual(MusicSearcher._extract_http_url(text), SHORT)
                self.assertEqual(MusicSearcher.detect_direct_platform(text), "qq")

    def test_markdown_destination_wins_over_visible_label(self):
        text = f"[{SHORT}](https://evil.example/song)"
        self.assertEqual(MusicSearcher._extract_http_url(text), "https://evil.example/song")
        self.assertEqual(MusicSearcher.detect_direct_platform(text), "")

    def test_html_entities_in_query_are_decoded(self):
        url = f"https://i.y.qq.com/v8/playsong.html?foo=bar&amp;songid={NUMERIC_ID}"
        clean = MusicSearcher._extract_http_url(url)
        self.assertEqual(MusicSearcher._extract_qq_song_id(clean), NUMERIC_ID)

    def test_current_and_legacy_song_url_shapes(self):
        cases = [
            (f"https://y.qq.com/n/ryqq_v2/songDetail/{NUMERIC_ID}", NUMERIC_ID),
            (f"https://y.qq.com/n/ryqq_v2/songDetail/{MID}", MID),
            (f"https://y.qq.com/n/ryqq/songDetail/{MID}", MID),
            (f"https://y.qq.com/n/yqq/song/{MID}.html", MID),
            (f"https://i.y.qq.com/v8/playsong.html?songid={NUMERIC_ID}", NUMERIC_ID),
            (f"https://i2.y.qq.com/n3/other/pages/playsong/index.html?songid={NUMERIC_ID}", NUMERIC_ID),
            (f"https://i.y.qq.com/v8/playsong.html?songmid={MID}&songid={NUMERIC_ID}", MID),
        ]
        for url, expected in cases:
            with self.subTest(url=url):
                self.assertEqual(MusicSearcher._extract_qq_song_id(url), expected)

    def test_playlist_and_bad_song_id_are_not_misread_as_songs(self):
        for url in [
            "https://y.qq.com/n/ryqq_v2/playlist/12345",
            "https://i.y.qq.com/playsong.html?songid=notanumber",
            "https://i.y.qq.com/playsong.html?songid=-123",
            "https://i.y.qq.com/playsong.html?id=12345",
            "https://y.qq.com/n/ryqq_v2/songDetail/abc.exe",
        ]:
            with self.subTest(url=url):
                self.assertEqual(MusicSearcher._extract_qq_song_id(url), "")

    def test_playlist_parser_uses_same_markdown_destination(self):
        url = "https://y.qq.com/n/ryqq_v2/playlist/123456"
        self.assertEqual(PlaylistImporter.parse_playlist_input(f"[QQ歌单]({url})"), ("qq", "123456"))


class QQSongLinkAsyncTests(unittest.IsolatedAsyncioTestCase):
    def make_searcher(self, responses=()):
        searcher = MusicSearcher()
        session = FakeSession(get_responses=responses)
        searcher._get_session = AsyncMock(return_value=session)
        searcher.fetch_song_by_id = AsyncMock(return_value=Song(
            id=MID, name="Test song", platform="qq",
            audio_url="https://isure.stream.qqmusic.qq.com/synthetic.mp3",
        ))
        return searcher, session

    async def test_short_link_resolves_numeric_id_before_fetching_details(self):
        landing = f"https://i.y.qq.com/v8/playsong.html?songid={NUMERIC_ID}&songtype=0"
        searcher, session = self.make_searcher([FakeResponse(status=302, headers={"Location": landing})])
        song = await searcher.fetch_direct_song(f"点歌&#x20;[{SHORT}]({SHORT})")
        self.assertEqual(song.id, MID)
        searcher.fetch_song_by_id.assert_awaited_once_with("qq", NUMERIC_ID)
        self.assertEqual(session.get_calls[0][0][0], SHORT)
        self.assertFalse(session.get_calls[0][1]["allow_redirects"])

    async def test_relative_redirects_and_new_song_detail_path(self):
        searcher, session = self.make_searcher([
            FakeResponse(status=302, headers={"Location": "/jump/two"}),
            FakeResponse(status=307, headers={"Location": f"https://y.qq.com/n/ryqq_v2/songDetail/{MID}"}),
        ])
        await searcher.fetch_direct_song(SHORT)
        self.assertEqual(session.get_calls[1][0][0], "https://c6.y.qq.com/jump/two")
        searcher.fetch_song_by_id.assert_awaited_once_with("qq", MID)

    async def test_direct_http_links_still_work_without_page_requests(self):
        for base in ("http://y.qq.com", "http://y.qq.com:80", "https://y.qq.com"):
            with self.subTest(base=base):
                searcher, session = self.make_searcher()
                await searcher.fetch_direct_song(f"{base}/n/ryqq/songDetail/{MID}")
                searcher.fetch_song_by_id.assert_awaited_once_with("qq", MID)
                self.assertEqual(session.get_calls, [])

    async def test_initial_http_short_link_is_upgraded_before_request(self):
        searcher, session = self.make_searcher([FakeResponse(status=302, headers={"Location": f"https://y.qq.com/n/ryqq/songDetail/{MID}"})])
        await searcher.fetch_direct_song(SHORT.replace("https://", "http://"))
        self.assertEqual(session.get_calls[0][0][0], SHORT)

    async def test_external_downgrade_credential_and_port_redirects_are_rejected(self):
        for target in [
            f"https://127.0.0.1/playsong.html?songmid={MID}",
            f"https://y.qq.com.evil.example/song?songmid={MID}",
            f"https://user:password@y.qq.com/song?songmid={MID}",
            f"https://y.qq.com:8443/song?songmid={MID}",
            f"http://y.qq.com/song?songmid={MID}",
            "https://y.qq.com:invalid/song",
            "https://[invalid",
        ]:
            with self.subTest(target=target):
                searcher, session = self.make_searcher([FakeResponse(status=302, headers={"Location": target})])
                self.assertIsNone(await searcher.fetch_direct_song(SHORT))
                searcher.fetch_song_by_id.assert_not_awaited()
                self.assertEqual(len(session.get_calls), 1)

    async def test_duplicate_redirect_stops_without_following_forever(self):
        searcher, session = self.make_searcher([FakeResponse(status=302, headers={"Location": SHORT})])
        self.assertIsNone(await searcher.fetch_direct_song(SHORT))
        self.assertEqual(len(session.get_calls), 1)

    async def test_redirect_count_is_bounded(self):
        searcher, session = self.make_searcher([
            FakeResponse(status=302, headers={"Location": f"/step/{index}"}) for index in range(8)
        ])
        self.assertIsNone(await searcher.fetch_direct_song(SHORT))
        self.assertEqual(len(session.get_calls), 6)

    async def test_missing_redirect_or_error_status_does_not_call_details(self):
        for response in (FakeResponse(status=302), FakeResponse(status=403), FakeResponse(status=200)):
            searcher, _ = self.make_searcher([response])
            self.assertIsNone(await searcher.fetch_direct_song(SHORT))
            searcher.fetch_song_by_id.assert_not_awaited()

    async def test_request_timeout_returns_no_song(self):
        searcher, session = self.make_searcher()
        session.get = lambda *_args, **_kwargs: (_ for _ in ()).throw(asyncio.TimeoutError())
        self.assertIsNone(await searcher.fetch_direct_song(SHORT))
        searcher.fetch_song_by_id.assert_not_awaited()

    async def test_numeric_detail_uses_song_id_and_returns_canonical_mid(self):
        session = FakeSession(post_responses=[FakeResponse(data={"req_0": {"data": {"track_info": {
            "id": int(NUMERIC_ID), "mid": MID, "name": "Test song", "interval": 189,
            "singer": [{"name": "Artist"}], "album": {"mid": "AlbumMid"},
        }}}})])
        searcher = MusicSearcher()
        searcher._get_session = AsyncMock(return_value=session)
        searcher._fill_qq_audio_url = AsyncMock()
        song = await searcher._fetch_qq_song_by_id(NUMERIC_ID)
        self.assertEqual(session.post_calls[0][1]["json"]["req_0"]["param"], {"song_id": int(NUMERIC_ID)})
        self.assertEqual(song.id, MID)
        searcher._fill_qq_audio_url.assert_awaited_once_with(session, song)

    async def test_mid_detail_keeps_song_mid_parameter(self):
        session = FakeSession(post_responses=[FakeResponse(data={})])
        searcher = MusicSearcher()
        searcher._get_session = AsyncMock(return_value=session)
        await searcher._fetch_qq_song_by_id(MID)
        self.assertEqual(session.post_calls[0][1]["json"]["req_0"]["param"], {"song_mid": MID})

    async def test_playlist_short_link_markdown_is_not_sent_verbatim(self):
        importer = PlaylistImporter()
        session = FakeSession(get_responses=[FakeResponse(status=302, headers={"Location": "https://y.qq.com/n/ryqq_v2/playlist/123456"}, url=SHORT)])
        importer._get_session = AsyncMock(return_value=session)
        self.assertEqual(await importer.resolve_playlist_input(f"[{SHORT}]({SHORT})"), ("qq", "123456"))
        self.assertEqual(session.get_calls[0][0][0], SHORT)

    async def test_kook_command_reaches_playback_with_resolved_short_link(self):
        searcher, session = self.make_searcher([
            FakeResponse(status=302, headers={"Location": f"https://i.y.qq.com/v8/playsong.html?songid={NUMERIC_ID}"}),
        ])
        plugin = object.__new__(main_module.KookMusicPlugin)
        plugin._kook_token = "synthetic-token"
        plugin.default_platform = "netease"
        plugin.searcher = searcher
        plugin._is_kook = lambda _event: True
        plugin._get_guild_id = lambda _event: "guild"
        plugin._get_channel_id = lambda _event: "text"
        plugin._delete_messages = AsyncMock()
        plugin._play_song = AsyncMock()
        event = _FakeEvent()
        event.message_str = f"点歌&#x20;[{SHORT}]({SHORT})"
        with patch.object(main_module, "send_text_message", AsyncMock(return_value="progress")):
            replies = [reply async for reply in plugin.on_play_music(event)]
        self.assertEqual(replies, [])
        self.assertEqual(session.get_calls[0][0][0], SHORT)
        searcher.fetch_song_by_id.assert_awaited_once_with("qq", NUMERIC_ID)
        plugin._play_song.assert_awaited_once_with(event, searcher.fetch_song_by_id.return_value, "guild")

    async def test_direct_link_failure_is_not_immediately_resolved_twice(self):
        plugin = object.__new__(main_module.KookMusicPlugin)
        plugin._kook_token = "synthetic-token"
        plugin.default_platform = "netease"
        plugin._is_kook = lambda _event: True
        plugin._get_guild_id = lambda _event: "guild"
        plugin._get_channel_id = lambda _event: "text"
        plugin._delete_messages = AsyncMock()
        plugin._play_song = AsyncMock()
        failed = Song(id=MID, name="Test song", platform="qq", unplayable_reason="解析服务暂时不可用，请稍后重试")
        plugin.searcher = type("Searcher", (), {"fetch_direct_song": AsyncMock(return_value=failed)})()
        event = _FakeEvent()
        event.message_str = f"点歌 {SHORT}"
        with patch.object(main_module, "send_text_message", AsyncMock(return_value="progress")):
            replies = [reply async for reply in plugin.on_play_music(event)]
        self.assertTrue(any("暂时不可用" in reply for reply in replies))
        plugin.searcher.fetch_direct_song.assert_awaited_once()
        plugin._play_song.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
