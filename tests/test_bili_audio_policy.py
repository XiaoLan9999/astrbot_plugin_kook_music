import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch


PLUGINS_DIR = Path(__file__).resolve().parents[2]
if str(PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGINS_DIR))

from astrbot_plugin_kook_music.music import bilibili as bilibili_module
from astrbot_plugin_kook_music.music.bilibili import BilibiliExtractor
from astrbot_plugin_kook_music.music.model import Song


BVID = "BV1dujdzrEA4"
MEDIA_URL = "https://audio.example.test/music.m4s?token=synthetic"


def make_song(duration=3 * 60 * 60 * 1000):
    return Song(
        id=BVID,
        name="Synthetic audio",
        duration=duration,
        platform="bilibili",
        provider_data={"bvid": BVID, "page": 1},
    )


class BiliAudioPreparationTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_and_unknown_media_never_fall_back_to_full_download(self):
        for duration in (0, -1, 30 * 60 * 1000, 12 * 60 * 60 * 1000):
            with self.subTest(duration=duration):
                extractor = BilibiliExtractor()
                song = make_song(duration)
                extractor.resolve_audio_stream = AsyncMock(return_value=song)
                extractor.download_audio = AsyncMock()

                result = await extractor.prepare_audio(song, Path("unused"), 30)

                self.assertIs(result, song)
                self.assertFalse(result.playback_source)
                self.assertTrue(result.unplayable_reason)
                extractor.resolve_audio_stream.assert_awaited_once_with(song)
                extractor.download_audio.assert_not_awaited()

    async def test_short_media_keeps_download_path(self):
        extractor = BilibiliExtractor()
        song = make_song(60 * 1000)
        extractor.resolve_audio_stream = AsyncMock()
        extractor.download_audio = AsyncMock(return_value=song)

        result = await extractor.prepare_audio(song, Path("unused"), 30)

        self.assertIs(result, song)
        extractor.resolve_audio_stream.assert_not_awaited()
        extractor.download_audio.assert_awaited_once_with(song, Path("unused"))

    async def test_explicit_streaming_disable_retains_download_behavior(self):
        extractor = BilibiliExtractor()
        song = make_song()
        extractor.resolve_audio_stream = AsyncMock()
        extractor.download_audio = AsyncMock(return_value=song)

        await extractor.prepare_audio(song, Path("unused"), 0)

        extractor.resolve_audio_stream.assert_not_awaited()
        extractor.download_audio.assert_awaited_once_with(song, Path("unused"))

    async def test_prepared_source_is_not_downloaded_again(self):
        for attr, value in (("stream_url", MEDIA_URL), ("file_path", "cached.m4a")):
            with self.subTest(attr=attr):
                extractor = BilibiliExtractor()
                song = make_song()
                setattr(song, attr, value)
                extractor.resolve_audio_stream = AsyncMock()
                extractor.download_audio = AsyncMock()

                result = await extractor.prepare_audio(song, Path("unused"), 30)

                self.assertEqual(result.playback_source, value)
                extractor.resolve_audio_stream.assert_not_awaited()
                extractor.download_audio.assert_not_awaited()

    async def test_explicit_video_or_missing_audio_is_not_played(self):
        cases = (
            {"vcodec": "h264", "acodec": "aac"},
            {"vcodec": "none", "acodec": "none"},
            {"vcodec": "avc1.64001f"},
            {"requested_formats": [{"vcodec": "hevc", "acodec": "aac"}]},
        )
        for codecs in cases:
            with self.subTest(codecs=codecs):
                extractor = BilibiliExtractor()
                song = make_song()
                song.stream_url = "https://old.example.test/expired"
                song.extra_headers = {"Referer": "https://old.example.test/"}
                payload = {"url": MEDIA_URL, **codecs}
                with patch.object(
                    bilibili_module.asyncio, "to_thread", AsyncMock(return_value=payload)
                ), patch.object(bilibili_module.asyncio, "sleep", AsyncMock()):
                    result = await extractor.resolve_audio_stream(song)

                self.assertFalse(result.playback_source)
                self.assertEqual(result.extra_headers, {})
                self.assertTrue(result.unplayable_reason)

    async def test_audio_and_older_missing_codec_payloads_remain_compatible(self):
        for codecs in ({}, {"vcodec": "none", "acodec": "mp4a.40.2"}):
            with self.subTest(codecs=codecs):
                extractor = BilibiliExtractor()
                song = make_song()
                song.unplayable_reason = "Previous temporary failure"
                with patch.object(
                    bilibili_module.asyncio,
                    "to_thread",
                    AsyncMock(return_value={"url": MEDIA_URL, **codecs}),
                ):
                    result = await extractor.resolve_audio_stream(song)

                self.assertEqual(result.stream_url, MEDIA_URL)
                self.assertEqual(result.unplayable_reason, "")

    async def test_exception_payload_does_not_leak_signed_url(self):
        extractor = BilibiliExtractor()
        song = make_song()
        extractor.download_audio = AsyncMock()
        with patch.object(
            bilibili_module.asyncio,
            "to_thread",
            AsyncMock(side_effect=RuntimeError(MEDIA_URL)),
        ), patch.object(bilibili_module.asyncio, "sleep", AsyncMock()):
            with self.assertLogs("astrbot", level="WARNING") as logs:
                result = await extractor.prepare_audio(song, Path("unused"), 30)

        self.assertFalse(result.playback_source)
        self.assertNotIn("token=synthetic", result.unplayable_reason)
        self.assertNotIn("token=synthetic", "\n".join(logs.output))
        extractor.download_audio.assert_not_awaited()

    async def test_invalid_payloads_fail_without_full_download(self):
        for payload in (None, [], "unexpected"):
            with self.subTest(payload=payload):
                extractor = BilibiliExtractor()
                song = make_song()
                extractor.download_audio = AsyncMock()
                with patch.object(
                    bilibili_module.asyncio, "to_thread", AsyncMock(return_value=payload)
                ), patch.object(bilibili_module.asyncio, "sleep", AsyncMock()):
                    result = await extractor.prepare_audio(song, Path("unused"), 30)

                self.assertFalse(result.playback_source)
                self.assertTrue(result.unplayable_reason)
                extractor.download_audio.assert_not_awaited()


