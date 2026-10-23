import asyncio
import json
import sys
import unittest
from http.cookies import SimpleCookie
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

from multidict import CIMultiDict

PLUGINS_DIR = Path(__file__).resolve().parents[2]
if str(PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGINS_DIR))

from astrbot_plugin_kook_music.music_auth.bilibili_backend import (  # noqa: E402
    BilibiliBackend,
    _EXCHANGE,
    _GENERATE,
    _NAV,
    _POLL,
    _cookies,
    _qr_url,
    _redirect_cookies,
    _response_cookies,
    _ticket_params,
)
from astrbot_plugin_kook_music.music_auth.types import AuthError, LoginChallenge  # noqa: E402

KEY = "0123456789abcdef0123456789abcdef"
TICKET_URL = (
    _EXCHANGE + "?ticket=synthetic-ticket-123&gourl=https%3A%2F%2Fwww.bilibili.com%2F"
)
COOKIES = {
    "SESSDATA": "synthetic%2Csession%2Conly",
    "bili_jct": "fake-csrf-token",
    "DedeUserID": "123456",
}


def qr_body(key=KEY, url=None):
    if url is None:
        url = "https://passport.bilibili.com/h5-app/passport/login/scan?" + urlencode(
            {"qrcode_key": key, "navhide": "1", "from": ""}
        )
    return {"code": 0, "data": {"url": url, "qrcode_key": key}}


def poll_body(code, url="", **extra):
    return {"code": 0, "data": {"code": code, "url": url, **extra}}


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def __call__(self, url, params, cookies):
        self.calls.append((url, params, dict(cookies)))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            response = await response()
        return response if isinstance(response, tuple) else (response, {})


class FakeContent:
    def __init__(self, raw):
        self.raw = raw

    async def iter_chunked(self, _size):
        for offset in range(0, len(self.raw), 17):
            yield self.raw[offset : offset + 17]


class FakeResponse:
    def __init__(self, body=None, *, raw=None, status=200, cookies=()):
        self.status = status
        self.content = FakeContent(
            raw if raw is not None else json.dumps(body).encode()
        )
        self.headers = CIMultiDict()
        for cookie in cookies:
            self.headers.add("Set-Cookie", cookie)
        self.cookies = SimpleCookie()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.close = AsyncMock()

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return (
            self.response.pop(0) if isinstance(self.response, list) else self.response
        )


