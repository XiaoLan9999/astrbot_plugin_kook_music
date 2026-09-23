import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PLUGINS_DIR = Path(__file__).resolve().parents[2]
if str(PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGINS_DIR))

from astrbot_plugin_kook_music.music.model import Song  # noqa: E402
from astrbot_plugin_kook_music.music_auth.qq_backend import (  # noqa: E402
    QQBackend,
    _audio_url,
    _load_sdk,
    _official_url,
    _OfficialTransport,
    _Response,
)
from astrbot_plugin_kook_music.music_auth.types import AuthError  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\nsynthetic-qr"
JPEG = b"\xff\xd8\xffsynthetic-qr"
MID = "003w2xz20QlUZt"
MEDIA = "004IHTw93vDf2F"
CREDENTIAL = {
    "musicid": 123456,
    "musickey": "TEST_PRIVATE_MUSIC_KEY",
    "refresh_token": "TEST_PRIVATE_REFRESH_TOKEN",
    "refresh_key": "TEST_PRIVATE_REFRESH_KEY",
    "loginType": 2,
}

try:
    SDK = _load_sdk()
except AuthError:
    SDK = None


def raw(body, *, status=200, headers=None, cookies=None):
    if not isinstance(body, bytes):
        body = json.dumps(body).encode() if isinstance(body, dict) else body.encode()
    return _Response(
        status,
        "https://u.y.qq.com/",
        headers or {},
        cookies or {},
        body,
        body.decode(errors="replace"),
    )


def cgi(data=None, code=0):
    return raw({"code": 0, "req_0": {"code": code, "data": data or {}}})


def audio(*, mid=MID, media=MEDIA, code=0, purl=None):
    filename = f"M500{media}.mp3"
    return cgi(
        {
            "midurlinfo": [
                {
                    "songmid": mid,
                    "filename": filename,
                    "purl": purl
                    if purl is not None
                    else filename + "?vkey=PRIVATE_AUDIO_TOKEN",
                    "vkey": "PRIVATE_AUDIO_TOKEN",
                    "ekey": "",
                    "result": code,
                }
            ]
        }
    )


class FakeTransport:
    def __init__(self):
        self.responses = []
        self.requests = []
        self.closed = False
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def request(self, request):
        self.requests.append(request)
        self.started.set()
        if not self.responses:
            raise AssertionError("Unexpected network request")
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        if result == "wait":
            try:
                await asyncio.Future()
            finally:
                self.cancelled.set()
        return result

    async def close(self):
        self.closed = True


