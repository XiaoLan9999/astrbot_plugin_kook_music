import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, call, patch


PLUGINS_DIR = Path(__file__).resolve().parents[2]
if str(PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGINS_DIR))

from astrbot_plugin_kook_music.music import bilibili as bilibili_module
from astrbot_plugin_kook_music import card_builder
from astrbot_plugin_kook_music.music.bilibili import BilibiliExtractor
from astrbot_plugin_kook_music.music.model import Song


BVID_ONE = "BV1dujdzrEA4"
BVID_TWO = "BV1oL411V7B7"
BVID_THREE = "BV1UU4y1c74o"


def make_view(page_count=3):
    return {
        "bvid": BVID_ONE,
        "title": "Example video",
        "pic": "http://images.example.test/cover.jpg",
        "owner": {"name": "Example uploader"},
        "pages": [
            {
                "page": page,
                "part": f"Chapter {page}",
                "duration": page * 10,
            }
            for page in range(1, page_count + 1)
        ],
    }


def make_favorite_item(bvid, title, *, item_type=2, page_count=1):
    return {
        "type": item_type,
        "bvid": bvid,
        "title": title,
        "upper": {"name": f"Uploader {title}"},
        "page": page_count,
        "duration": 123,
        "cover": "http://images.example.test/favorite.jpg",
    }


