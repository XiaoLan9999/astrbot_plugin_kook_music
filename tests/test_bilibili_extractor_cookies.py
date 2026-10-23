import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from urllib.request import Request

import yt_dlp

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_kook_music.music.bilibili import BilibiliExtractor
from astrbot_plugin_kook_music.music.model import Song


COOKIES = {
    "SESSDATA": "synthetic%2Csession%2Btoken%3D",
    "bili_jct": "synthetic-csrf",
    "DedeUserID": "123",
    "DedeUserID__ckMd5": "synthetic-md5",
    "buvid3": "synthetic-device-three",
    "buvid4": "synthetic-device-four",
}


def header_values(header):
    return dict(part.strip().split("=", 1) for part in header.split(";") if part.strip())


def netscape_export(cookies=COOKIES):
    return "# Netscape HTTP Cookie File\n" + "\n".join(
        f"#HttpOnly_.bilibili.com\tTRUE\t/\tTRUE\t0\t{name}\t{value}"
        for name, value in cookies.items()
    )


def cookie_exports():
    return (
        "; ".join(f"{name}={value}" for name, value in COOKIES.items()),
        json.dumps(
            [
                {
                    "name": name,
                    "value": value,
                    "domain": ".bilibili.com",
                    "path": "/",
                    "hostOnly": False,
                    "session": True,
                }
                for name, value in COOKIES.items()
            ]
        ),
        netscape_export(),
    )


def request_cookies(jar, url):
    request = Request(url)
    jar.add_cookie_header(request)
    return header_values(request.get_header("Cookie", ""))