@unittest.skipIf(SDK is None, "Optional pinned QQMusicApi is not installed")
class QQBackendTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.transport = FakeTransport()
        self.client = SDK.Client(platform=SDK.Platform.WEB, transport=self.transport)
        self.now = 100.0
        self.backend = QQBackend(
            client_factory=lambda: self.client,
            sdk=SDK,
            clock=lambda: self.now,
            timeout=0.2,
        )

    async def asyncTearDown(self):
        await self.backend.close()

    async def begin(self, method="qq"):
        if method == "qq":
            self.transport.responses.append(
                raw(PNG, cookies={"qrsig": "PRIVATE_QR_SIGNATURE"})
            )
        else:
            self.transport.responses.extend([raw('uuid=PRIVATE_WX_UUID"'), raw(JPEG)])
        return await self.backend.begin_login(method)

    def song(self):
        return Song(MID, platform="qq", provider_data={"media_mid": MEDIA})

    async def test_qq_begin_no_credentials_or_identifier_in_repr(self):
        challenge = await self.begin()
        self.assertEqual(challenge.qr_bytes, PNG)
        self.assertEqual(challenge.expires_in, 180)
        self.assertNotIn("PRIVATE", repr(challenge))
        self.assertIsNone(challenge.opaque)
        self.assertTrue(
            self.transport.requests[0].url.startswith("https://ssl.ptlogin2.qq.com/")
        )

    async def test_wechat_begin_is_two_official_requests(self):
        challenge = await self.begin("wechat")
        self.assertEqual(challenge.qr_bytes, JPEG)
        self.assertEqual(len(self.transport.requests), 2)
        self.assertTrue(
            all(_official_url(request.url) for request in self.transport.requests)
        )

    async def test_invalid_method_never_sends(self):
        with self.assertRaises(AuthError):
            await self.backend.begin_login("password")
        self.assertEqual(self.transport.requests, [])

    async def test_qr_html_is_not_uploaded(self):
        self.transport.responses.append(
            raw(b"<html>LOGIN_TOKEN</html>", cookies={"qrsig": "secret"})
        )
        with self.assertRaises(AuthError) as caught:
            await self.backend.begin_login("qq")
        self.assertNotIn("LOGIN_TOKEN", str(caught.exception))
        self.assertEqual(self.backend._pending, {})

    async def test_poll_waiting_and_scanned_are_not_reversed(self):
        challenge = await self.begin()
        for code, expected in [(66, "pending"), (67, "scanned")]:
            self.transport.responses.append(raw(f"ptuiCB('{code}','','')"))
            result = await self.backend.poll_login(challenge)
            self.assertEqual(result.status, expected)
            self.assertIsNone(result.credential)

    async def test_qq_authorized_exact_real_sdk_flow(self):
        challenge = await self.begin()
        self.transport.responses.extend(
            [
                raw(
                    "ptuiCB('0','0','https://graph.qq.com/?uin=123&service=x&ptsigx=SIGX&s_url=x')"
                ),
                raw("", cookies={"p_skey": "PRIVATE_P_SKEY"}),
                raw(
                    "",
                    status=302,
                    headers={"Location": "https://y.qq.com/?code=PRIVATE_CODE&state=x"},
                ),
                cgi(CREDENTIAL),
            ]
        )
        result = await self.backend.poll_login(challenge)
        self.assertEqual(result.status, "authorized")
        self.assertEqual(result.credential["musickey"], CREDENTIAL["musickey"])
        self.assertEqual(result.credential["str_musicid"], "123456")
        self.assertNotIn(CREDENTIAL["musickey"], repr(result))
        self.assertEqual((await self.backend.poll_login(challenge)).status, "expired")
        self.assertTrue(
            all(_official_url(request.url) for request in self.transport.requests)
        )

    async def test_wechat_authorized_exact_real_sdk_flow(self):
        challenge = await self.begin("wechat")
        self.transport.responses.extend(
            [
                raw("window.wx_errcode=405;window.wx_code='PRIVATE_WX_CODE'"),
                cgi({**CREDENTIAL, "loginType": 1, "musickey": "W_X_PRIVATE_KEY"}),
            ]
        )
        result = await self.backend.poll_login(challenge)
        self.assertEqual(result.status, "authorized")
        self.assertEqual(result.credential["login_type"], 1)
        self.assertNotIn("PRIVATE", repr(result))

    async def test_refusal_invalidates_challenge(self):
        challenge = await self.begin()
        self.transport.responses.append(raw("ptuiCB('68','','')"))
        self.assertEqual((await self.backend.poll_login(challenge)).status, "denied")
        self.assertEqual((await self.backend.poll_login(challenge)).status, "expired")

    async def test_expired_by_local_deadline_without_request(self):
        challenge = await self.begin()
        self.now += 181
        self.assertEqual((await self.backend.poll_login(challenge)).status, "expired")
        self.assertEqual(len(self.transport.requests), 1)

    async def test_expired_by_remote_qr_status(self):
        challenge = await self.begin()
        self.transport.responses.append(raw("ptuiCB('65','','')"))
        self.assertEqual((await self.backend.poll_login(challenge)).status, "expired")

    async def test_cancel_stops_inflight_poll(self):
        challenge = await self.begin()
        self.transport.started.clear()
        self.transport.responses.append("wait")
        task = asyncio.create_task(self.backend.poll_login(challenge))
        await self.transport.started.wait()
        await self.backend.cancel_login(challenge)
        self.assertTrue(task.cancelled())
        self.assertTrue(self.transport.cancelled.is_set())
        self.assertEqual((await self.backend.poll_login(challenge)).status, "expired")

    async def test_cancel_never_logs_out_existing_account(self):
        challenge = await self.begin()
        await self.backend.cancel_login(challenge)
        self.assertEqual(len(self.transport.requests), 1)

    async def test_poll_network_errors_keep_challenge_and_hide_tokens(self):
        challenge = await self.begin()
        self.transport.responses.append(RuntimeError("PRIVATE_QR_SIGNATURE"))
        with self.assertRaises(AuthError) as caught:
            await self.backend.poll_login(challenge)
        self.assertEqual(caught.exception.kind, "transient")
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertIn(challenge.challenge_id, self.backend._pending)

    async def test_check_known_login_expiry_only(self):
        for code, expected in [
            (0, "valid"),
            (1000, "expired"),
            (104400, "expired"),
            (104401, "expired"),
            (2001, "unknown"),
            (104604, "unknown"),
            (500, "unknown"),
        ]:
            with self.subTest(code=code):
                self.transport.responses.append(cgi(code=code))
                self.assertEqual(
                    await self.backend.check_credentials(CREDENTIAL), expected
                )
        payload = self.transport.requests[-1].kwargs["json"]
        self.assertEqual(payload["comm"]["authst"], CREDENTIAL["musickey"])
        self.assertEqual(payload["comm"]["tmeLoginType"], "2")

    async def test_missing_and_bad_credentials_are_expired_locally(self):
        for invalid in [
            None,
            {},
            {"musicid": 1},
            {"musicid": -1, "musickey": "PRIVATE"},
        ]:
            self.assertEqual(await self.backend.check_credentials(invalid), "expired")
        self.assertEqual(self.transport.requests, [])

    async def test_http_522_not_expired(self):
        self.transport.responses.append(raw("PRIVATE cookie debug", status=522))
        self.assertEqual(await self.backend.check_credentials(CREDENTIAL), "unknown")

    async def test_check_timeout_cancels_request_without_expiring_credentials(self):
        self.transport.responses.append("wait")
        self.assertEqual(await self.backend.check_credentials(CREDENTIAL), "unknown")
        self.assertTrue(self.transport.cancelled.is_set())

    async def test_refresh_uses_existing_identity_and_returns_new_tokens(self):
        self.transport.responses.append(
            cgi({**CREDENTIAL, "musickey": "NEW_PRIVATE_KEY"})
        )
        result = await self.backend.refresh_credentials(CREDENTIAL)
        self.assertEqual(result["musickey"], "NEW_PRIVATE_KEY")
        request = self.transport.requests[-1]
        self.assertEqual(request.url, "https://u.y.qq.com/cgi-bin/musicu.fcg")
        self.assertEqual(
            request.kwargs["json"]["req_0"]["param"]["musickey"], CREDENTIAL["musickey"]
        )

    async def test_refresh_missing_refresh_tokens_no_network(self):
        self.assertIsNone(
            await self.backend.refresh_credentials({"musicid": 1, "musickey": "test"})
        )
        self.assertEqual(self.transport.requests, [])

    async def test_refresh_network_problem_not_relogin(self):
        self.transport.responses.append(raw("bad", status=429))
        with self.assertRaises(AuthError) as caught:
            await self.backend.refresh_credentials(CREDENTIAL)
        self.assertEqual(caught.exception.kind, "transient")

    async def test_refresh_expired_returns_none(self):
        self.transport.responses.append(cgi(code=104401))
        self.assertIsNone(await self.backend.refresh_credentials(CREDENTIAL))

    async def test_audio_keeps_mid_and_media_mid_and_sends_authenticated_identity(self):
        self.transport.responses.append(audio())
        result = await self.backend.resolve_audio(self.song(), CREDENTIAL)
        self.assertEqual(result.status, "resolved")
        self.assertEqual(
            result.url,
            f"https://dl.stream.qqmusic.qq.com/M500{MEDIA}.mp3?vkey=PRIVATE_AUDIO_TOKEN",
        )
        self.assertNotIn("PRIVATE_AUDIO_TOKEN", repr(result))
        request = self.transport.requests[0]
        body = request.kwargs["json"]
        self.assertEqual(body["req_0"]["param"]["songmid"], [MID])
        self.assertEqual(body["req_0"]["param"]["filename"], [f"M500{MEDIA}.mp3"])
        self.assertEqual(body["req_0"]["param"]["uin"], "123456")
        self.assertEqual(body["comm"]["authst"], CREDENTIAL["musickey"])
        self.assertEqual(len(self.transport.requests), 1)

    async def test_wrong_song_mid_never_plays(self):
        self.transport.responses.append(audio(mid="anotherMID1234"))
        result = await self.backend.resolve_audio(self.song(), CREDENTIAL)
        self.assertEqual(result.status, "transient")
        self.assertEqual(result.url, "")

    async def test_wrong_media_or_absolute_audio_paths_are_rejected(self):
        for path in [
            "https://evil.example/song.mp3",
            "//evil.example/song.mp3",
            "../x.mp3",
            "another.mp3",
            f"M500{MEDIA}.mp3#secret",
        ]:
            self.transport.responses.append(audio(purl=path))
            result = await self.backend.resolve_audio(self.song(), CREDENTIAL)
            self.assertEqual(result.status, "transient")
            self.assertEqual(result.url, "")

    async def test_song_rights_do_not_expire_credentials(self):
        for code in [104003, 104013]:
            self.transport.responses.append(audio(code=code, purl=""))
            self.assertEqual(
                (await self.backend.resolve_audio(self.song(), CREDENTIAL)).status,
                "unavailable",
            )

    async def test_audio_network_error_does_not_expire_credentials(self):
        self.transport.responses.append(raw("test", status=522))
        self.assertEqual(
            (await self.backend.resolve_audio(self.song(), CREDENTIAL)).status,
            "transient",
        )

    async def test_audio_expired_code(self):
        self.transport.responses.append(cgi(code=1000))
        self.assertEqual(
            (await self.backend.resolve_audio(self.song(), CREDENTIAL)).status,
            "expired",
        )

    async def test_other_platform_and_invalid_id_never_sent(self):
        for song in [
            Song(MID, platform="netease"),
            Song("123", platform="qq"),
            Song("bad?id", platform="qq"),
        ]:
            self.assertEqual(
                (await self.backend.resolve_audio(song, CREDENTIAL)).status,
                "unavailable",
            )
        self.assertEqual(self.transport.requests, [])

    async def test_backend_close_stops_checks_and_transport(self):
        self.transport.responses.append("wait")
        task = asyncio.create_task(self.backend.check_credentials(CREDENTIAL))
        await self.transport.started.wait()
        await self.backend.close()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.transport.closed)
        self.assertEqual(self.backend._operations, set())

    async def test_missing_optional_dependency_has_safe_error(self):
        backend = QQBackend()
        with patch(
            "astrbot_plugin_kook_music.music_auth.qq_backend._load_sdk",
            side_effect=AuthError("unavailable", "dependency missing"),
        ):
            with self.assertRaises(AuthError) as caught:
                await backend.begin_login("qq")
        self.assertEqual(caught.exception.kind, "unavailable")
        await backend.close()


