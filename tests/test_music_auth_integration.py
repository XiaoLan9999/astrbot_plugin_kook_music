import asyncio
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_kook_music.music.model import Song
from astrbot_plugin_kook_music.music.searcher import MusicSearcher
from astrbot_plugin_kook_music.music_auth import kook_dm
from astrbot_plugin_kook_music.music_auth.integration import MusicAuthMixin
from astrbot_plugin_kook_music.music_auth.types import (
    AudioResult,
    LoginChallenge,
    LoginPoll,
)


class Plugin(MusicAuthMixin):
    def __init__(self, config=None, platforms=None):
        self.config = config or {}
        self.data_dir = Path("unused-test-directory")
        self.context = types.SimpleNamespace(
            platform_manager=types.SimpleNamespace(platform_insts=platforms or [])
        )
        self._configure_music_auth()


def platform(instance="bot-one"):
    return types.SimpleNamespace(
        meta=lambda: types.SimpleNamespace(id=instance, name="kook"),
        config={"kook_bot_token": "synthetic-token"},
        client=object(),
    )


class Event:
    def __init__(
        self, adapter, *, user="123", private=True, name="kook", text="音乐登录 qq"
    ):
        self.adapter, self.client = adapter, adapter.client
        self.user, self.private, self.name, self.message_str = user, private, name, text
        self.stopped = False

    def get_sender_id(self):
        return self.user

    def get_platform_id(self):
        return self.adapter.meta().id

    def get_platform_name(self):
        return self.name

    def is_private_chat(self):
        return self.private

    def stop_event(self):
        self.stopped = True


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_does_not_create_credentials_or_notify(self):
        plugin = Plugin()
        self.assertFalse(plugin._music_auth_enabled)
        self.assertIsNone(await plugin._ensure_music_auth())

    async def test_name_is_not_an_administrator_id(self):
        plugin = Plugin({"music_auth_admin_ids": ["Example#1234", True, "123"]})
        self.assertEqual(plugin._music_auth_admin_ids, {"123"})

    async def test_non_kook_is_silent_and_group_never_starts_login(self):
        adapter = platform()
        plugin = Plugin({"music_auth_admin_ids": ["123"]}, [adapter])
        plugin._ensure_music_auth = AsyncMock()
        other = Event(adapter, name="aiocqhttp")
        self.assertIsNone(await plugin._music_auth_command(other, "音乐登录"))
        self.assertFalse(other.stopped)
        group = Event(adapter, private=False)
        self.assertIn("私聊", await plugin._music_auth_command(group, "音乐登录"))
        plugin._ensure_music_auth.assert_not_awaited()

    async def test_unknown_user_never_starts_or_reads_status(self):
        adapter = platform()
        plugin = Plugin({"music_auth_admin_ids": ["123"]}, [adapter])
        plugin._ensure_music_auth = AsyncMock()
        for command in ("音乐登录", "音乐账号状态", "退出音乐账号", "取消音乐登录"):
            self.assertIn(
                "白名单",
                await plugin._music_auth_command(Event(adapter, user="456"), command),
            )
        plugin._ensure_music_auth.assert_not_awaited()

    async def test_multiple_adapters_require_explicit_binding(self):
        one, two = platform(), platform("bot-two")
        plugin = Plugin({"music_auth_admin_ids": ["123"]}, [one, two])
        self.assertIsNone(plugin._account_platform())
        plugin.config["music_auth_kook_bot_id"] = "bot-two"
        self.assertIs(plugin._account_platform(), two)
        self.assertIn("指定", await plugin._music_auth_command(Event(one), "音乐登录"))

    async def test_stale_client_is_denied(self):
        adapter = platform()
        plugin = Plugin({"music_auth_admin_ids": ["123"]}, [adapter])
        event = Event(adapter)
        event.client = object()
        self.assertIn("指定", await plugin._music_auth_command(event, "音乐登录"))

    async def test_kook_constant_metadata_id_uses_configured_instance_and_client(self):
        one, two = platform("kook"), platform("kook")
        one.config["id"], two.config["id"] = "configured-one", "configured-two"
        plugin = Plugin(
            {
                "music_auth_admin_ids": ["123"],
                "music_auth_kook_bot_id": "configured-two",
            },
            [one, two],
        )
        manager = types.SimpleNamespace(
            start_login=AsyncMock(return_value=(True, "started"))
        )
        plugin._ensure_music_auth = AsyncMock(return_value=manager)
        self.assertIs(plugin._account_platform(), two)
        self.assertIn("指定", await plugin._music_auth_command(Event(one), "音乐登录"))
        self.assertEqual(
            await plugin._music_auth_command(Event(two), "音乐登录"), "started"
        )
        manager.start_login.assert_awaited_once_with(
            "qq", "qq", "configured-two", "123"
        )
        plugin._music_auth_binding = ("configured-two", "synthetic-token")
        with patch(
            "astrbot_plugin_kook_music.music_auth.integration.send_private_auth",
            new_callable=AsyncMock,
        ) as send:
            await plugin._notify_music_auth("configured-two", "123", "safe")
            send.assert_awaited_once()

    async def test_login_choices_and_cancel_logout_are_scoped(self):
        adapter = platform()
        plugin = Plugin({"music_auth_admin_ids": ["123"]}, [adapter])
        manager = types.SimpleNamespace(
            start_login=AsyncMock(return_value=(True, "started")),
            cancel_login=AsyncMock(return_value=(True, "cancelled")),
            logout=AsyncMock(return_value=(True, "logged out")),
            status=lambda: "safe status",
        )
        plugin._ensure_music_auth = AsyncMock(return_value=manager)
        for argument, provider, method in (
            ("QQ", "qq", "qq"),
            ("微信", "qq", "wechat"),
            ("网易云", "netease", "netease"),
            ("网易云网页", "netease", "netease_web"),
        ):
            self.assertEqual(
                await plugin._music_auth_command(
                    Event(adapter, text="#音乐登录 " + argument), "音乐登录"
                ),
                "started",
            )
            manager.start_login.assert_awaited_with(provider, method, "bot-one", "123")
        self.assertEqual(
            await plugin._music_auth_command(Event(adapter), "音乐账号状态"),
            "safe status",
        )
        await plugin._music_auth_command(
            Event(adapter, text="取消音乐登录 qq"), "取消音乐登录"
        )
        manager.cancel_login.assert_awaited_once_with("qq", "bot-one", "123")
        await plugin._music_auth_command(
            Event(adapter, text="退出音乐账号 网易云"), "退出音乐账号"
        )
        manager.logout.assert_awaited_once_with("netease", "bot-one", "123")

    async def test_private_notify_rechecks_recipient_and_adapter_generation(self):
        adapter = platform()
        plugin = Plugin({"music_auth_admin_ids": ["123"]}, [adapter])
        plugin._music_auth_binding = ("bot-one", "synthetic-token")
        with patch(
            "astrbot_plugin_kook_music.music_auth.integration.send_private_auth",
            new_callable=AsyncMock,
        ) as send:
            await plugin._notify_music_auth("bot-one", "123", "safe")
            send.assert_awaited_once_with("synthetic-token", "123", "safe", None)
            for bot, user in (("bot-two", "123"), ("bot-one", "456")):
                with self.assertRaises(RuntimeError):
                    await plugin._notify_music_auth(bot, user, "hidden")
            adapter.config["kook_bot_token"] = "replacement"
            with self.assertRaises(RuntimeError):
                await plugin._notify_music_auth("bot-one", "123", "hidden")
            self.assertEqual(send.await_count, 1)

    def test_secret_dataclasses_are_redacted(self):
        values = [
            LoginChallenge("private-id", b"private-qr", opaque={"secret": "private"}),
            LoginPoll("authorized", {"cookie": "private"}),
            AudioResult("resolved", "https://host/?private"),
        ]
        for value in values:
            self.assertNotIn("private", repr(value))


class AccountAudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_qq_account_is_before_public_source_and_retains_version(self):
        searcher = MusicSearcher(qq_vip_resolver_url="https://resolver.example/api")
        song = Song(
            id="same-mid",
            name="Title (DJ)",
            platform="qq",
            provider_data={"resolver_status": "denied"},
        )
        searcher._fetch_qq_song_by_id = AsyncMock(return_value=song)
        searcher.account_resolver = AsyncMock(
            return_value=AudioResult("resolved", "https://aqqmusic.tc.qq.com/song.m4a")
        )
        searcher._fill_qq_vip_resolver_url = AsyncMock()
        result = await searcher.fetch_song_by_id("qq", "same-mid")
        self.assertIs(result, song)
        self.assertEqual(result.name, "Title (DJ)")
        self.assertEqual(result.extra_headers, {})
        searcher._fill_qq_vip_resolver_url.assert_not_awaited()

    async def test_account_failure_keeps_public_fallback(self):
        searcher = MusicSearcher(qq_vip_resolver_url="https://resolver.example/api")
        song = Song(
            id="mid", platform="qq", provider_data={"resolver_status": "denied"}
        )
        searcher._fetch_qq_song_by_id = AsyncMock(return_value=song)
        searcher.account_resolver = AsyncMock(return_value=AudioResult("expired"))
        searcher._fill_qq_vip_resolver_url = AsyncMock(return_value=False)
        await searcher.fetch_song_by_id("qq", "mid")
        searcher._fill_qq_vip_resolver_url.assert_awaited_once_with(song)

    async def test_netease_denied_can_use_owned_account(self):
        searcher = MusicSearcher()
        song = Song(
            id="123", platform="netease", provider_data={"resolver_status": "denied"}
        )
        searcher.account_resolver = AsyncMock(
            return_value=AudioResult("resolved", "https://m7.music.126.net/song.mp3")
        )
        searcher._fetch_netease_audio_url = AsyncMock()
        self.assertIs(await searcher.fetch_audio_url(song), song)
        self.assertTrue(song.audio_url)
        searcher._fetch_netease_audio_url.assert_not_awaited()

    async def test_owned_account_cannot_inject_foreign_or_credential_url(self):
        for provider, url in (
            ("qq", "https://evil.example/song"),
            ("netease", "https://m7.music.126.net.evil.example/song"),
            ("netease", "https://user:secret@m7.music.126.net/song"),
        ):
            searcher = MusicSearcher()
            searcher.account_resolver = AsyncMock(
                return_value=AudioResult("resolved", url)
            )
            song = Song(id="123", platform=provider)
            self.assertFalse(await searcher._fill_account_audio(song))
            self.assertFalse(song.audio_url)

    async def test_account_cancellation_propagates(self):
        searcher = MusicSearcher()
        searcher.account_resolver = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await searcher._fill_account_audio(Song(id="mid", platform="qq"))


