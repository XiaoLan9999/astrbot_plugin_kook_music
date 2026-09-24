import asyncio
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_kook_music.music_auth.manager import AuthManager
from astrbot_plugin_kook_music.music_auth.manual_cookie import parse_netease_cookie
from astrbot_plugin_kook_music.music_auth.types import AuthError, LoginChallenge

OLD = {"cookies": {"MUSIC_U": "synthetic-old"}}
NEW = {"cookies": {"MUSIC_U": "synthetic-new"}}


class CookieInputTests(unittest.TestCase):
    def test_request_cookie_header_is_filtered(self):
        self.assertEqual(
            parse_netease_cookie(
                "Cookie: MUSIC_U=synthetic==; __csrf=abc; other=private"
            ),
            {"cookies": {"MUSIC_U": "synthetic==", "__csrf": "abc"}},
        )

    def test_invalid_input_has_no_raw_value_in_error(self):
        for value in (
            None,
            "",
            "MUSIC_U=secret\nX-Other=x",
            "MUSIC_U=one; MUSIC_U=two",
            "secret",
            "__csrf=only",
            "MUSIC_U=" + "x" * 9000,
        ):
            with (
                self.subTest(value_type=type(value).__name__),
                self.assertRaises(ValueError) as error,
            ):
                parse_netease_cookie(value)
            self.assertNotIn("secret", str(error.exception))


class ManualImportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.backend = types.SimpleNamespace(
            check_credentials=AsyncMock(return_value="valid"),
            close=AsyncMock(),
            begin_login=AsyncMock(
                return_value=LoginChallenge("synthetic-qr", b"synthetic-image")
            ),
            poll_login=AsyncMock(
                side_effect=AuthError(
                    "verification_required", "secret", diagnostic="NETEASE_VERIFY"
                )
            ),
            cancel_login=AsyncMock(),
        )
        self.notify = AsyncMock()
        self.manager = AuthManager(
            Path(self.temporary.name) / "accounts",
            {"netease": self.backend},
            self.notify,
            {"owner", "second"},
            poll_interval=0.001,
        )
        state = self.manager.store.load()
        state["accounts"] = {
            "qq": {"credential": {"token": "synthetic-qq"}, "state": "valid"},
            "netease": {"credential": OLD, "state": "valid"},
        }
        self.manager._save(state)

    async def asyncTearDown(self):
        await self.manager.close()
        self.temporary.cleanup()

    async def test_valid_import_atomically_updates_only_netease(self):
        result = await self.manager.import_credentials("netease", NEW, "bot", "owner")
        self.assertTrue(result[0])
        self.assertEqual(self.manager._state["accounts"]["netease"]["credential"], NEW)
        self.assertEqual(
            self.manager._state["accounts"]["qq"]["credential"],
            {"token": "synthetic-qq"},
        )
        self.assertNotIn("synthetic-new", str(result))

    async def test_invalid_unknown_and_unauthorized_keep_old(self):
        for validity in ("expired", "unknown"):
            self.backend.check_credentials.return_value = validity
            result = await self.manager.import_credentials(
                "netease", NEW, "bot", "owner"
            )
            self.assertFalse(result[0])
            self.assertEqual(
                self.manager._state["accounts"]["netease"]["credential"], OLD
            )
        self.backend.check_credentials.reset_mock()
        self.assertFalse(
            (await self.manager.import_credentials("netease", NEW, "bot", "outsider"))[
                0
            ]
        )
        self.backend.check_credentials.assert_not_awaited()

    async def test_binding_revoked_during_check_prevents_commit(self):
        allowed = True

        async def check(_credential):
            nonlocal allowed
            allowed = False
            return "valid"

        self.backend.check_credentials.side_effect = check
        self.assertFalse(
            (
                await self.manager.import_credentials(
                    "netease", NEW, "bot", "owner", authorized=lambda: allowed
                )
            )[0]
        )
        self.assertEqual(self.manager._state["accounts"]["netease"]["credential"], OLD)

    async def blocked_import(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def check(_credential):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return "valid"

        self.backend.check_credentials.side_effect = check
        task = asyncio.create_task(
            self.manager.import_credentials("netease", NEW, "bot", "owner")
        )
        await entered.wait()
        return task, release

    async def test_other_admin_cannot_claim_active_import(self):
        task, release = await self.blocked_import()
        self.assertFalse(
            (await self.manager.prepare_manual_handoff("bot", "second"))[0]
        )
        self.assertFalse(
            (await self.manager.import_credentials("netease", NEW, "bot", "second"))[0]
        )
        release.set()
        self.assertTrue((await task)[0])

    async def test_logout_invalidates_cancel_resistant_import(self):
        task, release = await self.blocked_import()
        logout = asyncio.create_task(self.manager.logout("netease", "bot", "owner"))
        await asyncio.sleep(0)
        release.set()
        await logout
        await asyncio.gather(task, return_exceptions=True)
        self.assertNotIn("netease", self.manager._state["accounts"])
        self.assertIn("qq", self.manager._state["accounts"])

    async def test_new_link_handoff_invalidates_old_import(self):
        task, release = await self.blocked_import()
        handoff = asyncio.create_task(
            self.manager.prepare_manual_handoff("bot", "owner")
        )
        await asyncio.sleep(0)
        release.set()
        self.assertTrue((await handoff)[0])
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.manager._state["accounts"]["netease"]["credential"], OLD)

    async def test_8821_handoff_only_after_qr_cleanup_and_private_notify(self):
        guidance = "请私聊发送 #音乐Cookie 网易云 开始导入。"

        def handoff(bot, user):
            self.assertNotIn("netease", self.manager._pending)
            self.backend.cancel_login.assert_awaited_once()
            self.assertEqual((bot, user), ("bot", "owner"))
            return guidance

        self.manager.on_verification_required = handoff
        await self.manager.start_login("netease", "netease", "bot", "owner")
        await asyncio.wait_for(asyncio.shield(self.manager._pending["netease"].task), 1)
        terminal = self.notify.await_args.args[2]
        self.assertIn(guidance, terminal)
        self.assertNotIn("接入页", terminal)
        self.assertNotIn("secret", terminal)

    async def test_manual_diagnostics_distinguish_expired_unknown_without_values(self):
        for validity, stage in (
            ("expired", "CHECK_EXPIRED"),
            ("unknown", "CHECK_UNKNOWN"),
        ):
            self.backend.check_credentials.return_value = validity
            with self.assertLogs("astrbot", level="WARNING") as logs:
                result = await self.manager.import_credentials(
                    "netease", NEW, "bot", "owner"
                )
            self.assertFalse(result[0])
            self.assertIn(stage, result[1])
            self.assertIn(stage, " ".join(logs.output))
            self.assertNotIn("synthetic-new", " ".join(logs.output) + result[1])

    async def test_manual_unexpected_check_error_is_redacted(self):
        self.backend.check_credentials.side_effect = RuntimeError(
            "synthetic-secret-from-upstream"
        )
        with self.assertLogs("astrbot", level="WARNING") as logs:
            result = await self.manager.import_credentials(
                "netease", NEW, "bot", "owner"
            )
        self.assertFalse(result[0])
        self.assertIn("CHECK_ERROR", " ".join(logs.output))
        self.assertNotIn("synthetic-secret", " ".join(logs.output) + result[1])

    async def test_manual_save_failure_keeps_old_account_and_redacts_error(self):
        from unittest.mock import patch

        with patch.object(
            self.manager, "_save", side_effect=OSError("synthetic-secret-path")
        ):
            with self.assertLogs("astrbot", level="WARNING") as logs:
                result = await self.manager.import_credentials(
                    "netease", NEW, "bot", "owner"
                )
        self.assertIn("SAVE_FAILED", result[1])
        self.assertEqual(self.manager._state["accounts"]["netease"]["credential"], OLD)
        self.assertNotIn("synthetic-secret", " ".join(logs.output) + result[1])


if __name__ == "__main__":
    unittest.main()