class QQEndpointTests(unittest.TestCase):
    def test_exact_official_https_hosts_only(self):
        for url in [
            "https://u.y.qq.com/cgi-bin/musicu.fcg",
            "https://lp.open.weixin.qq.com/connect/l/qrconnect",
        ]:
            self.assertTrue(_official_url(url))
        for url in [
            "http://u.y.qq.com/",
            "https://u.y.qq.com.evil.test/",
            "https://u.y.qq.com:8443/",
            "https://user:pass@u.y.qq.com/",
            "https://u.y.qq.com/\\evil",
            "https://127.0.0.1/",
        ]:
            self.assertFalse(_official_url(url))

    def test_audio_domain_suffix_boundary_and_tls(self):
        self.assertEqual(
            _audio_url("http://dl.stream.qqmusic.qq.com/a.mp3"),
            "https://dl.stream.qqmusic.qq.com/a.mp3",
        )
        for url in [
            "https://stream.qqmusic.qq.com.evil.test/a.mp3",
            "https://qq.com/a.mp3",
            "https://dl.stream.qqmusic.qq.com:8443/a.mp3",
            "https://secret@dl.stream.qqmusic.qq.com/a.mp3",
        ]:
            self.assertEqual(_audio_url(url), "")


@unittest.skipIf(SDK is None, "Optional pinned QQMusicApi is not installed")
class QQOfficialTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_unsupported_hosts_and_options_are_never_sent(self):
        from qqmusic_api.core.transport import PreparedRequest, TransportError

        factory_calls = []
        transport = _OfficialTransport(
            session_factory=lambda **kwargs: factory_calls.append(kwargs)
        )
        for request in [
            PreparedRequest(
                "POST", "https://parser.example/", {"cookies": {"token": "PRIVATE"}}
            ),
            PreparedRequest(
                "POST", "https://u.y.qq.com/", {"proxy": "http://proxy.example/"}
            ),
        ]:
            with self.assertRaises(TransportError) as caught:
                await transport.request(request)
            self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertEqual(factory_calls, [])
        await transport.close()

    async def test_cookiejar_proxies_redirect_and_response_size_guards(self):
        from qqmusic_api.core.transport import PreparedRequest, TransportError

        factory_kwargs = {}
        request_kwargs = {}

        class Content:
            async def iter_chunked(self, size):
                yield b"x" * (2 * 1024 * 1024 + 1)

        class Context:
            async def __aenter__(self):
                return SimpleNamespace(content=Content())

            async def __aexit__(self, *args):
                return False

        class Session:
            def request(self, *args, **kwargs):
                request_kwargs.update(kwargs)
                return Context()

            async def close(self):
                pass

        def factory(**kwargs):
            factory_kwargs.update(kwargs)
            return Session()

        transport = _OfficialTransport(session_factory=factory)
        with self.assertRaises(TransportError):
            await transport.request(
                PreparedRequest("GET", "https://u.y.qq.com/", {"allow_redirects": True})
            )
        self.assertIs(factory_kwargs["trust_env"], False)
        self.assertEqual(type(factory_kwargs["cookie_jar"]).__name__, "DummyCookieJar")
        self.assertIs(request_kwargs["allow_redirects"], False)
        self.assertEqual(request_kwargs["timeout"].total, 20.0)
        await transport.close()


if __name__ == "__main__":
    unittest.main()