class CookieBoundaryTests(unittest.TestCase):
    def test_only_bilibili_account_fields_are_returned(self):
        self.assertEqual(
            _cookies({**COOKIES, "MUSIC_U": "other", "sid": "ignored"}), COOKIES
        )
        self.assertEqual(_cookies("SESSDATA=fake"), {})

    def test_invalid_values_cannot_become_request_headers(self):
        for value in [
            "bad\r\nInjected: yes",
            "semi;colon",
            'quote"value',
            "slash\\value",
            "",
            "wide\u4e2d",
        ]:
            with self.subTest(value=value):
                self.assertNotIn("SESSDATA", _cookies({"SESSDATA": value}))
        self.assertNotIn("DedeUserID", _cookies({"DedeUserID": "abc"}))

    def test_cookie_encoding_is_not_repeatedly_decoded(self):
        self.assertEqual(
            _cookies({"SESSDATA": "fake%252Csession"})["SESSDATA"], "fake%252Csession"
        )

    def test_redirect_cookie_escaping_round_trip(self):
        url = "https://passport.biligame.com/crossDomain?" + urlencode(COOKIES)
        self.assertEqual(_redirect_cookies(url, COOKIES), COOKIES)
        self.assertEqual(
            _redirect_cookies(url)["SESSDATA"],
            "synthetic%252Csession%252Conly",
        )
        single_encoding = "https://passport.biligame.com/crossDomain?SESSDATA=synthetic%2Csession%2Conly"
        self.assertEqual(
            _redirect_cookies(single_encoding)["SESSDATA"], COOKIES["SESSDATA"]
        )

    def test_empty_redirect_supports_header_only_success(self):
        self.assertEqual(_redirect_cookies(""), {})
        self.assertEqual(_redirect_cookies(None), {})

    def test_redirect_rejects_unexpected_targets(self):
        for url in [
            "http://passport.biligame.com/crossDomain?SESSDATA=fake",
            "https://passport.biligame.com.evil.test/crossDomain?SESSDATA=fake",
            "https://user:pass@passport.biligame.com/crossDomain?SESSDATA=fake",
            "https://passport.biligame.com:8443/crossDomain?SESSDATA=fake",
            "https://passport.biligame.com/other?SESSDATA=fake",
            "https://passport.bilibili.com/anything?SESSDATA=fake",
            "https://127.0.0.1/crossDomain?SESSDATA=fake",
            "https://passport.biligame.com/crossDomain?SESSDATA=fake#fragment",
            "https://passport.biligame.com\\@evil.test/crossDomain?SESSDATA=fake",
        ]:
            with self.subTest(url=url), self.assertRaises(AuthError):
                _redirect_cookies(url)

    def test_conflicting_redirect_cookies_are_rejected(self):
        with self.assertRaises(AuthError):
            _redirect_cookies(
                "https://passport.biligame.com/crossDomain?SESSDATA=first&SESSDATA=second"
            )

    def test_unsafe_cookie_from_redirect_is_rejected(self):
        with self.assertRaises(AuthError):
            _redirect_cookies(
                "https://passport.biligame.com/crossDomain?SESSDATA=fake%0D%0AInjected"
            )

    def test_response_cookie_scope_and_expiry(self):
        response = FakeResponse(
            cookies=[
                "SESSDATA=expected; Domain=.bilibili.com; Path=/; Secure; HttpOnly",
                "bili_jct=wrong-domain; Domain=evil.test; Path=/",
                "DedeUserID=123456; Domain=.bilibili.com; Path=/other",
                "buvid3=expired; Domain=.bilibili.com; Path=/; Max-Age=0",
                "buvid4=expired; Domain=.bilibili.com; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT",
                "sid=ignored; Domain=.bilibili.com; Path=/",
            ]
        )
        self.assertEqual(_response_cookies(response), {"SESSDATA": "expected"})

    def test_conflicting_set_cookie_headers_are_rejected(self):
        response = FakeResponse(
            cookies=[
                "SESSDATA=first; Domain=.bilibili.com; Path=/",
                "SESSDATA=second; Domain=.bilibili.com; Path=/",
            ]
        )
        with self.assertRaises(AuthError):
            _response_cookies(response)

    def test_conflicting_same_header_cookies_are_rejected(self):
        response = FakeResponse(
            cookies=["SESSDATA=first; SESSDATA=second; Domain=.bilibili.com; Path=/"]
        )
        with self.assertRaises(AuthError):
            _response_cookies(response)