class BilibiliInputParsingTests(unittest.TestCase):
    def test_video_input_accepts_bvid_and_official_urls(self):
        cases = {
            BVID_ONE: (BVID_ONE, 1, False),
            f"{BVID_ONE}?p=3": (BVID_ONE, 3, True),
            f"https://www.bilibili.com/video/{BVID_ONE}/": (
                BVID_ONE,
                1,
                False,
            ),
            f"https://www.bilibili.com/video/{BVID_ONE}?p=2&spm_id=x": (
                BVID_ONE,
                2,
                True,
            ),
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(BilibiliExtractor.parse_video_input(value), expected)

    def test_video_input_rejects_lookalike_hosts(self):
        value = f"https://www.bilibili.com.evil.test/video/{BVID_ONE}?p=2"
        self.assertEqual(
            BilibiliExtractor.parse_video_input(value),
            (None, 1, False),
        )

    def test_invalid_explicit_page_is_rejected_instead_of_expanding_all_parts(self):
        for suffix in ("?p=", "?p=abc", "?p=0", "?P=wrong"):
            with self.subTest(suffix=suffix):
                self.assertEqual(
                    BilibiliExtractor.parse_video_input(
                        f"https://www.bilibili.com/video/{BVID_ONE}{suffix}"
                    ),
                    (None, 1, True),
                )
        self.assertEqual(
            BilibiliExtractor.parse_video_input(f"{BVID_ONE}?P=2"),
            (BVID_ONE, 2, True),
        )

    def test_favorite_input_accepts_common_url_shapes(self):
        cases = (
            "https://space.bilibili.com/84912/favlist?fid=213003412",
            "https://www.bilibili.com/medialist/detail/ml213003412",
            "https://www.bilibili.com/medialist/play/ml213003412",
            "https://www.bilibili.com/list/ml213003412",
        )
        for value in cases:
            with self.subTest(value=value):
                self.assertEqual(
                    BilibiliExtractor.parse_favorite_input(value),
                    "213003412",
                )

        self.assertEqual(
            BilibiliExtractor.parse_favorite_input(
                "https://space.bilibili.com.evil.test/84912/favlist?fid=213003412"
            ),
            "",
        )


class BilibiliCollectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_official_short_link_is_expanded_with_redirect_allowlist(self):
        class FakeResponse:
            status = 302
            headers = {
                "Location": f"https://www.bilibili.com/video/{BVID_ONE}?p=2"
            }

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        class FakeSession:
            def get(self, *args, **kwargs):
                return FakeResponse()

        extractor = BilibiliExtractor()
        extractor._get_session = AsyncMock(return_value=FakeSession())

        resolved = await extractor.resolve_input("https://b23.tv/example")

        self.assertEqual(
            resolved,
            f"https://www.bilibili.com/video/{BVID_ONE}?p=2",
        )

    async def test_same_favorite_page_requests_share_one_in_flight_task(self):
        extractor = BilibiliExtractor()
        started = bilibili_module.asyncio.Event()
        release = bilibili_module.asyncio.Event()

        async def request_page(favorite_id, page_num):
            started.set()
            await release.wait()
            return {"medias": []}

        extractor._request_favorite_page = AsyncMock(side_effect=request_page)
        first = bilibili_module.asyncio.create_task(
            extractor._fetch_favorite_page("123", 2)
        )
        await started.wait()
        second = bilibili_module.asyncio.create_task(
            extractor._fetch_favorite_page("123", 2)
        )
        await bilibili_module.asyncio.sleep(0)
        release.set()

        self.assertEqual(await first, {"medias": []})
        self.assertEqual(await second, {"medias": []})
        extractor._request_favorite_page.assert_awaited_once_with("123", 2)

    async def test_download_rebuilds_exact_official_part_url(self):
        extractor = BilibiliExtractor()
        song = Song(
            id=f"{BVID_ONE}_p2",
            name="Part 2",
            audio_url="https://untrusted.example.test/audio",
            platform="bilibili",
            provider_data={"bvid": BVID_ONE, "page": 2},
        )
        captured_urls = []

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "result.m4a"

            async def fake_to_thread(func, url, output_base):
                captured_urls.append(url)
                output.write_bytes(b"audio")
                return str(output)

            with patch.object(
                bilibili_module.asyncio,
                "to_thread",
                AsyncMock(side_effect=fake_to_thread),
            ):
                result = await extractor.download_audio(song, Path(temp_dir))

            self.assertTrue(Path(result.file_path).exists())

        expected = f"https://www.bilibili.com/video/{BVID_ONE}?p=2"
        self.assertEqual(captured_urls, [expected])
        self.assertEqual(result.audio_url, expected)

    async def test_official_search_api_falls_back_before_yt_dlp_search(self):
        extractor = BilibiliExtractor()
        extractor._api_get_json = AsyncMock(return_value={
            "code": 0,
            "data": {
                "result": [
                    {"type": "video", "bvid": BVID_ONE},
                ]
            },
        })
        extractor._search_page_first_bvid = AsyncMock(return_value=None)
        extractor._fetch_video_view = AsyncMock(return_value=make_view(1))
        extractor._yt_search_first_url = Mock()

        song = await extractor.extract("Example keyword")

        self.assertIsNotNone(song)
        self.assertEqual(song.id, BVID_ONE)
        extractor._yt_search_first_url.assert_not_called()

    def test_server_rendered_search_page_returns_first_video(self):
        html = (
            '<a href="//www.bilibili.com/video/'
            f'{BVID_ONE}?from=search">first</a>'
            '<a href="//www.bilibili.com/video/'
            f'{BVID_TWO}">second</a>'
        )

        self.assertEqual(
            BilibiliExtractor._parse_search_page_bvid(html),
            BVID_ONE,
        )

    async def test_failed_video_view_cache_is_bounded(self):
        extractor = BilibiliExtractor()
        extractor._api_get_json = AsyncMock(return_value=None)

        for index in range(300):
            await extractor._fetch_video_view(f"BV{index:010d}")

        self.assertEqual(len(extractor._video_view_cache), 256)

    async def test_naked_multi_part_video_expands_in_source_order(self):
        extractor = BilibiliExtractor()
        extractor._fetch_video_view = AsyncMock(return_value=make_view(3))

        collection = await extractor.extract_collection(
            f"https://www.bilibili.com/video/{BVID_ONE}/"
        )

        self.assertIsNotNone(collection)
        self.assertEqual(collection.id, BVID_ONE)
        self.assertEqual(
            [song.id for song in collection.songs],
            [f"{BVID_ONE}_p1", f"{BVID_ONE}_p2", f"{BVID_ONE}_p3"],
        )
        self.assertEqual(
            [song.audio_url for song in collection.songs],
            [
                f"https://www.bilibili.com/video/{BVID_ONE}?p=1",
                f"https://www.bilibili.com/video/{BVID_ONE}?p=2",
                f"https://www.bilibili.com/video/{BVID_ONE}?p=3",
            ],
        )
        self.assertEqual(
            [song.provider_data["page"] for song in collection.songs],
            [1, 2, 3],
        )

    async def test_explicit_page_is_a_single_song_not_a_collection(self):
        extractor = BilibiliExtractor()
        extractor._fetch_video_view = AsyncMock(return_value=make_view(3))
        value = f"https://www.bilibili.com/video/{BVID_ONE}?p=2"

        collection = await extractor.extract_collection(value)
        song = await extractor.extract(value)

        self.assertIsNone(collection)
        self.assertIsNotNone(song)
        self.assertEqual(song.id, f"{BVID_ONE}_p2")
        self.assertEqual(song.name, "Example video - Chapter 2")
        self.assertEqual(song.duration, 20000)
        self.assertEqual(song.audio_url, value)
        self.assertEqual(song.provider_data, {"bvid": BVID_ONE, "page": 2})
        extractor._fetch_video_view.assert_awaited_once_with(BVID_ONE)

    async def test_favorite_initial_parse_only_builds_placeholders(self):
        extractor = BilibiliExtractor()
        first_page = {
            "info": {"title": "Large favorite", "media_count": 45},
            "medias": [make_favorite_item(BVID_ONE, "First")],
        }
        extractor._fetch_favorite_page = AsyncMock(return_value=first_page)

        collection = await extractor.extract_collection(
            "https://space.bilibili.com/84912/favlist?fid=213003412"
        )

        self.assertIsNotNone(collection)
        self.assertEqual(collection.id, "213003412")
        self.assertEqual(collection.title, "Large favorite")
        self.assertEqual(len(collection.songs), 45)
        self.assertEqual(
            [song.provider_data["favorite_index"] for song in collection.songs],
            list(range(1, 46)),
        )
        self.assertTrue(
            all(
                song.provider_data.get("needs_materialize")
                for song in collection.songs
            )
        )
        extractor._fetch_favorite_page.assert_awaited_once_with("213003412", 1)

    async def test_favorite_materializes_only_pages_covering_selection(self):
        extractor = BilibiliExtractor()
        first_page = {
            "info": {"title": "Large favorite", "media_count": 45},
            "medias": [],
        }
        extractor._fetch_favorite_page = AsyncMock(return_value=first_page)
        collection = await extractor._extract_favorite_collection("213003412")
        extractor._fetch_favorite_page.reset_mock()
        extractor._fetch_favorite_page.return_value = {
            "info": {"title": "Large favorite", "media_count": 45},
            "medias": [
                make_favorite_item(BVID_TWO, "Twenty one"),
                make_favorite_item(BVID_THREE, "Twenty two", page_count=4),
            ],
        }

        resolved = await extractor.materialize_collection_songs(
            collection.songs[20:22]
        )

        self.assertIsNotNone(resolved)
        self.assertEqual(
            [song.id for song in resolved],
            [f"{BVID_TWO}_p1", f"{BVID_THREE}_p1"],
        )
        self.assertEqual(
            [song.audio_url for song in resolved],
            [
                f"https://www.bilibili.com/video/{BVID_TWO}?p=1",
                f"https://www.bilibili.com/video/{BVID_THREE}?p=1",
            ],
        )
        self.assertEqual([song.duration for song in resolved], [123000, 0])
        self.assertEqual(
            extractor._fetch_favorite_page.await_args_list,
            [call("213003412", 2)],
        )

    async def test_invalid_favorite_entries_are_skipped(self):
        extractor = BilibiliExtractor()
        placeholders = [
            Song(
                id=f"placeholder-{index}",
                platform="bilibili",
                provider_data={
                    "favorite_id": "77",
                    "favorite_index": index,
                    "needs_materialize": True,
                },
            )
            for index in range(1, 4)
        ]
        extractor._fetch_favorite_page = AsyncMock(return_value={
            "medias": [
                make_favorite_item(BVID_ONE, "Valid"),
                make_favorite_item(BVID_TWO, "Wrong type", item_type=12),
                make_favorite_item("not-a-bvid", "Invalid id"),
            ]
        })

        resolved = await extractor.materialize_collection_songs(placeholders)

        self.assertIsNotNone(resolved)
        self.assertEqual([song.id for song in resolved], [f"{BVID_ONE}_p1"])
        extractor._fetch_favorite_page.assert_awaited_once_with("77", 1)

    async def test_private_favorite_api_failure_returns_none(self):
        extractor = BilibiliExtractor()
        extractor._api_get_json = AsyncMock(return_value={
            "code": -403,
            "message": "Access denied",
        })

        result = await extractor._fetch_favorite_page("private-id", 1)

        self.assertIsNone(result)
        extractor._api_get_json.assert_awaited_once()


class BilibiliYtDlpOptionTests(unittest.TestCase):
    def test_metadata_and_download_disable_playlist_expansion(self):
        created_options = []
        extract_calls = []

        class FakeYoutubeDL:
            def __init__(self, options):
                created_options.append(options)

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def extract_info(self, url, download):
                extract_calls.append((url, download))
                return {"id": BVID_ONE, "ext": "m4a"}

        extractor = BilibiliExtractor()
        with patch.object(bilibili_module.yt_dlp, "YoutubeDL", FakeYoutubeDL):
            metadata = extractor._yt_extract_info(
                f"https://www.bilibili.com/video/{BVID_ONE}?p=2"
            )
            output = extractor._yt_download_audio(
                f"https://www.bilibili.com/video/{BVID_ONE}?p=2",
                "output-base",
            )

        self.assertEqual(metadata["id"], BVID_ONE)
        self.assertEqual(output, "output-base.m4a")
        self.assertEqual(len(created_options), 2)
        self.assertTrue(all(options["noplaylist"] for options in created_options))
        self.assertEqual(
            extract_calls,
            [
                (f"https://www.bilibili.com/video/{BVID_ONE}?p=2", False),
                (f"https://www.bilibili.com/video/{BVID_ONE}?p=2", True),
            ],
        )

    def test_cookie_reader_uses_exact_domain_boundary_and_known_names(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cookies.txt"
            path.write_text(
                ".bilibili.com\tTRUE\t/\tFALSE\t0\tSESSDATA\tgood\n"
                "evilbilibili.com\tTRUE\t/\tFALSE\t0\tSESSDATA\tbad\n"
                ".bilibili.com\tTRUE\t/\tFALSE\t0\tUNRELATED\tignored\n",
                encoding="utf-8",
            )

            header = BilibiliExtractor._read_cookie_header(path)

        self.assertEqual(header, "SESSDATA=good")

    def test_bilibili_card_links_to_exact_part(self):
        song = Song(
            id=f"{BVID_ONE}_p3",
            name="Part 3",
            platform="bilibili",
            provider_data={"bvid": BVID_ONE, "page": 3},
        )

        card = card_builder.build_bilibili_playing_card(song)

        self.assertIn(
            f"https://www.bilibili.com/video/{BVID_ONE}?p=3",
            str(card),
        )
        self.assertNotIn(f"{BVID_ONE}_p3/", str(card))


if __name__ == "__main__":
    unittest.main()
