import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


PLUGINS_DIR = Path(__file__).resolve().parents[2]
if str(PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGINS_DIR))

from astrbot_plugin_kook_music.music.playlist_import import PlaylistImporter


class FakeResponse:
    def __init__(self, data=None, status=200, headers=None, url="https://example.test/"):
        self.data = data
        self.status = status
        self.headers = headers or {}
        self.url = url

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self, content_type=None):
        return self.data


class FakeSession:
    def __init__(self, *, get_responses=(), post_responses=()):
        self.get_responses = list(get_responses)
        self.post_responses = list(post_responses)
        self.get_calls = []
        self.post_calls = []

    def get(self, *args, **kwargs):
        self.get_calls.append((args, kwargs))
        if not self.get_responses:
            raise AssertionError("unexpected GET request")
        return self.get_responses.pop(0)

    def post(self, *args, **kwargs):
        self.post_calls.append((args, kwargs))
        if not self.post_responses:
            raise AssertionError("unexpected POST request")
        return self.post_responses.pop(0)


def qq_track(index: int) -> dict:
    return {
        "mid": f"MID{index}",
        "name": f"QQ Song {index}",
        "interval": 180 + index,
        "singer": [{"name": f"QQ Artist {index}"}],
        "album": {"mid": f"ALBUM{index}"},
        "file": {"media_mid": f"MEDIA{index}"},
        "pay": {"pay_play": 1 if index == 2 else 0},
    }


def qq_page(song_indexes, *, has_more, filtered=(), invalid=(), total=0) -> dict:
    return {
        "code": 0,
        "req_0": {
            "code": 0,
            "data": {
                "code": 0,
                "dirinfo": {
                    "id": "123456",
                    "mtime": "1700000000",
                    "songnum": total,
                },
                "songlist": [qq_track(index) for index in song_indexes],
                "filtered_song": list(filtered),
                "invalid_song": list(invalid),
                "hasmore": 1 if has_more else 0,
            },
        },
    }


def kugou_track(index: int, **overrides) -> dict:
    item = {
        "hash": f"{index:032X}",
        "filename": f"KG Artist {index} - KG Song {index}",
        "duration": 200 + index,
        "album_id": 1000 + index,
        "album_audio_id": 2000 + index,
        "trans_param": {
            "union_cover": f"http://img.kugou.test/{{size}}/{index}.jpg"
        },
    }
    item.update(overrides)
    return item


class PlaylistInputParsingTests(unittest.TestCase):
    def test_playlist_links_auto_detect_platform_and_id(self):
        cases = {
            "https://y.qq.com/n/ryqq/playlist/123456": ("qq", "123456"),
            "https://y.qq.com/n/ryqq_v2/playlist/234567": ("qq", "234567"),
            "https://y.qq.com/n/yqq/playlist/345678.html": ("qq", "345678"),
            "https://i.y.qq.com/n2/m/share/details/taoge.html?id=456789": (
                "qq",
                "456789",
            ),
            "https://www.kugou.com/songlist/gcid_3ZRVJTOPZ7JZ080/": (
                "kugou",
                "gcid_3zrvjtopz7jz080",
            ),
            "https://www.kugou.com/yy/special/single/2113899.html": (
                "kugou",
                "2113899",
            ),
            "https://www.kugou.com/playlist/collection_3_1465461015_264_0": (
                "kugou",
                "collection_3_1465461015_264_0",
            ),
        }

        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(
                    PlaylistImporter.parse_playlist_input(value),
                    expected,
                )

    def test_explicit_platform_can_precede_or_follow_id(self):
        cases = {
            "qq 123456": ("qq", "123456"),
            "123456 QQ音乐": ("qq", "123456"),
            "腾讯音乐：123456": ("qq", "123456"),
            "kugou 2113899": ("kugou", "2113899"),
            "gcid_3ZRVJTOPZ7JZ080 酷狗": (
                "kugou",
                "gcid_3zrvjtopz7jz080",
            ),
            "酷狗音乐:collection_3_1465461015_264_0": (
                "kugou",
                "collection_3_1465461015_264_0",
            ),
            "123456": ("netease", "123456"),
        }

        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(
                    PlaylistImporter.parse_playlist_input(value),
                    expected,
                )

    def test_lookalike_domains_are_not_treated_as_music_platforms(self):
        self.assertEqual(
            PlaylistImporter.parse_playlist_input(
                "https://y.qq.com.evil.test/n/ryqq/playlist/123456"
            ),
            ("netease", ""),
        )
        self.assertEqual(
            PlaylistImporter.parse_playlist_input(
                "https://kugou.com.evil.test/songlist/gcid_123/"
            ),
            ("netease", ""),
        )


