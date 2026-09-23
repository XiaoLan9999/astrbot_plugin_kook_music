"""Replay synthetic NetEase QR HTTP responses through real aiohttp parsing."""

import asyncio
import json
import sys
import unittest
from pathlib import Path

import aiohttp

PLUGINS_DIR = Path(__file__).resolve().parents[2]
if str(PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGINS_DIR))

from astrbot_plugin_kook_music.music_auth.netease_backend import (  # noqa: E402
    NeteaseBackend,
)
from astrbot_plugin_kook_music.music_auth.types import AuthError  # noqa: E402


class NeteaseQrTypeTests(unittest.IsolatedAsyncioTestCase):
    async def check_type(self, expected, *, method="netease", **options):
        calls = []

        async def request(path, payload, cookies):
            calls.append((path, dict(payload)))
            if path.endswith("unikey"):
                return {"code": 200, "unikey": "synthetic-mode-key-123"}, {}
            return {"code": 801}, {}

        backend = NeteaseBackend(request=request, **options)
        self.addAsyncCleanup(backend.close)
        challenge = await backend.begin_login(method)
        self.assertEqual((await backend.poll_login(challenge)).status, "pending")
        self.assertEqual(
            [payload["type"] for _, payload in calls], [expected, expected]
        )
        self.assertEqual(
            [path for path, _ in calls],
            ["/weapi/login/qrcode/unikey", "/weapi/login/qrcode/client/login"],
        )
        return backend, challenge, calls

    async def test_production_default_remains_type_three(self):
        await self.check_type(3)

    async def test_supported_protocol_types_match_both_requests(self):
        for qr_type in [1, 3]:
            with self.subTest(qr_type=qr_type):
                await self.check_type(qr_type, qr_type=qr_type)

    async def test_challenge_keeps_initial_type(self):
        backend, challenge, calls = await self.check_type(1, qr_type=1)
        backend._qr_type = 3
        self.assertEqual((await backend.poll_login(challenge)).status, "pending")
        self.assertEqual(calls[-1][1]["type"], 1)

    async def test_explicit_web_method_uses_type_one(self):
        backend, _, _ = await self.check_type(1, method="netease_web")
        self.assertEqual(backend._qr_type, 3)

    async def test_normal_login_methods_keep_configured_type(self):
        for method in ["netease", "qr"]:
            with self.subTest(method=method):
                await self.check_type(3, method=method)

    async def test_interleaved_web_and_normal_logins_keep_separate_types(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def request(path, payload, cookies):
            calls.append((path, dict(payload)))
            if path.endswith("unikey"):
                if payload["type"] == 1:
                    entered.set()
                    await release.wait()
                return {
                    "code": 200,
                    "unikey": f"synthetic-mode-key-{payload['type']}",
                }, {}
            return {"code": 801}, {}

        backend = NeteaseBackend(request=request)
        self.addAsyncCleanup(backend.close)
        web_task = asyncio.create_task(backend.begin_login("netease_web"))
        await entered.wait()
        normal = await backend.begin_login("netease")
        release.set()
        web = await web_task
        await backend.poll_login(normal)
        await backend.poll_login(web)
        await backend.poll_login(normal)
        self.assertEqual([payload["type"] for _, payload in calls], [1, 3, 3, 1, 3])
        self.assertEqual(
            [payload["key"] for _, payload in calls[2:]],
            ["synthetic-mode-key-3", "synthetic-mode-key-1", "synthetic-mode-key-3"],
        )
        self.assertEqual(backend._qr_type, 3)

    def test_invalid_types_are_rejected_before_network(self):
        for qr_type in [None, True, False, 0, 2, 4, "1", "3", 1.0, [], {}]:
            with self.subTest(qr_type=qr_type):
                with self.assertRaisesRegex(ValueError, "qr_type must be 1 or 3"):
                    NeteaseBackend(qr_type=qr_type)


class NeteaseConfirmTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.responses = []
        self.seen = []
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        local_url = f"http://127.0.0.1:{port}/synthetic-netease"
        owner = self

        class LocalSession:
            def __init__(self):
                self.session = aiohttp.ClientSession(
                    cookie_jar=aiohttp.DummyCookieJar(), trust_env=False
                )

            def post(self, url, **kwargs):
                owner.seen.append((url, kwargs))
                return self.session.post(local_url, **kwargs)

            async def close(self):
                await self.session.close()

        self.backend = NeteaseBackend()
        self.backend._session = LocalSession()

    async def asyncTearDown(self):
        await self.backend.close()
        self.server.close()
        await self.server.wait_closed()

    async def _serve(self, reader, writer):
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            for line in request.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    await reader.readexactly(int(line.split(b":", 1)[1]))
                    break
            writer.write(self.responses.pop(0))
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    def response(self, body, *headers, status="200 OK"):
        raw = json.dumps(body).encode()
        head = (
            "\r\n".join(
                [
                    f"HTTP/1.1 {status}",
                    *headers,
                    f"Content-Length: {len(raw)}",
                    "Connection: close",
                    "",
                    "",
                ]
            )
        ).encode()
        self.responses.append(head + raw)

    async def challenge(self):
        self.response({"code": 200, "unikey": "synthetic-netease-key-123"})
        return await self.backend.begin_login()

    async def authorized(self, *headers):
        challenge = await self.challenge()
        self.response({"code": 803}, *headers)
        result = await self.backend.poll_login(challenge)
        self.assertEqual(result.status, "authorized")
        return result.credential["cookies"]

    async def test_pending_scanned_authorized_real_http(self):
        challenge = await self.challenge()
        for code, expected in [(801, "pending"), (802, "scanned")]:
            self.response({"code": code})
            self.assertEqual(
                (await self.backend.poll_login(challenge)).status, expected
            )
        self.response(
            {"code": 803},
            "Set-Cookie: MUSIC_U=SYNTHETIC; Domain=.music.163.com; Path=/; Secure",
            "Set-Cookie: __csrf=SYNTHETIC_CSRF; Domain=.music.163.com; Path=/; Secure",
        )
        result = await self.backend.poll_login(challenge)
        self.assertEqual(result.status, "authorized")
        self.assertEqual(result.credential["cookies"]["MUSIC_U"], "SYNTHETIC")
        self.assertEqual(result.credential["cookies"]["__csrf"], "SYNTHETIC_CSRF")
        self.assertNotIn("SYNTHETIC", repr(result))
        self.assertEqual((await self.backend.poll_login(challenge)).status, "expired")

    async def test_common_first_party_cookie_domains(self):
        for scope in ["", "Domain=.music.163.com; ", "Domain=.163.com; "]:
            with self.subTest(scope=scope):
                cookies = await self.authorized(
                    f"Set-Cookie: MUSIC_U=SYNTHETIC; {scope}Path=/; Secure"
                )
                self.assertEqual(cookies["MUSIC_U"], "SYNTHETIC")

    async def test_broad_domain_deletion_does_not_erase_specific_cookie(self):
        for headers in [
            (
                "Set-Cookie: MUSIC_U=LIVE; Domain=.music.163.com; Path=/; Secure",
                "Set-Cookie: MUSIC_U=; Domain=.163.com; Path=/; Max-Age=0",
            ),
            (
                "Set-Cookie: MUSIC_U=; Domain=.163.com; Path=/; Max-Age=0",
                "Set-Cookie: MUSIC_U=LIVE; Domain=.music.163.com; Path=/; Secure",
            ),
        ]:
            with self.subTest(headers=headers):
                self.assertEqual((await self.authorized(*headers))["MUSIC_U"], "LIVE")

    async def test_specific_domain_wins_independently_of_header_order(self):
        for headers in [
            (
                "Set-Cookie: MUSIC_U=SPECIFIC; Domain=.music.163.com; Path=/",
                "Set-Cookie: MUSIC_U=PARENT; Domain=.163.com; Path=/",
            ),
            (
                "Set-Cookie: MUSIC_U=PARENT; Domain=.163.com; Path=/",
                "Set-Cookie: MUSIC_U=SPECIFIC; Domain=.music.163.com; Path=/",
            ),
        ]:
            with self.subTest(headers=headers):
                self.assertEqual(
                    (await self.authorized(*headers))["MUSIC_U"], "SPECIFIC"
                )

    async def test_foreign_cookie_does_not_erase_first_party_cookie(self):
        cookies = await self.authorized(
            "Set-Cookie: MUSIC_U=LIVE; Domain=.music.163.com; Path=/",
            "Set-Cookie: MUSIC_U=FOREIGN; Domain=.example.test; Path=/",
        )
        self.assertEqual(cookies["MUSIC_U"], "LIVE")

    async def test_irrelevant_path_cannot_override_account_cookie(self):
        cookies = await self.authorized(
            "Set-Cookie: MUSIC_U=LIVE; Domain=.music.163.com; Path=/",
            "Set-Cookie: MUSIC_U=OTHER; Domain=.music.163.com; Path=/unrelated",
        )
        self.assertEqual(cookies["MUSIC_U"], "LIVE")

    async def test_expired_nonempty_cookie_is_not_an_authorized_account(self):
        for expiry in [
            "Max-Age=0",
            "Expires=Thu, 01 Jan 1970 00:00:00 GMT",
            "Expires=Fri, 01 Jan 1999 00:00:00 GMT",
        ]:
            with self.subTest(expiry=expiry):
                challenge = await self.challenge()
                self.response(
                    {"code": 803},
                    f"Set-Cookie: MUSIC_U=EXPIRED; Domain=.music.163.com; Path=/; {expiry}",
                )
                with self.assertRaises(AuthError):
                    await self.backend.poll_login(challenge)

    async def test_valid_max_age_overrides_expired_date(self):
        cookies = await self.authorized(
            "Set-Cookie: MUSIC_U=LIVE; Domain=.music.163.com; Path=/; Max-Age=300; Expires=Thu, 01 Jan 1970 00:00:00 GMT"
        )
        self.assertEqual(cookies["MUSIC_U"], "LIVE")

    async def test_body_cookie_string_never_authenticates(self):
        challenge = await self.challenge()
        self.response({"code": 803, "cookie": "MUSIC_U=SYNTHETIC"})
        with self.assertRaises(AuthError):
            await self.backend.poll_login(challenge)

    async def test_other_cookie_attributes_never_become_tokens(self):
        cookies = await self.authorized(
            "Set-Cookie: MUSIC_U=LIVE; Domain=.music.163.com; Path=/; Secure; SameSite=None",
            "Set-Cookie: __csrf=CSRF; Domain=.music.163.com; Path=/; Expires=Wed, 21 Oct 2037 07:28:00 GMT",
            "Set-Cookie: unrelated=OTHER; Domain=.music.163.com; Path=/",
        )
        self.assertEqual(cookies, {"MUSIC_U": "LIVE", "__csrf": "CSRF"})

    async def test_response_cookies_never_enter_ambient_session(self):
        await self.authorized("Set-Cookie: MUSIC_U=LIVE; Path=/")
        self.assertEqual(len(self.backend._session.session.cookie_jar), 0)
        await self.challenge()
        self.assertEqual(self.seen[-1][1]["cookies"], {})
        self.assertTrue(all(not options["allow_redirects"] for _, options in self.seen))

    async def test_request_failure_has_only_static_numeric_diagnostics(self):
        self.response({"message": "SYNTHETIC_SECRET"}, status="503 Service Unavailable")
        with self.assertLogs(self.backend.__module__, level="WARNING") as logs:
            with self.assertRaises(AuthError) as caught:
                await self.backend.begin_login()
        self.assertEqual(caught.exception.diagnostic, "NETEASE_REQUEST:HTTP503")
        self.assertNotIn("SYNTHETIC_SECRET", str(caught.exception) + str(logs.output))

    async def test_non_numeric_remote_code_is_never_logged(self):
        challenge = await self.challenge()
        self.response(
            {
                "code": "SYNTHETIC_SECRET",
                "message": "PRIVATE_MESSAGE",
                "data": {"token": "PRIVATE_TOKEN"},
            }
        )
        with self.assertLogs(self.backend.__module__, level="WARNING") as logs:
            with self.assertRaises(AuthError) as caught:
                await self.backend.poll_login(challenge)
        self.assertEqual(caught.exception.diagnostic, "NETEASE_POLL:HTTP200:MUSIC_U0")
        rendered = str(caught.exception) + str(logs.output)
        for private in ["SYNTHETIC_SECRET", "PRIVATE_MESSAGE", "PRIVATE_TOKEN"]:
            self.assertNotIn(private, rendered)

    async def test_missing_success_cookie_has_distinct_stage(self):
        challenge = await self.challenge()
        self.response({"code": 803})
        with self.assertRaises(AuthError) as caught:
            await self.backend.poll_login(challenge)
        self.assertEqual(
            caught.exception.diagnostic, "NETEASE_COOKIE:HTTP200:CODE803:MUSIC_U0"
        )

    async def test_verification_challenge_is_not_retried_or_authorized(self):
        for headers, received in [
            ([], False),
            (["Set-Cookie: MUSIC_U=UNVERIFIED; Domain=.music.163.com; Path=/"], True),
        ]:
            with self.subTest(received=received):
                challenge = await self.challenge()
                self.response({"code": 802})
                self.assertEqual(
                    (await self.backend.poll_login(challenge)).status, "scanned"
                )
                self.response(
                    {
                        "code": 8821,
                        "message": "PRIVATE_SERVER_MESSAGE",
                        "data": {"token": "PRIVATE_TOKEN"},
                    },
                    *headers,
                )
                with self.assertLogs(self.backend.__module__, level="WARNING") as logs:
                    with self.assertRaises(AuthError) as caught:
                        await self.backend.poll_login(challenge)
                error = caught.exception
                self.assertEqual(error.kind, "verification_required")
                self.assertEqual(
                    error.diagnostic,
                    f"NETEASE_VERIFY:HTTP200:CODE8821:MUSIC_U{int(received)}",
                )
                self.assertEqual(
                    (await self.backend.poll_login(challenge)).status, "expired"
                )
                self.assertNotIn(challenge.challenge_id, self.backend._challenges)
                for private in [
                    "PRIVATE_SERVER_MESSAGE",
                    "PRIVATE_TOKEN",
                    "UNVERIFIED",
                ]:
                    self.assertNotIn(private, str(error) + str(logs.output))


if __name__ == "__main__":
    unittest.main()
