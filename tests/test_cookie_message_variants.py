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


NETSCAPE = (
    "# Netscape HTTP Cookie File\n"
    "#HttpOnly_.music.163.com\tTRUE\t/\tFALSE\t0\tMUSIC_U\tsynthetic-session\n"
    ".music.163.com\tTRUE\t/\tFALSE\t0\t__csrf\tsynthetic-csrf"
)
RENDERED_NETSCAPE = NETSCAPE.replace("#HttpOnly_", "#HttpOnly").replace(
    "MUSIC_U", "MUSICU"
)


class CookieMessageVariantTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.adapter = platform()
        self.adapter.client = types.SimpleNamespace(
            event_callback=AsyncMock(), bot_id="321"
        )
        self.original = self.adapter.client.event_callback
        self.plugin = Plugin({"music_auth_admin_ids": ["123"]}, [self.adapter])
        self.manager = types.SimpleNamespace(
            prepare_manual_handoff=AsyncMock(return_value=(True, "ok")),
            import_credentials=AsyncMock(return_value=(True, "saved")),
            start_login=AsyncMock(return_value=(True, "started")),
            cancel_login=AsyncMock(return_value=(True, "cancelled")),
            logout=AsyncMock(return_value=(True, "deleted")),
            status=lambda: "safe status",
        )
        self.plugin._music_auth = self.manager
        self.plugin._music_auth_binding = ("bot-one", "synthetic-token")
        self.plugin._ensure_music_auth = AsyncMock(return_value=self.manager)
        self.plugin._notify_music_auth = AsyncMock()
        self.intake = self.plugin._music_cookie_intake
        self.bridge = KookEventBridge(AsyncMock(), self.plugin._intercept_music_auth)
        self.bridge.sync([self.adapter])
        self.session = AsyncMock(return_value=object())
        self.delete = AsyncMock()
        self.patches = (
            patch.object(private_cookie, "_get_session", self.session),
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

    async def receive(
        self, content, *, raw=None, kind=9, actor="123", channel="PERSON"
    ):
        self.counter += 1
        event = {
            "type": kind,
            "channel_type": channel,
            "author_id": actor,
            "content": content,
            "msg_id": f"variant-{self.counter}",
        }
        if raw is not None:
            event["extra"] = {"kmarkdown": {"raw_content": raw}}
        await asyncio.wait_for(self.adapter.client.event_callback(event), 0.2)
        while self.intake.tasks:
            await asyncio.wait_for(
                asyncio.gather(*tuple(self.intake.tasks), return_exceptions=True), 1
            )
        return event

    async def open_window(self, provider="网易云"):
        await self.receive("#音乐Cookie " + provider)
        self.plugin._notify_music_auth.reset_mock()

    def imported(self):
        self.manager.import_credentials.assert_awaited_once()
        return self.manager.import_credentials.await_args.args

    def assert_no_secret_reply(self, *secrets):
        replies = str(self.plugin._notify_music_auth.await_args_list)
        for secret in secrets:
            self.assertNotIn(secret, replies)

    async def test_original_netscape_survives_markdown_http_only_removal(self):
        await self.open_window()
        await self.receive(NETSCAPE, raw=RENDERED_NETSCAPE)
        provider, credential, _, _ = self.imported()
        self.assertEqual(provider, "netease")
        self.assertEqual(
            credential["cookies"],
            {"MUSIC_U": "synthetic-session", "__csrf": "synthetic-csrf"},
        )
        self.original.assert_not_awaited()
        self.assert_no_secret_reply("synthetic-session", "synthetic-csrf")

    async def test_valid_raw_cookie_is_consumed_when_display_is_placeholder(self):
        await self.open_window()
        await self.receive("safe rendered placeholder", raw="MUSIC_U=synthetic-raw")
        self.assertEqual(self.imported()[1]["cookies"], {"MUSIC_U": "synthetic-raw"})
        self.original.assert_not_awaited()

    async def test_inline_command_uses_valid_original_export(self):
        prefix = "#音乐Cookie 网易云\n"
        await self.receive(prefix + NETSCAPE, raw=prefix + RENDERED_NETSCAPE)
        self.assertEqual(self.imported()[1]["cookies"]["MUSIC_U"], "synthetic-session")
        self.manager.prepare_manual_handoff.assert_awaited_once_with("bot-one", "123")
        self.original.assert_not_awaited()

    async def test_original_header_ambiguity_does_not_restore_rendered_csrf(self):
        await self.open_window()
        await self.receive(
            "MUSIC_U=synthetic-session; __csrf=synthetic-parent; __csrf=synthetic-host",
            raw="MUSIC_U=synthetic-session; __csrf=synthetic-host",
        )
        self.assertEqual(
            self.imported()[1]["cookies"], {"MUSIC_U": "synthetic-session"}
        )
        self.original.assert_not_awaited()
        self.assert_no_secret_reply(
            "synthetic-session", "synthetic-parent", "synthetic-host"
        )

    async def test_two_valid_different_accounts_are_rejected_without_import(self):
        await self.open_window()
        await self.receive(
            "MUSIC_U=synthetic-original", raw="MUSIC_U=synthetic-rendered"
        )
        self.manager.import_credentials.assert_not_awaited()
        self.delete.assert_not_awaited()
        self.original.assert_not_awaited()
        replies = str(self.plugin._notify_music_auth.await_args_list)
        self.assertTrue("不一致" in replies or "冲突" in replies)
        self.assert_no_secret_reply("synthetic-original", "synthetic-rendered")

    async def test_inline_conflicting_accounts_are_rejected(self):
        await self.receive(
            "#音乐Cookie 网易云\nMUSIC_U=synthetic-original",
            raw="#音乐Cookie 网易云\nMUSIC_U=synthetic-rendered",
        )
        self.manager.import_credentials.assert_not_awaited()
        self.original.assert_not_awaited()
        self.assert_no_secret_reply("synthetic-original", "synthetic-rendered")

    async def test_valid_rendered_header_cannot_bypass_original_auth_domain(self):
        await self.open_window()
        await self.receive(
            json.dumps(
                [
                    {
                        "name": "MUSIC_U",
                        "value": "synthetic-scope-secret",
                        "domain": "evil.example",
                        "path": "/",
                    }
                ]
            ),
            raw="MUSIC_U=synthetic-scope-secret",
        )
        self.manager.import_credentials.assert_not_awaited()
        self.delete.assert_not_awaited()
        self.original.assert_not_awaited()
        self.assertIn(
            "COOKIE_SCOPE", str(self.plugin._notify_music_auth.await_args_list)
        )
        self.assert_no_secret_reply("synthetic-scope-secret")

    async def test_valid_rendered_header_cannot_hide_original_account_conflict(self):
        await self.open_window()
        await self.receive(
            "MUSIC_U=synthetic-first-account; MUSIC_U=synthetic-second-account",
            raw="MUSIC_U=synthetic-first-account",
        )
        self.manager.import_credentials.assert_not_awaited()
        self.delete.assert_not_awaited()
        self.original.assert_not_awaited()
        self.assertIn(
            "COOKIE_CONFLICT", str(self.plugin._notify_music_auth.await_args_list)
        )
        self.assert_no_secret_reply(
            "synthetic-first-account", "synthetic-second-account"
        )

    async def test_attachment_is_not_fetched_or_treated_as_cookie_text(self):
        await self.open_window()
        await self.receive(
            "https://img.kookapp.cn/assets/synthetic-cookie.json", kind=4
        )
        self.manager.import_credentials.assert_not_awaited()
        self.session.assert_not_awaited()
        self.delete.assert_not_awaited()
        self.original.assert_not_awaited()
        replies = str(self.plugin._notify_music_auth.await_args_list)
        self.assertIn("附件", replies)
        self.assertIn("文字", replies)
        self.assertNotIn("https://img.kookapp.cn", replies)

    async def test_private_window_does_not_consume_ordinary_group_message(self):
        await self.open_window()
        event = await self.receive(
            "ordinary synthetic group message",
            raw="ordinary synthetic group message",
            channel="GROUP",
        )
        self.original.assert_awaited_once_with(event)
        self.manager.import_credentials.assert_not_awaited()
        self.assertIn((id(self.adapter.client), "123"), self.intake.windows)

    async def test_unauthorized_variant_is_consumed_without_reply(self):
        await self.receive(NETSCAPE, raw=RENDERED_NETSCAPE, actor="789")
        self.manager.import_credentials.assert_not_awaited()
        self.plugin._notify_music_auth.assert_not_awaited()
        self.original.assert_not_awaited()

    async def test_bilibili_uses_valid_original_export_too(self):
        await self.open_window("B站")
        content = (
            "# Netscape HTTP Cookie File\n"
            "#HttpOnly_.bilibili.com\tTRUE\t/\tTRUE\t0\tSESSDATA\tsynthetic-bili"
        )
        await self.receive(content, raw=content.replace("#HttpOnly_", "#HttpOnly"))
        provider, credential, _, _ = self.imported()
        self.assertEqual(provider, "bilibili")
        self.assertEqual(credential["cookies"], {"SESSDATA": "synthetic-bili"})
        self.original.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