class QQPlaylistImportTests(unittest.IsolatedAsyncioTestCase):
    async def test_official_qq_short_link_redirect_resolves_playlist_id(self):
        short_url = "https://c6.y.qq.com/base/fcgi-bin/u?__=token"
        session = FakeSession(get_responses=[FakeResponse(
            status=302,
            url=short_url,
            headers={
                "Location": (
                    "https://i.y.qq.com/n2/m/share/details/taoge.html?id=1009789507"
                )
            },
        )])
        importer = PlaylistImporter()
        importer._get_session = AsyncMock(return_value=session)

        parsed = await importer.resolve_playlist_input(short_url)

        self.assertEqual(parsed, ("qq", "1009789507"))
        self.assertFalse(session.get_calls[0][1]["allow_redirects"])

    async def test_qq_short_link_rejects_cross_domain_redirect(self):
        short_url = "https://c6.y.qq.com/base/fcgi-bin/u?__=token"
        session = FakeSession(get_responses=[FakeResponse(
            status=302,
            url=short_url,
            headers={"Location": "http://127.0.0.1/private"},
        )])
        importer = PlaylistImporter()
        importer._get_session = AsyncMock(return_value=session)

        parsed = await importer.resolve_playlist_input(short_url)

        self.assertEqual(parsed, ("qq", ""))

    async def test_complete_legacy_response_preserves_original_order(self):
        tracks = [qq_track(1), qq_track(2), qq_track(3)]
        tracks[0]["interval"] = 1200
        session = FakeSession(get_responses=[FakeResponse({
            "code": 0,
            "cdlist": [{"total_song_num": 3, "songlist": tracks}],
        })])
        importer = PlaylistImporter()

        songs = await importer._fetch_qq_legacy_playlist(
            session,
            "123456",
            requester_id="user",
            requester_name="tester",
        )

        self.assertEqual([song.id for song in songs], ["MID1", "MID2", "MID3"])
        self.assertEqual(songs[0].duration, 1200000)
        self.assertEqual(songs[1].audio_url, "")
        self.assertEqual(songs[1].requester_id, "user")

    async def test_incomplete_legacy_response_is_rejected(self):
        session = FakeSession(get_responses=[FakeResponse({
            "code": 0,
            "cdlist": [{"total_song_num": 3, "songlist": [qq_track(1)]}],
        })])
        importer = PlaylistImporter()

        songs = await importer._fetch_qq_legacy_playlist(
            session, "123456", requester_id="", requester_name=""
        )

        self.assertIsNone(songs)

    async def test_incomplete_musicu_fallback_is_rejected(self):
        session = FakeSession(post_responses=[
            FakeResponse(qq_page([1], has_more=False, total=3))
        ])
        importer = PlaylistImporter()

        songs = await importer._fetch_qq_playlist_once(
            session, "123456", requester_id="", requester_name=""
        )

        self.assertIsNone(songs)

    async def test_three_pages_use_fixed_offsets_when_a_page_has_filtered_tracks(self):
        session = FakeSession(
            post_responses=[
                FakeResponse(qq_page([0, 1], has_more=True, total=5)),
                FakeResponse(
                    qq_page(
                        [2],
                        has_more=True,
                        filtered=[{"mid": "FILTERED"}],
                        total=5,
                    )
                ),
                FakeResponse(qq_page([3], has_more=False, total=5)),
            ]
        )
        importer = PlaylistImporter()

        songs = await importer._fetch_qq_playlist_once(
            session,
            "123456",
            requester_id="user",
            requester_name="tester",
        )

        page_size = PlaylistImporter.QQ_PLAYLIST_PAGE_SIZE
        offsets = [
            call[1]["json"]["req_0"]["param"]["song_begin"]
            for call in session.post_calls
        ]
        self.assertEqual(offsets, [0, page_size, page_size * 2])
        self.assertEqual(
            [call[1]["json"]["req_0"]["param"]["song_num"] for call in session.post_calls],
            [page_size, page_size, page_size],
        )
        self.assertEqual(
            [song.id for song in songs],
            ["MID0", "MID1", "MID2", "FILTERED", "MID3"],
        )
        self.assertTrue(all(song.platform == "qq" for song in songs))
        self.assertTrue(all(song.requester_id == "user" for song in songs))
        self.assertTrue(all(song.requester_name == "tester" for song in songs))
        self.assertEqual(songs[2].duration, 182000)
        self.assertEqual(songs[2].provider_data["media_mid"], "MEDIA2")
        self.assertEqual(songs[2].audio_url, "")
        self.assertEqual(songs[3].provider_data["resolver_status"], "denied")


