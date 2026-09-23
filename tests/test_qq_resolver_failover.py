import asyncio
import errno
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp

from test_music_platforms import FakeResponse, FakeSequenceSession
from test_qq_resolver_retry import (
    CDN_URL,
    MID,
    FailureResponse,
    SlowResponse,
    denied_song,
)
from astrbot_plugin_kook_music.music.searcher import MusicSearcher


PRIMARY = "https://primary.example/api"
BACKUP = "https://backup.example/api"


def result(mid=MID, url=CDN_URL):
    return FakeResponse(data={"code": 0, "data": {mid: url}})


class QQResolverFailoverTests(unittest.IsolatedAsyncioTestCase):
    def make_searcher(self, responses, *, backups=None, cooldown=60):
        searcher = MusicSearcher(
            qq_vip_resolver_url=PRIMARY,
            qq_vip_resolver_backups=[BACKUP] if backups is None else backups,
            qq_vip_resolver_cooldown_seconds=cooldown,
        )
        searcher.QQ_VIP_RESOLVER_RETRY_DELAY = 0
        session = FakeSequenceSession(responses)
        searcher._get_session = AsyncMock(return_value=session)
        return searcher, session

    async def test_522_switches_immediately_and_preserves_official_song(self):
        searcher, session = self.make_searcher([FakeResponse(status=522), result()])
        song = denied_song()
        with self.assertLogs("astrbot", level="INFO") as captured:
            self.assertTrue(await searcher._fill_qq_vip_resolver_url(song))
        self.assertEqual([call[0][0] for call in session.get_calls], [PRIMARY, BACKUP])
        self.assertEqual(song.audio_url, CDN_URL)
        self.assertEqual(song.name, "Official title (DJ edition)")
        self.assertEqual(song.artists, "Official artist")
        self.assertEqual(song.duration, 223000)
        self.assertEqual(song.unplayable_reason, "")
        self.assertEqual(song.provider_data["qq_vip_resolver_source_host"], "backup.example")
        self.assertEqual(song.provider_data["qq_vip_resolver_source_index"], 2)
        self.assertIn("HTTP 522", "\n".join(captured.output))
        self.assertTrue(all(call[1]["params"]["id"] == MID for call in session.get_calls))

    async def test_backup_order_is_preserved_and_stops_after_success(self):
        third = "https://third.example/api"
        fourth = "https://fourth.example/api"
        searcher, session = self.make_searcher(
            [FakeResponse(status=522), FakeResponse(status=503), result()],
            backups=[BACKUP, third, fourth],
        )
        self.assertTrue(await searcher._fill_qq_vip_resolver_url(denied_song()))
        self.assertEqual([call[0][0] for call in session.get_calls], [PRIMARY, BACKUP, third])

    async def test_shared_timeout_reserves_time_for_every_source(self):
        responses = [SlowResponse(1) for _ in range(4)]
        searcher, session = self.make_searcher(
            responses, backups=[BACKUP, "https://third.example/api", "https://fourth.example/api"],
        )
        searcher.QQ_VIP_RESOLVER_TIMEOUT = 0.12
        song = denied_song()
        start = asyncio.get_running_loop().time()
        self.assertFalse(await searcher._fill_qq_vip_resolver_url(song))
        self.assertLess(asyncio.get_running_loop().time() - start, 0.3)
        self.assertEqual(len(session.get_calls), 4)
        self.assertTrue(all(response.cancelled for response in responses))
        self.assertTrue(all(call[1]["timeout"].total < 0.06 for call in session.get_calls))
        self.assertIn("暂时超时", song.unplayable_reason)

    async def test_primary_timeout_cannot_consume_backup_budget(self):
        slow = SlowResponse(1)
        searcher, session = self.make_searcher([slow, result()])
        searcher.QQ_VIP_RESOLVER_TIMEOUT = 0.08
        self.assertTrue(await searcher._fill_qq_vip_resolver_url(denied_song()))
        self.assertTrue(slow.cancelled)
        self.assertEqual(len(session.get_calls), 2)

    async def test_two_temporary_failures_cool_primary_but_keep_backup(self):
        searcher, session = self.make_searcher([
            FakeResponse(status=522), result(),
            FakeResponse(status=522), result(), result(),
        ])
        for _ in range(3):
            self.assertTrue(await searcher._fill_qq_vip_resolver_url(denied_song()))
        self.assertEqual(
            [call[0][0] for call in session.get_calls],
            [PRIMARY, BACKUP, PRIMARY, BACKUP, BACKUP],
        )
        self.assertGreater(searcher._qq_resolver_cooldowns[PRIMARY], asyncio.get_running_loop().time())

    async def test_all_cooling_sources_fail_fast_without_network(self):
        searcher, session = self.make_searcher([FakeResponse(status=522) for _ in range(4)])
        for _ in range(2):
            self.assertFalse(await searcher._fill_qq_vip_resolver_url(denied_song()))
        searcher._get_session.reset_mock()
        song = denied_song()
        start = asyncio.get_running_loop().time()
        self.assertFalse(await searcher._fill_qq_vip_resolver_url(song))
        self.assertLess(asyncio.get_running_loop().time() - start, 0.1)
        self.assertIn("冷却", song.unplayable_reason)
        self.assertEqual(song.provider_data["resolver_status"], "transient")
        self.assertEqual(len(session.get_calls), 4)
        searcher._get_session.assert_not_awaited()

    async def test_expired_source_recovery_clears_failures_and_restores_priority(self):
        searcher, session = self.make_searcher([result(), result()])
        searcher._qq_resolver_failures[PRIMARY] = 2
        searcher._qq_resolver_cooldowns[PRIMARY] = asyncio.get_running_loop().time() - 1
        for _ in range(2):
            self.assertTrue(await searcher._fill_qq_vip_resolver_url(denied_song()))
        self.assertNotIn(PRIMARY, searcher._qq_resolver_failures)
        self.assertNotIn(PRIMARY, searcher._qq_resolver_cooldowns)
        self.assertEqual([call[0][0] for call in session.get_calls], [PRIMARY, PRIMARY])

    async def test_cooling_source_plus_unmatched_backup_is_not_membership_denial(self):
        searcher, session = self.make_searcher([result(mid="other-mid")])
        searcher._qq_resolver_cooldowns[PRIMARY] = asyncio.get_running_loop().time() + 60
        song = denied_song()
        self.assertFalse(await searcher._fill_qq_vip_resolver_url(song))
        self.assertEqual(song.provider_data["resolver_status"], "transient")
        self.assertIn("暂时不可用", song.unplayable_reason)
        self.assertEqual([call[0][0] for call in session.get_calls], [BACKUP])

    async def test_wrong_mid_never_plays_or_cools_source(self):
        searcher, session = self.make_searcher([
            result(mid="other-mid"), result(), result(mid="other-mid"), result(),
        ])
        for _ in range(2):
            song = denied_song()
            self.assertTrue(await searcher._fill_qq_vip_resolver_url(song))
            self.assertEqual(song.provider_data["qq_vip_resolver_source_index"], 2)
        self.assertNotIn(PRIMARY, searcher._qq_resolver_failures)
        self.assertNotIn(PRIMARY, searcher._qq_resolver_cooldowns)
        self.assertEqual(len(session.get_calls), 4)

    async def test_song_specific_empty_or_restricted_result_does_not_cool_site(self):
        for data in ([], {"code": 0, "data": {MID: ""}}, {"code": 403}, {"code": 404}):
            with self.subTest(data=data):
                searcher, session = self.make_searcher([
                    FakeResponse(data=data), result(), FakeResponse(data=data), result(),
                ])
                for _ in range(2):
                    self.assertTrue(await searcher._fill_qq_vip_resolver_url(denied_song()))
                self.assertNotIn(PRIMARY, searcher._qq_resolver_failures)
                self.assertNotIn(PRIMARY, searcher._qq_resolver_cooldowns)
                self.assertEqual(len(session.get_calls), 4)

    async def test_all_wrong_mids_preserve_original_denial_without_name_search(self):
        searcher, session = self.make_searcher([
            result(mid="other-mid"), FakeResponse(data=[{"songmid": "other-mid", "url": CDN_URL}]),
        ])
        song = denied_song()
        self.assertFalse(await searcher._fill_qq_vip_resolver_url(song))
        self.assertEqual(song.audio_url, "")
        self.assertEqual(song.unplayable_reason, "old membership restriction")
        self.assertEqual(song.provider_data["resolver_status"], "denied")
        self.assertEqual(len(session.get_calls), 2)

    async def test_unsafe_audio_urls_are_rejected_on_every_source(self):
        for url in (
            "http://127.0.0.1/private.mp3",
            "https://evil.example/song.mp3",
            "https://aqqmusic.tc.qq.com.evil.example/song.mp3",
            "https://user:secret@aqqmusic.tc.qq.com/song.mp3",
            "file:///tmp/song.mp3",
        ):
            with self.subTest(url=url):
                searcher, session = self.make_searcher([result(url=url), result(url=url)])
                song = denied_song()
                self.assertFalse(await searcher._fill_qq_vip_resolver_url(song))
                self.assertEqual(song.audio_url, "")
                self.assertEqual(len(session.get_calls), 2)

    async def test_empty_primary_disables_backups_and_preserves_song_state(self):
        searcher = MusicSearcher(qq_vip_resolver_backups=[BACKUP])
        searcher._get_session = AsyncMock()
        song = denied_song()
        self.assertFalse(await searcher._fill_qq_vip_resolver_url(song))
        self.assertEqual(song.provider_data["resolver_status"], "denied")
        self.assertEqual(searcher._qq_resolver_urls, ())
        searcher._get_session.assert_not_awaited()

    async def test_deduplicate_and_bound_sources_without_losing_priority(self):
        searcher, session = self.make_searcher(
            [FakeResponse(status=522) for _ in range(4)],
            backups=[PRIMARY, " " + PRIMARY + "/ ", BACKUP, BACKUP, "", None,
                     "https://third.example/api", "https://fourth.example/api", "https://fifth.example/api"],
        )
        self.assertEqual(searcher.qq_vip_resolver_backups, [BACKUP, "https://third.example/api", "https://fourth.example/api"])
        self.assertFalse(await searcher._fill_qq_vip_resolver_url(denied_song()))
        self.assertEqual(len(session.get_calls), 4)
        self.assertEqual(len({call[0][0] for call in session.get_calls}), 4)

    async def test_rate_limit_moves_to_backup_without_sleeping_retry_after(self):
        searcher, session = self.make_searcher([
            FakeResponse(status=429, headers={"Retry-After": "120"}), result(),
        ])
        start = asyncio.get_running_loop().time()
        self.assertTrue(await searcher._fill_qq_vip_resolver_url(denied_song()))
        self.assertLess(asyncio.get_running_loop().time() - start, 0.1)
        self.assertEqual(len(session.get_calls), 2)

    async def test_logs_and_provenance_never_contain_credentials_or_queries(self):
        searcher = MusicSearcher(
            qq_vip_resolver_url=PRIMARY + "?key=primary-secret",
            qq_vip_resolver_backups=["https://user:password@backup.example/api?key=backup-secret"],
        )
        searcher._get_session = AsyncMock(return_value=FakeSequenceSession([
            FailureResponse(aiohttp.ClientConnectionError("token=exception-secret")), result(),
        ]))
        song = denied_song()
        with self.assertLogs("astrbot", level="INFO") as captured:
            self.assertTrue(await searcher._fill_qq_vip_resolver_url(song))
        output = "\n".join(captured.output) + str(song.provider_data)
        for secret in ("primary-secret", "backup-secret", "password", "exception-secret", "?key=", "user:"):
            self.assertNotIn(secret, output)
        self.assertIn("backup.example", output)

    async def test_failed_backup_clears_only_its_own_dns_cache(self):
        error = aiohttp.ClientConnectorError(
            SimpleNamespace(host="backup.example", port=443, ssl=True),
            OSError(errno.ENETUNREACH, "Network is unreachable"),
        )
        searcher, session = self.make_searcher([FakeResponse(status=522), FailureResponse(error)])
        session.connector = SimpleNamespace(clear_dns_cache=Mock())
        self.assertFalse(await searcher._fill_qq_vip_resolver_url(denied_song()))
        session.connector.clear_dns_cache.assert_called_once_with("backup.example", 443)

    async def test_cancellation_propagates_before_failover(self):
        searcher, session = self.make_searcher([FailureResponse(asyncio.CancelledError()), result()])
        song = denied_song()
        with self.assertRaises(asyncio.CancelledError):
            await searcher._fill_qq_vip_resolver_url(song)
        self.assertEqual(len(session.get_calls), 1)
        self.assertEqual(song.provider_data["resolver_status"], "denied")
        self.assertEqual(searcher._qq_resolver_failures, {})

    async def test_zero_cooldown_keeps_sources_available(self):
        searcher, session = self.make_searcher([FakeResponse(status=522) for _ in range(6)], cooldown=0)
        for _ in range(3):
            self.assertFalse(await searcher._fill_qq_vip_resolver_url(denied_song()))
        self.assertEqual(len(session.get_calls), 6)

    async def test_concurrent_songs_never_mutate_primary_or_cross_source_stages(self):
        searcher, _ = self.make_searcher([])
        calls = []

        class InterleavedResponse(FakeResponse):
            async def __aenter__(inner):
                await asyncio.sleep(0)
                self.assertEqual(searcher.qq_vip_resolver_url, PRIMARY)
                return inner

        class RoutedSession:
            def get(inner, url, **kwargs):
                params = kwargs["params"]
                mid = params["id"]
                calls.append((url, mid, params["type"]))
                if url == PRIMARY and mid == "song-a":
                    return InterleavedResponse(status=522)
                if params["type"] == "song":
                    return InterleavedResponse(data=[{"songmid": mid}])
                return InterleavedResponse(status=302, headers={"Location": CDN_URL + "&mid=" + mid})

        searcher._get_session = AsyncMock(return_value=RoutedSession())
        first, second = denied_song(), denied_song()
        first.id, second.id = "song-a", "song-b"
        results = await asyncio.gather(
            searcher._fill_qq_vip_resolver_url(first),
            searcher._fill_qq_vip_resolver_url(second),
        )
        self.assertEqual(results, [True, True])
        self.assertEqual(first.audio_url, CDN_URL + "&mid=song-a")
        self.assertEqual(second.audio_url, CDN_URL + "&mid=song-b")
        self.assertIn((BACKUP, "song-a", "url"), calls)
        self.assertIn((PRIMARY, "song-b", "url"), calls)
        self.assertNotIn((PRIMARY, "song-a", "url"), calls)
        self.assertNotIn((BACKUP, "song-b", "url"), calls)
        self.assertEqual(searcher.qq_vip_resolver_url, PRIMARY)


if __name__ == "__main__":
    unittest.main()
