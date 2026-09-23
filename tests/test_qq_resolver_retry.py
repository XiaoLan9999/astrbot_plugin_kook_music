import asyncio
import errno
import ssl
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp

from test_music_platforms import FakeResponse, FakeSequenceSession
from astrbot_plugin_kook_music.music.model import Song
from astrbot_plugin_kook_music.music.searcher import MusicSearcher


MID = "test-mid"
CDN_URL = "https://aqqmusic.tc.qq.com/C400test-media.m4a?vkey=test"


class FailureResponse(FakeResponse):
    def __init__(self, error):
        super().__init__()
        self.error = error

    async def __aenter__(self):
        raise self.error


class InvalidJsonResponse(FakeResponse):
    async def json(self, content_type=None):
        raise ValueError("invalid json")


class SlowResponse(FakeResponse):
    def __init__(self, delay):
        super().__init__()
        self.delay = delay
        self.cancelled = False

    async def __aenter__(self):
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return self


def metadata():
    return FakeResponse(data=[{
        "songmid": MID,
        "title": "must not replace the official title",
        "url": f"https://resolver.example/api?server=tencent&type=url&id={MID}",
    }])


def audio():
    return FakeResponse(status=302, headers={"Location": CDN_URL})


def denied_song():
    return Song(
        id=MID,
        name="Official title (DJ edition)",
        artists="Official artist",
        duration=223000,
        platform="qq",
        unplayable_reason="old membership restriction",
        provider_data={"resolver_status": "denied"},
    )


