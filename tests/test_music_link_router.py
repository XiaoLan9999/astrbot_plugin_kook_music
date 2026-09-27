import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_kook_music.music.link_router import resolve_music_link  # noqa: E402


class Response:
    def __init__(self, status=302, location=""):
        self.status = status
        self.headers = {"Location": location} if location else {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


class Session:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("Unexpected synthetic request")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class MusicLinkRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_plain_song_names_allow_search(self):
        for value in ("青花瓷", "song about music", "null", "网易云音乐", "12345", ""):
            with self.subTest(value=value):
                result = await resolve_music_link(value, Session())
                self.assertEqual(result.kind, "none")

    async def test_netease_direct_song_program_and_playlist_routes(self):
        for path, kind, resource in (
            ("song", "song", "song"),
            ("program", "song", "program"),
            ("dj", "song", "program"),
            ("playlist", "playlist", "playlist"),
            ("djradio", "playlist", "djradio"),
        ):
            for prefix in ("/", "/m/", "/#/"):
                with self.subTest(path=path, prefix=prefix):
                    session = Session()
                    result = await resolve_music_link(
                        f"https://music.163.com{prefix}{path}?id=12345", session
                    )
                    self.assertEqual(
                        (result.kind, result.platform, result.id, result.resource_type),
                        (kind, "netease", "12345", resource),
                    )
                    self.assertFalse(session.calls)

    async def test_null_invalid_or_conflicting_netease_playlist_ids_never_search(self):
        for query in (
            "id=null&uct2=synthetic",
            "id=0",
            "id=123abc",
            "id=",
            "uct2=synthetic",
            "id=1&id=2",
            "id=null&id=123",
            "id=-1",
            "id=%EF%BC%91",
            "id=1.5",
            "id=1&ID=2",
        ):
            result = await resolve_music_link(
                "https://music.163.com/playlist?" + query, Session()
            )
            self.assertEqual(result.kind, "invalid")
            self.assertEqual(result.id, "")
            self.assertIn("#导入歌单", result.reason)
            self.assertNotIn("synthetic", result.reason)

    async def test_same_duplicate_ids_are_unambiguous_but_query_fragment_conflicts_fail(
        self,
    ):
        result = await resolve_music_link(
            "https://music.163.com/song?id=123&id=123", Session()
        )
        self.assertEqual((result.kind, result.id), ("song", "123"))
        result = await resolve_music_link(
            "https://music.163.com/?id=123#/song?id=456", Session()
        )
        self.assertEqual(result.kind, "invalid")

    async def test_nested_query_paths_and_user_home_cannot_be_resources(self):
        for url in (
            "https://music.163.com/user/home?id=123",
            "https://music.163.com/?next=song?id=123",
            "https://music.163.com/playlist?id=null&foo=song?id=123",
            "https://music.163.com/playlist?id=123abc",
        ):
            result = await resolve_music_link(url, Session())
            self.assertIn(result.kind, {"invalid", "unsupported"})
        result = await resolve_music_link(
            "https://music.163.com/song?id=123&fake=program?id=456", Session()
        )
        self.assertEqual(
            (result.kind, result.id, result.resource_type), ("song", "123", "song")
        )

    async def test_markdown_actual_destination_wins_even_for_non_http_destinations(
        self,
    ):
        label = "https://music.163.com/song?id=123"
        for destination in (
            "https://other.invalid/",
            "javascript:synthetic",
            "ftp://music.163.com/song?id=123",
        ):
            result = await resolve_music_link(f"[{label}]({destination})", Session())
            self.assertEqual(result.kind, "unsupported")
        result = await resolve_music_link(
            "**[网易云](https://music.163.com/song?id=123&amp;foo=1)**", Session()
        )
        self.assertEqual((result.kind, result.id), ("song", "123"))

    async def test_known_qq_song_and_playlist_shapes(self):
        cases = {
            "https://y.qq.com/n/ryqq/songDetail/002Example": ("song", "002Example"),
            "https://y.qq.com/n/ryqq_v2/songDetail/123": ("song", "123"),
            "https://y.qq.com/n/yqq/song/002Example.html": ("song", "002Example"),
            "https://i.y.qq.com/v8/playsong.html?songid=123": ("song", "123"),
            "https://i2.y.qq.com/n3/other/pages/playsong/index.html?songid=123": (
                "song",
                "123",
            ),
            "https://i.y.qq.com/playsong.html?songmid=002Example&songid=123": (
                "song",
                "002Example",
            ),
            "https://y.qq.com/n/ryqq_v2/playlist/123": ("playlist", "123"),
            "https://y.qq.com/n/yqq/playlist/123.html": ("playlist", "123"),
            "https://i.y.qq.com/n2/m/share/details/taoge.html?id=123": (
                "playlist",
                "123",
            ),
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                result = await resolve_music_link(url, Session())
                self.assertEqual((result.kind, result.id), expected)
                self.assertEqual(result.platform, "qq")

    async def test_qq_invalid_ids_and_generic_queries_are_not_songs(self):
        for url in (
            "https://y.qq.com/n/ryqq/songDetail/null",
            "https://y.qq.com/n/ryqq/songDetail/0",
            "https://i.y.qq.com/playsong.html?songid=null",
            "https://i.y.qq.com/playsong.html?songid=123abc",
            "https://i.y.qq.com/playsong.html?songmid=abc&mid=def",
            "https://i.y.qq.com/playsong.html?id=123",
            "https://y.qq.com/n/ryqq/playlist/null",
        ):
            self.assertEqual((await resolve_music_link(url, Session())).kind, "invalid")

    async def test_known_kugou_song_hash_and_playlist_shapes(self):
        digest = "a" * 32
        song = await resolve_music_link(
            "https://www.kugou.com/song/#hash=" + digest, Session()
        )
        self.assertEqual((song.kind, song.id), ("song", digest.upper()))
        for suffix, expected in (
            ("songlist/gcid_ABC123/", "gcid_abc123"),
            ("playlist/collection_1_2_3_0", "collection_1_2_3_0"),
            ("yy/special/single/123.html", "123"),
            ("plist/list/123", "123"),
        ):
            result = await resolve_music_link(
                "https://www.kugou.com/" + suffix, Session()
            )
            self.assertEqual(
                (result.kind, result.platform, result.id),
                ("playlist", "kugou", expected),
            )
        self.assertEqual(
            (
                await resolve_music_link(
                    "https://www.kugou.com/song/#hash=not-a-hash", Session()
                )
            ).kind,
            "invalid",
        )

    async def test_bilibili_video_and_collection_guide_use_play_command(self):
        for value in (
            "BV1AB411c7mD",
            "https://www.bilibili.com/video/BV1AB411c7mD/?p=2",
        ):
            result = await resolve_music_link(value, Session())
            self.assertEqual(result.kind, "video")
            self.assertIn("#播放", result.reason)
        result = await resolve_music_link(
            "https://space.bilibili.com/123/favlist?fid=456", Session()
        )
        self.assertEqual(result.kind, "playlist")
        self.assertIn("#播放", result.reason)

    async def test_unknown_malformed_and_non_http_links_never_allow_search(self):
        for url in (
            "https://example.invalid/song",
            "https://music.163.com.evil.invalid/song?id=123",
            "https://user:password@music.163.com/song?id=123",
            "https://@music.163.com/song?id=123",
            "https://:@music.163.com/song?id=123",
            "https://music.163.com:8443/song?id=123",
            "https://music.163.com:invalid/song?id=123",
            "https://[invalid",
            "https://",
            "https:/example.invalid/song",
            "example.invalid/song",
            "ftp://music.163.com/song?id=123",
            "music.163.com/song?id=123",
            "www.y.qq.com/",
            "//music.163.com/song?id=123",
        ):
            session = Session()
            result = await resolve_music_link(url, session)
            self.assertIn(result.kind, {"unsupported", "invalid"})
            self.assertFalse(session.calls)

    async def test_direct_http_links_upgrade_without_request(self):
        result = await resolve_music_link(
            "http://music.163.com:80/song?id=123", Session()
        )
        self.assertEqual(result.kind, "song")
        self.assertEqual(result.url, "https://music.163.com/song?id=123")

    async def test_shared_result_repr_does_not_include_share_token(self):
        result = await resolve_music_link(
            "https://music.163.com/playlist?id=123&uct2=synthetic-private-share",
            Session(),
        )
        self.assertNotIn("synthetic-private-share", repr(result))
        self.assertNotIn("synthetic-private-share", result.reason)

    async def test_official_short_links_resolve_once_to_song_playlist_or_video(self):
        for short, final, kind, platform in (
            (
                "https://c6.y.qq.com/base/fcgi-bin/u?__=synthetic",
                "https://y.qq.com/n/ryqq/songDetail/001Example",
                "song",
                "qq",
            ),
            (
                "https://c6.y.qq.com/base/fcgi-bin/u?__=synthetic",
                "https://y.qq.com/n/ryqq/playlist/123",
                "playlist",
                "qq",
            ),
            (
                "https://163cn.tv/synthetic",
                "https://music.163.com/song?id=123",
                "song",
                "netease",
            ),
            (
                "https://163cn.tv/synthetic",
                "https://music.163.com/playlist?id=123",
                "playlist",
                "netease",
            ),
            (
                "https://t.kugou.com/synthetic",
                "https://www.kugou.com/song/#hash=" + "a" * 32,
                "song",
                "kugou",
            ),
            (
                "https://b23.tv/synthetic",
                "https://www.bilibili.com/video/BV1AB411c7mD",
                "video",
                "bilibili",
            ),
        ):
            session = Session([Response(location=final)])
            result = await resolve_music_link(short, session)
            self.assertEqual(
                (result.kind, result.platform, result.url), (kind, platform, final)
            )
            self.assertEqual(len(session.calls), 1)
            self.assertFalse(session.calls[0][1]["allow_redirects"])
            self.assertNotIn("Cookie", session.calls[0][1]["headers"])

    async def test_relative_intermediate_redirect_is_bounded_and_reused(self):
        session = Session(
            [
                Response(location="/jump/two"),
                Response(location="https://y.qq.com/n/ryqq/playlist/123"),
            ]
        )
        result = await resolve_music_link(
            "https://c6.y.qq.com/base/fcgi-bin/u?__=synthetic", session
        )
        self.assertEqual(result.kind, "playlist")
        self.assertEqual(session.calls[1][0], "https://c6.y.qq.com/jump/two")

    async def test_redirect_credentials_downgrade_foreign_family_and_ports_rejected(
        self,
    ):
        for target in (
            "http://y.qq.com/n/ryqq/playlist/123",
            "https://127.0.0.1/private",
            "https://y.qq.com.evil.invalid/",
            "https://user:pass@y.qq.com/",
            "https://y.qq.com:8443/",
            "https://music.163.com/song?id=123",
            "https://[invalid",
        ):
            session = Session([Response(location=target)])
            result = await resolve_music_link(
                "https://c6.y.qq.com/base/fcgi-bin/u?__=synthetic", session
            )
            self.assertEqual(result.kind, "invalid")
            self.assertEqual(len(session.calls), 1)

    async def test_redirect_loop_and_hop_limit(self):
        short = "https://c6.y.qq.com/base/fcgi-bin/u?__=synthetic"
        session = Session([Response(location=short)])
        self.assertEqual((await resolve_music_link(short, session)).kind, "invalid")
        self.assertEqual(len(session.calls), 1)
        session = Session([Response(location=f"/step/{index}") for index in range(8)])
        self.assertEqual((await resolve_music_link(short, session)).kind, "invalid")
        self.assertEqual(len(session.calls), 5)

    async def test_http_errors_missing_location_and_timeout_are_terminal(self):
        for response in (Response(status=503), Response(), asyncio.TimeoutError()):
            result = await resolve_music_link(
                "https://163cn.tv/synthetic", Session([response])
            )
            self.assertEqual(result.kind, "invalid")
        session = Session([asyncio.CancelledError()])
        with self.assertRaises(asyncio.CancelledError):
            await resolve_music_link("https://163cn.tv/synthetic", session)

    async def test_trusted_qq_and_kugou_200_short_page_can_use_legacy_parser(self):
        for url, platform in (
            ("https://c6.y.qq.com/base/fcgi-bin/u?__=synthetic", "qq"),
            ("https://t.kugou.com/synthetic", "kugou"),
        ):
            result = await resolve_music_link(url, Session([Response(status=200)]))
            self.assertEqual(
                (result.kind, result.platform, result.id, result.url),
                ("song", platform, "", url),
            )
        self.assertEqual(
            (
                await resolve_music_link(
                    "https://163cn.tv/synthetic", Session([Response(status=200)])
                )
            ).kind,
            "invalid",
        )

    async def test_budget_is_total_not_per_hop(self):
        with patch(
            "astrbot_plugin_kook_music.music.link_router.monotonic",
            side_effect=[0, 0, 11],
        ):
            session = Session([Response(location="/step/two")])
            result = await resolve_music_link(
                "https://c6.y.qq.com/base/fcgi-bin/u?__=synthetic", session
            )
        self.assertEqual(result.kind, "invalid")
        self.assertEqual(len(session.calls), 1)

    async def test_default_short_request_uses_cookie_free_session(self):
        fake = Session([Response(location="https://music.163.com/song?id=123")])
        context = AsyncMock()
        context.__aenter__.return_value = fake
        with patch(
            "astrbot_plugin_kook_music.music.link_router.aiohttp.ClientSession",
            return_value=context,
        ) as construct:
            result = await resolve_music_link("https://163cn.tv/synthetic")
        self.assertEqual(result.kind, "song")
        self.assertIsInstance(
            construct.call_args.kwargs["cookie_jar"], aiohttp.DummyCookieJar
        )
        self.assertFalse(construct.call_args.kwargs["trust_env"])


if __name__ == "__main__":
    unittest.main()
