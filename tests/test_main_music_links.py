import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from test_main_bilibili_flow import _FakeEvent, main_module
from astrbot_plugin_kook_music.music.link_router import MusicLinkResult
from astrbot_plugin_kook_music.music.model import Song


class MusicCommandLinkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = object.__new__(main_module.KookMusicPlugin)
        self.plugin._kook_token = "synthetic-token"
        self.plugin.default_platform = "netease"
        self.plugin.search_limit = 1
        self.plugin._is_kook = lambda event: True
        self.plugin._get_guild_id = lambda event: "guild"
        self.plugin._get_channel_id = lambda event: "text"
        self.plugin._delete_messages = AsyncMock()
        self.plugin._play_song = AsyncMock()
        self.song = Song(id="123", name="synthetic", audio_url="https://cdn.test/audio")
        self.plugin.searcher = NS(
            search=AsyncMock(return_value=[]),
            fetch_song_by_id=AsyncMock(return_value=self.song),
            fetch_direct_song=AsyncMock(return_value=self.song),
        )
        self.plugin.playlist_importer = NS(
            fetch_netease_song=AsyncMock(return_value=self.song),
            fetch_netease_program=AsyncMock(return_value=self.song),
            resolve_playlist_input=AsyncMock(),
        )
        self.event = _FakeEvent()

    async def command(self, text):
        self.event.message_str = text
        with patch.object(main_module, "send_text_message", AsyncMock(return_value="progress")):
            return [result async for result in self.plugin.on_play_music(self.event)]

    async def test_valid_playlists_hint_import_without_search(self):
        for url in (
            "https://music.163.com/playlist?id=123",
            "https://music.163.com/#/djradio?id=123",
            "https://y.qq.com/n/ryqq/playlist/123",
            "https://www.kugou.com/yy/special/single/123.html",
        ):
            with self.subTest(url=url):
                replies = await self.command("#点歌 " + url)
                self.assertTrue(any("#导入歌单" in reply for reply in replies), replies)
        self.plugin.searcher.search.assert_not_awaited()
        self.plugin._play_song.assert_not_awaited()

    async def test_null_playlist_does_not_guess_share_token_or_search(self):
        replies = await self.command(
            "点歌 https://music.163.com/playlist?id=null&uct2=synthetic-private-marker"
        )
        self.assertTrue(any("有效 ID" in reply for reply in replies))
        self.assertNotIn("synthetic-private-marker", str(replies))
        self.plugin.searcher.search.assert_not_awaited()

    async def test_unknown_or_malformed_link_never_searches(self):
        for text in (
            "https://example.test/music?id=123",
            "https://music.163.com/song?id=12oops",
            "https://music.163.com/song?id=1&id=2",
            "https://music.163.com/user/home?id=123",
        ):
            with self.subTest(text=text):
                replies = await self.command("#点歌 " + text)
                self.assertTrue(replies)
        self.plugin.searcher.search.assert_not_awaited()
        self.plugin._play_song.assert_not_awaited()

    async def test_netease_canonical_markdown_destination_is_used(self):
        await self.command(
            "点歌 [https://music.163.com/song?id=111]"
            "(https://music.163.com/#/program?id=222)"
        )
        self.plugin.playlist_importer.fetch_netease_program.assert_awaited_once_with(
            "222", "user", "tester"
        )
        self.plugin.playlist_importer.fetch_netease_song.assert_not_awaited()
        self.plugin._play_song.assert_awaited_once()

    async def test_qq_direct_id_is_not_resolved_twice(self):
        await self.command("点歌 https://y.qq.com/n/ryqq/songDetail/001Synthetic")
        self.plugin.searcher.fetch_song_by_id.assert_awaited_once_with("qq", "001Synthetic")
        self.plugin.searcher.fetch_direct_song.assert_not_awaited()

    async def test_short_link_resolved_to_playlist_stops_before_song_lookup(self):
        route = MusicLinkResult("playlist", "qq", "https://y.qq.com/n/ryqq/playlist/123", "123")
        with patch.object(main_module, "resolve_music_link", AsyncMock(return_value=route)):
            replies = await self.command("点歌 https://c6.y.qq.com/base/fcgi-bin/u?__=synthetic")
        self.assertTrue(any("#导入歌单" in reply for reply in replies))
        self.plugin.searcher.fetch_direct_song.assert_not_awaited()

    async def test_video_dispatch_preserves_original_message(self):
        seen = []

        async def video_handler(event, resolved_input=None):
            seen.append((event, resolved_input))
            yield "video-dispatched"

        self.plugin._on_play_video_request = video_handler
        url = "https://www.bilibili.com/video/BV1dujdzrEA4?p=2"
        text = "#点歌 " + url
        self.assertEqual(await self.command(text), ["video-dispatched"])
        self.assertEqual(seen, [(self.event, url)])
        self.assertEqual(self.event.message_str, text)
        self.plugin.searcher.search.assert_not_awaited()

    async def test_plain_keyword_still_searches(self):
        await self.command("#点歌 synthetic title qq")
        self.plugin.searcher.search.assert_awaited_once_with("synthetic title", "qq", 1)

    async def test_import_rejects_song_and_invalid_urls_before_playlist_parser(self):
        for url, expected in (
            ("https://music.163.com/song?id=123", "#点歌"),
            ("https://music.163.com/playlist?id=null", "有效 ID"),
            ("https://music.163.com/playlist?id=123oops", "有效 ID"),
        ):
            self.event.message_str = "#导入歌单 " + url
            replies = [r async for r in self.plugin.on_import_playlist(self.event)]
            self.assertTrue(any(expected in r for r in replies), replies)
        self.plugin.playlist_importer.resolve_playlist_input.assert_not_awaited()
        self.assertFalse(main_module._playlist_import_requests)


if __name__ == "__main__":
    unittest.main()
