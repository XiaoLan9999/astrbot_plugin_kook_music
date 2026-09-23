"""Exercise real aiohttp response parsing without calling Tencent endpoints."""

import asyncio
import sys
import unittest
from pathlib import Path

import aiohttp

PLUGINS_DIR = Path(__file__).resolve().parents[2]
if str(PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGINS_DIR))

from astrbot_plugin_kook_music.music_auth.qq_backend import (  # noqa: E402
    _load_sdk,
    _OfficialTransport,
)
from astrbot_plugin_kook_music.music_auth.types import AuthError  # noqa: E402

try:
    SDK = _load_sdk()
except AuthError:
    SDK = None


@unittest.skipIf(SDK is None, "Optional pinned QQMusicApi is not installed")
class QQTransportCookieTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.responses = []
        self.seen = []
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        local_url = f"http://127.0.0.1:{port}/synthetic-check-sig"
        owner = self

        class LocalSession:
            def __init__(self, **kwargs):
                owner.session_kwargs = kwargs
                self.session = aiohttp.ClientSession(**kwargs)

            def request(self, method, url, **kwargs):
                owner.seen.append((method, url, kwargs))
                return self.session.request(method, local_url, **kwargs)

            async def close(self):
                await self.session.close()

        self.transport = _OfficialTransport(session_factory=LocalSession)

    async def asyncTearDown(self):
        await self.transport.close()
        self.server.close()
        await self.server.wait_closed()

    async def _serve(self, reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(self.responses.pop(0))
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def request(self, *headers, status="200 OK"):
        from qqmusic_api.core.transport import PreparedRequest

        self.responses.append(
            (
                f"HTTP/1.1 {status}\r\n"
                + "\r\n".join(headers)
                + "\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            ).encode()
        )
        return await self.transport.request(
            PreparedRequest("GET", "https://ssl.ptlogin2.graph.qq.com/check_sig", {})
        )

    async def test_response_cookies_are_not_filtered_by_domain_or_dummy_jar(self):
        response = await self.request(
            "Set-Cookie: p_skey=SYNTHETIC; Domain=.graph.qq.com; Path=/; Secure; HttpOnly"
        )
        self.assertEqual(response.cookies, {"p_skey": "SYNTHETIC"})
        self.assertIsInstance(self.session_kwargs["cookie_jar"], aiohttp.DummyCookieJar)
        self.assertEqual(len(self.session_kwargs["cookie_jar"]), 0)

    async def test_multiple_set_cookie_headers_and_semicolon_attributes(self):
        response = await self.request(
            "Set-Cookie: p_uin=o000123; Domain=.graph.qq.com; Path=/; Secure",
            "Set-Cookie: p_skey=SYNTHETIC; Domain=.graph.qq.com; Path=/; SameSite=None; Secure",
            "Set-Cookie: pt4_token=TEST_ONLY; Domain=.graph.qq.com; Path=/; Max-Age=300",
        )
        self.assertEqual(
            response.cookies,
            {"p_uin": "o000123", "p_skey": "SYNTHETIC", "pt4_token": "TEST_ONLY"},
        )

    async def test_expires_comma_does_not_merge_adjacent_headers(self):
        response = await self.request(
            "Set-Cookie: other=ONE; Domain=.graph.qq.com; Expires=Wed, 21 Oct 2037 07:28:00 GMT; Path=/",
            "Set-Cookie: p_skey=SYNTHETIC; Domain=.graph.qq.com; Path=/",
        )
        self.assertEqual(response.cookies["p_skey"], "SYNTHETIC")
        self.assertEqual(response.cookies["other"], "ONE")

    async def test_first_redirect_response_cookie_is_kept_without_following(self):
        response = await self.request(
            "Set-Cookie: p_skey=SYNTHETIC; Domain=.graph.qq.com; Path=/; Secure",
            "Location: https://untrusted.invalid/never-follow",
            status="302 Found",
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.cookies["p_skey"], "SYNTHETIC")
        self.assertIs(self.seen[0][2]["allow_redirects"], False)
        self.assertEqual(len(self.seen), 1)

    async def test_cookies_do_not_leak_into_following_request(self):
        await self.request("Set-Cookie: p_skey=SYNTHETIC; Path=/")
        response = await self.request("Content-Type: text/plain")
        self.assertEqual(response.cookies, {})
        self.assertNotIn("cookies", self.seen[-1][2])
        self.assertIs(self.session_kwargs["trust_env"], False)

    async def test_broad_domain_deletion_keeps_graph_domain_cookie(self):
        response = await self.request(
            "Set-Cookie: p_skey=GRAPH_ONLY; Domain=.graph.qq.com; Path=/; Secure",
            "Set-Cookie: p_skey=; Domain=.qq.com; Path=/; Max-Age=0",
            status="302 Found",
        )
        self.assertEqual(response.cookies, {"p_skey": "GRAPH_ONLY"})

    async def test_earlier_broad_domain_deletion_keeps_graph_domain_cookie(self):
        response = await self.request(
            "Set-Cookie: p_skey=; Domain=.qq.com; Path=/; Max-Age=0",
            "Set-Cookie: p_skey=GRAPH_ONLY; Domain=.graph.qq.com; Path=/; Secure",
        )
        self.assertEqual(response.cookies, {"p_skey": "GRAPH_ONLY"})

    async def test_same_domain_max_age_deletion_removes_cookie(self):
        response = await self.request(
            "Set-Cookie: p_skey=OLD_VALUE; Domain=.graph.qq.com; Path=/; Secure",
            "Set-Cookie: p_skey=; Domain=.graph.qq.com; Path=/; Max-Age=0",
        )
        self.assertNotIn("p_skey", response.cookies)

    async def test_same_domain_expired_cookie_removes_cookie(self):
        response = await self.request(
            "Set-Cookie: p_skey=OLD_VALUE; Domain=.graph.qq.com; Path=/; Secure",
            "Set-Cookie: p_skey=; Domain=.graph.qq.com; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT",
        )
        self.assertNotIn("p_skey", response.cookies)

    async def test_expired_nonempty_cookie_is_not_revived(self):
        for expired in [
            "Thu, 01 Jan 1970 00:00:00 GMT",
            "Fri, 01 Jan 1999 00:00:00 GMT",
        ]:
            with self.subTest(expires=expired):
                response = await self.request(
                    "Set-Cookie: p_skey=OLD_VALUE; Domain=.graph.qq.com; Path=/",
                    f"Set-Cookie: p_skey=EXPIRED_VALUE; Domain=.graph.qq.com; Path=/; Expires={expired}",
                )
                self.assertNotIn("p_skey", response.cookies)

    async def test_valid_max_age_takes_precedence_over_past_expires(self):
        response = await self.request(
            "Set-Cookie: p_skey=LIVE; Domain=.graph.qq.com; Path=/; Max-Age=300; Expires=Thu, 01 Jan 1970 00:00:00 GMT"
        )
        self.assertEqual(response.cookies["p_skey"], "LIVE")

    async def test_new_cookie_after_same_domain_deletion_is_kept(self):
        response = await self.request(
            "Set-Cookie: p_skey=; Domain=.graph.qq.com; Path=/; Max-Age=0",
            "Set-Cookie: p_skey=NEW_VALUE; Domain=.graph.qq.com; Path=/; Secure",
        )
        self.assertEqual(response.cookies["p_skey"], "NEW_VALUE")

    async def test_sibling_unrelated_and_host_only_cookies_are_not_forwarded(self):
        response = await self.request(
            "Set-Cookie: p_skey=GRAPH_ONLY; Domain=.graph.qq.com; Path=/; Secure",
            "Set-Cookie: p_skey=SIBLING; Domain=.y.qq.com; Path=/",
            "Set-Cookie: p_skey=UNRELATED; Domain=.example.com; Path=/",
            "Set-Cookie: p_skey=HOST_ONLY; Path=/",
            "Set-Cookie: other=OTHER_HOST; Domain=ssl.ptlogin2.graph.qq.com; Path=/",
        )
        self.assertEqual(response.cookies, {"p_skey": "GRAPH_ONLY"})

    async def test_cookie_paths_are_filtered_for_oauth_authorize(self):
        response = await self.request(
            "Set-Cookie: p_skey=ROOT; Domain=.graph.qq.com; Path=/",
            "Set-Cookie: p_skey=OAUTH; Domain=.graph.qq.com; Path=/oauth2.0",
            "Set-Cookie: p_skey=OTHER; Domain=.graph.qq.com; Path=/unrelated",
        )
        self.assertEqual(response.cookies["p_skey"], "OAUTH")

    async def test_more_specific_domain_wins_independently_of_header_order(self):
        for headers in [
            (
                "Set-Cookie: p_skey=GRAPH_ONLY; Domain=.graph.qq.com; Path=/",
                "Set-Cookie: p_skey=PARENT; Domain=.qq.com; Path=/",
            ),
            (
                "Set-Cookie: p_skey=PARENT; Domain=.qq.com; Path=/",
                "Set-Cookie: p_skey=GRAPH_ONLY; Domain=.graph.qq.com; Path=/",
            ),
        ]:
            with self.subTest(headers=headers):
                response = await self.request(*headers)
                self.assertEqual(response.cookies["p_skey"], "GRAPH_ONLY")


if __name__ == "__main__":
    unittest.main()
