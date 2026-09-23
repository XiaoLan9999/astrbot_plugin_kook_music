import types
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

from test_music_auth_integration import Event, Plugin, platform
from test_verification_portal import Context, Request


class ManualAccountIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.adapter = platform()
        self.plugin = Plugin(
            {
                "music_auth_admin_ids": ["123", "456"],
                "music_auth_web_base_url": "https://bot.example",
            },
            [self.adapter],
        )
        context = Context()
        context.platform_manager = self.plugin.context.platform_manager
        self.plugin.context = context
        self.manager = types.SimpleNamespace(
            prepare_manual_handoff=AsyncMock(return_value=(True, "ok")),
            import_credentials=AsyncMock(return_value=(True, "ok")),
            start_login=AsyncMock(return_value=(True, "started")),
            cancel_login=AsyncMock(return_value=(True, "cancelled")),
            logout=AsyncMock(return_value=(True, "deleted")),
        )
        self.plugin._music_auth = self.manager
        self.plugin._music_auth_binding = ("bot-one", "synthetic-token")
        self.plugin._ensure_music_auth = AsyncMock(return_value=self.manager)
        self.plugin._notify_music_auth = AsyncMock()
        self.plugin._ensure_verification_portal()

    async def asyncTearDown(self):
        if self.plugin._music_auth_portal:
            await self.plugin._music_auth_portal.close()

    async def test_link_is_sent_privately_but_not_returned_to_core_logging(self):
        event = Event(self.adapter, text="音乐验证 网易云")
        reply = await self.plugin._music_auth_command(event, "音乐验证")
        self.assertIn("已在本私聊", reply)
        self.assertNotIn("ticket=", reply)
        self.assertNotIn("https://", reply)
        sent = self.plugin._notify_music_auth.await_args.args
        self.assertEqual(sent[:2], ("bot-one", "123"))
        self.assertIn("#ticket=", sent[2])
        self.manager.prepare_manual_handoff.assert_awaited_once_with("bot-one", "123")

    async def test_group_unknown_user_and_other_bot_cannot_get_link(self):
        for event in (
            Event(self.adapter, private=False),
            Event(self.adapter, user="999"),
            Event(platform("other")),
        ):
            await self.plugin._music_auth_command(event, "音乐验证")
        self.plugin._notify_music_auth.assert_not_awaited()
        self.manager.prepare_manual_handoff.assert_not_awaited()

    async def test_unconfigured_or_insecure_page_does_not_emit_credentials_link(self):
        await self.plugin._music_auth_portal.close()
        self.plugin._music_auth_portal = None
        self.plugin.config["music_auth_web_base_url"] = "http://public.example"
        reply = await self.plugin._music_auth_command(
            Event(self.adapter, text="音乐验证 网易云"), "音乐验证"
        )
        self.assertIn("尚未配置", reply)
        self.plugin._notify_music_auth.assert_not_awaited()

    async def test_manual_cookie_is_filtered_and_permission_rechecked(self):
        ok, _ = await self.plugin._import_netease_cookie(
            "bot-one", "123", "MUSIC_U=synthetic; unrelated=private"
        )
        self.assertTrue(ok)
        args = self.manager.import_credentials.await_args
        self.assertEqual(
            args.args,
            ("netease", {"cookies": {"MUSIC_U": "synthetic"}}, "bot-one", "123"),
        )
        self.assertTrue(args.kwargs["authorized"]())
        self.plugin._music_auth_binding = ("other", "token")
        self.assertFalse(args.kwargs["authorized"]())

    async def test_new_handoff_revokes_other_admins_older_link(self):
        first = self.plugin._verification_link("bot-one", "123")
        self.plugin._verification_link("bot-one", "456")
        ticket = parse_qs(urlsplit(first).fragment)["ticket"][0]
        reply = await self.plugin._music_auth_portal.handle(
            Request({"action": "begin", "ticket": ticket})
        )
        self.assertEqual(reply.status, 403)

    async def test_netease_account_commands_revoke_all_old_links(self):
        for command in ("音乐登录", "取消音乐登录", "退出音乐账号"):
            self.plugin._verification_link("bot-one", "456")
            with patch.object(
                self.plugin._music_auth_portal,
                "revoke",
                wraps=self.plugin._music_auth_portal.revoke,
            ) as revoke:
                await self.plugin._music_auth_command(
                    Event(self.adapter, text=command + " 网易云"), command
                )
                revoke.assert_called_with("bot-one")

    async def test_in_memory_config_revocation_immediately_invalidates_grants(self):
        self.assertTrue(self.plugin._manual_auth_allowed("bot-one", "123"))
        self.plugin.config["music_auth_admin_ids"] = ["456"]
        self.assertFalse(self.plugin._manual_auth_allowed("bot-one", "123"))
        self.plugin.config["music_auth_admin_ids"] = ["123", "456"]
        self.plugin.config["music_auth_web_base_url"] = "https://replacement.example"
        self.assertFalse(self.plugin._manual_auth_allowed("bot-one", "123"))


if __name__ == "__main__":
    unittest.main()