class BilibiliExtractorCookieTests(unittest.TestCase):
    def setUp(self):
        self.extractor = BilibiliExtractor()
        temporary = tempfile.TemporaryDirectory()
        self.directory = Path(temporary.name)
        self.addCleanup(temporary.cleanup)

    def assert_no_cookie_transport_options(self, opts):
        self.assertNotIn("cookiefile", opts)
        self.assertNotIn("cookiesfrombrowser", opts)
        self.assertNotIn("Cookie", opts.get("http_headers", {}))
        self.assertFalse(
            any(name.lower() == "cookie" for name in opts.get("http_headers", {}))
        )

    def test_three_formats_match_header_and_real_ydl_cookiejar_without_writing(self):
        for index, text in enumerate(cookie_exports()):
            with self.subTest(format=index):
                data_dir = self.directory / f"format-{index}"
                self.assertTrue(self.extractor.set_cookie(text, data_dir))
                self.assertEqual(self.extractor._cookie_values, COOKIES)
                self.assertEqual(header_values(self.extractor._cookie_header), COOKIES)
                self.assertEqual(list(data_dir.rglob("*")), [])
                opts = self.extractor._base_ydl_opts()
                self.assert_no_cookie_transport_options(opts)
                with self.extractor._create_ydl(opts) as ydl:
                    self.assertIsInstance(ydl, yt_dlp.YoutubeDL)
                    self.assert_no_cookie_transport_options(ydl.params)
                    self.assertEqual(
                        request_cookies(
                            ydl.cookiejar,
                            "https://api.bilibili.com/x/web-interface/nav",
                        ),
                        COOKIES,
                    )
                    self.assertEqual(
                        request_cookies(
                            ydl.cookiejar,
                            "https://www.bilibili.com/video/BV1dujdzrEA4",
                        ),
                        COOKIES,
                    )
                    self.assertEqual(
                        {cookie.domain for cookie in ydl.cookiejar}, {".bilibili.com"}
                    )

    def test_legacy_bare_sessdata_keeps_encoded_value_in_memory(self):
        self.assertTrue(
            self.extractor.set_cookie(COOKIES["SESSDATA"], self.directory)
        )
        self.assertEqual(
            self.extractor._cookie_values, {"SESSDATA": COOKIES["SESSDATA"]}
        )
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_real_ydl_cookiejar_does_not_authenticate_media_or_lookalike_hosts(self):
        self.assertTrue(self.extractor.set_auth_cookies(dict(COOKIES)))
        with self.extractor._create_ydl(self.extractor._base_ydl_opts()) as ydl:
            for url in (
                "https://upos-sz-mirrorcos.bilivideo.com/audio.m4s",
                "https://mcdn.bilivideo.cn/audio.m4s",
                "https://bilibili.com.evil.example/audio.m4s",
                "https://notbilibili.com/audio.m4s",
                "https://evil.example/audio.m4s",
                "http://api.bilibili.com/x/web-interface/nav",
            ):
                with self.subTest(url=url):
                    self.assertEqual(request_cookies(ydl.cookiejar, url), {})

    def test_invalid_paste_cannot_replace_current_authentication_or_cache(self):
        self.assertTrue(self.extractor.set_auth_cookies(dict(COOKIES)))
        self.extractor._video_view_cache["old"] = (99999999999, {"old": True})
        for text in (
            "SESSDATA=",
            "SESSDATA=synthetic\r\nX-Other: value",
            json.dumps(
                [
                    {
                        "name": "SESSDATA",
                        "value": "different",
                        "domain": "evil.example",
                        "path": "/",
                    }
                ]
            ),
        ):
            with self.subTest(text=text[:15]):
                self.assertFalse(self.extractor.set_cookie(text, self.directory))
                self.assertEqual(self.extractor._cookie_values, COOKIES)
                self.assertEqual(header_values(self.extractor._cookie_header), COOKIES)
                self.assertIn("old", self.extractor._video_view_cache)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_account_update_copies_input_and_invalid_input_preserves_account(self):
        values = dict(COOKIES)
        self.assertTrue(self.extractor.set_auth_cookies(values))
        values["SESSDATA"] = "mutated-outside-extractor"
        self.assertEqual(self.extractor._cookie_values, COOKIES)
        for invalid in (
            {"bili_jct": "csrf"},
            {"SESSDATA": "bad\ncredential"},
            {"SESSDATA": "synthetic; bili_jct=other"},
        ):
            with self.subTest(invalid=tuple(invalid)):
                try:
                    result = self.extractor.set_auth_cookies(invalid)
                except ValueError as error:
                    self.assertNotIn("synthetic", str(error))
                else:
                    self.assertFalse(result)
                self.assertEqual(self.extractor._cookie_values, COOKIES)

    def test_clear_account_clears_authentication_and_metadata_caches(self):
        self.assertTrue(self.extractor.set_auth_cookies(dict(COOKIES)))
        self.extractor._video_view_cache["old"] = (99999999999, {"old": True})
        self.extractor._favorite_page_cache[("12", 1)] = (99999999999, {"old": True})
        self.extractor.set_auth_cookies(None)
        self.assertEqual(self.extractor._cookie_values, {})
        self.assertEqual(self.extractor._cookie_header, "")
        self.assertEqual(self.extractor._video_view_cache, {})
        self.assertEqual(self.extractor._favorite_page_cache, {})
        self.assert_no_cookie_transport_options(self.extractor._base_ydl_opts())
        with self.extractor._create_ydl(self.extractor._base_ydl_opts()) as ydl:
            self.assertEqual(list(ydl.cookiejar), [])

    def test_manual_netscape_file_is_loaded_read_only_and_never_attached_to_ydl(self):
        path = self.directory / "operator-cookies.txt"
        original = netscape_export().encode("utf-8")
        path.write_bytes(original)
        before_mtime = path.stat().st_mtime_ns
        self.assertTrue(self.extractor.set_cookies_file(str(path)))
        self.assertEqual(self.extractor._cookie_values, COOKIES)
        self.assertEqual(header_values(self.extractor._cookie_header), COOKIES)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(path.stat().st_mtime_ns, before_mtime)
        self.assertEqual(list(self.directory.iterdir()), [path])
        opts = self.extractor._base_ydl_opts()
        self.assert_no_cookie_transport_options(opts)
        with self.extractor._create_ydl(opts) as ydl:
            self.assertEqual(
                request_cookies(ydl.cookiejar, "https://api.bilibili.com/"), COOKIES
            )
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(path.stat().st_mtime_ns, before_mtime)

    def test_missing_or_invalid_manual_file_preserves_current_cookie(self):
        self.assertTrue(self.extractor.set_auth_cookies(dict(COOKIES)))
        missing = self.directory / "missing.txt"
        self.assertFalse(self.extractor.set_cookies_file(str(missing)))
        invalid = self.directory / "invalid.txt"
        invalid.write_text("# Netscape HTTP Cookie File\nevil.example\tTRUE\t/\tTRUE\t0\tSESSDATA\twrong", encoding="utf-8")
        self.assertFalse(self.extractor.set_cookies_file(str(invalid)))
        self.assertEqual(self.extractor._cookie_values, COOKIES)
        self.assertEqual(header_values(self.extractor._cookie_header), COOKIES)

    def test_static_read_cookie_header_retains_netscape_http_only_compatibility(self):
        path = self.directory / "static-cookies.txt"
        path.write_text(netscape_export(), encoding="utf-8")
        self.assertEqual(
            header_values(BilibiliExtractor._read_cookie_header(path)), COOKIES
        )


class BilibiliExtractorApiCookieTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_format_is_used_on_api_requests_without_touching_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            for index, text in enumerate(cookie_exports()):
                with self.subTest(format=index):
                    extractor = BilibiliExtractor()
                    self.assertTrue(extractor.set_cookie(text, Path(directory)))
                    response = Mock()
                    response.status = 200
                    response.headers = {}
                    response.json = AsyncMock(return_value={"code": 0, "data": {}})
                    context = Mock()
                    context.__aenter__ = AsyncMock(return_value=response)
                    context.__aexit__ = AsyncMock(return_value=False)
                    session = Mock()
                    session.get.return_value = context
                    extractor._get_session = AsyncMock(return_value=session)
                    self.assertEqual(
                        await extractor._api_get_json(
                            extractor.VIEW_API_URL, {"bvid": "BV1dujdzrEA4"}
                        ),
                        {"code": 0, "data": {}},
                    )
                    headers = session.get.call_args.kwargs["headers"]
                    self.assertEqual(header_values(headers["Cookie"]), COOKIES)
                    self.assertEqual(list(Path(directory).iterdir()), [])


class BilibiliCredentialRevocationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.extractor = BilibiliExtractor()
        self.extractor.set_auth_cookies(dict(COOKIES))
        self.pending = []
        self.release_events = []

    async def asyncTearDown(self):
        for event in self.release_events:
            event.set()
        for task in self.pending:
            if not task.done():
                task.cancel()
        if self.pending:
            await asyncio.gather(*self.pending, return_exceptions=True)
        await self.extractor.close()

    def gate(self):
        started = asyncio.Event()
        release = asyncio.Event()
        self.release_events.append(release)
        return started, release

    def start(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.pending.append(task)
        return task

    def change_account(self):
        values = dict(COOKIES)
        values["SESSDATA"] = "synthetic-different-account"
        self.extractor.set_auth_cookies(values)

    async def test_waiting_api_request_does_not_send_revoked_cookie(self):
        response = Mock()
        response.status = 200
        response.headers = {}
        response.json = AsyncMock(return_value={"code": 0, "data": {}})
        context = Mock()
        context.__aenter__ = AsyncMock(return_value=response)
        context.__aexit__ = AsyncMock(return_value=False)
        session = Mock()
        session.get.return_value = context
        self.extractor._get_session = AsyncMock(return_value=session)
        await self.extractor._api_lock.acquire()
        try:
            pending = self.start(
                self.extractor._api_get_json(self.extractor.VIEW_API_URL, {})
            )
            await asyncio.sleep(0)
            self.assertFalse(pending.done())
            self.extractor.set_auth_cookies(None)
        finally:
            self.extractor._api_lock.release()
        self.assertIsNone(await asyncio.wait_for(pending, 1))
        session.get.assert_not_called()

    async def test_late_view_result_cannot_repopulate_cache_after_account_change(self):
        started, release = self.gate()

        async def late_api(*args, **kwargs):
            started.set()
            await release.wait()
            return {"code": 0, "data": {"title": "old-account-view"}}

        self.extractor._api_get_json = AsyncMock(side_effect=late_api)
        pending = self.start(self.extractor._fetch_video_view("BV1dujdzrEA4"))
        await asyncio.wait_for(started.wait(), 1)
        self.change_account()
        release.set()
        self.assertIsNone(await asyncio.wait_for(pending, 1))
        self.assertEqual(self.extractor._video_view_cache, {})

    async def test_cancel_resistant_favorite_request_cannot_restore_private_cache(self):
        started, release = self.gate()
        cancelled = asyncio.Event()

        async def late_api(*args, **kwargs):
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            return {"code": 0, "data": {"medias": [{"title": "old-private-item"}]}}

        self.extractor._api_get_json = AsyncMock(side_effect=late_api)
        pending = self.start(self.extractor._fetch_favorite_page("123", 1))
        await asyncio.wait_for(started.wait(), 1)
        self.assertIn(("123", 1), self.extractor._favorite_page_tasks)
        self.extractor.set_auth_cookies(None)
        await asyncio.wait_for(cancelled.wait(), 1)
        release.set()
        self.assertIsNone(await asyncio.wait_for(pending, 1))
        self.assertEqual(self.extractor._favorite_page_cache, {})
        self.assertEqual(self.extractor._favorite_page_tasks, {})

    async def test_late_stream_resolution_is_discarded_without_retrying(self):
        started, release = self.gate()
        calls = []

        async def late_thread(function, *args, **kwargs):
            calls.append(function)
            started.set()
            await release.wait()
            return {
                "url": "https://upos-sz-mirrorcos.bilivideo.com/audio.m4s",
                "vcodec": "none",
                "acodec": "mp4a.40.2",
                "title": "old-account-stream",
                "http_headers": {"Referer": "https://www.bilibili.com/"},
            }

        song = Song(id="BV1dujdzrEA4", name="original-title", platform="bilibili")
        with patch(
            "astrbot_plugin_kook_music.music.bilibili.asyncio.to_thread",
            side_effect=late_thread,
        ):
            pending = self.start(self.extractor.resolve_audio_stream(song))
            await asyncio.wait_for(started.wait(), 1)
            self.change_account()
            release.set()
            self.assertIs(await asyncio.wait_for(pending, 1), song)
        self.assertEqual(len(calls), 1)
        self.assertEqual(song.stream_url, "")
        self.assertEqual(song.extra_headers, {})
        self.assertEqual(song.name, "original-title")

    async def test_late_metadata_fallback_cannot_create_song_after_logout(self):
        started, release = self.gate()
        calls = []

        async def late_thread(function, *args, **kwargs):
            calls.append(function)
            started.set()
            await release.wait()
            return {"title": "old-account-title", "uploader": "old-uploader", "duration": 100}

        url = "https://www.bilibili.com/video/BV1dujdzrEA4?p=1"
        self.extractor.resolve_input = AsyncMock(return_value=url)
        self.extractor._fetch_video_view = AsyncMock(return_value=None)
        with patch(
            "astrbot_plugin_kook_music.music.bilibili.asyncio.to_thread",
            side_effect=late_thread,
        ):
            pending = self.start(self.extractor.extract(url))
            await asyncio.wait_for(started.wait(), 1)
            self.extractor.set_auth_cookies(None)
            release.set()
            self.assertIsNone(await asyncio.wait_for(pending, 1))
        self.assertEqual(len(calls), 1)

    async def test_late_download_is_removed_without_replacing_song_filepath(self):
        started, release = self.gate()
        derived_paths = []

        async def late_thread(function, url, output_base):
            started.set()
            await release.wait()
            for suffix in (".mp3", ".mp3.part"):
                path = Path(output_base + suffix)
                path.write_bytes(b"synthetic-derived-file")
                derived_paths.append(path)
            return str(derived_paths[0])

        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            preserved = cache_dir / "existing-unrelated.mp3"
            preserved.write_bytes(b"existing-unrelated-file")
            song = Song(id="BV1dujdzrEA4", name="original-title", platform="bilibili")
            with patch(
                "astrbot_plugin_kook_music.music.bilibili.asyncio.to_thread",
                side_effect=late_thread,
            ):
                pending = self.start(self.extractor.download_audio(song, cache_dir))
                await asyncio.wait_for(started.wait(), 1)
                self.extractor.set_auth_cookies(None)
                release.set()
                self.assertIs(await asyncio.wait_for(pending, 1), song)
            self.assertEqual(song.file_path, "")
            self.assertTrue(derived_paths)
            self.assertTrue(all(not path.exists() for path in derived_paths))
            self.assertEqual(preserved.read_bytes(), b"existing-unrelated-file")
            self.assertEqual(list(cache_dir.iterdir()), [preserved])


if __name__ == "__main__":
    unittest.main()
