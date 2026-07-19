import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock


PLUGINS_DIR = Path(__file__).resolve().parents[2]
if str(PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGINS_DIR))

from astrbot_plugin_kook_music.music.downloader import MusicDownloader
from astrbot_plugin_kook_music.music.model import Song
from astrbot_plugin_kook_music.music.playlist_import import PlaylistImporter
from astrbot_plugin_kook_music.music.searcher import MusicSearcher


class FakeContent:
    def __init__(self, body: bytes):
        self.body = body

    async def read(self, size: int = -1) -> bytes:
        return self.body if size < 0 else self.body[:size]

    async def iter_chunked(self, size: int):
        for offset in range(0, len(self.body), size):
            yield self.body[offset:offset + size]


class FakeResponse:
    def __init__(
        self,
        data=None,
        body=b"",
        status=200,
        content_type="application/json",
        headers=None,
    ):
        self.data = data
        self.status = status
        self.headers = {"Content-Type": content_type}
        self.headers.update(headers or {})
        self.content = FakeContent(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self, content_type=None):
        return self.data

    async def text(self):
        return self.body.decode("utf-8", errors="replace")


class FakeSession:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.last_json = None

    def get(self, *args, **kwargs):
        return self.response

    def post(self, *args, **kwargs):
        self.last_json = kwargs.get("json")
        return self.response


class FakeSequenceSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.get_calls = []

    def get(self, *args, **kwargs):
        self.get_calls.append((args, kwargs))
        return self.responses.pop(0)


