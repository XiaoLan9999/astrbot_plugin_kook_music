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
from test_music_auth_integration import Event, Plugin, platform  # noqa: E402


class ManualAccountIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.adapter = platform()
        self.adapter.client = types.SimpleNamespace(
            event_callback=AsyncMock(), bot_id="321"
        )
        self.original = self.adapter.client.event_callback
        self.plugin = Plugin({"music_auth_admin_ids": ["123", "456"]}, [self.adapter])
        self.manager = types.SimpleNamespace(
            prepare_manual_handoff=AsyncMock(return_value=(True, "ok")),
            import_credentials=AsyncMock(return_value=(True, "已校验并加密保存")),
            start_login=AsyncMock(return_value=(True, "started")),
            cancel_login=AsyncMock(return_value=(True, "cancelled")),
            logout=AsyncMock(return_value=(True, "deleted")),
            status=lambda: "safe status",
            close=AsyncMock(),
        )
        self.plugin._music_auth = self.manager
        self.plugin._music_auth_binding = ("bot-one", "synthetic-token")
        self.plugin._ensure_music_auth = AsyncMock(return_value=self.manager)
        self.plugin._notify_music_auth = AsyncMock()
        self.intake = self.plugin._music_cookie_intake
        self.bridge = KookEventBridge(AsyncMock(), self.plugin._intercept_music_auth)
        self.bridge.sync([self.adapter])
        self.delete = AsyncMock()
        self.session_patch = patch.object(
            private_cookie, "_get_session", AsyncMock(return_value=object())
        )
        self.delete_patch = patch.object(private_cookie, "_post", self.delete)
        self.session_patch.start()
        self.delete_patch.start()
        self.counter = 0

    async def asyncTearDown(self):
        await self.intake.close()
        self.bridge.close()
        self.session_patch.stop()
        self.delete_patch.stop()

    async def receive(
        self, text, *, user="123", channel="PERSON", message_id=None, drain=True
    ):
        self.counter += 1
        event = {
            "type": 9,
            "channel_type": channel,
            "author_id": user,
            "content": text,
            "msg_id": message_id or f"message-{self.counter}",
        }
        await asyncio.wait_for(self.adapter.client.event_callback(event), 0.1)
        if drain:
            await self.drain()
        return event

    async def drain(self):
        tasks = tuple(self.intake.tasks)
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 1)

    async def test_window_and_header_import_never_reach_adapter(self):
        await self.receive("#音乐Cookie 网易云")
        self.assertIn((id(self.adapter.client), "123"), self.intake.windows)
        await self.receive("MUSIC_U=synthetic; _ntes_nnid=tracking,123")
        self.original.assert_not_awaited()
        self.manager.import_credentials.assert_awaited_once()
        credential = self.manager.import_credentials.await_args.args[1]
        self.assertEqual(credential["cookies"]["MUSIC_U"], "synthetic")
        self.delete.assert_awaited_once()
        self.assertEqual(self.delete.await_args.kwargs["json"], {"msg_id": "message-2"})
        self.assertFalse(self.intake.windows)

    async def test_same_message_json_and_netscape_import(self):
        forms = (
            json.dumps(
                [
                    {
                        "domain": ".music.163.com",
                        "path": "/",
                        "name": "MUSIC_U",
                        "value": "synthetic",
                    }
                ]
            ),
            "# Netscape HTTP Cookie File\n#HttpOnly_.music.163.com\tTRUE\t/\tTRUE\t0\tMUSIC_U\tsynthetic",
        )
        for payload in forms:
            await self.receive("#音乐Cookie 网易云\n" + payload)
        self.assertEqual(self.manager.import_credentials.await_count, 2)
        self.original.assert_not_awaited()

    async def test_old_alias_opens_window_without_web_registration(self):
        await self.receive("#音乐验证 网易云")
        self.assertTrue(self.intake.windows)
        self.assertFalse(hasattr(self.plugin, "_music_auth_portal"))
        self.assertNotIn("https://", self.plugin._notify_music_auth.await_args.args[2])

    async def test_unarmed_or_expired_cookie_is_consumed_not_imported(self):
        await self.receive("MUSIC_U=synthetic")
        await self.receive("#音乐Cookie 网易云")
        self.intake.clock = lambda: 1000000000000
        await self.receive("MUSIC_U=synthetic")
        self.manager.import_credentials.assert_not_awaited()
        self.original.assert_not_awaited()

    async def test_group_unknown_user_disabled_and_wrong_client_cannot_import_or_echo(
        self,
    ):
        await self.receive("#音乐Cookie 网易云 MUSIC_U=synthetic", channel="GROUP")
        await self.receive("#音乐Cookie 网易云 MUSIC_U=synthetic", user="999")
        self.plugin.config["music_auth_enabled"] = False
        await self.receive("#音乐Cookie 网易云 MUSIC_U=synthetic")
        self.plugin.config["music_auth_enabled"] = True
        event = {
            "type": 9,
            "channel_type": "PERSON",
            "author_id": "123",
            "msg_id": "wrong",
            "content": "MUSIC_U=synthetic",
        }
        self.assertTrue(self.intake.intercept(object(), event, "synthetic-token"))
        self.assertTrue(
            self.intake.intercept(self.adapter.client, event, "changed-token")
        )
        self.manager.import_credentials.assert_not_awaited()
        self.plugin._notify_music_auth.assert_not_awaited()
        self.original.assert_not_awaited()

    async def test_normal_messages_and_system_events_are_unchanged(self):
        await self.receive("#点歌 example")
        await self.receive("hi")
        self.assertEqual(self.original.await_count, 2)

    async def test_window_is_single_use_even_format_invalid_or_attachment(self):
        await self.receive("#音乐Cookie 网易云")
        await self.receive("https://example.invalid/file.txt")
        self.assertFalse(self.intake.windows)
        self.assertIn(
            "COOKIE_FORMAT", self.plugin._notify_music_auth.await_args.args[2]
        )
        await self.receive("MUSIC_U=synthetic")
        self.manager.import_credentials.assert_not_awaited()
        self.original.assert_not_awaited()

    async def test_replay_same_message_is_ignored(self):
        text = "#音乐Cookie 网易云 MUSIC_U=synthetic"
        await self.receive(text, message_id="replay")
        await self.receive(text, message_id="replay")
        self.manager.import_credentials.assert_awaited_once()

    async def test_error_messages_and_deletion_failure_do_not_echo_secret(self):
        self.manager.import_credentials.side_effect = ValueError("MUSIC_U=do-not-echo")
        await self.receive("#音乐Cookie 网易云 MUSIC_U=synthetic")
        sent = self.plugin._notify_music_auth.await_args.args[2]
        self.assertIn("PRIVATE_IMPORT", sent)
        self.assertNotIn("do-not-echo", sent)
        self.manager.import_credentials.side_effect = None
        self.delete.side_effect = OSError("secret")
        await self.receive("#音乐Cookie 网易云 MUSIC_U=synthetic")
        self.assertEqual(
            self.plugin._notify_music_auth.await_args.args[2], "已校验并加密保存"
        )

    async def test_slow_import_does_not_block_gateway_and_cancel_revokes_authorization(
        self,
    ):
        entered, release = asyncio.Event(), asyncio.Event()
        results = []

        async def import_slow(*args, authorized):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass
            results.append(authorized())
            return False, "cancelled"

        self.manager.import_credentials.side_effect = import_slow
        await self.receive("#音乐Cookie 网易云 MUSIC_U=synthetic", drain=False)
        await asyncio.wait_for(entered.wait(), 1)
        await self.receive("#取消音乐登录 网易云")
        self.assertEqual(results, [False])
        self.manager.cancel_login.assert_awaited_once()
        self.original.assert_not_awaited()

    async def test_new_window_other_admin_cannot_replace_owners_window(self):
        await self.receive("#音乐Cookie 网易云")
        await self.receive("#音乐Cookie 网易云", user="456")
        self.manager.import_credentials.assert_not_awaited()
        self.assertIn("其他管理员", self.plugin._notify_music_auth.await_args.args[2])
        self.assertIn((id(self.adapter.client), "123"), self.intake.windows)
        self.assertNotIn((id(self.adapter.client), "456"), self.intake.windows)

    async def test_config_change_cancels_old_window_even_if_changed_back(self):
        await self.receive("#音乐Cookie 网易云")
        self.plugin.config["music_auth_admin_ids"] = ["456"]
        self.intake.refresh()
        self.plugin.config["music_auth_admin_ids"] = ["123", "456"]
        await self.receive("MUSIC_U=synthetic")
        self.manager.import_credentials.assert_not_awaited()

    async def test_configured_timeout_and_status_preserve_window(self):
        self.plugin.config["music_auth_cookie_timeout"] = 30
        self.intake.clock = lambda: 100
        await self.receive("#音乐Cookie 网易云")
        window = self.intake.windows[(id(self.adapter.client), "123")]
        self.assertEqual(window[0], 130)
        await self.receive("#音乐账号状态")
        self.assertEqual(self.intake.windows[(id(self.adapter.client), "123")], window)

    async def test_close_cancels_tasks_and_leaves_no_cookie_window(self):
        await self.receive("#音乐Cookie 网易云")
        await self.intake.close()
        await self.receive("MUSIC_U=synthetic")
        self.assertFalse(self.intake.windows)
        self.manager.import_credentials.assert_not_awaited()
        self.original.assert_not_awaited()

    async def test_fallback_command_refuses_to_import_already_logged_text(self):
        result = await self.plugin._music_auth_command(
            Event(self.adapter, text="#音乐Cookie 网易云 MUSIC_U=synthetic"),
            "音乐Cookie",
        )
        self.assertIn("不接收 Cookie", result)
        self.manager.import_credentials.assert_not_awaited()

    async def test_bounded_rate_limit_and_missing_message_id_fail_closed(self):
        for _ in range(10):
            await self.receive("MUSIC_U=synthetic")
        self.assertEqual(self.plugin._notify_music_auth.await_count, 8)
        self.manager.import_credentials.assert_not_awaited()
        self.original.assert_not_awaited()

    async def test_escaped_music_cookie_is_detected_before_parser(self):
        await self.receive(r"MUSIC\_U=synthetic")
        self.original.assert_not_awaited()
        self.manager.import_credentials.assert_not_awaited()

    async def test_bridge_interceptor_exception_does_not_fallthrough(self):
        self.bridge.intercept = lambda *_: (_ for _ in ()).throw(
            ValueError("synthetic")
        )
        await self.receive("MUSIC_U=synthetic")
        self.original.assert_not_awaited()

    async def test_late_authorization_checks_binding_and_actor_again(self):
        ok, _ = await self.plugin._import_netease_cookie(
            "bot-one", "123", "MUSIC_U=synthetic"
        )
        self.assertTrue(ok)
        permitted = self.manager.import_credentials.await_args.kwargs["authorized"]
        self.assertTrue(permitted())
        self.plugin.config["music_auth_admin_ids"] = ["456"]
        self.assertFalse(permitted())

    async def test_expired_window_does_not_swallow_next_ordinary_message(self):
        self.intake.clock = lambda: 100
        await self.receive("#音乐Cookie 网易云")
        self.intake.clock = lambda: 281
        await self.receive("#点歌 example")
        self.original.assert_awaited_once()
        self.assertFalse(self.intake.windows)

    async def test_success_notification_failure_still_attempts_deletion(self):
        self.plugin._notify_music_auth.side_effect = OSError("synthetic secret")
        await self.receive("#音乐Cookie 网易云 MUSIC_U=synthetic")
        self.manager.import_credentials.assert_awaited_once()
        self.delete.assert_awaited_once()
        self.assertEqual(self.plugin._notify_music_auth.await_count, 1)

    async def test_slow_old_success_notice_does_not_revoke_new_window(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def notify(*args):
            if "已校验" in args[2]:
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    pass

        self.plugin._notify_music_auth.side_effect = notify
        await self.receive("#音乐Cookie 网易云 MUSIC_U=synthetic", drain=False)
        await asyncio.wait_for(entered.wait(), 1)
        await self.receive("#音乐Cookie 网易云")
        self.assertIn((id(self.adapter.client), "123"), self.intake.windows)
        self.delete.assert_awaited_once()

    async def test_raw_content_and_content_are_both_checked_for_secrets(self):
        for visible, raw in (
            ("safe", "MUSIC_U=synthetic"),
            ("MUSIC_U=synthetic", "safe"),
        ):
            self.counter += 1
            event = {
                "type": 9,
                "channel_type": "PERSON",
                "author_id": "123",
                "content": visible,
                "msg_id": f"raw-{self.counter}",
                "extra": {"kmarkdown": {"raw_content": raw}},
            }
            await self.adapter.client.event_callback(event)
            await self.drain()
        self.original.assert_not_awaited()
        self.manager.import_credentials.assert_not_awaited()

    async def test_plain_text_cookie_values_are_not_html_decoded(self):
        event = {
            "type": 1,
            "channel_type": "PERSON",
            "author_id": "123",
            "msg_id": "plain-html",
            "content": "#音乐Cookie 网易云 MUSIC_U=synthetic&notit",
        }
        await self.adapter.client.event_callback(event)
        await self.drain()
        credential = self.manager.import_credentials.await_args.args[1]
        self.assertEqual(credential["cookies"]["MUSIC_U"], "synthetic&notit")

    async def test_markdown_command_entities_and_codeblock_are_supported(self):
        await self.receive("#音乐Cookie&#x20;网易云\n```text\nMUSIC_U=synthetic\n```")
        self.manager.import_credentials.assert_awaited_once()
        self.original.assert_not_awaited()

    async def test_replaced_underlying_client_token_prevents_import(self):
        self.adapter.client.config = types.SimpleNamespace(token="stale-token")
        await self.receive("#音乐Cookie 网易云 MUSIC_U=synthetic")
        self.manager.import_credentials.assert_not_awaited()
        self.plugin._notify_music_auth.assert_not_awaited()

    async def test_missing_message_id_is_consumed(self):
        event = {
            "type": 9,
            "channel_type": "PERSON",
            "author_id": "123",
            "content": "#音乐Cookie 网易云 MUSIC_U=synthetic",
        }
        await self.adapter.client.event_callback(event)
        await self.drain()
        self.manager.import_credentials.assert_not_awaited()
        self.original.assert_not_awaited()

    async def test_private_window_does_not_swallow_group_play_command(self):
        await self.receive("#音乐Cookie 网易云")
        await self.receive("#点歌 example", channel="GROUP")
        self.original.assert_awaited_once()
        self.assertTrue(self.intake.windows)

    async def test_json_equivalent_escaped_cookie_names_are_consumed_without_window(
        self,
    ):
        for name in (r"MUSIC\u005fU", r"MUSIC\\_U"):
            raw = (
                '[{"domain":".music.163.com","path":"/","name":"'
                + name
                + '","value":"synthetic"}]'
            )
            await self.receive(raw)
            await self.receive("```json\n" + raw + "\n```")
        self.original.assert_not_awaited()
        self.manager.import_credentials.assert_not_awaited()
        self.assertEqual(self.plugin._notify_music_auth.await_count, 4)

    async def test_system_and_ordinary_messages_do_not_depend_on_auth_refresh(self):
        with patch.object(self.intake, "refresh", side_effect=ValueError("not-secret")):
            event = {"type": 255, "extra": {"type": "message_btn_click"}}
            await self.adapter.client.event_callback(event)
            await self.receive("#点歌 example")
        self.assertEqual(self.original.await_count, 2)

    async def test_other_admin_cannot_cancel_active_import(self):
        entered, release = asyncio.Event(), asyncio.Event()
        authorization = []

        async def slow_import(*args, authorized):
            entered.set()
            await release.wait()
            authorization.append(authorized())
            return True, "已校验并加密保存"

        self.manager.import_credentials.side_effect = slow_import
        await self.receive("#音乐Cookie 网易云 MUSIC_U=synthetic", drain=False)
        await asyncio.wait_for(entered.wait(), 1)
        await self.receive("#取消音乐登录 网易云", user="456", drain=False)
        await asyncio.sleep(0)
        self.manager.cancel_login.assert_not_awaited()
        self.assertIn("其他管理员", self.plugin._notify_music_auth.await_args.args[2])
        release.set()
        await self.drain()
        self.assertEqual(authorization, [True])
        self.manager.import_credentials.assert_awaited_once()

    async def test_two_admin_commands_in_same_tick_preserve_first_operation(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_ensure():
            entered.set()
            await release.wait()
            return self.manager

        self.plugin._ensure_music_auth.side_effect = slow_ensure
        for actor, suffix in (("123", "first"), ("456", "second")):
            event = {
                "type": 9,
                "channel_type": "PERSON",
                "author_id": actor,
                "msg_id": "same-tick-" + suffix,
                "content": "#音乐Cookie 网易云 MUSIC_U=synthetic",
            }
            self.assertTrue(
                self.intake.intercept(self.adapter.client, event, "synthetic-token")
            )
        self.assertEqual(set(self.intake.task_owners.values()), {"123"})
        await asyncio.wait_for(entered.wait(), 1)
        self.assertFalse(any(task.cancelling() for task in self.intake.tasks))
        release.set()
        await self.drain()
        self.manager.import_credentials.assert_awaited_once()
        self.assertEqual(self.manager.import_credentials.await_args.args[3], "123")
        self.assertTrue(
            any(
                call.args[1] == "456" and "其他管理员" in call.args[2]
                for call in self.plugin._notify_music_auth.await_args_list
            )
        )

    async def test_bom_escaped_json_and_indented_codeblock_are_consumed(self):
        raw = r'[{"domain":".music.163.com","path":"/","name":"MUSIC\u005fU","value":"synthetic"}]'
        await self.receive("\ufeff" + raw)
        await self.receive("```json\n  \ufeff" + raw + "\n```")
        self.original.assert_not_awaited()
        self.manager.import_credentials.assert_not_awaited()

    async def test_other_admin_cancel_does_not_revoke_window(self):
        await self.receive("#音乐Cookie 网易云")
        await self.receive("#取消音乐登录 网易云", user="456")
        self.manager.cancel_login.assert_not_awaited()
        self.assertIn((id(self.adapter.client), "123"), self.intake.windows)
        await self.receive("#取消音乐登录 网易云")
        self.manager.cancel_login.assert_awaited_once()
        self.assertFalse(self.intake.windows)


if __name__ == "__main__":
    unittest.main()
