import asyncio
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_kook_music.kook_events import KookEventBridge  # noqa: E402
from astrbot_plugin_kook_music.music_auth import private_cookie  # noqa: E402
from test_music_auth_integration import Plugin, platform  # noqa: E402


class BilibiliPrivateCookieTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.adapter = platform()
        self.adapter.client = types.SimpleNamespace(
            event_callback=AsyncMock(), bot_id="321"
        )
        self.original = self.adapter.client.event_callback
        self.plugin = Plugin({"music_auth_admin_ids": ["123", "456"]}, [self.adapter])
        self.manager = types.SimpleNamespace(
            prepare_manual_handoff=AsyncMock(return_value=(True, "ok")),
            start_login=AsyncMock(return_value=(True, "started")),
            cancel_login=AsyncMock(return_value=(True, "cancelled")),
            logout=AsyncMock(return_value=(True, "deleted")),
            import_credentials=AsyncMock(return_value=(True, "saved")),
            status=lambda: "safe status",
        )
        self.plugin._music_auth = self.manager
        self.plugin._music_auth_binding = ("bot-one", "synthetic-token")
        self.plugin._ensure_music_auth = AsyncMock(return_value=self.manager)
        self.plugin._notify_music_auth = AsyncMock()
        self.plugin._import_music_cookie = AsyncMock(return_value=(True, "saved"))
        self.intake = self.plugin._music_cookie_intake
        self.bridge = KookEventBridge(AsyncMock(), self.plugin._intercept_music_auth)
        self.bridge.sync([self.adapter])
        self.delete = AsyncMock()
        self.patches = (
            patch.object(private_cookie, "_get_session", AsyncMock(return_value=object())),
            patch.object(private_cookie, "_post", self.delete),
        )
        for item in self.patches:
            item.start()
        self.counter = 0

    async def asyncTearDown(self):
        await self.intake.close()
        self.bridge.close()
        for item in reversed(self.patches):
            item.stop()

    async def drain(self):
        while self.intake.tasks:
            await asyncio.wait_for(
                asyncio.gather(*tuple(self.intake.tasks), return_exceptions=True), 1
            )

    async def receive(self, text, *, user="123", channel="PERSON", drain=True):
        self.counter += 1
        event = {
            "type": 9,
            "channel_type": channel,
            "author_id": user,
            "content": text,
            "msg_id": f"bili-{self.counter}",
        }
        await asyncio.wait_for(self.adapter.client.event_callback(event), 0.1)
        if drain:
            await self.drain()
        return event

    async def test_three_export_formats_reach_bilibili_import_without_adapter(self):
        formats = (
            "SESSDATA=synthetic-session; bili_jct=synthetic-csrf; DedeUserID=42",
            json.dumps(
                [
                    {"domain": ".bilibili.com", "name": "SESSDATA", "value": "synthetic"},
                    {"domain": ".bilibili.com", "name": "DedeUserID", "value": "42"},
                ]
            ),
            "# Netscape HTTP Cookie File\n"
            "#HttpOnly_.bilibili.com\tTRUE\t/\tTRUE\t0\tSESSDATA\tsynthetic",
        )
        for index, text in enumerate(formats):
            with self.subTest(index=index):
                await self.receive("#音乐Cookie B站")
                window = self.intake.windows[(id(self.adapter.client), "123")]
                self.assertEqual(window[2], "bilibili")
                self.assertEqual(
                    self.manager.prepare_manual_handoff.await_args.kwargs,
                    {"provider": "bilibili"},
                )
                await self.receive(text)
                args = self.plugin._import_music_cookie.await_args
                self.assertEqual(args.args, ("bilibili", "bot-one", "123", text))
                self.assertFalse(self.intake.windows)
        self.assertEqual(self.plugin._import_music_cookie.await_count, 3)
        self.assertEqual(self.delete.await_count, 3)
        self.original.assert_not_awaited()

    async def test_aliases_inline_payload_and_codeblock_are_provider_bound(self):
        payload = "```text\nSESSDATA=synthetic\n```"
        for name in ("B站", "哔哩哔哩", "bilibili", "BILI"):
            await self.receive(f"#音乐Cookie {name}\n" + payload)
            self.assertEqual(
                self.plugin._import_music_cookie.await_args.args,
                ("bilibili", "bot-one", "123", payload),
            )
        self.original.assert_not_awaited()

    async def test_bilibili_window_status_and_timeout(self):
        self.plugin.config["music_auth_cookie_timeout"] = 30
        self.intake.clock = lambda: 100
        await self.receive("#音乐Cookie B站")
        window = self.intake.windows[(id(self.adapter.client), "123")]
        self.assertEqual(window[0], 130)
        await self.receive("#音乐账号状态")
        self.assertEqual(self.intake.windows[(id(self.adapter.client), "123")], window)
        self.intake.clock = lambda: 131
        await self.receive("SESSDATA=expired-window")
        self.plugin._import_music_cookie.assert_not_awaited()
        self.assertFalse(self.intake.windows)
        self.assertIn("窗口已过期", self.plugin._notify_music_auth.await_args.args[2])
        self.original.assert_not_awaited()

    async def test_bilibili_cookie_tokens_are_consumed_even_without_window(self):
        for payload in (
            "SESSDATA=synthetic",
            "bili_jct=synthetic",
            r"bili\_jct=synthetic",
            "DedeUserID=42",
            "DedeUserID__ckMd5=synthetic",
            "ac_time_value=synthetic-refresh",
            '[{"name":"SESSDATA","value":"synthetic"}]',
        ):
            await self.receive(payload)
        self.original.assert_not_awaited()
        self.plugin._import_music_cookie.assert_not_awaited()

    async def test_group_unlisted_actor_and_wrong_bot_are_consumed_without_reply(self):
        await self.receive("#音乐Cookie B站 SESSDATA=synthetic", channel="GROUP")
        await self.receive("#音乐Cookie B站 SESSDATA=synthetic", user="999")
        event = {
            "type": 9,
            "channel_type": "PERSON",
            "author_id": "123",
            "msg_id": "wrong-bili-bot",
            "content": "SESSDATA=synthetic",
        }
        self.assertTrue(self.intake.intercept(object(), event, "synthetic-token"))
        self.assertTrue(self.intake.intercept(self.adapter.client, event, "wrong-token"))
        self.plugin._import_music_cookie.assert_not_awaited()
        self.plugin._notify_music_auth.assert_not_awaited()
        self.original.assert_not_awaited()

    async def test_window_is_single_use_after_invalid_cookie(self):
        self.plugin._import_music_cookie.return_value = (False, "COOKIE_FORMAT")
        await self.receive("#音乐Cookie B站")
        await self.receive("incomplete text")
        await self.receive("SESSDATA=synthetic")
        self.plugin._import_music_cookie.assert_awaited_once()
        self.assertFalse(self.intake.windows)
        self.original.assert_not_awaited()

    async def test_other_admin_cannot_replace_provider_or_cancel(self):
        await self.receive("#音乐Cookie B站")
        await self.receive("#音乐Cookie 网易云", user="456")
        await self.receive("#取消音乐登录 B站", user="456")
        window = self.intake.windows[(id(self.adapter.client), "123")]
        self.assertEqual(window[2], "bilibili")
        self.manager.cancel_login.assert_not_awaited()
        await self.receive("SESSDATA=synthetic")
        self.plugin._import_music_cookie.assert_awaited_once()

    async def test_qr_command_closes_cookie_window_and_delegates_bilibili(self):
        await self.receive("#音乐Cookie B站")
        await self.receive("#音乐登录 B站")
        self.assertFalse(self.intake.windows)
        self.manager.start_login.assert_awaited_once()
        self.assertEqual(
            self.manager.start_login.await_args.args,
            ("bilibili", "qr", "bot-one", "123"),
        )
        self.original.assert_not_awaited()

    async def test_cancel_prevents_late_bilibili_import_commit(self):
        entered, release = asyncio.Event(), asyncio.Event()
        allowed = []

        async def slow_import(*args, authorized):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass
            allowed.append(authorized())
            return True, "must-not-notify"

        self.plugin._import_music_cookie.side_effect = slow_import
        await self.receive("#音乐Cookie B站 SESSDATA=synthetic", drain=False)
        await asyncio.wait_for(entered.wait(), 1)
        await self.receive("#取消音乐登录 B站")
        self.assertEqual(allowed, [False])
        self.manager.cancel_login.assert_awaited_once()
        self.assertFalse(
            any(
                "must-not-notify" in call.args[2]
                for call in self.plugin._notify_music_auth.await_args_list
            )
        )

    async def test_reload_and_admin_config_change_revoke_windows(self):
        await self.receive("#音乐Cookie B站")
        self.plugin.config["music_auth_admin_ids"] = ["456"]
        self.intake.refresh()
        self.plugin.config["music_auth_admin_ids"] = ["123", "456"]
        await self.receive("SESSDATA=synthetic")
        await self.receive("#音乐Cookie B站")
        await self.intake.close()
        await self.receive("SESSDATA=synthetic")
        self.plugin._import_music_cookie.assert_not_awaited()
        self.assertFalse(self.intake.windows)
        self.original.assert_not_awaited()

    async def test_failed_import_never_echoes_exception_cookie(self):
        self.plugin._import_music_cookie.side_effect = ValueError("SESSDATA=do-not-echo")
        await self.receive("#音乐Cookie B站 SESSDATA=synthetic")
        reply = self.plugin._notify_music_auth.await_args.args[2]
        self.assertIn("PRIVATE_IMPORT", reply)
        self.assertNotIn("do-not-echo", reply)
        self.delete.assert_not_awaited()
        self.original.assert_not_awaited()

    async def test_expired_bilibili_window_consumes_one_late_bare_sessdata(self):
        self.plugin.config["music_auth_cookie_timeout"] = 30
        self.intake.clock = lambda: 100
        await self.receive("#音乐Cookie B站")
        self.intake.clock = lambda: 131
        await self.receive("ordinary channel message", channel="GROUP")
        self.original.assert_awaited_once()
        self.assertTrue(self.intake.expired_windows)
        await self.receive("#音乐账号状态")
        self.assertTrue(self.intake.expired_windows)
        self.original.reset_mock()
        await self.receive("synthetic-bare-session")
        self.original.assert_not_awaited()
        self.plugin._import_music_cookie.assert_not_awaited()
        self.assertFalse(self.intake.expired_windows)
        reply = self.plugin._notify_music_auth.await_args.args[2]
        self.assertIn("窗口已过期", reply)
        self.assertNotIn("synthetic-bare-session", reply)
        await self.receive("ordinary private message")
        self.original.assert_awaited_once()

    async def test_expired_bilibili_grace_is_bounded_and_revoked_by_new_window(self):
        self.plugin.config["music_auth_cookie_timeout"] = 30
        self.intake.clock = lambda: 100
        await self.receive("#音乐Cookie B站")
        self.intake.clock = lambda: 431
        await self.receive("ordinary private message")
        self.original.assert_awaited_once()
        self.assertFalse(self.intake.expired_windows)
        await self.receive("#音乐Cookie B站")
        self.intake.clock = lambda: 462
        await self.receive("#音乐账号状态")
        self.assertTrue(self.intake.expired_windows)
        await self.receive("#音乐Cookie B站")
        self.assertFalse(self.intake.expired_windows)
        await self.receive("synthetic-bare-session")
        self.plugin._import_music_cookie.assert_awaited_once()
        self.assertEqual(
            self.plugin._import_music_cookie.await_args.args[-1],
            "synthetic-bare-session",
        )
        self.assertFalse(self.intake.windows)
        self.assertFalse(self.intake.expired_windows)


if __name__ == "__main__":
    unittest.main()