class MusicPlatformParsingTests(unittest.TestCase):
    def test_platform_aliases_include_qq_and_kugou(self):
        self.assertEqual(MusicSearcher.resolve_platform("qq\u97f3\u4e50"), "qq")
        self.assertEqual(MusicSearcher.resolve_platform("\u817e\u8baf\u97f3\u4e50"), "qq")
        self.assertEqual(MusicSearcher.resolve_platform("\u9177\u72d7\u97f3\u4e50"), "kugou")
        self.assertEqual(MusicSearcher.resolve_platform("kg"), "kugou")

    def test_current_aggregator_data_field_is_supported(self):
        result = {
            "code": 200,
            "data": [{
                "songid": "qq-mid",
                "title": "title",
                "author": "artist",
                "url": "http://aqqmusic.tc.qq.com/C400qq-mid.m4a?vkey=x",
                "pic": "https://example.test/cover.jpg",
            }],
        }
        songs = MusicSearcher._parse_aggregator_songs(result, "qq", 5)
        self.assertEqual(len(songs), 1)
        self.assertEqual(songs[0].id, "qq-mid")
        self.assertTrue(songs[0].audio_url.startswith("https://"))
        self.assertEqual(songs[0].extra_headers["Referer"], "https://y.qq.com/")

    def test_qq_search_uses_pay_play_not_membership_flag(self):
        free = {
            "mid": "free-mid",
            "name": "free",
            "interval": 180,
            "singer": [{"name": "singer"}],
            "album": {"mid": "album-mid"},
            "file": {"media_mid": "media-mid"},
            "pay": {"pay_play": 0, "pay_month": 1},
        }
        paid = {
            **free,
            "mid": "paid-mid",
            "name": "paid",
            "pay": {"pay_play": 1, "pay_month": 1},
        }
        result = {
            "req_0": {
                "code": 0,
                "data": {"body": {"song": {"list": [paid, free]}}},
            }
        }
        songs = MusicSearcher._parse_qq_search_songs(result, 5)
        self.assertEqual([song.id for song in songs], ["free-mid"])
        self.assertEqual(songs[0].duration, 180000)
        self.assertEqual(songs[0].provider_data["media_mid"], "media-mid")

    def test_kugou_search_keeps_candidates_for_real_url_verification(self):
        free = {
            "FileHash": "db711f2e78c179a52bb660c3ed2651aa",
            "SongName": "<em>You</em>",
            "SingerName": "<em>TSAR</em>",
            "Duration": 190,
            "Privilege": 0,
            "PayType": 0,
            "Image": "http://img.test/{size}/cover.jpg",
            "trans_param": "unexpected",
        }
        paid = {**free, "FileHash": "B" * 32, "Privilege": 10, "PayType": 3}
        result = {"status": 1, "data": {"lists": [paid, free]}}
        songs = MusicSearcher._parse_kugou_search_songs(result, 5)
        self.assertEqual(len(songs), 2)
        self.assertEqual(songs[1].id, "DB711F2E78C179A52BB660C3ED2651AA")
        self.assertEqual(songs[1].name, "You")
        self.assertEqual(songs[1].artists, "TSAR")
        self.assertEqual(songs[1].duration, 190000)
        self.assertIn("/400/", songs[1].cover_url)

    def test_legacy_qq_track_fields_are_supported(self):
        track = {
            "songmid": "OLDMID",
            "songname": "Legacy Song",
            "strMediaMid": "OLDMEDIA",
            "albummid": "OLDALBUM",
            "interval": 199,
            "singer": [{"name": "Legacy Singer"}],
            "pay": {"pay_play": 0},
        }
        song = MusicSearcher._parse_qq_track(track, require_free=True)
        self.assertEqual(song.id, "OLDMID")
        self.assertEqual(song.name, "Legacy Song")
        self.assertEqual(song.provider_data["media_mid"], "OLDMEDIA")
        self.assertIn("OLDALBUM", song.cover_url)

    def test_common_qq_and_kugou_song_links_are_parsed(self):
        qq_mid = "0039MnYb0qxYhV"
        self.assertEqual(
            MusicSearcher._extract_qq_song_id(
                f"https://y.qq.com/n/ryqq/songDetail/{qq_mid}"
            ),
            qq_mid,
        )
        self.assertEqual(
            MusicSearcher._extract_qq_song_id(
                f"https://i.y.qq.com/v8/playsong.html?songmid={qq_mid}"
            ),
            qq_mid,
        )
        kugou_hash = "D7689BEC4AE13D6FFB741B3598759376"
        self.assertEqual(
            MusicSearcher._extract_kugou_hash(
                f"https://www.kugou.com/song/#hash={kugou_hash}&album_id=1"
            ),
            kugou_hash,
        )
        self.assertEqual(
            PlaylistImporter.parse_direct_song_input(
                f"https://y.qq.com/n/yqq/song/{qq_mid}.html"
            ),
            ("", ""),
        )
        self.assertEqual(
            MusicSearcher.detect_direct_platform("https://not-y.qq.com.evil.test/song"),
            "",
        )

    def test_meting_uses_tencent_server_id_instead_of_qq(self):
        result = [{
            "name": "song",
            "artist": "artist",
            "url": (
                "https://api.qijieya.cn/meting/"
                "?server=tencent&type=url&id=qq-mid"
            ),
        }]
        songs = MusicSearcher._parse_meting_songs(result, "qq", 1)
        self.assertEqual(songs[0].id, "qq-mid")
        self.assertEqual(songs[0].platform, "qq")


class MusicPlatformAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_qq_search_with_vip_resolver_keeps_paid_candidates(self):
        free = {
            "mid": "free-mid",
            "name": "free",
            "file": {"media_mid": "free-media"},
            "pay": {"pay_play": 0},
        }
        paid = {
            "mid": "paid-mid",
            "name": "paid",
            "file": {"media_mid": "paid-media"},
            "pay": {"pay_play": 1},
        }
        response = FakeResponse(data={
            "req_0": {
                "code": 0,
                "data": {"body": {"song": {"list": [paid, free]}}},
            }
        })
        searcher = MusicSearcher(
            qq_vip_resolver_url="https://resolver.example/api"
        )
        searcher._get_session = AsyncMock(return_value=FakeSession(response))
        songs = await searcher._search_qq_direct("keyword", 5)
        self.assertEqual([song.id for song in songs], ["paid-mid", "free-mid"])

    async def test_qq_vkey_uses_media_mid_and_builds_stream_url(self):
        response = FakeResponse(data={
            "req_0": {
                "data": {
                    "sip": ["https://returned.example/"],
                    "midurlinfo": [{"result": "0", "purl": "M500media-mid.mp3?vkey=x"}],
                }
            }
        })
        session = FakeSession(response)
        searcher = MusicSearcher()
        song = Song(
            id="song-mid",
            platform="qq",
            provider_data={"media_mid": "media-mid"},
            unplayable_reason="old failure",
        )
        await searcher._fill_qq_audio_url(session, song)
        self.assertEqual(
            session.last_json["req_0"]["param"]["filename"],
            ["M500media-mid.mp3"],
        )
        self.assertTrue(song.audio_url.startswith("https://returned.example/"))
        self.assertEqual(song.unplayable_reason, "")

    async def test_qq_denied_song_uses_meting_vip_resolver_by_exact_mid(self):
        song_mid = "paid-mid"
        direct = Song(
            id=song_mid,
            name="official title",
            artists="official artist",
            duration=162000,
            cover_url="https://cover.example/image.jpg",
            platform="qq",
            unplayable_reason="该歌曲需要 QQ 音乐会员或受版权/地区限制",
            provider_data={
                "media_mid": "official-media-mid",
                "resolver_status": "denied",
            },
        )
        audio_url = "https://aqqmusic.tc.qq.com/C400official-media-mid.m4a?vkey=x"
        session = FakeSequenceSession([
            FakeResponse(data=[{
                "songmid": song_mid,
                "title": "resolver title must not replace official metadata",
                "author": "resolver artist",
                "url": (
                    "https://resolver.example/api"
                    f"?server=tencent&type=url&id={song_mid}"
                ),
            }]),
            FakeResponse(
                status=302,
                headers={"Location": audio_url},
            ),
        ])
        searcher = MusicSearcher(
            qq_vip_resolver_url="https://resolver.example/api",
            qq_vip_resolver_quality="flac",
        )
        searcher._fetch_qq_song_by_id = AsyncMock(return_value=direct)
        searcher._get_session = AsyncMock(return_value=session)
        searcher._search_via_aggregator = AsyncMock()

        resolved = await searcher.fetch_song_by_id("qq", song_mid)

        self.assertIs(resolved, direct)
        self.assertEqual(resolved.audio_url, audio_url)
        self.assertEqual(resolved.name, "official title")
        self.assertEqual(resolved.artists, "official artist")
        self.assertEqual(resolved.duration, 162000)
        self.assertEqual(resolved.provider_data["media_mid"], "official-media-mid")
        self.assertEqual(resolved.provider_data["resolver_status"], "resolved")
        self.assertEqual(resolved.unplayable_reason, "")
        self.assertEqual(session.get_calls[0][1]["params"]["id"], song_mid)
        self.assertNotIn("mid", session.get_calls[0][1]["params"])
        self.assertNotIn("quality", session.get_calls[0][1]["params"])
        self.assertFalse(session.get_calls[1][1]["allow_redirects"])
        searcher._search_via_aggregator.assert_not_awaited()

    async def test_qq_meting_resolver_extracts_exact_mid_from_url(self):
        song_mid = "paid-mid"
        direct = Song(
            id=song_mid,
            name="official title",
            platform="qq",
            unplayable_reason="会员歌曲",
            provider_data={"resolver_status": "denied"},
        )
        audio_url = "https://aqqmusic.tc.qq.com/C400paid-media.m4a?vkey=x"
        session = FakeSequenceSession([
            FakeResponse(data=[{
                "title": "resolver title",
                "author": "resolver artist",
                "url": (
                    "https://resolver.example/api"
                    f"?server=tencent&type=url&id={song_mid}"
                ),
                "lrc": (
                    "https://resolver.example/api"
                    f"?server=tencent&type=lrc&id={song_mid}"
                ),
            }]),
            FakeResponse(status=302, headers={"Location": audio_url}),
        ])
        searcher = MusicSearcher(
            qq_vip_resolver_url="https://resolver.example/api"
        )
        searcher._fetch_qq_song_by_id = AsyncMock(return_value=direct)
        searcher._get_session = AsyncMock(return_value=session)

        resolved = await searcher.fetch_song_by_id("qq", song_mid)

        self.assertIs(resolved, direct)
        self.assertEqual(resolved.audio_url, audio_url)
        self.assertEqual(resolved.provider_data["resolver_status"], "resolved")

    async def test_qq_vip_resolver_rejects_non_qq_public_audio_host(self):
        direct = Song(
            id="paid-mid",
            platform="qq",
            unplayable_reason="会员歌曲",
            provider_data={"resolver_status": "denied"},
        )
        searcher = MusicSearcher(
            qq_vip_resolver_url="https://api.ygking.top/api/song/url"
        )
        searcher._fetch_qq_song_by_id = AsyncMock(return_value=direct)
        searcher._get_session = AsyncMock(return_value=FakeSequenceSession([
            FakeResponse(data={
                "code": 0,
                "data": {
                    "paid-mid": "https://audio.example.test/paid.mp3",
                },
            }),
        ]))

        resolved = await searcher.fetch_song_by_id("qq", "paid-mid")

        self.assertIs(resolved, direct)
        self.assertEqual(resolved.audio_url, "")
        self.assertEqual(resolved.provider_data["resolver_status"], "denied")

    async def test_qq_denied_song_uses_ygking_vip_resolver_by_exact_mid(self):
        song_mid = "paid-mid"
        audio_url = "https://isure.stream.qqmusic.qq.com/paid.mp3?vkey=x"
        direct = Song(
            id=song_mid,
            name="official title",
            platform="qq",
            unplayable_reason="会员歌曲",
            provider_data={"resolver_status": "denied", "media_mid": "media-mid"},
        )
        session = FakeSequenceSession([FakeResponse(data={
            "code": 0,
            "data": {song_mid: audio_url},
            "quality": "320",
        })])
        searcher = MusicSearcher(
            qq_vip_resolver_url="https://api.ygking.top/api/song/url"
        )
        searcher._fetch_qq_song_by_id = AsyncMock(return_value=direct)
        searcher._get_session = AsyncMock(return_value=session)

        resolved = await searcher.fetch_song_by_id("qq", song_mid)

        self.assertIs(resolved, direct)
        self.assertEqual(resolved.audio_url, audio_url)
        self.assertEqual(resolved.provider_data["resolver_status"], "resolved")
        self.assertEqual(resolved.unplayable_reason, "")
        self.assertEqual(session.get_calls[0][1]["params"], {
            "mid": song_mid,
            "quality": "320",
        })

    async def test_qq_vip_resolver_rejects_wrong_mid_and_unsafe_url(self):
        cases = {
            "wrong mid": {
                "code": 0,
                "data": {"other-mid": "https://audio.example/wrong.mp3"},
            },
            "unsafe url": {
                "code": 0,
                "data": {"paid-mid": "http://127.0.0.1/private.mp3"},
            },
        }
        for label, response_data in cases.items():
            with self.subTest(label=label):
                direct = Song(
                    id="paid-mid",
                    name="official title",
                    platform="qq",
                    unplayable_reason="会员歌曲",
                    provider_data={"resolver_status": "denied"},
                )
                searcher = MusicSearcher(
                    qq_vip_resolver_url="https://resolver.example/api"
                )
                searcher._fetch_qq_song_by_id = AsyncMock(return_value=direct)
                searcher._get_session = AsyncMock(return_value=FakeSequenceSession([
                    FakeResponse(data=response_data),
                ]))
                searcher._search_via_aggregator = AsyncMock()

                resolved = await searcher.fetch_song_by_id("qq", "paid-mid")

                self.assertIs(resolved, direct)
                self.assertEqual(resolved.audio_url, "")
                self.assertEqual(resolved.unplayable_reason, "会员歌曲")
                self.assertEqual(
                    resolved.provider_data["resolver_status"],
                    "denied",
                )
                searcher._search_via_aggregator.assert_not_awaited()

    async def test_qq_vip_resolver_exception_preserves_denied_reason(self):
        direct = Song(
            id="paid-mid",
            platform="qq",
            unplayable_reason="会员歌曲",
            provider_data={"resolver_status": "denied"},
        )
        searcher = MusicSearcher(
            qq_vip_resolver_url="https://resolver.example/api"
        )
        searcher._fetch_qq_song_by_id = AsyncMock(return_value=direct)
        searcher._get_session = AsyncMock(side_effect=RuntimeError("offline"))
        searcher._search_via_aggregator = AsyncMock()

        resolved = await searcher.fetch_song_by_id("qq", "paid-mid")

        self.assertIs(resolved, direct)
        self.assertEqual(resolved.audio_url, "")
        self.assertEqual(resolved.unplayable_reason, "会员歌曲")
        self.assertEqual(resolved.provider_data["resolver_status"], "denied")
        searcher._search_via_aggregator.assert_not_awaited()

    async def test_qq_vip_resolver_retry_clears_prior_denied_reason(self):
        searcher = MusicSearcher(
            qq_vip_resolver_url="https://resolver.example/api"
        )
        searcher.fetch_song_by_id = AsyncMock(return_value=Song(
            id="paid-mid",
            platform="qq",
            audio_url="https://aqqmusic.tc.qq.com/paid.m4a?vkey=x",
            provider_data={"resolver_status": "resolved"},
        ))
        song = Song(
            id="paid-mid",
            platform="qq",
            unplayable_reason="会员歌曲",
            provider_data={"resolver_status": "denied"},
        )

        resolved = await searcher.fetch_audio_url(song)

        self.assertTrue(resolved.audio_url)
        self.assertEqual(resolved.unplayable_reason, "")
        self.assertEqual(resolved.provider_data["resolver_status"], "resolved")

    async def test_qq_transient_resolver_failure_keeps_exact_id_fallback(self):
        direct = Song(
            id="same-mid",
            platform="qq",
            provider_data={"resolver_status": "transient"},
        )
        fallback = Song(
            id="same-mid",
            platform="qq",
            audio_url="https://aqqmusic.tc.qq.com/audio.m4a",
        )
        searcher = MusicSearcher(
            qq_vip_resolver_url="https://resolver.example/api"
        )
        searcher._fetch_qq_song_by_id = AsyncMock(return_value=direct)
        searcher._get_session = AsyncMock(return_value=FakeSequenceSession([
            FakeResponse(data={"code": 0, "data": {"wrong-mid": "https://x.test/a"}}),
        ]))
        searcher._search_via_aggregator = AsyncMock(return_value=[fallback])

        resolved = await searcher.fetch_song_by_id("qq", "same-mid")

        self.assertIs(resolved, fallback)
        searcher._search_via_aggregator.assert_awaited_once_with(
            "same-mid", "qq", 5, filter_type="id"
        )

    async def test_exact_qq_failure_never_falls_back_to_wrong_name(self):
        searcher = MusicSearcher()
        searcher.fetch_song_by_id = AsyncMock(return_value=Song(
            id="paid-mid",
            name="same-name",
            platform="qq",
            unplayable_reason="paid",
        ))
        searcher.search = AsyncMock()
        song = Song(id="paid-mid", name="same-name", platform="qq")
        resolved = await searcher.fetch_audio_url(song)
        self.assertEqual(resolved.audio_url, "")
        self.assertEqual(resolved.unplayable_reason, "paid")
        searcher.search.assert_not_awaited()

    async def test_kugou_paid_response_preserves_reason(self):
        response = FakeResponse(data={
            "status": 0,
            "error": "\u9700\u8981\u4ed8\u8d39",
            "songName": "paid-song",
            "author_name": "artist",
            "url": "",
            "backup_url": ["https://example.test/trial.mp3"],
        })
        searcher = MusicSearcher()
        searcher._get_session = AsyncMock(return_value=FakeSession(response))
        song = await searcher._fetch_kugou_song_by_id("B" * 32)
        self.assertIsNotNone(song)
        self.assertEqual(song.audio_url, "")
        self.assertEqual(song.unplayable_reason, "\u9700\u8981\u4ed8\u8d39")

    async def test_kugou_candidates_are_filtered_by_resolved_url(self):
        searcher = MusicSearcher()
        candidates = [
            Song(id="field-says-paid", name="playable", platform="kugou"),
            Song(id="field-says-free", name="blocked", platform="kugou"),
        ]

        async def resolve(song_id):
            if song_id == "field-says-paid":
                return Song(
                    id=song_id,
                    name="playable",
                    platform="kugou",
                    audio_url="https://sharefs.kugou.com/full.mp3",
                )
            return Song(
                id=song_id,
                name="blocked",
                platform="kugou",
                provider_data={"resolver_status": "denied"},
            )

        searcher._fetch_kugou_song_by_id = AsyncMock(side_effect=resolve)
        songs = await searcher._filter_kugou_playable_songs(candidates, 5)
        self.assertEqual([song.id for song in songs], ["field-says-paid"])

    async def test_transient_direct_failure_uses_exact_id_fallback(self):
        searcher = MusicSearcher()
        searcher._fetch_qq_song_by_id = AsyncMock(return_value=Song(
            id="same-mid",
            platform="qq",
            provider_data={"resolver_status": "transient"},
        ))
        fallback = Song(
            id="same-mid",
            name="same-song",
            platform="qq",
            audio_url="https://aqqmusic.tc.qq.com/audio.m4a",
        )
        searcher._search_via_aggregator = AsyncMock(return_value=[fallback])
        searcher._fetch_meting_song_by_id = AsyncMock()
        resolved = await searcher.fetch_song_by_id("qq", "same-mid")
        self.assertEqual(resolved.audio_url, fallback.audio_url)
        searcher._fetch_meting_song_by_id.assert_not_awaited()

    async def test_meting_mismatched_id_is_rejected(self):
        response = FakeResponse(data=[{
            "name": "wrong",
            "artist": "wrong",
            "url": (
                "https://api.qijieya.cn/meting/"
                "?server=tencent&type=url&id=wrong-mid"
            ),
        }])
        searcher = MusicSearcher()
        searcher._get_session = AsyncMock(return_value=FakeSession(response))
        song = await searcher._fetch_meting_song_by_id("qq", "wanted-mid")
        self.assertIsNone(song)

    async def test_downloader_uses_m4a_extension_for_qq_audio(self):
        body = b"\x00\x00\x00 ftypmp42" + b"audio-data" * 200
        response = FakeResponse(body=body, content_type="audio/mp4")
        with tempfile.TemporaryDirectory() as tmpdir:
            downloader = MusicDownloader(Path(tmpdir))
            downloader._get_session = AsyncMock(return_value=FakeSession(response))
            song = Song(
                id="qq-mid",
                name="song",
                platform="qq",
                audio_url="https://example.test/file",
            )
            result = await downloader.download(song)
            self.assertTrue(result.file_path.endswith(".m4a"))
            self.assertEqual(Path(result.file_path).read_bytes(), body)

    async def test_downloader_rejects_empty_html_parser_response(self):
        response = FakeResponse(body=b"", content_type="text/html; charset=UTF-8")
        with tempfile.TemporaryDirectory() as tmpdir:
            downloader = MusicDownloader(Path(tmpdir))
            downloader._get_session = AsyncMock(return_value=FakeSession(response))
            song = Song(
                id="paid",
                name="paid",
                platform="kugou",
                audio_url="https://example.test/parser",
            )
            result = await downloader.download(song)
            self.assertEqual(result.file_path, "")
            self.assertTrue(result.unplayable_reason)
            self.assertEqual(list(Path(tmpdir).iterdir()), [])

    async def test_downloader_rejects_disguised_octet_stream_html(self):
        body = b"<html>not audio</html>" * 100
        response = FakeResponse(body=body, content_type="application/octet-stream")
        with tempfile.TemporaryDirectory() as tmpdir:
            downloader = MusicDownloader(Path(tmpdir))
            downloader._get_session = AsyncMock(return_value=FakeSession(response))
            song = Song(
                id="bad",
                name="bad",
                audio_url="https://example.test/file",
            )
            result = await downloader.download(song)
            self.assertEqual(result.file_path, "")
            self.assertEqual(list(Path(tmpdir).iterdir()), [])

    async def test_downloader_blocks_literal_private_address(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            downloader = MusicDownloader(Path(tmpdir))
            downloader._get_session = AsyncMock()
            song = Song(
                id="ssrf",
                name="ssrf",
                audio_url="http://127.0.0.1/audio.mp3",
            )
            result = await downloader.download(song)
            self.assertEqual(result.file_path, "")
            downloader._get_session.assert_not_awaited()

    async def test_downloader_blocks_ambiguous_private_address_notation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            downloader = MusicDownloader(Path(tmpdir))
            downloader._get_session = AsyncMock()
            for audio_url in (
                "http://2130706433/audio.mp3",
                "http://127.1/audio.mp3",
                "http://0177.0.0.1/audio.mp3",
                "http://0x7f000001/audio.mp3",
            ):
                with self.subTest(audio_url=audio_url):
                    song = Song(
                        id="unsafe",
                        name="unsafe",
                        platform="qq",
                        audio_url=audio_url,
                    )
                    result = await downloader.download(song)
                    self.assertEqual(result.file_path, "")
            downloader._get_session.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
