import asyncio
import base64
import json
import sys
import unittest
from http.cookies import SimpleCookie
from pathlib import Path
from unittest.mock import AsyncMock, patch

PLUGINS_DIR = Path(__file__).resolve().parents[2]
if str(PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGINS_DIR))

from astrbot_plugin_kook_music.music.model import Song  # noqa: E402
from astrbot_plugin_kook_music.music_auth.netease_backend import (  # noqa: E402
    NeteaseBackend,
    _audio_url,
    _cookies,
    _weapi,
)
from astrbot_plugin_kook_music.music_auth.types import AuthError  # noqa: E402

COOKIE = {"MUSIC_U": "synthetic-test-account-secret", "__csrf": "fake-csrf"}
SONG = Song(id="12345", platform="netease")
KEY = "synthetic-qr-key-12345"


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def __call__(self, path, payload, cookies):
        self.calls.append((path, payload, dict(cookies)))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response if isinstance(response, tuple) else (response, {})


class FakeContent:
    def __init__(self, data):
        self.data = data

    async def iter_chunked(self, _size):
        # Force split JSON tokens to catch read(n) being mistaken for read-all.
        for offset in range(0, len(self.data), 5):
            yield self.data[offset : offset + 5]


class FakeResponse:
    def __init__(self, data=None, status=200, raw=None):
        self.status = status
        self.content = FakeContent(
            raw if raw is not None else json.dumps(data).encode()
        )
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

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def row(**kwargs):
    return {
        "code": 200,
        "data": [
            {
                "id": 12345,
                "code": 200,
                "url": "https://m701.music.126.net/file.mp3",
                "freeTrialInfo": None,
                **kwargs,
            }
        ],
    }


class ProtocolTests(unittest.TestCase):
    def test_weapi_encryption_round_trip(self):
        from cryptography.hazmat.primitives import padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        def decrypt(value, key):
            decryptor = Cipher(
                algorithms.AES(key), modes.CBC(b"0102030405060708")
            ).decryptor()
            padded = decryptor.update(base64.b64decode(value)) + decryptor.finalize()
            unpadder = padding.PKCS7(128).unpadder()
            return unpadder.update(padded) + unpadder.finalize()

        data = {"key": KEY, "csrf_token": "fake-csrf", "type": 3}
        encoded = _weapi(data, secret=b"0123456789abcdef")
        inner = decrypt(encoded["params"], b"0123456789abcdef")
        self.assertEqual(json.loads(decrypt(inner, b"0CoJUm6Qyw8W8jud")), data)
        self.assertEqual(len(encoded["encSecKey"]), 256)
        self.assertNotIn(KEY, str(encoded))

    def test_cookie_whitelist_and_header_safety(self):
        value = {
            **COOKIE,
            "token": "other-platform",
            "NMTID": "x\r\nInjected: yes",
            "_ntes_nuid": "a;b",
            "MUSIC_A": "a\\b",
        }
        self.assertEqual(_cookies(value), COOKIE)
        self.assertEqual(_cookies("MUSIC_U=fake"), {})

    def test_cdn_allowlist(self):
        valid = "http://m701.music.126.net/a%20b.mp3?auth=test"
        self.assertEqual(_audio_url(valid), valid.replace("http:", "https:"))
        for url in [
            "https://music.126.net.evil.test/a.mp3",
            "https://evil.test/a.mp3",
            "https://m701.music.126.net@evil.test/a.mp3",
            "https://user:pass@m701.music.126.net/a",
            "https://m701.music.126.net:8080/a",
            "https://127.0.0.1/a",
            "file:///secret",
            "ftp://m701.music.126.net/a",
            "https://m701.music.126.net/a#fragment",
            "https://m701.music.126.net\\@evil.test/a",
            "https://m701.music.126.net/a\n",
            "https://m701.music.126.net./a",
            "https://m701.music.126.net",
        ]:
            with self.subTest(url=url):
                self.assertEqual(_audio_url(url), "")