class BiliAudioFormatTests(unittest.TestCase):
    def test_selector_prefers_m4a_and_never_falls_back_to_muxed_video(self):
        formats = [
            {"format_id": "aac", "ext": "m4a", "vcodec": "none", "acodec": "aac"},
            {"format_id": "flac", "ext": "flac", "vcodec": "none", "acodec": "flac"},
            {"format_id": "mux", "ext": "mp4", "vcodec": "h264", "acodec": "aac"},
        ]
        with bilibili_module.yt_dlp.YoutubeDL({"quiet": True}) as ydl:
            selector = ydl.build_format_selector(BilibiliExtractor.AUDIO_FORMAT)
            self.assertEqual(ydl._select_formats(formats, selector)[0]["format_id"], "aac")
            self.assertEqual(ydl._select_formats(formats[1:], selector)[0]["format_id"], "flac")
            self.assertEqual(ydl._select_formats(formats[2:], selector), [])

    def test_stream_and_download_use_same_audio_only_selector(self):
        options = []
        downloads = []

        class FakeYoutubeDL:
            def __init__(self, opts):
                options.append(opts)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, url, download):
                downloads.append(download)
                return {"url": MEDIA_URL, "ext": "m4a", "vcodec": "none", "acodec": "aac"}

        extractor = BilibiliExtractor()
        with patch.object(bilibili_module.yt_dlp, "YoutubeDL", FakeYoutubeDL):
            stream = extractor._yt_resolve_audio_stream("https://www.bilibili.com/")
            output = extractor._yt_download_audio("https://www.bilibili.com/", "output")

        self.assertEqual(stream["url"], MEDIA_URL)
        self.assertEqual(stream["vcodec"], "none")
        self.assertEqual(output, "output.m4a")
        self.assertEqual(downloads, [False, True])
        self.assertTrue(all(opt["format"] == "ba[ext=m4a]/ba" for opt in options))
        self.assertTrue(all(opt["noplaylist"] for opt in options))
        self.assertTrue(options[0]["skip_download"])

    def test_ytdlp_muxed_result_is_rejected_and_download_artifact_removed(self):
        ydl = Mock()
        ydl.__enter__ = Mock(return_value=ydl)
        ydl.__exit__ = Mock(return_value=False)
        ydl.extract_info.return_value = {
            "url": MEDIA_URL,
            "ext": "mp4",
            "vcodec": "h264",
            "acodec": "aac",
        }
        extractor = BilibiliExtractor()
        with tempfile.TemporaryDirectory() as temp_dir:
            output_base = Path(temp_dir) / "test-output"
            artifact = output_base.with_suffix(".mp4")
            artifact.write_bytes(b"synthetic media")
            with patch.object(bilibili_module.yt_dlp, "YoutubeDL", return_value=ydl):
                stream = extractor._yt_resolve_audio_stream("https://www.bilibili.com/")
                output = extractor._yt_download_audio("https://www.bilibili.com/", str(output_base))

            self.assertEqual(stream, {})
            self.assertEqual(output, "")
            self.assertFalse(artifact.exists())

    def test_download_chunks_work_with_a_cdn_that_rejects_non_range_requests(self):
        payload = bytes(range(256)) * 10000
        ranges = []

        class RangeOnlyHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                value = self.headers.get("Range", "")
                ranges.append(value)
                if not value.startswith("bytes="):
                    self.send_error(567)
                    return
                start, end = value[6:].split("-", 1)
                start = int(start)
                end = min(int(end) if end else len(payload) - 1, len(payload) - 1)
                body = payload[start:end + 1]
                self.send_response(206)
                self.send_header("Content-Type", "audio/mp4")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), RangeOnlyHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        extractor = BilibiliExtractor()
        real_ydl = bilibili_module.yt_dlp.YoutubeDL
        source = f"http://127.0.0.1:{server.server_port}/audio.m4a"

        class SeededYoutubeDL:
            def __init__(self, options):
                self.ydl = real_ydl({**options, "noprogress": True, "logger": Mock()})

            def __enter__(self):
                self.ydl.__enter__()
                return self

            def __exit__(self, *args):
                return self.ydl.__exit__(*args)

            def extract_info(self, url, download):
                return self.ydl.process_ie_result(
                    {
                        "id": BVID,
                        "title": "Synthetic range audio",
                        "url": source,
                        "ext": "m4a",
                        "vcodec": "none",
                        "acodec": "aac",
                    },
                    download=download,
                )

        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                output_base = str(Path(temp_dir) / "range-audio")
                with patch.object(
                    extractor,
                    "_create_ydl",
                    lambda options: SeededYoutubeDL(
                        {**options, "http_chunk_size": 0, "retries": 0}
                    ),
                ), self.assertRaises(bilibili_module.yt_dlp.utils.DownloadError):
                    extractor._yt_download_audio(
                        f"https://www.bilibili.com/video/{BVID}",
                        str(Path(temp_dir) / "non-range-audio"),
                    )
                self.assertEqual(ranges, [""])
                ranges.clear()
                with patch.object(extractor, "_create_ydl", SeededYoutubeDL):
                    output = extractor._yt_download_audio(
                        f"https://www.bilibili.com/video/{BVID}", output_base
                    )
                self.assertEqual(Path(output).read_bytes(), payload)
            self.assertGreaterEqual(len(ranges), 3)
            self.assertTrue(all(value.startswith("bytes=") for value in ranges))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
