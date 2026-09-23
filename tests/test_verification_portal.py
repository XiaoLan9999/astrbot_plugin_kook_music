import asyncio
import json
import sys
import types
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_kook_music.music_auth.verification_portal import (
    PortalError,
    VerificationPortal,
)


class Request:
    def __init__(
        self,
        payload=None,
        *,
        method="POST",
        username="admin",
        origin="https://bot.example",
        headers=None,
    ):
        self.method, self.username = method, username
        self.raw = json.dumps(payload).encode()
        self.reads = 0
        self.headers = {
            "origin": origin,
            "content-type": "application/json",
            "content-length": str(len(self.raw)),
            **(headers or {}),
        }

    async def body(self):
        self.reads += 1
        return self.raw


class Context:
    def __init__(self):
        self.registered_web_apis = []

    def register_web_api(self, *entry):
        self.registered_web_apis.append(entry)


class PortalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 1000
        self.authorized = True
        self.callback = AsyncMock(return_value=(True, "safe result"))
        self.portal = VerificationPortal(
            "https://bot.example",
            self.callback,
            lambda bot, user: self.authorized and (bot, user) == ("bot", "123"),
            clock=lambda: self.now,
        )

    async def asyncTearDown(self):
        await self.portal.close()

    def ticket(self, portal=None):
        url = (portal or self.portal).issue_link("bot", "123")
        return parse_qs(urlsplit(url).fragment)["ticket"][0]

    async def begin(self, **kwargs):
        response = await self.portal.handle(
            Request({"action": "begin", "ticket": self.ticket()}, **kwargs)
        )
        self.assertEqual(response.status, 200)
        return response.payload

    @staticmethod
    def submission(session, **changes):
        return {
            "action": "import",
            "session": session["session"],
            "csrf": session["csrf"],
            "cookie": "MUSIC_U=synthetic-secret; __csrf=csrf-value",
            **changes,
        }

    async def test_success_only_forwards_to_injected_first_party_callback(self):
        session = await self.begin()
        result = await self.portal.handle(Request(self.submission(session)))
        self.assertEqual(result.status, 200)
        self.assertTrue(result.payload["ok"])
        self.callback.assert_awaited_once_with(
            "bot", "123", "MUSIC_U=synthetic-secret; __csrf=csrf-value"
        )
        self.assertNotIn("synthetic-secret", json.dumps(result.payload))
        self.assertNotIn("safe result", json.dumps(result.payload))

    def test_link_secret_is_fragment_not_query_or_path(self):
        url = self.portal.issue_link("bot", "123")
        parsed = urlsplit(url)
        self.assertEqual(parsed.query, "")
        self.assertEqual(
            parsed.path, "/api/plug/astrbot_plugin_kook_music/account/netease"
        )
        self.assertTrue(parsed.fragment.startswith("ticket="))
        ticket = parse_qs(parsed.fragment)["ticket"][0]
        self.assertNotIn(ticket, repr(self.portal._tickets))

    async def test_viewing_page_does_not_consume_ticket(self):
        ticket = self.ticket()
        page = await self.portal.handle(Request(method="GET"))
        self.assertEqual(page.status, 200)
        self.assertEqual(len(self.portal._tickets), 1)
        self.assertNotIn(ticket, page.payload)

    async def test_get_requires_authenticated_dashboard(self):
        result = await self.portal.handle(Request(method="GET", username=None))
        self.assertEqual(result.status, 401)
        self.assertNotIn("<html", str(result.payload))

    async def test_post_requires_authenticated_dashboard(self):
        req = Request({"action": "begin", "ticket": self.ticket()}, username="")
        self.assertEqual((await self.portal.handle(req)).status, 401)
        self.assertEqual(req.reads, 0)

    async def test_untrusted_origin_and_missing_origin_rejected_before_body_read(self):
        ticket = self.ticket()
        for origin in (
            None,
            "null",
            "http://bot.example",
            "https://bot.example.evil",
            "https://bot.example:8443",
            "https://other.example",
        ):
            with self.subTest(origin=origin):
                req = Request({"action": "begin", "ticket": ticket}, origin=origin)
                self.assertEqual((await self.portal.handle(req)).status, 403)
                self.assertEqual(req.reads, 0)
        self.assertEqual(len(self.portal._tickets), 1)

    async def test_cross_site_fetch_rejected_even_if_origin_claim_matches(self):
        req = Request(
            {"action": "begin", "ticket": self.ticket()},
            headers={"sec-fetch-site": "cross-site"},
        )
        self.assertEqual((await self.portal.handle(req)).status, 403)

    async def test_form_and_simple_requests_rejected(self):
        for content_type in (
            "text/plain",
            "application/x-www-form-urlencoded",
            "multipart/form-data",
        ):
            req = Request({}, headers={"content-type": content_type})
            self.assertEqual((await self.portal.handle(req)).status, 415)
            self.assertEqual(req.reads, 0)

    async def test_ticket_is_one_use(self):
        value = {"action": "begin", "ticket": self.ticket()}
        self.assertEqual((await self.portal.handle(Request(value))).status, 200)
        self.assertEqual((await self.portal.handle(Request(value))).status, 403)

    async def test_ticket_expiry_is_monotonic_and_not_extended_by_opening(self):
        value = {"action": "begin", "ticket": self.ticket()}
        self.now += 599
        await self.portal.handle(Request(method="GET"))
        self.now += 1
        self.assertEqual((await self.portal.handle(Request(value))).status, 403)

    async def test_import_session_uses_original_expiry(self):
        token = self.ticket()
        self.now += 590
        result = await self.portal.handle(Request({"action": "begin", "ticket": token}))
        self.assertEqual(result.payload["expires_in"], 10)
        self.now += 10
        self.assertEqual(
            (await self.portal.handle(Request(self.submission(result.payload)))).status,
            403,
        )
        self.callback.assert_not_awaited()

    async def test_slow_request_cannot_extend_ticket_expiry(self):
        req = Request({"action": "begin", "ticket": self.ticket()})

        async def slow_body():
            self.now += 600
            return req.raw

        req.body = slow_body
        self.assertEqual((await self.portal.handle(req)).status, 403)

    async def test_slow_request_cannot_extend_session_expiry(self):
        req = Request(self.submission(await self.begin()))

        async def slow_body():
            self.now += 600
            return req.raw

        req.body = slow_body
        self.assertEqual((await self.portal.handle(req)).status, 403)
        self.callback.assert_not_awaited()

    async def test_new_link_revokes_old_ticket_and_session(self):
        old_ticket = self.ticket()
        new_ticket = self.ticket()
        self.assertEqual(
            (
                await self.portal.handle(
                    Request({"action": "begin", "ticket": old_ticket})
                )
            ).status,
            403,
        )
        begin = await self.portal.handle(
            Request({"action": "begin", "ticket": new_ticket})
        )
        self.ticket()
        self.assertEqual(
            (await self.portal.handle(Request(self.submission(begin.payload)))).status,
            403,
        )

    async def test_explicit_revoke_invalidates_ticket_and_session(self):
        session = await self.begin()
        self.portal.revoke("bot", "123")
        self.assertEqual(
            (await self.portal.handle(Request(self.submission(session)))).status, 403
        )

    async def test_admin_removal_revokes_pending_session(self):
        session = await self.begin()
        self.authorized = False
        self.assertEqual(
            (await self.portal.handle(Request(self.submission(session)))).status, 403
        )
        self.callback.assert_not_awaited()

    async def test_auth_callback_failure_is_denied_not_leaked(self):
        self.portal.authorized = lambda *args: (_ for _ in ()).throw(
            RuntimeError("private")
        )
        with self.assertRaises(PortalError) as caught:
            self.ticket()
        self.assertNotIn("private", str(caught.exception))

    async def test_csrf_and_dashboard_identity_are_both_required(self):
        session = await self.begin()
        wrong_csrf = self.submission(session, csrf="A" * 43)
        self.assertEqual((await self.portal.handle(Request(wrong_csrf))).status, 403)
        self.assertEqual(
            (
                await self.portal.handle(
                    Request(self.submission(session), username="other-admin")
                )
            ).status,
            403,
        )
        self.assertEqual(
            (await self.portal.handle(Request(self.submission(session)))).status, 200
        )

    async def test_session_is_consumed_before_import_and_cannot_race(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def slow(*_):
            started.set()
            await release.wait()
            return True, "ok"

        self.callback.side_effect = slow
        session = await self.begin()
        first = asyncio.create_task(
            self.portal.handle(Request(self.submission(session)))
        )
        await started.wait()
        second = await self.portal.handle(Request(self.submission(session)))
        self.assertEqual(second.status, 403)
        release.set()
        self.assertEqual((await first).status, 200)
        self.callback.assert_awaited_once()

    async def test_callback_exception_never_leaks_cookie(self):
        self.callback.side_effect = RuntimeError("MUSIC_U=synthetic-secret")
        session = await self.begin()
        result = await self.portal.handle(Request(self.submission(session)))
        self.assertEqual(result.status, 400)
        self.assertNotIn("synthetic-secret", json.dumps(result.payload))
        self.assertEqual(
            (await self.portal.handle(Request(self.submission(session)))).status, 403
        )

    async def test_callback_message_never_reflected_even_if_unsafe(self):
        for success in (False, True):
            self.callback.return_value = (success, "MUSIC_U=synthetic-secret")
            session = await self.begin()
            result = await self.portal.handle(Request(self.submission(session)))
            self.assertNotIn("synthetic-secret", json.dumps(result.payload))

    async def test_invalid_callback_contract_fails_closed(self):
        self.callback.return_value = (1, "not a real bool")
        result = await self.portal.handle(Request(self.submission(await self.begin())))
        self.assertEqual(result.status, 400)

    async def test_cookie_shape_and_length_checks(self):
        session = await self.begin()
        for cookie in (
            None,
            {},
            "",
            " ",
            "a" * 8193,
            "秘密" * 1500,
            "MUSIC_U=x\r\nHost:evil",
            "MUSIC_U=\x00",
            "MUSIC_U=\ud800",
        ):
            with self.subTest(cookie_type=type(cookie).__name__):
                result = await self.portal.handle(
                    Request(self.submission(session, cookie=cookie))
                )
                self.assertEqual(result.status, 400)
        self.callback.assert_not_awaited()

    async def test_large_declared_body_rejected_before_read(self):
        req = Request({}, headers={"content-length": "16385"})
        self.assertEqual((await self.portal.handle(req)).status, 400)
        self.assertEqual(req.reads, 0)

    async def test_actual_large_body_rejected_even_with_lying_length(self):
        req = Request({}, headers={"content-length": "2"})
        req.raw = b"x" * 16385
        self.assertEqual((await self.portal.handle(req)).status, 400)

    async def test_streamed_chunked_body_is_bounded_without_content_length(self):
        chunks_read = []

        async def stream():
            for index in range(10):
                chunks_read.append(index)
                yield b"x" * 4096

        req = Request({})
        req.headers.pop("content-length")
        req._request = types.SimpleNamespace(stream=stream)
        self.assertEqual((await self.portal.handle(req)).status, 400)
        self.assertEqual(chunks_read, [0, 1, 2, 3, 4])

    async def test_streamed_normal_body_without_length_works(self):
        req = Request({"action": "begin", "ticket": self.ticket()})

        async def stream():
            yield req.raw[:20]
            yield req.raw[20:]

        req.headers.pop("content-length")
        req._request = types.SimpleNamespace(stream=stream)
        self.assertEqual((await self.portal.handle(req)).status, 200)
        self.assertEqual(req.reads, 0)

    async def test_missing_size_on_legacy_body_rejected_before_read(self):
        req = Request({})
        req.headers.pop("content-length")
        self.assertEqual((await self.portal.handle(req)).status, 400)
        self.assertEqual(req.reads, 0)

    async def test_invalid_json_or_unexpected_fields_fail_closed(self):
        for raw in (
            b"bad",
            b"[]",
            b"null",
            b'{"action":"unknown"}',
            b'{"action":"begin","ticket":"bad","callback":"https://evil.example"}',
        ):
            req = Request({})
            req.raw = raw
            req.headers["content-length"] = str(len(raw))
            self.assertEqual((await self.portal.handle(req)).status, 400)

    async def test_deep_json_fails_without_unhandled_parser_error(self):
        req = Request({})
        req.raw = b"[" * 4000 + b"]" * 4000
        req.headers["content-length"] = str(len(req.raw))
        self.assertEqual((await self.portal.handle(req)).status, 400)

    async def test_body_secrets_do_not_appear_in_reply_repr(self):
        session = await self.begin()
        response = await self.portal.handle(Request(self.submission(session)))
        self.assertNotIn("synthetic-secret", repr(response))

    async def test_no_ip_binding(self):
        session = await self.begin()
        req = Request(self.submission(session))
        req.client_host = "198.51.100.5"
        self.assertEqual((await self.portal.handle(req)).status, 200)

    async def test_get_has_strict_headers_and_no_external_assets_or_secret_storage(
        self,
    ):
        result = await self.portal.handle(Request(method="GET"))
        for header, value in (
            ("Cache-Control", "no-store"),
            ("Referrer-Policy", "no-referrer"),
            ("X-Frame-Options", "DENY"),
            ("Content-Security-Policy", "frame-ancestors 'none'"),
        ):
            self.assertIn(value, result.headers[header])
        self.assertIn("connect-src 'self'", result.headers["Content-Security-Policy"])
        for value in (
            "localStorage",
            "sessionStorage",
            "document.cookie",
            "<script src=",
            "<img",
            "innerHTML",
        ):
            self.assertNotIn(value, result.payload)
        self.assertIn("history.replaceState", result.payload)
        self.assertIn("music.163.com", result.payload)
        self.assertIn("不是网易云验证码页面", result.payload)

    async def test_rate_limit_requests_is_bounded(self):
        for _ in range(20):
            self.assertEqual(
                (await self.portal.handle(Request(method="GET"))).status, 200
            )
        self.assertEqual((await self.portal.handle(Request(method="GET"))).status, 429)
        self.now += 61
        self.assertEqual((await self.portal.handle(Request(method="GET"))).status, 200)

    def test_issue_rate_limit(self):
        for _ in range(5):
            self.ticket()
        with self.assertRaises(PortalError):
            self.ticket()
        self.now += 61
        self.ticket()

    async def test_close_unregisters_only_own_route_and_invalidates_tickets(self):
        context = Context()
        self.portal.register(context)
        self.portal.register(context)
        self.assertEqual(len(context.registered_web_apis), 1)
        foreign = ("/other", object(), ["GET"], "foreign")
        context.registered_web_apis.append(foreign)
        self.ticket()
        await self.portal.close()
        self.assertEqual(context.registered_web_apis, [foreign])
        self.assertFalse(self.portal._tickets)
        self.assertEqual((await self.portal.handle(Request(method="GET"))).status, 410)
        with self.assertRaises(PortalError):
            self.ticket()

    async def test_route_collision_does_not_replace_foreign_handler(self):
        context = Context()
        foreign = (
            "/astrbot_plugin_kook_music/account/netease",
            object(),
            ["POST"],
            "foreign",
        )
        context.registered_web_apis.append(foreign)
        with self.assertRaises(PortalError):
            self.portal.register(context)
        self.assertEqual(context.registered_web_apis, [foreign])

    async def test_close_preserves_newer_handler_that_replaced_own_route(self):
        context = Context()
        self.portal.register(context)
        foreign = (
            context.registered_web_apis[0][0],
            object(),
            ["GET", "POST"],
            "foreign",
        )
        context.registered_web_apis[:] = [foreign]
        await self.portal.close()
        self.assertEqual(context.registered_web_apis, [foreign])

    async def test_close_cancels_inflight_import(self):
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def slow(*_):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.callback.side_effect = slow
        session = await self.begin()
        task = asyncio.create_task(
            self.portal.handle(Request(self.submission(session)))
        )
        await started.wait()
        await self.portal.close()
        self.assertTrue(cancelled.is_set())
        with self.assertRaises(asyncio.CancelledError):
            await task


class BaseUrlTests(unittest.TestCase):
    def portal(self, url):
        return VerificationPortal(url, AsyncMock(), lambda *_: True)

    def test_https_or_literal_loopback_only(self):
        for url in (
            "https://bot.example",
            "https://bot.example/prefix/",
            "http://127.0.0.1:6185",
            "http://[::1]:6185",
            "https://192.0.2.1:8443",
        ):
            with self.subTest(url=url):
                self.assertTrue(self.portal(url).page_url.endswith("/netease"))

    def test_unsafe_or_ambiguous_base_urls_rejected(self):
        for url in (
            "",
            None,
            "http://bot.example",
            "http://localhost:6185",
            "http://127.0.0.1.evil:6185",
            "https://user:secret@bot.example",
            "https://bot.example/?secret=x",
            "https://bot.example/#fragment",
            "https://bot.example/../other",
            "https://bot.example/%2Fother",
            "https://bot.example:bad",
            "https://bot.example:0",
            "https://bot.example:65536",
            "https://bot.example/\n",
            "https://bot.example\\@evil",
            "file:///tmp/page",
            "javascript:alert(1)",
        ):
            with self.subTest(url_type=type(url).__name__):
                with self.assertRaises(PortalError) as caught:
                    self.portal(url)
                self.assertNotIn("secret", str(caught.exception))

    def test_standard_ports_and_host_case_normalized_for_origin(self):
        portal = self.portal("https://BOT.example:443/reverse-proxy/")
        self.assertEqual(portal.origin, "https://bot.example")
        self.assertEqual(portal.base_url, "https://bot.example/reverse-proxy")


if __name__ == "__main__":
    unittest.main()