class LoginTests(unittest.IsolatedAsyncioTestCase):
    async def new_backend(self, *responses, clock=None):
        transport = FakeTransport(*responses)
        options = {"request": transport}
        if clock is not None:
            options["clock"] = clock
        backend = NeteaseBackend(**options)
        self.addAsyncCleanup(backend.close)
        return backend, transport

    async def test_qr_created_locally_without_leaking_key_in_repr(self):
        backend, transport = await self.new_backend({"code": 200, "unikey": KEY})
        challenge = await backend.begin_login()
        self.assertTrue(challenge.qr_bytes.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertNotIn(KEY, repr(challenge))
        self.assertNotEqual(challenge.challenge_id, KEY)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(transport.calls[0][2], {})
        self.assertEqual(transport.calls[0][1]["type"], 3)

    async def test_nested_qr_data(self):
        backend, _ = await self.new_backend({"code": 200, "data": {"unikey": KEY}})
        self.assertTrue((await backend.begin_login()).qr_bytes)

    async def test_wrong_login_method_rejected_without_network(self):
        backend, transport = await self.new_backend()
        with self.assertRaises(AuthError):
            await backend.begin_login("wechat")
        self.assertFalse(transport.calls)

    async def test_invalid_key_not_rendered_as_qr(self):
        backend, _ = await self.new_backend(
            {"code": 200, "unikey": "https://evil.test/scan"}
        )
        with self.assertRaises(AuthError):
            await backend.begin_login()
        self.assertFalse(backend._challenges)

    async def test_request_cancellation_propagates(self):
        backend, _ = await self.new_backend(asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await backend.begin_login()

    async def test_poll_pending_scanned_authorized(self):
        backend, transport = await self.new_backend(
            {"code": 200, "unikey": KEY},
            {"code": 801},
            {"code": 802},
            ({"code": 803}, COOKIE),
        )
        challenge = await backend.begin_login()
        self.assertEqual((await backend.poll_login(challenge)).status, "pending")
        self.assertEqual((await backend.poll_login(challenge)).status, "scanned")
        result = await backend.poll_login(challenge)
        self.assertEqual(result.status, "authorized")
        self.assertEqual(result.credential, {"cookies": COOKIE})
        self.assertNotIn(COOKIE["MUSIC_U"], repr(result))
        self.assertEqual((await backend.poll_login(challenge)).status, "expired")
        self.assertEqual(len(transport.calls), 4)

    async def test_body_cookie_not_accepted(self):
        backend, _ = await self.new_backend(
            {"code": 200, "unikey": KEY},
            {"code": 803, "cookie": "MUSIC_U=untrusted"},
        )
        challenge = await backend.begin_login()
        with self.assertRaises(AuthError):
            await backend.poll_login(challenge)

    async def test_separate_challenges_do_not_share_cookies(self):
        backend, transport = await self.new_backend(
            ({"code": 200, "unikey": KEY}, {"NMTID": "first-device"}),
            ({"code": 200, "unikey": KEY + "b"}, {"NMTID": "second-device"}),
            {"code": 801},
            {"code": 801},
        )
        first, second = await backend.begin_login(), await backend.begin_login()
        await backend.poll_login(first)
        await backend.poll_login(second)
        self.assertEqual(transport.calls[2][2], {"NMTID": "first-device"})
        self.assertEqual(transport.calls[3][2], {"NMTID": "second-device"})

    async def test_deadline_not_extended_by_poll(self):
        now = [100]
        backend, transport = await self.new_backend(
            {"code": 200, "unikey": KEY},
            {"code": 801},
            clock=lambda: now[0],
        )
        challenge = await backend.begin_login()
        now[0] = 279
        self.assertEqual((await backend.poll_login(challenge)).status, "pending")
        now[0] = 280
        self.assertEqual((await backend.poll_login(challenge)).status, "expired")
        self.assertEqual(len(transport.calls), 2)

    async def test_cancelled_poll_does_not_publish_late_credential(self):
        started, finish = asyncio.Event(), asyncio.Event()

        async def request(path, payload, cookies):
            if path.endswith("unikey"):
                return {"code": 200, "unikey": KEY}, {}
            started.set()
            await finish.wait()
            return {"code": 803}, COOKIE

        backend = NeteaseBackend(request=request)
        self.addAsyncCleanup(backend.close)
        challenge = await backend.begin_login()
        task = asyncio.create_task(backend.poll_login(challenge))
        await started.wait()
        await backend.cancel_login(challenge)
        finish.set()
        result = await task
        self.assertEqual(result.status, "expired")
        self.assertIsNone(result.credential)

    async def test_close_cancels_challenges(self):
        backend, transport = await self.new_backend({"code": 200, "unikey": KEY})
        challenge = await backend.begin_login()
        await backend.close()
        self.assertEqual((await backend.poll_login(challenge)).status, "expired")
        self.assertEqual(len(transport.calls), 1)

    async def test_server_expiry_and_denial(self):
        for code, expected in [(800, "expired"), (804, "denied")]:
            backend, _ = await self.new_backend(
                {"code": 200, "unikey": KEY}, {"code": code}
            )
            result = await backend.poll_login(await backend.begin_login())
            self.assertEqual(result.status, expected)

    async def test_transient_poll_keeps_challenge(self):
        backend, _ = await self.new_backend(
            {"code": 200, "unikey": KEY},
            asyncio.TimeoutError("secret"),
            {"code": 801},
        )
        challenge = await backend.begin_login()
        with self.assertRaises(AuthError) as error:
            await backend.poll_login(challenge)
        self.assertNotIn("secret", str(error.exception))
        self.assertEqual((await backend.poll_login(challenge)).status, "pending")


class CredentialAudioTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, operation, response):
        backend = NeteaseBackend(request=FakeTransport(response))
        self.addAsyncCleanup(backend.close)
        return await getattr(backend, operation)({"cookies": COOKIE})

    async def test_valid_account(self):
        self.assertEqual(
            await self.call("check_credentials", {"code": 200, "account": {"id": 123}}),
            "valid",
        )

    async def test_failed_check_logs_only_code_and_boolean_shape(self):
        payload = {
            "code": 8821,
            "account": {"id": "private-account-not-an-id"},
            "profile": {"nickname": "private-nickname"},
            "message": "private-server-response",
        }
        with self.assertLogs(
            "astrbot_plugin_kook_music.music_auth.netease_backend", level="WARNING"
        ) as logs:
            self.assertEqual(await self.call("check_credentials", payload), "unknown")
        output = " ".join(logs.output)
        self.assertIn("NETEASE_CHECK:HTTP200:CODE8821", output)
        self.assertIn("ACCOUNT_VALID0 PROFILE_PRESENT1", output)
        self.assertNotIn("private-", output)
        self.assertNotIn(COOKIE["MUSIC_U"], output)

    async def test_missing_account_expired(self):
        self.assertEqual(
            await self.call(
                "check_credentials", {"code": 200, "account": None, "profile": None}
            ),
            "expired",
        )

    async def test_explicit_login_error_expired(self):
        self.assertEqual(await self.call("check_credentials", {"code": 301}), "expired")

    async def test_network_error_unknown_not_expired(self):
        self.assertEqual(
            await self.call("check_credentials", asyncio.TimeoutError("secret")),
            "unknown",
        )

    async def test_rate_limit_unknown_not_expired(self):
        self.assertEqual(await self.call("check_credentials", {"code": 429}), "unknown")

    async def test_malformed_status_unknown(self):
        self.assertEqual(await self.call("check_credentials", {"code": 200}), "unknown")

    async def test_malformed_account_id_unknown(self):
        self.assertEqual(
            await self.call(
                "check_credentials", {"code": 200, "account": {"id": "\u00b2"}}
            ),
            "unknown",
        )

    async def test_no_saved_cookie_expires_without_network(self):
        transport = FakeTransport()
        backend = NeteaseBackend(request=transport)
        self.addAsyncCleanup(backend.close)
        self.assertEqual(await backend.check_credentials({}), "expired")
        self.assertEqual((await backend.resolve_audio(SONG, {})).status, "expired")
        self.assertIsNone(await backend.refresh_credentials({}))
        self.assertFalse(transport.calls)

    async def test_refresh_returns_copy_without_mutation(self):
        old = {"cookies": dict(COOKIE)}
        backend = NeteaseBackend(
            request=FakeTransport(({"code": 200}, {"MUSIC_U": "renewed"}))
        )
        self.addAsyncCleanup(backend.close)
        refreshed = await backend.refresh_credentials(old)
        self.assertEqual(refreshed["cookies"]["MUSIC_U"], "renewed")
        self.assertEqual(old["cookies"], COOKIE)

    async def test_refresh_timeout_returns_none(self):
        self.assertIsNone(
            await self.call("refresh_credentials", asyncio.TimeoutError("secret"))
        )

    async def resolve(self, response, song=SONG):
        transport = FakeTransport(response)
        backend = NeteaseBackend(request=transport)
        self.addAsyncCleanup(backend.close)
        result = await backend.resolve_audio(song, {"cookies": COOKIE})
        return result, transport

    async def test_same_id_full_song_resolved(self):
        result, transport = await self.resolve(row())
        self.assertEqual(result.status, "resolved")
        self.assertEqual(json.loads(transport.calls[0][1]["ids"]), [12345])
        self.assertEqual(transport.calls[0][2], COOKIE)

    async def test_different_id_not_fuzzy_matched(self):
        result, _ = await self.resolve(row(id=54321))
        self.assertEqual(result.status, "unavailable")

    async def test_trial_excerpt_rejected(self):
        result, _ = await self.resolve(row(freeTrialInfo={"start": 10, "end": 40}))
        self.assertEqual(result.status, "unavailable")
        self.assertFalse(result.url)

    async def test_null_url_does_not_expire_credential(self):
        result, _ = await self.resolve(row(url=None))
        self.assertEqual(result.status, "unavailable")

    async def test_audio_timeout_transient_not_expired(self):
        result, _ = await self.resolve(asyncio.TimeoutError("fake-cookie-leak"))
        self.assertEqual(result.status, "transient")
        self.assertNotIn("fake-cookie-leak", repr(result))

    async def test_audio_explicit_login_required(self):
        result, _ = await self.resolve({"code": 301})
        self.assertEqual(result.status, "expired")

    async def test_audio_untrusted_host_rejected(self):
        result, _ = await self.resolve(row(url="https://thirdparty.example/audio.mp3"))
        self.assertEqual(result.status, "unavailable")

    async def test_malformed_audio_code_unavailable(self):
        result, _ = await self.resolve(row(code={"unexpected": 1}))
        self.assertEqual(result.status, "unavailable")

    async def test_null_data_transient(self):
        result, _ = await self.resolve({"code": 200, "data": None})
        self.assertEqual(result.status, "transient")

    async def test_other_platform_does_not_receive_cookie(self):
        result, transport = await self.resolve(row(), Song(id="12345", platform="qq"))
        self.assertEqual(result.status, "unavailable")
        self.assertFalse(transport.calls)

    async def test_invalid_song_id_never_requested(self):
        result, transport = await self.resolve(
            row(), Song(id="123,456", platform="netease")
        )
        self.assertEqual(result.status, "unavailable")
        self.assertFalse(transport.calls)


class HttpIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_party_only_no_redirect_cookiejar_or_query_secrets(self):
        response = FakeResponse({"code": 200, "account": {"id": 1}})
        session = FakeSession(response)
        backend = NeteaseBackend()
        with patch(
            "astrbot_plugin_kook_music.music_auth.netease_backend.aiohttp.ClientSession",
            return_value=session,
        ) as create:
            self.assertEqual(
                await backend.check_credentials({"cookies": COOKIE}), "valid"
            )
        options = create.call_args.kwargs
        import aiohttp

        self.assertIsInstance(options["cookie_jar"], aiohttp.DummyCookieJar)
        self.assertFalse(options["trust_env"])
        url, request = session.calls[0]
        self.assertEqual(url, "https://music.163.com/weapi/w/nuser/account/get")
        self.assertFalse(request["allow_redirects"])
        self.assertEqual(request["cookies"], COOKIE)
        self.assertNotIn(COOKIE["MUSIC_U"], str(request["data"]))
        self.assertNotIn(COOKIE["__csrf"], url)
        await backend.close()
        session.close.assert_awaited_once()

    async def test_redirect_is_transient_not_followed(self):
        backend = NeteaseBackend()
        backend._session = FakeSession(FakeResponse({}, status=302))
        self.addAsyncCleanup(backend.close)
        self.assertEqual(
            await backend.check_credentials({"cookies": COOKIE}), "unknown"
        )
        self.assertEqual(len(backend._session.calls), 1)

    async def test_foreign_cookie_domain_not_retained(self):
        response = FakeResponse({"code": 200})
        response.cookies["MUSIC_U"] = "untrusted"
        response.cookies["MUSIC_U"]["domain"] = "evil.test"
        response.cookies["__csrf"] = "csrf"
        response.cookies["__csrf"]["domain"] = ".music.163.com"
        backend = NeteaseBackend()
        backend._session = FakeSession(response)
        self.addAsyncCleanup(backend.close)
        _, received = await backend._request("/weapi/w/nuser/account/get", {})
        self.assertEqual(received, {"__csrf": "csrf"})

    async def test_non_json_not_interpreted_as_expired(self):
        backend = NeteaseBackend()
        backend._session = FakeSession(FakeResponse(raw=b"<html>CDN error</html>"))
        self.addAsyncCleanup(backend.close)
        self.assertEqual(
            await backend.check_credentials({"cookies": COOKIE}), "unknown"
        )

    async def test_unsupported_endpoint_rejected_before_network(self):
        backend = NeteaseBackend()
        self.addAsyncCleanup(backend.close)
        with self.assertRaises(AuthError):
            await backend._request("https://thirdparty.example/api", {}, COOKIE)
        self.assertIsNone(backend._session)


if __name__ == "__main__":
    unittest.main()