class QQResolverRetryTests(unittest.IsolatedAsyncioTestCase):
    def make_searcher(self, responses):
        searcher = MusicSearcher(qq_vip_resolver_url="https://resolver.example/api")
        searcher.QQ_VIP_RESOLVER_RETRY_DELAY = 0
        session = FakeSequenceSession(responses)
        searcher._get_session = AsyncMock(return_value=session)
        return searcher, session

    async def test_network_failure_retries_once_and_keeps_same_id_and_metadata(self):
        searcher, session = self.make_searcher([
            FailureResponse(aiohttp.ClientConnectionError("Network is unreachable")),
            metadata(), audio(),
        ])
        song = denied_song()

        self.assertTrue(await searcher._fill_qq_vip_resolver_url(song))

        self.assertEqual(len(session.get_calls), 3)
        self.assertTrue(all(call[1]["params"]["id"] == MID for call in session.get_calls))
        self.assertEqual(song.name, "Official title (DJ edition)")
        self.assertEqual(song.artists, "Official artist")
        self.assertEqual(song.duration, 223000)
        self.assertEqual(song.audio_url, CDN_URL)
        self.assertEqual(song.unplayable_reason, "")
        self.assertEqual(song.provider_data["resolver_status"], "resolved")

    async def test_connector_failure_invalidates_only_resolver_host_dns_cache(self):
        error = aiohttp.ClientConnectorError(
            SimpleNamespace(host="resolver.example", port=443, ssl=True),
            OSError(errno.ENETUNREACH, "Network is unreachable"),
        )
        searcher, session = self.make_searcher([
            FailureResponse(error), metadata(), audio(),
        ])
        session.connector = SimpleNamespace(clear_dns_cache=Mock())

        self.assertTrue(await searcher._fill_qq_vip_resolver_url(denied_song()))

        session.connector.clear_dns_cache.assert_called_once_with("resolver.example", 443)

    async def test_two_network_failures_are_transient_without_serial_fallbacks(self):
        searcher, session = self.make_searcher([
            FailureResponse(aiohttp.ClientConnectionError("offline")),
            FailureResponse(aiohttp.ClientConnectionError("offline")),
        ])
        song = denied_song()
        searcher._fetch_qq_song_by_id = AsyncMock(return_value=song)
        searcher._search_via_aggregator = AsyncMock()
        searcher._fetch_meting_song_by_id = AsyncMock()

        resolved = await searcher.fetch_song_by_id("qq", MID)

        self.assertIs(resolved, song)
        self.assertEqual(len(session.get_calls), 2)
        self.assertEqual(song.provider_data["resolver_status"], "transient")
        self.assertEqual(song.provider_data["qq_vip_resolver_status"], "transient")
        self.assertIn("暂时不可用", song.unplayable_reason)
        self.assertNotIn("会员", song.unplayable_reason)
        searcher._search_via_aggregator.assert_not_awaited()
        searcher._fetch_meting_song_by_id.assert_not_awaited()

    async def test_http_server_failure_and_rate_limit_retry(self):
        for status in (429, 500, 502, 503, 504):
            with self.subTest(status=status):
                searcher, session = self.make_searcher([
                    FakeResponse(status=status), metadata(), audio(),
                ])
                self.assertTrue(await searcher._fill_qq_vip_resolver_url(denied_song()))
                self.assertEqual(len(session.get_calls), 3)

    async def test_retry_after_larger_than_budget_does_not_wait_or_retry(self):
        searcher, session = self.make_searcher([
            FakeResponse(status=429, headers={"Retry-After": "120"}),
        ])
        song = denied_song()
        start = asyncio.get_running_loop().time()

        self.assertFalse(await searcher._fill_qq_vip_resolver_url(song))

        self.assertLess(asyncio.get_running_loop().time() - start, 1)
        self.assertEqual(len(session.get_calls), 1)
        self.assertEqual(song.provider_data["resolver_status"], "transient")

    async def test_http_client_error_is_not_retried_or_called_membership_denial(self):
        for status in (400, 401, 403, 404):
            with self.subTest(status=status):
                searcher, session = self.make_searcher([FakeResponse(status=status)])
                song = denied_song()
                self.assertFalse(await searcher._fill_qq_vip_resolver_url(song))
                self.assertEqual(len(session.get_calls), 1)
                self.assertEqual(song.provider_data["resolver_status"], "transient")
                self.assertNotIn("会员", song.unplayable_reason)
                if status in {401, 403}:
                    self.assertIn("解析服务拒绝访问", song.unplayable_reason)

    async def test_invalid_endpoint_error_does_not_repeat_request(self):
        for error in (
            aiohttp.InvalidURL("invalid resolver endpoint"),
            aiohttp.ClientConnectorCertificateError(
                SimpleNamespace(host="resolver.example", port=443, ssl=True),
                ssl.CertificateError("certificate verify failed"),
            ),
        ):
            with self.subTest(error=type(error).__name__):
                searcher, session = self.make_searcher([FailureResponse(error)])
                song = denied_song()
                self.assertFalse(await searcher._fill_qq_vip_resolver_url(song))
                self.assertEqual(len(session.get_calls), 1)
                self.assertEqual(song.provider_data["resolver_status"], "transient")

    async def test_resolver_json_access_denied_is_not_song_membership_denial(self):
        for code in (401, "403"):
            with self.subTest(code=code):
                searcher, session = self.make_searcher([FakeResponse(data={"code": code})])
                song = denied_song()
                self.assertFalse(await searcher._fill_qq_vip_resolver_url(song))
                self.assertEqual(len(session.get_calls), 1)
                self.assertIn("解析服务拒绝访问", song.unplayable_reason)
                self.assertNotIn("会员", song.unplayable_reason)

    async def test_empty_or_malformed_response_retries(self):
        responses = [
            FakeResponse(data=None), FakeResponse(data=[]), FakeResponse(data={}),
            FakeResponse(data={"code": 0, "data": {}}),
            FakeResponse(data={"code": 0, "data": {MID: ""}}),
            InvalidJsonResponse(),
        ]
        for response in responses:
            with self.subTest(response=response.data):
                searcher, session = self.make_searcher([response, metadata(), audio()])
                self.assertTrue(await searcher._fill_qq_vip_resolver_url(denied_song()))
                self.assertEqual(len(session.get_calls), 3)

    async def test_failure_in_second_stage_restarts_same_id_once(self):
        searcher, session = self.make_searcher([
            metadata(), FakeResponse(status=503), metadata(), audio(),
        ])
        self.assertTrue(await searcher._fill_qq_vip_resolver_url(denied_song()))
        self.assertEqual(
            [call[1]["params"]["type"] for call in session.get_calls],
            ["song", "url", "song", "url"],
        )

    async def test_empty_redirect_is_retried_and_remains_transient_when_exhausted(self):
        searcher, session = self.make_searcher([
            metadata(), FakeResponse(status=302), metadata(), FakeResponse(status=302),
        ])
        song = denied_song()
        self.assertFalse(await searcher._fill_qq_vip_resolver_url(song))
        self.assertEqual(len(session.get_calls), 4)
        self.assertEqual(song.provider_data["resolver_status"], "transient")

    async def test_retry_cannot_bypass_song_identity_or_audio_host_validation(self):
        for response in (
            FakeResponse(data=[{"songmid": "wrong-mid", "url": CDN_URL}]),
            FakeResponse(data={"code": 0, "data": {MID: "https://evil.test/a.mp3"}}),
        ):
            with self.subTest(data=response.data):
                searcher, session = self.make_searcher([
                    FailureResponse(aiohttp.ClientConnectionError("offline")), response,
                ])
                song = denied_song()
                self.assertFalse(await searcher._fill_qq_vip_resolver_url(song))
                self.assertEqual(len(session.get_calls), 2)
                self.assertEqual(song.audio_url, "")
                self.assertEqual(song.name, "Official title (DJ edition)")

    async def test_timeout_budget_includes_all_attempts_and_cancels_requests(self):
        first = SlowResponse(1)
        second = SlowResponse(1)
        searcher, session = self.make_searcher([first, second])
        searcher.QQ_VIP_RESOLVER_TIMEOUT = 0.08
        song = denied_song()
        start = asyncio.get_running_loop().time()

        self.assertFalse(await searcher._fill_qq_vip_resolver_url(song))

        elapsed = asyncio.get_running_loop().time() - start
        self.assertLess(elapsed, 0.3)
        self.assertEqual(len(session.get_calls), 2)
        self.assertTrue(first.cancelled)
        self.assertTrue(second.cancelled)
        self.assertIn("暂时超时", song.unplayable_reason)
        self.assertTrue(all(call[1]["timeout"].total <= 0.08 for call in session.get_calls))

    async def test_budget_also_includes_session_acquisition(self):
        searcher, session = self.make_searcher([])
        searcher.QQ_VIP_RESOLVER_TIMEOUT = 0.04

        async def acquire():
            await asyncio.sleep(1)
            return session

        searcher._get_session = AsyncMock(side_effect=acquire)
        start = asyncio.get_running_loop().time()
        song = denied_song()
        self.assertFalse(await searcher._fill_qq_vip_resolver_url(song))
        self.assertLess(asyncio.get_running_loop().time() - start, 0.3)
        self.assertEqual(session.get_calls, [])
        self.assertIn("暂时超时", song.unplayable_reason)

    async def test_cancellation_propagates_without_retry_or_relabel(self):
        searcher, session = self.make_searcher([FailureResponse(asyncio.CancelledError())])
        song = denied_song()
        with self.assertRaises(asyncio.CancelledError):
            await searcher._fill_qq_vip_resolver_url(song)
        self.assertEqual(len(session.get_calls), 1)
        self.assertEqual(song.provider_data["resolver_status"], "denied")

    async def test_success_after_prior_transient_failure_clears_failure_state(self):
        searcher, session = self.make_searcher([metadata(), audio()])
        song = denied_song()
        song.provider_data.update({
            "resolver_status": "transient", "qq_vip_resolver_status": "transient",
        })
        song.unplayable_reason = "temporary timeout"
        self.assertTrue(await searcher._fill_qq_vip_resolver_url(song))
        self.assertEqual(song.provider_data["resolver_status"], "resolved")
        self.assertEqual(song.provider_data["qq_vip_resolver_status"], "resolved")
        self.assertEqual(song.unplayable_reason, "")


if __name__ == "__main__":
    unittest.main()