class Response:
    status = 200

    def __init__(self, data):
        self.data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self):
        return self.data


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_qr_only_uses_direct_message_and_returns_owned_cleanup(self):
        calls = []
        responses = iter(
            [
                {"code": 0, "data": {"url": "https://img.kookapp.cn/assets/qr.png"}},
                {"code": 0, "data": {"msg_id": "text"}},
                {"code": 0, "data": {"msg_id": "qr"}},
                {"code": 0, "data": {}},
            ]
        )

        def post(url, **kwargs):
            calls.append((url, kwargs))
            return Response(next(responses))

        session = types.SimpleNamespace(post=post, closed=False)
        with patch.object(kook_dm, "_get_session", AsyncMock(return_value=session)):
            cleanup = await kook_dm.send_private_auth(
                "test-token", "123", "scan", b"\x89PNG\r\n\x1a\nsynthetic"
            )
            await cleanup()
        self.assertEqual(
            [url.rsplit("/", 2)[-2:] for url, _ in calls],
            [
                ["asset", "create"],
                ["direct-message", "create"],
                ["direct-message", "create"],
                ["direct-message", "delete"],
            ],
        )
        self.assertEqual(calls[2][1]["json"]["target_id"], "123")
        self.assertEqual(calls[3][1]["json"], {"msg_id": "qr"})
        self.assertTrue(all(kwargs["allow_redirects"] is False for _, kwargs in calls))

    async def test_delivery_error_does_not_echo_response_secrets(self):
        session = types.SimpleNamespace(
            post=lambda *_args, **_kw: Response(
                {"code": 403, "message": "token-secret"}
            )
        )
        with patch.object(kook_dm, "_get_session", AsyncMock(return_value=session)):
            with self.assertRaises(kook_dm.PrivateDeliveryError) as raised:
                await kook_dm.send_private_auth("test-token", "123", "safe")
        self.assertNotIn("secret", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
