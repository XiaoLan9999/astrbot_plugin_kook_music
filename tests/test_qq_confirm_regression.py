"""QQ confirmation contract tests: real SDK and official transport, no network."""

import asyncio
import json
import sys
import unittest
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from multidict import CIMultiDict

PLUGINS_DIR = Path(__file__).resolve().parents[2]
if str(PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGINS_DIR))

from astrbot_plugin_kook_music.music_auth.qq_backend import (  # noqa: E402
    QQBackend,
    _load_sdk,
    _OfficialTransport,
)
from astrbot_plugin_kook_music.music_auth.types import AuthError  # noqa: E402

try:
    SDK = _load_sdk()
except AuthError:
    SDK = None

PNG = b"\x89PNG\r\n\x1a\nsynthetic-qr"
PRIVATE = "SYNTHETIC_SECRET_NEVER_LOG"
CREDENTIAL = {"musicid": 123456, "musickey": PRIVATE, "loginType": 2}
CALLBACK = (
    "ptuiCB('0','0','https://ssl.ptlogin2.graph.qq.com/check_sig?"
    f"uin=123456&service=ptqrlogin&ptsigx={PRIVATE}&s_url=https%3A%2F%2Fgraph.qq.com%2F')"
)


def response(body=b"", status=200, headers=None, cookies=None, delay=0):
    if isinstance(body, dict):
        body = json.dumps(body).encode()
    elif isinstance(body, str):
        body = body.encode()
    return SimpleNamespace(
        body=body,
        status=status,
        headers=headers or {},
        cookies=cookies or {},
        delay=delay,
    )


def exchange(code=0):
    return response(
        {
            "code": 0,
            "req_0": {
                "code": code,
                "data": CREDENTIAL if code == 0 else {"raw": PRIVATE},
            },
        }
    )


def oauth(location=None):
    return response(
        status=302,
        headers={
            "location": location
            or f"https://y.qq.com/portal/wx_redirect.html?state=state&code={PRIVATE}"
        },
    )


class SyntheticSession:
    def __init__(self):
        self.responses = []
        self.requests = []
        self.waiting = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.closed = False

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("Unexpected network request")
        item = self.responses.pop(0)
        session = self

        class Content:
            async def iter_chunked(self, size):
                if item.body == b"wait":
                    session.waiting.set()
                    try:
                        await asyncio.Future()
                    finally:
                        session.cancelled.set()
                yield item.body

        class Context:
            async def __aenter__(self):
                if item.delay:
                    await asyncio.sleep(item.delay)
                cookies = SimpleCookie()
                headers = CIMultiDict(item.headers)
                for key, value in item.cookies.items():
                    cookies[key] = value
                    cookies[key]["domain"] = (
                        ".graph.qq.com"
                        if urlsplit(url).hostname == "ssl.ptlogin2.graph.qq.com"
                        else ".qq.com"
                    )
                    cookies[key]["path"] = "/"
                    headers.add("Set-Cookie", cookies[key].OutputString())
                return SimpleNamespace(
                    status=item.status,
                    url=url,
                    headers=headers,
                    cookies=cookies,
                    content=Content(),
                )

            async def __aexit__(self, *args):
                return False

        return Context()

    async def close(self):
        self.closed = True