class LoginTests(unittest.IsolatedAsyncioTestCase):
    async def new_backend(self, *responses, clock=None):
        transport = FakeTransport(*responses)
        options = {"request": transport}
        if clock is not None:
            options["clock"] = clock
        backend = BilibiliBackend(**options)
        self.addAsyncCleanup(backend.close)
        return backend, transport

    async def test_qr_generated_locally_and_key_hidden(self):
        backend, transport = await self.new_backend(qr_body())
        challenge = await backend.begin_login()
        self.assertTrue(challenge.qr_bytes.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(challenge.expires_in, 180)
        self.assertNotIn(KEY, repr(challenge))
        self.assertNotEqual(challenge.challenge_id, KEY)
        self.assertEqual(transport.calls, [(_GENERATE, {}, {})])

    async def test_account_scan_web_qr_retains_complete_official_url(self):
        url = "https://account.bilibili.com/h5/account-h5/auth/scan-web?" + urlencode(
            [("navhide", "1"), ("callback", "close"), ("qrcode_key", KEY), ("from", "")]
        )
        backend, transport = await self.new_backend(qr_body(url=url))
        self.assertEqual(_qr_url(url, KEY), url)
        with patch("qrcode.make") as make:
            challenge = await backend.begin_login()
        make.assert_called_once_with(url)
        self.assertEqual(challenge.expires_in, 180)
        self.assertEqual(transport.calls, [(_GENERATE, {}, {})])

    async def test_safe_identifier_and_bilibili_callback_urls_preserve_legacy_qr(self):
        for callback in [
            "close",
            "onLoginComplete",
            "https://www.bilibili.com/",
            "https://account.bilibili.com:443/login/complete.html",
        ]:
            with self.subTest(callback=callback):
                url = "https://passport.bilibili.com/h5-app/passport/login/scan?" + urlencode(
                    {"qrcode_key": KEY, "callback": callback, "navhide": "1"}
                )
                self.assertEqual(_qr_url(url, KEY), url)
                backend, _ = await self.new_backend(qr_body(url=url))
                with patch("qrcode.make") as make:
                    await backend.begin_login()
                make.assert_called_once_with(url)

    async def test_account_scan_web_rejects_unsafe_callback(self):
        for callback in [
            "",
            "javascript:alert(1)",
            "data:text/html,login",
            "close\r\nInjected: yes",
            "close\x00",
            "http://www.bilibili.com/",
            "https://bilibili.com.evil.test/",
            "https://evil.test/bilibili.com/",
            "https://www.bilibili.com@evil.test/",
            "https://user:password@www.bilibili.com/",
            "https://www.bilibili.com:8443/",
            "https://www.bilibili.com/#fragment",
            "https://www.bilibili.com/?redirect=https://evil.test/",
            "https://www.bilibili.com/../login",
            "https://www.bilibili.com/%2e%2e/login",
            "https://www.bilibili.com//evil.test/",
            "https://www.bilibili.com\\@evil.test/",
            "x" * 65,
            "https://www.bilibili.com/" + "x" * 1024,
        ]:
            with self.subTest(callback=callback):
                url = "https://account.bilibili.com/h5/account-h5/auth/scan-web?" + urlencode(
                    {"qrcode_key": KEY, "callback": callback}
                )
                backend, _ = await self.new_backend(qr_body(url=url))
                with self.assertRaises(AuthError):
                    await backend.begin_login()
                self.assertFalse(backend._challenges)

    async def test_account_scan_web_rejects_ambiguous_or_overlong_query(self):
        prefix = "https://account.bilibili.com/h5/account-h5/auth/scan-web?"
        for query in [
            urlencode([("qrcode_key", KEY), ("callback", "close"), ("callback", "close")]),
            "qrcode_key=" + KEY + "&callback=close&%63allback=close",
            urlencode({"qrcode_key": "f" * 32, "callback": "close"}),
            urlencode([("qrcode_key", KEY), ("qrcode_key", KEY), ("callback", "close")]),
            urlencode({"qrcode_key": KEY, "from": "x" * 2049, "callback": "close"}),
            urlencode({"qrcode_key": KEY, "from": "x" * 2048, "padding": "y" * 2048}),
        ]:
            with self.subTest(query=query[:80]):
                backend, _ = await self.new_backend(qr_body(url=prefix + query))
                with self.assertRaises(AuthError):
                    await backend.begin_login()
                self.assertFalse(backend._challenges)

    async def test_new_qr_host_and_path_must_match_exactly(self):
        for prefix in [
            "https://account.bilibili.com.evil.test/h5/account-h5/auth/scan-web",
            "https://account.bilibili.com/h5-app/passport/login/scan",
            "https://passport.bilibili.com/h5/account-h5/auth/scan-web",
            "https://account.bilibili.com/h5/account-h5/auth/scan-web/",
        ]:
            with self.subTest(prefix=prefix):
                backend, _ = await self.new_backend(qr_body(url=prefix + "?qrcode_key=" + KEY))
                with self.assertRaises(AuthError):
                    await backend.begin_login()
                self.assertFalse(backend._challenges)

    async def test_wrong_method_does_not_request_qr(self):
        backend, transport = await self.new_backend()
        with self.assertRaises(AuthError):
            await backend.begin_login("qq")
        self.assertFalse(transport.calls)

    async def test_qr_url_must_match_official_host_path_and_key(self):
        for url in [
            "https://evil.test/h5-app/passport/login/scan?qrcode_key=" + KEY,
            "https://passport.bilibili.com/other?qrcode_key=" + KEY,
            "http://passport.bilibili.com/h5-app/passport/login/scan?qrcode_key=" + KEY,
            "https://passport.bilibili.com/h5-app/passport/login/scan?qrcode_key=different",
            "https://passport.bilibili.com/h5-app/passport/login/scan?qrcode_key="
            + KEY
            + "&qrcode_key="
            + KEY,
        ]:
            with self.subTest(url=url):
                backend, _ = await self.new_backend(qr_body(url=url))
                with self.assertRaises(AuthError):
                    await backend.begin_login()
                self.assertFalse(backend._challenges)

    async def test_malformed_qr_response_never_produces_challenge(self):
        for body in [
            {},
            {"code": 0, "data": []},
            qr_body(key="bad"),
            {**qr_body(), "code": False},
        ]:
            backend, _ = await self.new_backend(body)
            with self.assertRaises(AuthError):
                await backend.begin_login()
            self.assertFalse(backend._challenges)

    async def test_pending_scanned_header_authorized(self):
        backend, transport = await self.new_backend(
            qr_body(),
            poll_body(86101),
            poll_body(86090),
            (poll_body(0, refresh_token="synthetic-refresh-token"), COOKIES),
        )
        challenge = await backend.begin_login()
        self.assertEqual((await backend.poll_login(challenge)).status, "pending")
        self.assertEqual((await backend.poll_login(challenge)).status, "scanned")
        result = await backend.poll_login(challenge)
        self.assertEqual(result.status, "authorized")
        self.assertEqual(result.credential["cookies"], COOKIES)
        self.assertEqual(result.credential["refresh_token"], "synthetic-refresh-token")
        self.assertNotIn(COOKIES["SESSDATA"], repr(result))
        self.assertNotIn("synthetic-refresh-token", repr(result))
        self.assertEqual((await backend.poll_login(challenge)).status, "expired")
        self.assertTrue(all(cookies == {} for _, _, cookies in transport.calls))

    async def test_authorized_cookies_from_safe_query_without_following_url(self):
        url = "https://passport.biligame.com/crossDomain?" + "&".join(
            f"{name}={value}" for name, value in COOKIES.items()
        )
        backend, transport = await self.new_backend(qr_body(), poll_body(0, url))
        challenge = await backend.begin_login()
        result = await backend.poll_login(challenge)
        self.assertEqual(result.credential, {"cookies": COOKIES})
        self.assertEqual([call[0] for call in transport.calls], [_GENERATE, _POLL])

    async def test_new_ticket_is_consumed_once_without_old_credentials(self):
        backend, transport = await self.new_backend(
            qr_body(), poll_body(0, TICKET_URL), (None, COOKIES)
        )
        challenge = await backend.begin_login()
        result = await backend.poll_login(challenge)
        self.assertEqual(result.credential["cookies"], COOKIES)
        self.assertEqual(
            [call[0] for call in transport.calls], [_GENERATE, _POLL, _EXCHANGE]
        )
        self.assertEqual(
            transport.calls[-1][1],
            {
                "ticket": "synthetic-ticket-123",
                "gourl": "https://www.bilibili.com/",
                "first_domain": ".bilibili.com",
            },
        )
        self.assertEqual(transport.calls[-1][2], {})
        self.assertEqual((await backend.poll_login(challenge)).status, "expired")
        self.assertEqual(len(transport.calls), 3)

    async def test_complete_poll_headers_do_not_consume_ticket(self):
        backend, transport = await self.new_backend(
            qr_body(), (poll_body(0, TICKET_URL), COOKIES)
        )
        challenge = await backend.begin_login()
        self.assertEqual(
            (await backend.poll_login(challenge)).credential["cookies"], COOKIES
        )
        self.assertEqual(len(transport.calls), 2)

    async def test_malformed_ticket_urls_cannot_be_requested(self):
        cases = [
            TICKET_URL.replace("https://", "http://"),
            TICKET_URL.replace("passport.biligame.com", "evil.test"),
            TICKET_URL.replace("passport.biligame.com", "user@passport.biligame.com"),
            TICKET_URL.replace("passport.biligame.com", "passport.biligame.com:443"),
            TICKET_URL + "&ticket=another-ticket",
            _EXCHANGE + "?ticket=",
            _EXCHANGE + "?ticket=short",
            _EXCHANGE + "?ticket=synthetic%0D%0Aticket",
            TICKET_URL + "&unexpected=value",
            TICKET_URL + "#fragment",
        ]
        for url in cases:
            with self.subTest(url=url):
                backend, transport = await self.new_backend(
                    qr_body(), poll_body(0, url)
                )
                challenge = await backend.begin_login()
                with self.assertRaises(AuthError):
                    await backend.poll_login(challenge)
                self.assertEqual(
                    (await backend.poll_login(challenge)).status, "expired"
                )
                self.assertEqual(len(transport.calls), 2)

    async def test_failed_ticket_exchange_requires_new_qr(self):
        for response in [
            TimeoutError("private-ticket-material"),
            (None, {}),
            (None, {"SESSDATA": "incomplete"}),
        ]:
            backend, transport = await self.new_backend(
                qr_body(), poll_body(0, TICKET_URL), response
            )
            challenge = await backend.begin_login()
            with self.assertRaises(AuthError) as raised:
                await backend.poll_login(challenge)
            self.assertEqual(raised.exception.diagnostic, "BILIBILI_TICKET")
            self.assertNotIn("synthetic-ticket", str(raised.exception))
            self.assertNotIn("private-ticket-material", str(raised.exception))
            self.assertEqual((await backend.poll_login(challenge)).status, "expired")
            self.assertEqual(len(transport.calls), 3)

    async def test_partial_poll_cookie_conflict_does_not_overwrite_identity(self):
        backend, _ = await self.new_backend(
            qr_body(),
            (poll_body(0, TICKET_URL), {"DedeUserID": "999"}),
            (None, COOKIES),
        )
        challenge = await backend.begin_login()
        with self.assertRaises(AuthError):
            await backend.poll_login(challenge)

    async def test_cancel_during_ticket_exchange_discards_result_and_never_repolls(
        self,
    ):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def exchange():
            entered.set()
            await release.wait()
            return None, COOKIES

        backend, transport = await self.new_backend(
            qr_body(), poll_body(0, TICKET_URL), exchange
        )
        challenge = await backend.begin_login()
        task = asyncio.create_task(backend.poll_login(challenge))
        await entered.wait()
        self.assertEqual((await backend.poll_login(challenge)).status, "expired")
        await backend.cancel_login(challenge)
        release.set()
        self.assertEqual((await task).status, "expired")
        self.assertEqual(len(transport.calls), 3)

    async def test_legacy_percent_encoding_uses_authoritative_header_unchanged(self):
        cookies = {**COOKIES, "SESSDATA": "synthetic%2Fsession%2Asignature%2Cend"}
        for query_value in [
            cookies["SESSDATA"],
            urlencode({"SESSDATA": cookies["SESSDATA"]}).split("=", 1)[1],
        ]:
            url = "https://passport.biligame.com/crossDomain?SESSDATA=" + query_value
            backend, _ = await self.new_backend(qr_body(), (poll_body(0, url), cookies))
            challenge = await backend.begin_login()
            self.assertEqual(
                (await backend.poll_login(challenge)).credential["cookies"], cookies
            )

    async def test_legacy_query_only_preserves_percent_and_plus_values(self):
        cookies = {**COOKIES, "SESSDATA": "synthetic%2Fsession%2Asignature+plus%2Cend"}
        url = "https://passport.biligame.com/crossDomain?" + "&".join(
            f"{k}={v}" for k, v in cookies.items()
        )
        backend, _ = await self.new_backend(qr_body(), poll_body(0, url))
        challenge = await backend.begin_login()
        self.assertEqual(
            (await backend.poll_login(challenge)).credential["cookies"], cookies
        )

    async def test_response_and_query_conflict_does_not_authorize(self):
        url = "https://passport.biligame.com/crossDomain?" + urlencode(
            {**COOKIES, "SESSDATA": "different"}
        )
        backend, _ = await self.new_backend(qr_body(), (poll_body(0, url), COOKIES))
        challenge = await backend.begin_login()
        with self.assertRaises(AuthError):
            await backend.poll_login(challenge)

    async def test_same_credentials_in_query_and_header_are_accepted(self):
        url = "https://passport.biligame.com/crossDomain?" + urlencode(COOKIES)
        backend, _ = await self.new_backend(qr_body(), (poll_body(0, url), COOKIES))
        challenge = await backend.begin_login()
        self.assertEqual(
            (await backend.poll_login(challenge)).credential["cookies"], COOKIES
        )

    async def test_body_cookie_and_incomplete_cookie_never_authorize(self):
        for body, cookies in [
            (poll_body(0, cookie="SESSDATA=untrusted"), {}),
            (poll_body(0), {"SESSDATA": "incomplete"}),
            (poll_body(0), {**COOKIES, "DedeUserID": "not-numeric"}),
        ]:
            backend, _ = await self.new_backend(qr_body(), (body, cookies))
            challenge = await backend.begin_login()
            with self.assertRaises(AuthError):
                await backend.poll_login(challenge)

    async def test_expired_response_cleans_challenge(self):
        backend, _ = await self.new_backend(qr_body(), poll_body(86038))
        challenge = await backend.begin_login()
        state = backend._challenges[challenge.challenge_id]
        self.assertEqual((await backend.poll_login(challenge)).status, "expired")
        self.assertEqual(state.key, "")
        self.assertFalse(backend._challenges)

    async def test_clock_expiry_and_cancel_never_poll_again(self):
        now = [0]
        backend, transport = await self.new_backend(qr_body(), clock=lambda: now[0])
        challenge = await backend.begin_login()
        now[0] = 180
        self.assertEqual((await backend.poll_login(challenge)).status, "expired")
        self.assertEqual(len(transport.calls), 1)
        await backend.cancel_login(challenge)

    async def test_unknown_challenge_and_unexpected_status(self):
        backend, transport = await self.new_backend(qr_body(), poll_body(99999))
        self.assertEqual(
            (await backend.poll_login(LoginChallenge("unknown", b""))).status, "expired"
        )
        challenge = await backend.begin_login()
        with self.assertRaises(AuthError):
            await backend.poll_login(challenge)
        self.assertEqual(len(transport.calls), 2)

    async def test_cancel_during_poll_discards_authorized_result(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def response():
            entered.set()
            await release.wait()
            return poll_body(0), COOKIES

        backend, _ = await self.new_backend(qr_body(), response)
        challenge = await backend.begin_login()
        task = asyncio.create_task(backend.poll_login(challenge))
        await entered.wait()
        await backend.cancel_login(challenge)
        release.set()
        self.assertEqual((await task).status, "expired")

    async def test_closed_backend_and_state_are_cleared(self):
        backend, _ = await self.new_backend(qr_body())
        challenge = await backend.begin_login()
        state = backend._challenges[challenge.challenge_id]
        await backend.close()
        self.assertEqual(state.key, "")
        self.assertFalse(backend._challenges)
        self.assertEqual((await backend.poll_login(challenge)).status, "expired")
        with self.assertRaises(AuthError):
            await backend.begin_login()

    async def test_active_challenges_are_bounded(self):
        backend, transport = await self.new_backend(*(qr_body() for _ in range(8)))
        for _ in range(8):
            await backend.begin_login()
        with self.assertRaises(AuthError) as raised:
            await backend.begin_login()
        self.assertEqual(raised.exception.kind, "busy")
        self.assertEqual(len(transport.calls), 8)

    async def test_request_cancellation_propagates(self):
        backend, _ = await self.new_backend(asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await backend.begin_login()


class CredentialTests(unittest.IsolatedAsyncioTestCase):
    async def test_nav_identity_conflict_cannot_be_saved_as_valid(self):
        transport = FakeTransport({"code": 0, "data": {"isLogin": True, "mid": 999999}})
        backend = BilibiliBackend(request=transport)
        self.addAsyncCleanup(backend.close)
        self.assertEqual(
            await backend.check_credentials({"cookies": COOKIES}), "expired"
        )

    async def test_nav_valid_expired_unknown_classification(self):
        cases = [
            ({"code": 0, "data": {"isLogin": True, "mid": 123456}}, "valid"),
            ({"code": 0, "data": {"isLogin": False}}, "expired"),
            ({"code": -101}, "expired"),
            ({"code": -352}, "unknown"),
            ({"code": 0}, "unknown"),
            ({"code": 0, "data": {"isLogin": True, "mid": True}}, "unknown"),
            ({"code": 0, "data": {"isLogin": True, "mid": 999}}, "expired"),
            ({"code": False, "data": {"isLogin": True, "mid": 123456}}, "unknown"),
            (TimeoutError("raw-cookie-secret"), "unknown"),
        ]
        for response, expected in cases:
            with self.subTest(expected=expected):
                transport = FakeTransport(response)
                backend = BilibiliBackend(request=transport)
                self.addAsyncCleanup(backend.close)
                self.assertEqual(
                    await backend.check_credentials({"cookies": COOKIES}), expected
                )
                self.assertEqual(transport.calls[0], (_NAV, {}, COOKIES))

    async def test_missing_session_has_no_network_and_no_fake_refresh(self):
        transport = FakeTransport()
        backend = BilibiliBackend(request=transport)
        self.addAsyncCleanup(backend.close)
        self.assertEqual(await backend.check_credentials({}), "expired")
        self.assertIsNone(await backend.refresh_credentials({"cookies": COOKIES}))
        self.assertFalse(transport.calls)
        self.assertEqual((await backend.resolve_audio(None, {})).status, "unavailable")


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def backend_with_response(self, response):
        backend = BilibiliBackend()
        backend._session = FakeSession(response)
        self.addAsyncCleanup(backend.close)
        return backend

    async def test_exact_endpoints_and_no_redirects_or_qr_cookie_forwarding(self):
        backend = await self.backend_with_response(FakeResponse(qr_body()))
        await backend._request(_GENERATE, cookies=COOKIES)
        self.assertEqual(backend._session.calls[0][1]["cookies"], {})
        self.assertIs(backend._session.calls[0][1]["allow_redirects"], False)
        with self.assertRaises(AuthError):
            await backend._request(
                "https://evil.test/x/web-interface/nav", cookies=COOKIES
            )
        self.assertEqual(len(backend._session.calls), 1)

    async def test_real_transport_parses_split_json_and_set_cookie(self):
        backend = await self.backend_with_response(
            FakeResponse(
                poll_body(0),
                cookies=[
                    f"{name}={value}; Domain=.bilibili.com; Path=/"
                    for name, value in COOKIES.items()
                ],
            )
        )
        body, received = await backend._request(_POLL, {"qrcode_key": KEY})
        self.assertEqual(body["data"]["code"], 0)
        self.assertEqual(received, COOKIES)

    async def test_ticket_exchange_accepts_empty_html_or_json_body_from_headers(self):
        for raw in [b"", b"<html>account response</html>", b'{"code":0}']:
            backend = await self.backend_with_response(
                FakeResponse(
                    raw=raw,
                    cookies=[
                        f"{name}={value}; Domain=.bilibili.com; Path=/"
                        for name, value in COOKIES.items()
                    ],
                )
            )
            body, received = await backend._request(
                _EXCHANGE, _ticket_params(TICKET_URL), cookies=COOKIES
            )
            self.assertEqual(body, {})
            self.assertEqual(received, COOKIES)
            options = backend._session.calls[0][1]
            self.assertEqual(options["cookies"], {})
            self.assertIs(options["allow_redirects"], False)

    async def test_ticket_exchange_accepts_exact_biligame_cookie_domains(self):
        for domain in [
            "biligame.com",
            ".biligame.com",
            "passport.biligame.com",
            ".passport.biligame.com",
        ]:
            backend = await self.backend_with_response(
                FakeResponse(
                    raw=b"",
                    cookies=[
                        f"{name}={value}; Domain={domain}; Path=/"
                        for name, value in COOKIES.items()
                    ],
                )
            )
            _, received = await backend._request(_EXCHANGE, _ticket_params(TICKET_URL))
            self.assertEqual(received, COOKIES)

    async def test_non_ticket_response_never_accepts_biligame_domains(self):
        for endpoint in [_GENERATE, _POLL, _NAV]:
            for domain in [
                ".biligame.com",
                ".passport.biligame.com",
                "evil.biligame.com",
                "biligame.com.evil.test",
            ]:
                backend = await self.backend_with_response(
                    FakeResponse(
                        {"code": 0},
                        cookies=[
                            f"{name}={value}; Domain={domain}; Path=/"
                            for name, value in COOKIES.items()
                        ],
                    )
                )
                _, received = await backend._request(endpoint)
                self.assertEqual(received, {})
        backend = await self.backend_with_response(
            FakeResponse(
                raw=b"",
                cookies=["SESSDATA=synthetic; Domain=evil.biligame.com; Path=/"],
            )
        )
        _, received = await backend._request(_EXCHANGE, _ticket_params(TICKET_URL))
        self.assertEqual(received, {})

    async def test_ticket_302_and_303_cookie_responses_are_read_without_following(self):
        for status in (302, 303):
            backend = await self.backend_with_response(
                FakeResponse(
                    status=status,
                    cookies=[
                        f"{name}={value}; Domain=.bilibili.com; Path=/"
                        for name, value in COOKIES.items()
                    ],
                )
            )
            _, received = await backend._request(_EXCHANGE, _ticket_params(TICKET_URL))
            self.assertEqual(received, COOKIES)
            self.assertEqual(len(backend._session.calls), 1)
            self.assertIs(backend._session.calls[0][1]["allow_redirects"], False)

    async def test_ticket_302_without_cookies_is_terminal_incomplete_login(self):
        backend = await self.backend_with_response(
            [
                FakeResponse(qr_body()),
                FakeResponse(poll_body(0, TICKET_URL)),
                FakeResponse(raw=b"", status=302),
            ]
        )
        challenge = await backend.begin_login()
        with self.assertRaises(AuthError) as raised:
            await backend.poll_login(challenge)
        self.assertEqual(raised.exception.diagnostic, "BILIBILI_TICKET")
        self.assertEqual((await backend.poll_login(challenge)).status, "expired")
        self.assertEqual(len(backend._session.calls), 3)

    async def test_ticket_external_location_is_never_requested_or_given_credentials(
        self,
    ):
        response = FakeResponse(
            raw=b"",
            status=302,
            cookies=[
                f"{name}={value}; Domain=.bilibili.com; Path=/"
                for name, value in COOKIES.items()
            ],
        )
        response.headers["Location"] = "https://evil.test/collect"
        backend = await self.backend_with_response(
            [
                FakeResponse(qr_body()),
                FakeResponse(poll_body(0, TICKET_URL)),
                response,
            ]
        )
        challenge = await backend.begin_login()
        self.assertEqual(
            (await backend.poll_login(challenge)).credential["cookies"], COOKIES
        )
        self.assertEqual(
            [url for url, _ in backend._session.calls], [_GENERATE, _POLL, _EXCHANGE]
        )
        self.assertTrue(
            all(options["cookies"] == {} for _, options in backend._session.calls)
        )
        self.assertTrue(
            all(
                options["allow_redirects"] is False
                for _, options in backend._session.calls
            )
        )

    async def test_redirect_exception_is_limited_to_ticket_endpoint_and_valid_statuses(
        self,
    ):
        for endpoint, statuses in [
            (_EXCHANGE, [301, 307, 308, 403, 500, 503]),
            (_GENERATE, [302, 303]),
            (_POLL, [302, 303]),
            (_NAV, [302, 303]),
        ]:
            for status in statuses:
                backend = await self.backend_with_response(
                    FakeResponse(
                        status=status,
                        cookies=[
                            f"{name}={value}; Domain=.bilibili.com; Path=/"
                            for name, value in COOKIES.items()
                        ],
                    )
                )
                with self.assertRaises(AuthError):
                    await backend._request(
                        endpoint,
                        _ticket_params(TICKET_URL) if endpoint == _EXCHANGE else {},
                    )
                self.assertEqual(len(backend._session.calls), 1)

    async def test_ticket_transport_rejects_noncanonical_params_without_network(self):
        backend = await self.backend_with_response(FakeResponse())
        for params in [
            {},
            {"ticket": "synthetic-ticket-123"},
            {**_ticket_params(TICKET_URL), "gourl": "https://evil.test/"},
        ]:
            with self.assertRaises(AuthError):
                await backend._request(_EXCHANGE, params)
        self.assertFalse(backend._session.calls)

    async def test_session_is_isolated_and_created_only_on_request(self):
        session = FakeSession(FakeResponse({"code": -101}))
        with patch(
            "astrbot_plugin_kook_music.music_auth.bilibili_backend.aiohttp.ClientSession",
            return_value=session,
        ) as factory:
            backend = BilibiliBackend()
            self.addAsyncCleanup(backend.close)
            factory.assert_not_called()
            await backend._request(_NAV, cookies=COOKIES)
            options = factory.call_args.kwargs
            self.assertFalse(options["trust_env"])
            self.assertEqual(type(options["cookie_jar"]).__name__, "DummyCookieJar")
            self.assertEqual(session.calls[0][1]["cookies"], COOKIES)

    async def test_non_json_oversized_and_non_200_are_safe_errors(self):
        for response in [
            FakeResponse(raw=b"secret-non-json"),
            FakeResponse(raw=b"x" * 100, status=302),
            FakeResponse(raw=b"[1]"),
        ]:
            backend = await self.backend_with_response(response)
            with self.assertRaises(AuthError) as raised:
                await backend._request(_POLL, {"qrcode_key": KEY})
            self.assertNotIn("secret-non-json", str(raised.exception))
            self.assertNotIn(KEY, str(raised.exception))
        backend = await self.backend_with_response(FakeResponse(raw=b"x" * 101))
        with patch(
            "astrbot_plugin_kook_music.music_auth.bilibili_backend._MAX_RESPONSE", 100
        ):
            with self.assertRaises(AuthError):
                await backend._request(_NAV)

    async def test_injected_transport_timeout_and_exception_do_not_leak(self):
        async def blocked(_url, _params, _cookies):
            await asyncio.Event().wait()

        backend = BilibiliBackend(request=blocked)
        backend._timeout = 0.01
        self.addAsyncCleanup(backend.close)
        with self.assertRaises(AuthError) as raised:
            await backend._request(_NAV, cookies=COOKIES)
        self.assertEqual(raised.exception.kind, "transient")
        backend._request_override = FakeTransport(RuntimeError(COOKIES["SESSDATA"]))
        with self.assertRaises(AuthError) as raised:
            await backend._request(_NAV, cookies=COOKIES)
        self.assertNotIn(COOKIES["SESSDATA"], str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)


if __name__ == "__main__":
    unittest.main()
