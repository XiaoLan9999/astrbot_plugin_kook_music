import asyncio
import copy
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_kook_music.music_auth.manager import AuthManager  # noqa: E402
from astrbot_plugin_kook_music.music_auth.store import CredentialStore  # noqa: E402
from astrbot_plugin_kook_music.music_auth.types import (  # noqa: E402
    AuthError,
    LoginChallenge,
    LoginPoll,
)
from test_music_auth_integration import Event, Plugin, platform  # noqa: E402


OLD_BILI = {"cookies": {"SESSDATA": "synthetic-old", "DedeUserID": "42"}}
NEW_BILI = {
    "cookies": {
        "SESSDATA": "synthetic-new",
        "bili_jct": "synthetic-csrf",
        "DedeUserID": "42",
    }
}


def backend(credential=NEW_BILI):
    return types.SimpleNamespace(
        check_credentials=AsyncMock(return_value="valid"),
        begin_login=AsyncMock(
            return_value=LoginChallenge("synthetic-key", b"synthetic-qr")
        ),
        poll_login=AsyncMock(return_value=LoginPoll("authorized", credential)),
        cancel_login=AsyncMock(),
        close=AsyncMock(),
    )


class BilibiliManagerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name) / "accounts"
        self.backends = {name: backend() for name in ("qq", "netease", "bilibili")}
        self.notify = AsyncMock()
        self.manager = AuthManager(
            self.directory,
            self.backends,
            self.notify,
            {"owner", "second"},
            poll_interval=0.001,
        )
        self.managers = [self.manager]
        self.seed = self.manager.store.load()
        self.seed["accounts"] = {
            "qq": {"credential": {"token": "synthetic-qq"}, "state": "valid"},
            "netease": {
                "credential": {"cookies": {"MUSIC_U": "synthetic-netease"}},
                "state": "valid",
            },
            "bilibili": {"credential": copy.deepcopy(OLD_BILI), "state": "valid"},
        }
        self.manager._save(copy.deepcopy(self.seed))
        self.changes = []

        def changed(provider, record):
            persisted = CredentialStore(self.directory).load()["accounts"].get(provider)
            self.assertEqual(persisted, record)
            self.changes.append((provider, copy.deepcopy(record)))

        self.manager.on_account_changed = changed

    async def asyncTearDown(self):
        for manager in self.managers:
            await manager.close()
        self.temporary.cleanup()

    def assert_other_accounts_unchanged(self):
        for provider in ("qq", "netease"):
            self.assertEqual(
                self.manager._state["accounts"][provider],
                self.seed["accounts"][provider],
            )

    async def test_manual_import_official_check_then_encrypted_save_then_callback(self):
        result = await self.manager.import_credentials(
            "bilibili", NEW_BILI, "bot", "owner"
        )
        self.assertTrue(result[0])
        self.backends["bilibili"].check_credentials.assert_awaited_once_with(NEW_BILI)
        self.assertEqual(
            self.manager._state["accounts"]["bilibili"]["credential"], NEW_BILI
        )
        self.assertEqual(len(self.changes), 1)
        self.assertEqual(self.changes[0][0], "bilibili")
        self.assert_other_accounts_unchanged()
        encrypted = self.manager.store.state_path.read_bytes()
        self.assertNotIn(b"synthetic-new", encrypted)
        self.assertNotIn(b"synthetic-csrf", encrypted)
        self.assertNotIn("synthetic", str(result))

    async def test_invalid_unknown_and_backend_error_keep_existing_bili(self):
        for validity in ("expired", "unknown"):
            with self.subTest(validity=validity):
                self.backends["bilibili"].check_credentials.return_value = validity
                ok, detail = await self.manager.import_credentials(
                    "bilibili", NEW_BILI, "bot", "owner"
                )
                self.assertFalse(ok)
                self.assertNotIn("synthetic", detail)
                self.assertEqual(
                    self.manager._state["accounts"]["bilibili"]["credential"], OLD_BILI
                )
        self.backends["bilibili"].check_credentials.side_effect = RuntimeError(
            "SESSDATA=synthetic-private-error"
        )
        with self.assertLogs("astrbot", level="WARNING") as logs:
            result = await self.manager.import_credentials(
                "bilibili", NEW_BILI, "bot", "owner"
            )
        self.assertFalse(result[0])
        self.assertNotIn("synthetic-private-error", str(result) + " ".join(logs.output))
        self.assertEqual(self.changes, [])
        self.assert_other_accounts_unchanged()

    async def test_save_failure_keeps_existing_account_and_never_calls_callback(self):
        with patch.object(
            self.manager.store, "save", side_effect=OSError("synthetic-private-path")
        ):
            result = await self.manager.import_credentials(
                "bilibili", NEW_BILI, "bot", "owner"
            )
        self.assertFalse(result[0])
        self.assertIn("SAVE_FAILED", result[1])
        self.assertNotIn("synthetic-private-path", str(result))
        self.assertEqual(
            self.manager._state["accounts"]["bilibili"]["credential"], OLD_BILI
        )
        self.assertEqual(self.changes, [])
        self.assert_other_accounts_unchanged()

    async def test_unauthorized_import_never_checks_or_commits(self):
        for bot_id, actor, allowed in (("bot", "outsider", None), ("bot", "owner", False)):
            result = await self.manager.import_credentials(
                "bilibili", NEW_BILI, bot_id, actor,
                authorized=None if allowed is None else lambda: allowed,
            )
            self.assertFalse(result[0])
        self.assertEqual(self.changes, [])
        self.assertEqual(
            self.manager._state["accounts"]["bilibili"]["credential"], OLD_BILI
        )

    async def test_bilibili_qr_is_private_and_saved_only_after_official_check(self):
        cleanup = AsyncMock()
        self.notify.return_value = cleanup
        result = await self.manager.start_login("bilibili", "qr", "bot", "owner")
        self.assertTrue(result[0])
        task = self.manager._pending["bilibili"].task
        await asyncio.wait_for(asyncio.shield(task), 1)
        self.backends["bilibili"].begin_login.assert_awaited_once_with("qr")
        self.backends["bilibili"].check_credentials.assert_awaited_once_with(NEW_BILI)
        qr_call = next(call for call in self.notify.await_args_list if call.args[3])
        self.assertEqual(qr_call.args[:2], ("bot", "owner"))
        self.assertEqual(qr_call.args[3], b"synthetic-qr")
        self.assertIn("B站", qr_call.args[2])
        self.assertTrue(all(call.args[1] == "owner" for call in self.notify.await_args_list))
        self.assertNotIn("synthetic-new", " ".join(call.args[2] for call in self.notify.await_args_list))
        self.assertEqual(len(self.changes), 1)
        cleanup.assert_awaited_once()
        self.assert_other_accounts_unchanged()

    async def test_cancel_resistant_qr_response_cannot_replace_old_credentials(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_poll(_challenge):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return LoginPoll("authorized", NEW_BILI)

        self.backends["bilibili"].poll_login.side_effect = slow_poll
        await self.manager.start_login("bilibili", "qr", "bot", "owner")
        task = self.manager._pending["bilibili"].task
        await asyncio.wait_for(entered.wait(), 1)
        cancellation = asyncio.create_task(
            self.manager.cancel_login("bilibili", "bot", "owner")
        )
        await asyncio.sleep(0)
        release.set()
        self.assertTrue((await cancellation)[0])
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.changes, [])
        self.assertEqual(
            self.manager._state["accounts"]["bilibili"]["credential"], OLD_BILI
        )
        self.backends["bilibili"].check_credentials.assert_not_awaited()

    async def test_consumed_bilibili_qr_ticket_error_is_not_retried(self):
        self.backends["bilibili"].poll_login.side_effect = AuthError(
            "transient", "synthetic-private-ticket", diagnostic="BILIBILI_TICKET"
        )
        before = self.manager.store.state_path.read_bytes()
        self.assertTrue((await self.manager.start_login(
            "bilibili", "qr", "bot", "owner"
        ))[0])
        task = self.manager._pending["bilibili"].task
        with self.assertLogs("astrbot", level="WARNING") as logs:
            await asyncio.wait_for(asyncio.shield(task), 1)
        self.backends["bilibili"].poll_login.assert_awaited_once()
        self.backends["bilibili"].check_credentials.assert_not_awaited()
        reply = self.notify.await_args.args[2]
        self.assertIn("BILIBILI_TICKET", reply)
        self.assertNotIn("synthetic-private-ticket", reply + " ".join(logs.output))
        self.assertEqual(self.manager.store.state_path.read_bytes(), before)
        self.assertEqual(
            self.manager._state["accounts"]["bilibili"]["credential"], OLD_BILI
        )
        self.assertEqual(self.changes, [])
        self.assert_other_accounts_unchanged()

    async def test_handoff_only_invalidates_target_platform_generation(self):
        before = dict(self.manager._generation)
        self.assertTrue(
            (await self.manager.prepare_manual_handoff(
                "bot", "owner", provider="bilibili"
            ))[0]
        )
        self.assertEqual(self.manager._generation["bilibili"], before["bilibili"] + 1)
        self.assertEqual(self.manager._generation["netease"], before["netease"])
        self.assertEqual(self.manager._generation["qq"], before["qq"])
        self.assertTrue((await self.manager.prepare_manual_handoff("bot", "owner"))[0])
        self.assertEqual(self.manager._generation["netease"], before["netease"] + 1)
        self.assertEqual(self.changes, [])

    async def test_logout_notifies_extractor_to_clear_only_bilibili(self):
        self.assertTrue((await self.manager.logout("bilibili", "bot", "owner"))[0])
        self.assertEqual(self.changes, [("bilibili", None)])
        self.assertNotIn("bilibili", self.manager._state["accounts"])
        self.assert_other_accounts_unchanged()

    async def test_saved_credentials_load_in_new_manager_without_plaintext(self):
        self.assertTrue(
            (await self.manager.import_credentials("bilibili", NEW_BILI, "bot", "owner"))[0]
        )
        restored = AuthManager(self.directory, {"bilibili": backend()}, AsyncMock(), {"owner"})
        self.managers.append(restored)
        self.assertEqual(restored._state["accounts"]["bilibili"]["credential"], NEW_BILI)
        self.assertIn("B站", restored.status())

    async def test_callback_receives_detached_snapshot(self):
        def changed(_provider, record):
            record["credential"]["cookies"]["SESSDATA"] = "mutated-by-consumer"

        self.manager.on_account_changed = changed
        self.assertTrue((await self.manager.import_credentials(
            "bilibili", NEW_BILI, "bot", "owner"
        ))[0])
        self.assertEqual(
            self.manager._state["accounts"]["bilibili"]["credential"], NEW_BILI
        )
        self.assertEqual(CredentialStore(self.directory).load()["accounts"]["bilibili"]["credential"], NEW_BILI)

    async def test_callback_failure_does_not_report_saved_credentials_as_failed(self):
        self.manager.on_account_changed = Mock(side_effect=ValueError("synthetic-private"))
        with self.assertLogs("astrbot", level="WARNING") as logs:
            result = await self.manager.import_credentials(
                "bilibili", NEW_BILI, "bot", "owner"
            )
        self.assertTrue(result[0])
        self.assertNotIn("synthetic-private", " ".join(logs.output))
        self.assertEqual(
            self.manager._state["accounts"]["bilibili"]["credential"], NEW_BILI
        )


class BilibiliMixinIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.adapter = platform()
        self.plugin = Plugin({"music_auth_admin_ids": ["123"]}, [self.adapter])
        self.plugin.data_dir = Path(self.temporary.name)
        self.plugin.bilibili = types.SimpleNamespace(set_auth_cookies=lambda value: None)
        self.manager = types.SimpleNamespace(
            import_credentials=AsyncMock(return_value=(True, "safe saved")),
            start_login=AsyncMock(return_value=(True, "started")),
            cancel_login=AsyncMock(return_value=(True, "cancelled")),
            logout=AsyncMock(return_value=(True, "logged out")),
        )
        self.plugin._music_auth = self.manager
        self.plugin._music_auth_binding = ("bot-one", "synthetic-token")
        self.plugin._ensure_music_auth = AsyncMock(return_value=self.manager)

    async def asyncTearDown(self):
        await self.plugin._music_cookie_intake.close()
        self.temporary.cleanup()

    async def test_three_formats_parse_before_manager_official_validation(self):
        formats = (
            "SESSDATA=synthetic-new; bili_jct=synthetic-csrf; DedeUserID=42",
            json.dumps([
                {"domain": ".bilibili.com", "path": "/", "name": name, "value": value}
                for name, value in NEW_BILI["cookies"].items()
            ]),
            "# Netscape HTTP Cookie File\n" + "\n".join(
                f".bilibili.com\tTRUE\t/\tTRUE\t0\t{name}\t{value}"
                for name, value in NEW_BILI["cookies"].items()
            ),
        )
        for payload in formats:
            self.assertTrue((await self.plugin._import_music_cookie(
                "bilibili", "bot-one", "123", payload
            ))[0])
            call = self.manager.import_credentials.await_args
            self.assertEqual(call.args, ("bilibili", NEW_BILI, "bot-one", "123"))
            self.assertTrue(call.kwargs["authorized"]())

    async def test_bilibili_command_aliases_and_cancel_logout_are_scoped(self):
        for alias in ("B站", "bilibili", "bili", "哔哩哔哩"):
            self.assertEqual(
                await self.plugin._music_auth_command(
                    Event(self.adapter, text="#音乐登录 " + alias), "音乐登录"
                ), "started"
            )
            self.manager.start_login.assert_awaited_with("bilibili", "qr", "bot-one", "123")
        await self.plugin._music_auth_command(
            Event(self.adapter, text="#取消音乐登录 B站"), "取消音乐登录"
        )
        self.manager.cancel_login.assert_awaited_once_with("bilibili", "bot-one", "123")
        await self.plugin._music_auth_command(
            Event(self.adapter, text="#退出音乐账号 B站"), "退出音乐账号"
        )
        self.manager.logout.assert_awaited_once_with("bilibili", "bot-one", "123")

    async def test_invalid_cookie_never_reaches_backend_and_diagnostics_are_safe(self):
        for payload in ("Cookie: unrelated=synthetic-private", "SESSDATA=one; SESSDATA=two"):
            result = await self.plugin._import_music_cookie(
                "bilibili", "bot-one", "123", payload
            )
            self.assertFalse(result[0])
            self.assertNotIn("synthetic-private", str(result))
        self.manager.import_credentials.assert_not_awaited()

    async def test_permission_and_bot_binding_rechecked_before_late_commit(self):
        result = await self.plugin._import_music_cookie(
            "bilibili", "bot-one", "123", "SESSDATA=synthetic"
        )
        self.assertTrue(result[0])
        allowed = self.manager.import_credentials.await_args.kwargs["authorized"]
        self.assertTrue(allowed())
        self.adapter.config["kook_bot_token"] = "changed-token"
        self.assertFalse(allowed())
        self.assertFalse((await self.plugin._import_music_cookie(
            "bilibili", "bot-one", "123", "SESSDATA=synthetic"
        ))[0])
        self.assertEqual(self.manager.import_credentials.await_count, 1)


class BilibiliAccountBindingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary.name)
        self.plugins = []
        self.backend_instances = []

        def create_backend():
            instance = backend()
            self.backend_instances.append(instance)
            return instance

        self.patches = (
            patch("astrbot_plugin_kook_music.music_auth.qq_backend.QQBackend", side_effect=create_backend),
            patch("astrbot_plugin_kook_music.music_auth.netease_backend.NeteaseBackend", side_effect=create_backend),
            patch("astrbot_plugin_kook_music.music_auth.bilibili_backend.BilibiliBackend", side_effect=create_backend),
            patch.object(AuthManager, "start", AsyncMock()),
        )
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        for plugin in self.plugins:
            await plugin._close_music_auth()
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    def create_plugin(self, *, static_cookie=""):
        config = {"music_auth_admin_ids": ["123"], "bili_cookie": static_cookie}
        plugin = Plugin(config, [platform()])
        plugin.data_dir = self.data_dir
        plugin.bilibili = types.SimpleNamespace(set_auth_cookies=Mock())
        plugin._notify_music_auth = AsyncMock()
        self.plugins.append(plugin)
        return plugin

    def save_bilibili(self, credential=NEW_BILI, *, state="valid"):
        store = CredentialStore(self.data_dir / "music_accounts")
        saved = store.load()
        saved["accounts"]["bilibili"] = {"credential": copy.deepcopy(credential), "state": state}
        store.save(saved)

    async def test_ensure_applies_encrypted_saved_bili_before_background_start(self):
        self.save_bilibili()
        plugin = self.create_plugin(static_cookie="SESSDATA=synthetic-static")
        manager = await plugin._ensure_music_auth()
        self.assertIsNotNone(manager)
        plugin.bilibili.set_auth_cookies.assert_called_with(NEW_BILI["cookies"])
        await asyncio.wait_for(plugin._music_auth_start_task, 1)
        self.assertEqual(manager._state["accounts"]["bilibili"]["credential"], NEW_BILI)
        for instance in self.backend_instances:
            instance.check_credentials.assert_not_awaited()
        self.assertEqual(plugin.config["bili_cookie"], "SESSDATA=synthetic-static")

    async def test_officially_validated_import_updates_extractor_and_restart_loads_it(self):
        self.save_bilibili(OLD_BILI)
        plugin = self.create_plugin()
        manager = await plugin._ensure_music_auth()
        await asyncio.wait_for(plugin._music_auth_start_task, 1)
        result = await plugin._import_music_cookie(
            "bilibili", "bot-one", "123", "SESSDATA=synthetic-new; bili_jct=synthetic-csrf; DedeUserID=42"
        )
        self.assertTrue(result[0])
        manager.backends["bilibili"].check_credentials.assert_awaited_once_with(NEW_BILI)
        plugin.bilibili.set_auth_cookies.assert_called_with(NEW_BILI["cookies"])
        self.assertEqual(plugin.config["bili_cookie"], "")
        await plugin._close_music_auth()
        restored = self.create_plugin()
        self.assertIsNotNone(await restored._ensure_music_auth())
        restored.bilibili.set_auth_cookies.assert_called_with(NEW_BILI["cookies"])

    async def test_logout_clears_extractor_and_does_not_reimport_static_cookie(self):
        self.save_bilibili()
        plugin = self.create_plugin(static_cookie="SESSDATA=synthetic-static")
        manager = await plugin._ensure_music_auth()
        await asyncio.wait_for(plugin._music_auth_start_task, 1)
        self.assertTrue((await manager.logout("bilibili", "bot-one", "123"))[0])
        plugin.bilibili.set_auth_cookies.assert_called_with(None)
        await plugin._close_music_auth()
        restored = self.create_plugin(static_cookie="SESSDATA=synthetic-static")
        manager = await restored._ensure_music_auth()
        await asyncio.wait_for(restored._music_auth_start_task, 1)
        self.assertNotIn("bilibili", manager._state["accounts"])
        restored.bilibili.set_auth_cookies.assert_called_with(None)
        manager.backends["bilibili"].check_credentials.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