@unittest.skipIf(SDK is None, "Optional pinned QQMusicApi is not installed")
class QQConfirmRegressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session = SyntheticSession()
        self.transport = _OfficialTransport(
            session_factory=lambda **kwargs: self.session
        )
        self.client = SDK.Client(platform=SDK.Platform.WEB, transport=self.transport)
        self.backend = QQBackend(client_factory=lambda: self.client, sdk=SDK, timeout=1)

    async def asyncTearDown(self):
        await self.backend.close()

    async def begin(self):
        self.session.responses.append(response(PNG, cookies={"qrsig": PRIVATE}))
        return await self.backend.begin_login("qq")

    def success(self, callback=CALLBACK, location=None):
        return [
            response(callback),
            response(status=302, cookies={"p_skey": PRIVATE}),
            oauth(location),
            exchange(),
        ]

    def paths(self):
        return [url.rsplit("/", 1)[-1] for _, url, _ in self.session.requests]

    async def test_complete_real_sdk_and_official_transport_flow(self):
        challenge = await self.begin()
        self.session.responses.extend(self.success())
        result = await self.backend.poll_login(challenge)
        self.assertEqual(result.status, "authorized")
        self.assertEqual(result.credential["musicid"], 123456)
        self.assertEqual(result.credential["str_musicid"], "123456")
        self.assertNotIn(PRIVATE, repr(result))
        self.assertEqual(
            self.paths(),
            ["ptqrshow", "ptqrlogin", "check_sig", "authorize", "musicu.fcg"],
        )
        self.assertTrue(
            all(
                kwargs["allow_redirects"] is False
                for _, _, kwargs in self.session.requests
            )
        )

    async def test_callback_query_order_is_irrelevant(self):
        challenge = await self.begin()
        callback = f"ptuiCB('0','0','https://graph.qq.com/?service=x&s_url=x&ptsigx={PRIVATE}&uin=123456')"
        self.session.responses.extend(self.success(callback))
        self.assertEqual(
            (await self.backend.poll_login(challenge)).status, "authorized"
        )

    async def test_oauth_code_may_be_the_last_or_only_parameter(self):
        for query in [f"code={PRIVATE}", f"state=state&code={PRIVATE}"]:
            challenge = await self.begin()
            self.session.responses.extend(
                self.success(location=f"https://y.qq.com/?{query}")
            )
            self.assertEqual(
                (await self.backend.poll_login(challenge)).status, "authorized"
            )

    async def test_callback_rejects_untrusted_urls_and_duplicate_identity(self):
        urls = [
            "https://graph.qq.com.evil.test/?uin=123&ptsigx=x",
            "http://graph.qq.com/?uin=123&ptsigx=x",
            "https://user@graph.qq.com/?uin=123&ptsigx=x",
            "https://graph.qq.com:8443/?uin=123&ptsigx=x",
            "https://graph.qq.com/?uin=123&uin=456&ptsigx=x",
            "https://graph.qq.com/?uin=123&ptsigx=x&ptsigx=y",
            "https://graph.qq.com/?uin=123&ptsigx=x#fragment",
        ]
        for url in urls:
            with self.subTest(url=url):
                challenge = await self.begin()
                self.session.responses.append(response(f"ptuiCB('0','0','{url}')"))
                before = len(self.session.requests)
                with self.assertRaises(AuthError) as caught:
                    await self.backend.poll_login(challenge)
                self.assertTrue(caught.exception.diagnostic.startswith("QQ_CALLBACK:"))
                self.assertEqual(len(self.session.requests), before + 1)
                await self.backend.cancel_login(challenge)

    async def test_oauth_rejects_untrusted_redirect_or_duplicate_code(self):
        for location in [
            "https://evil.test/?code=x",
            "http://y.qq.com/?code=x",
            "https://y.qq.com/?code=x&code=y",
            "https://y.qq.com/?state=x&state=y&code=x",
            "https://y.qq.com/?code=x#fragment",
            "https://y.qq.com/?code=%0Ainvalid",
        ]:
            challenge = await self.begin()
            self.session.responses.extend(self.success(location=location)[:-1])
            with self.assertRaises(AuthError) as caught:
                await self.backend.poll_login(challenge)
            self.assertTrue(caught.exception.diagnostic.startswith("QQ_OAUTH:"))
            self.assertEqual(self.paths()[-1], "authorize")
            await self.backend.cancel_login(challenge)

    async def test_check_sig_retry_does_not_repeat_confirmation(self):
        challenge = await self.begin()
        self.session.responses.extend(
            [response(CALLBACK), response(PRIVATE, status=503)]
        )
        with self.assertRaises(AuthError) as caught:
            await self.backend.poll_login(challenge)
        self.assertEqual(caught.exception.diagnostic, "QQ_CHECK_SIG:HTTPError:HTTP503")
        state = self.backend._pending[challenge.challenge_id]
        self.assertEqual(state.uin, "123456")
        self.assertNotIn(PRIVATE, repr(state))
        self.session.responses.extend(self.success()[1:])
        self.assertEqual(
            (await self.backend.poll_login(challenge)).status, "authorized"
        )
        self.assertEqual(self.paths().count("ptqrlogin"), 1)
        self.assertEqual(self.paths().count("check_sig"), 2)

    async def test_oauth_retry_keeps_confirmed_signature_cookies(self):
        challenge = await self.begin()
        self.session.responses.extend(
            self.success()[:2] + [response(PRIVATE, status=502)]
        )
        with self.assertRaises(AuthError):
            await self.backend.poll_login(challenge)
        self.session.responses.extend([oauth(), exchange()])
        self.assertEqual(
            (await self.backend.poll_login(challenge)).status, "authorized"
        )
        self.assertEqual(self.paths().count("check_sig"), 1)
        self.assertEqual(self.paths().count("authorize"), 2)
        self.assertEqual(self.session.requests[-2][2]["cookies"]["p_skey"], PRIVATE)

    async def test_exchange_retry_reuses_code_without_reauthorizing(self):
        challenge = await self.begin()
        self.session.responses.extend(
            self.success()[:-1] + [response(PRIVATE, status=522)]
        )
        with self.assertRaises(AuthError) as caught:
            await self.backend.poll_login(challenge)
        self.assertEqual(caught.exception.diagnostic, "QQ_EXCHANGE:HTTPError:HTTP522")
        self.session.responses.append(exchange())
        self.assertEqual(
            (await self.backend.poll_login(challenge)).status, "authorized"
        )
        self.assertEqual(self.paths().count("authorize"), 1)
        self.assertEqual(self.paths().count("musicu.fcg"), 2)

    async def test_temporary_stage_timeout_preserves_prior_authorization(self):
        self.backend.timeout = 0.02
        challenge = await self.begin()
        self.session.responses.extend(self.success()[:-1] + [response(b"wait")])
        with self.assertRaises(AuthError) as caught:
            await self.backend.poll_login(challenge)
        self.assertTrue(
            caught.exception.diagnostic.startswith("QQ_EXCHANGE:TimeoutError")
        )
        self.assertTrue(self.session.cancelled.is_set())
        self.session.responses.append(exchange())
        self.assertEqual(
            (await self.backend.poll_login(challenge)).status, "authorized"
        )
        self.assertEqual(self.paths().count("authorize"), 1)

    async def test_each_stage_has_its_own_operation_budget(self):
        self.backend.timeout = 0.15
        challenge = await self.begin()
        steps = self.success()
        for step in steps:
            step.delay = 0.06
        self.session.responses.extend(steps)
        self.assertEqual(
            (await self.backend.poll_login(challenge)).status, "authorized"
        )

    async def test_unknown_callback_status_stops_before_authorization(self):
        challenge = await self.begin()
        self.session.responses.append(response("ptuiCB('12345','','')"))
        with self.assertRaises(AuthError) as caught:
            await self.backend.poll_login(challenge)
        self.assertEqual(caught.exception.kind, "protocol")
        self.assertEqual(self.paths(), ["ptqrshow", "ptqrlogin"])

    async def test_cancel_during_exchange_discards_pending_state(self):
        challenge = await self.begin()
        self.session.responses.extend(self.success()[:-1] + [response(b"wait")])
        poll = asyncio.create_task(self.backend.poll_login(challenge))
        await asyncio.wait_for(self.session.waiting.wait(), 1)
        await self.backend.cancel_login(challenge)
        self.assertTrue(poll.cancelled())
        self.assertTrue(self.session.cancelled.is_set())
        self.assertEqual(self.backend._pending, {})
        self.assertEqual((await self.backend.poll_login(challenge)).status, "expired")

    async def test_expired_deadline_never_resumes_cached_authorization(self):
        challenge = await self.begin()
        self.session.responses.extend(self.success()[:-1] + [response(status=503)])
        with self.assertRaises(AuthError):
            await self.backend.poll_login(challenge)
        self.backend._pending[challenge.challenge_id].deadline = 0
        before = len(self.session.requests)
        self.assertEqual((await self.backend.poll_login(challenge)).status, "expired")
        self.assertEqual(len(self.session.requests), before)
        self.assertEqual(self.backend._pending, {})

    async def test_safe_diagnostics_do_not_disclose_body_cookies_or_codes(self):
        challenge = await self.begin()
        self.session.responses.extend(self.success()[:-1] + [exchange(20279)])
        with self.assertLogs(
            "astrbot_plugin_kook_music.music_auth.qq_backend", level="WARNING"
        ) as logs:
            with self.assertRaises(AuthError) as caught:
                await self.backend.poll_login(challenge)
        self.assertEqual(caught.exception.kind, "denied")
        self.assertEqual(
            caught.exception.diagnostic,
            "QQ_EXCHANGE:LoginDeviceLimitError:HTTP200:CODE20279",
        )
        self.assertNotIn(PRIVATE, str(caught.exception))
        self.assertNotIn(PRIVATE, " ".join(logs.output))
        self.assertNotIn("https://", " ".join(logs.output))


if __name__ == "__main__":
    unittest.main()