class KugouPlaylistImportTests(unittest.IsolatedAsyncioTestCase):
    def test_missing_hash_keeps_unplayable_playlist_position(self):
        songs = PlaylistImporter._parse_kugou_playlist_songs(
            [{"filename": "Artist - Removed Song", "duration": 120}],
            requester_id="user",
            requester_name="tester",
        )

        self.assertEqual(len(songs), 1)
        self.assertEqual(songs[0].id, "unavailable-1")
        self.assertEqual(songs[0].name, "Removed Song")
        self.assertEqual(songs[0].provider_data["resolver_status"], "denied")

    def test_decode_signature_matches_fixed_vector(self):
        params = {
            "dfid": "-",
            "appid": 1005,
            "mid": "0",
            "clientver": 20109,
            "clienttime": 640612895,
            "uuid": "-",
        }
        body = (
            '{"ret_info":1,"data":['
            '{"id":"gcid_3zshg5myz1tjz03a","id_type":2}]}'
        )

        self.assertEqual(
            PlaylistImporter._kugou_signature(params, body),
            "81060bd1616b8cff61f4bc7c1eccbaef",
        )

    def test_web_signatures_match_fixed_official_request_vectors(self):
        info_params = {
            "appid": 1058,
            "specialid": 6319673,
            "format": "jsonp",
            "srcappid": 2919,
            "clientver": 20000,
            "clienttime": "1586163242519",
            "mid": "1586163242519",
            "uuid": "1586163242519",
            "dfid": "-",
        }
        song_params = {
            "appid": 1058,
            "global_specialid": "collection_3_1496816054_2259_0",
            "specialid": 0,
            "plat": 0,
            "version": 8000,
            "page": 1,
            "pagesize": 30,
            "srcappid": 2919,
            "clientver": 20000,
            "clienttime": "1586163263991",
            "mid": "1586163263991",
            "uuid": "1586163263991",
            "dfid": "-",
        }

        self.assertEqual(
            PlaylistImporter._kugou_web_signature(info_params),
            "4540b9e4b113c3904444ae145c4c8eed",
        )
        self.assertEqual(
            PlaylistImporter._kugou_web_signature(song_params),
            "9d6842a5aa2fdcd95c4ef5949f3cbf3b",
        )

    async def test_gcid_decode_posts_compact_signed_body(self):
        collection_id = "collection_3_1465461015_264_0"
        session = FakeSession(
            post_responses=[
                FakeResponse(
                    {
                        "status": 1,
                        "data": {
                            "list": [
                                {"global_collection_id": collection_id.upper()}
                            ]
                        },
                    }
                )
            ]
        )
        importer = PlaylistImporter()

        with patch(
            "astrbot_plugin_kook_music.music.playlist_import.time.time",
            return_value=1721376000,
        ):
            decoded = await importer._decode_kugou_gcid(
                session, "GCID_3ZRVJTOPZ7JZ080"
            )

        self.assertEqual(decoded, collection_id)
        self.assertEqual(len(session.post_calls), 1)
        args, kwargs = session.post_calls[0]
        self.assertEqual(args[0], PlaylistImporter.KUGOU_GCID_DECODE_API)
        expected_body = (
            b'{"ret_info":1,"data":['
            b'{"id":"gcid_3zrvjtopz7jz080","id_type":2}]}'
        )
        self.assertEqual(kwargs["data"], expected_body)
        self.assertEqual(json.loads(kwargs["data"]), {
            "ret_info": 1,
            "data": [{"id": "gcid_3zrvjtopz7jz080", "id_type": 2}],
        })
        unsigned_params = {
            key: value
            for key, value in kwargs["params"].items()
            if key != "signature"
        }
        self.assertEqual(
            kwargs["params"]["signature"],
            PlaylistImporter._kugou_signature(
                unsigned_params, expected_body.decode("utf-8")
            ),
        )

    async def test_numeric_playlist_paginates_and_maps_songs(self):
        collection_id = "collection_3_1565727119_15_0"
        first = kugou_track(1)
        second = kugou_track(
            2,
            filename="",
            songname="Explicit Song",
            singername="Explicit Artist",
            timelen=345678,
            duration=1,
        )
        third = kugou_track(3)
        fourth = kugou_track(4)
        session = FakeSession(
            get_responses=[
                FakeResponse(
                    {
                        "status": 1,
                        "data": {"global_specialid": collection_id},
                    }
                ),
                FakeResponse(
                    {
                        "error_code": 0,
                        "data": {
                            "count": 4,
                            "songs": [first, second],
                            "list_info": {"update_time": 1},
                        },
                    }
                ),
                FakeResponse(
                    {
                        "error_code": 0,
                        "data": {
                            "count": 4,
                            "songs": [third, fourth],
                            "list_info": {"update_time": 1},
                        },
                    }
                ),
            ]
        )
        importer = PlaylistImporter()
        importer._get_session = AsyncMock(return_value=session)

        with patch(
            "astrbot_plugin_kook_music.music.playlist_import.time.time",
            return_value=1721376000,
        ):
            songs = await importer.import_kugou_playlist(
                "2113899", requester_id="user", requester_name="tester"
            )

        self.assertEqual(
            session.get_calls[0][0][0],
            PlaylistImporter.KUGOU_PLAYLIST_INFO_V2_API,
        )
        self.assertEqual(session.get_calls[0][1]["params"]["specialid"], "2113899")
        song_calls = session.get_calls[1:]
        self.assertEqual(
            [call[1]["params"]["begin_idx"] for call in song_calls],
            [0, PlaylistImporter.KUGOU_COLLECTION_PAGE_SIZE],
        )
        self.assertEqual(
            [call[1]["params"]["pagesize"] for call in song_calls],
            [
                PlaylistImporter.KUGOU_COLLECTION_PAGE_SIZE,
                PlaylistImporter.KUGOU_COLLECTION_PAGE_SIZE,
            ],
        )
        self.assertEqual(
            [call[1]["params"]["global_collection_id"] for call in song_calls],
            [collection_id, collection_id],
        )
        self.assertEqual(len(songs), 4)
        self.assertEqual(songs[0].id, f"{1:032X}")
        self.assertEqual(songs[0].name, "KG Song 1")
        self.assertEqual(songs[0].artists, "KG Artist 1")
        self.assertEqual(songs[0].duration, 201000)
        self.assertEqual(
            songs[0].cover_url,
            "https://img.kugou.test/400/1.jpg",
        )
        self.assertEqual(songs[0].provider_data, {
            "album_id": "1001",
            "audio_id": "2001",
        })
        self.assertEqual(songs[1].name, "Explicit Song")
        self.assertEqual(songs[1].artists, "Explicit Artist")
        self.assertEqual(songs[1].duration, 345678)
        self.assertTrue(all(song.platform == "kugou" for song in songs))
        self.assertTrue(all(song.requester_id == "user" for song in songs))
        self.assertTrue(all(song.requester_name == "tester" for song in songs))
        self.assertTrue(all(song.audio_url == "" for song in songs))

    async def test_collection_playlist_paginates_and_maps_file_fields(self):
        collection_id = "collection_3_1465461015_264_0"
        first = kugou_track(10)
        second = kugou_track(11)
        third = kugou_track(
            12,
            hash="ABCDEF0123456789ABCDEF0123456789",
            filename="Collection Artist - Collection Song",
            album_audio_id="",
            audio_id=777,
        )
        session = FakeSession(
            get_responses=[
                FakeResponse(
                    {
                        "error_code": 0,
                        "data": {
                            "count": 3,
                            "songs": [first, second],
                            "list_info": {"update_time": 1},
                        },
                    }
                ),
                FakeResponse(
                    {
                        "error_code": 0,
                        "data": {
                            "count": 3,
                            "songs": [third],
                            "list_info": {"update_time": 1},
                        },
                    }
                ),
            ]
        )
        importer = PlaylistImporter()
        importer._get_session = AsyncMock(return_value=session)

        with patch(
            "astrbot_plugin_kook_music.music.playlist_import.time.time",
            return_value=1721376000,
        ):
            songs = await importer.import_kugou_playlist(collection_id)

        self.assertEqual(
            [call[1]["params"]["begin_idx"] for call in session.get_calls],
            [0, PlaylistImporter.KUGOU_COLLECTION_PAGE_SIZE],
        )
        self.assertEqual(
            [call[1]["params"]["global_collection_id"] for call in session.get_calls],
            [collection_id, collection_id],
        )
        for _, kwargs in session.get_calls:
            unsigned_params = {
                key: value
                for key, value in kwargs["params"].items()
                if key != "signature"
            }
            self.assertEqual(
                kwargs["params"]["signature"],
                PlaylistImporter._kugou_signature(unsigned_params),
            )
        self.assertEqual(len(songs), 3)
        self.assertEqual(songs[2].id, "ABCDEF0123456789ABCDEF0123456789")
        self.assertEqual(songs[2].name, "Collection Song")
        self.assertEqual(songs[2].artists, "Collection Artist")
        self.assertEqual(songs[2].provider_data["audio_id"], "777")


if __name__ == "__main__":
    unittest.main()
