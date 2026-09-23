import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_kook_music.music_auth.manager import AuthManager
from astrbot_plugin_kook_music.music_auth.types import AuthError, LoginPoll
from test_music_auth_manager import NEW, OLD, FakeBackend


class LoginRetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.backend = FakeBackend()
        self.notify = AsyncMock()
        self.manager = AuthManager(
            Path(self.temporary.name) / "auth",
            {"qq": self.backend},
            self.notify,
            {"owner"},
            poll_interval=0.001,
            operation_timeout=0.2,
        )

    async def asyncTearDown(self):
        await self.manager.close()
        self.temporary.cleanup()

    async def run_login(self):
        self.assertTrue((await self.manager.start_login("qq", "qq", "bot", "owner"))[0])
        await asyncio.wait_for(asyncio.shield(self.manager._pending["qq"].task), 2)

    def texts(self):
        return "\n".join(call.args[2] for call in self.notify.await_args_list)

    async def test_transient_poll_retry_keeps_same_challenge_then_saves(self):
        self.backend.poll_login.side_effect = [
            AuthError("transient", "secret raw text", diagnostic="QQ_OAUTH"),
            AuthError("transient", "secret raw text", diagnostic="QQ_EXCHANGE"),
            LoginPoll("authorized", NEW),
        ]
        with self.assertLogs("astrbot", "WARNING") as logs:
            await self.run_login()
        self.assertEqual(self.backend.begin_login.await_count, 1)
        self.assertEqual(self.backend.poll_login.await_count, 3)
        self.assertEqual(self.manager._state["accounts"]["qq"]["credential"], NEW)
        self.assertNotIn("secret raw text", "\n".join(logs.output) + self.texts())

    async def test_three_failures_end_flow_with_safe_stage(self):
        self.backend.poll_login.side_effect = AuthError(
            "transient", "cookie=DO_NOT_EMIT", diagnostic="QQ_CHECK_SIG"
        )
        with self.assertLogs("astrbot", "WARNING") as logs:
            await self.run_login()
        self.assertEqual(self.backend.poll_login.await_count, 3)
        self.assertNotIn("qq", self.manager._state["accounts"])
        self.assertIn("QQ_CHECK_SIG", self.texts())
        self.assertNotIn("DO_NOT_EMIT", "\n".join(logs.output) + self.texts())
        self.backend.cancel_login.assert_awaited_once()

    async def test_nonretryable_error_does_not_loop(self):
        self.backend.poll_login.side_effect = AuthError("invalid_method", "bad method")
        await self.run_login()
        self.backend.poll_login.assert_awaited_once()

    async def test_untrusted_diagnostic_is_not_exposed(self):
        self.backend.poll_login.side_effect = AuthError(
            "transient", "raw secret", diagnostic="TOKEN_SECRET_123"
        )
        with self.assertLogs("astrbot", "WARNING") as logs:
            await self.run_login()
        self.assertNotIn("TOKEN_SECRET", self.texts() + "\n".join(logs.output))
        self.assertIn("LOGIN_POLL", self.texts())

    async def test_unknown_verification_retries_without_reauthorizing(self):
        self.backend.poll_login.return_value = LoginPoll("authorized", NEW)
        self.backend.check_credentials.side_effect = ["unknown", "unknown", "valid"]
        await self.run_login()
        self.backend.poll_login.assert_awaited_once()
        self.assertEqual(self.backend.check_credentials.await_count, 3)
        self.assertEqual(self.manager._state["accounts"]["qq"]["credential"], NEW)

    async def test_combined_diagnostic_only_displays_allowed_stage(self):
        self.backend.poll_login.side_effect = AuthError(
            "protocol", "private", diagnostic="QQ_OAUTH:PRIVATE_COOKIE"
        )
        with self.assertLogs("astrbot", "WARNING") as logs:
            await self.run_login()
        self.assertIn("QQ_OAUTH", self.texts())
        self.assertNotIn("PRIVATE_COOKIE", self.texts() + "\n".join(logs.output))

    async def test_expired_verification_does_not_retry_or_replace(self):
        stored = self.manager.store.load()
        stored["accounts"]["qq"] = {"credential": OLD, "state": "valid"}
        self.manager._save(stored)
        self.backend.poll_login.return_value = LoginPoll("authorized", NEW)
        self.backend.check_credentials.return_value = "expired"
        await self.run_login()
        self.backend.check_credentials.assert_awaited_once()
        self.assertEqual(self.manager._state["accounts"]["qq"]["credential"], OLD)

    async def test_unknown_verification_is_bounded_and_not_saved(self):
        self.backend.poll_login.return_value = LoginPoll("authorized", NEW)
        self.backend.check_credentials.return_value = "unknown"
        await self.run_login()
        self.assertEqual(self.backend.check_credentials.await_count, 3)
        self.assertNotIn("qq", self.manager._state["accounts"])
        self.assertIn("有效性校验", self.texts())

    async def test_cancelling_retry_keeps_credentials_empty(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def wait_poll(_challenge):
            entered.set()
            await release.wait()
            raise AuthError("transient", "secret")

        self.backend.poll_login.side_effect = wait_poll
        await self.manager.start_login("qq", "qq", "bot", "owner")
        await entered.wait()
        await self.manager.cancel_login("qq", "bot", "owner")
        self.assertNotIn("qq", self.manager._state["accounts"])
        self.assertFalse(self.manager._pending)

    async def test_netease_verification_required_stops_without_touching_qq(self):
        netease = FakeBackend()
        netease.poll_login.side_effect = AuthError(
            "verification_required",
            "untrusted-response-secret",
            diagnostic="NETEASE_VERIFY:HTTP200:CODE8821:MUSIC_U0",
        )
        manager = AuthManager(
            Path(self.temporary.name) / "separate",
            {"qq": self.backend, "netease": netease},
            self.notify,
            {"owner"},
            poll_interval=0.001,
        )
        self.addAsyncCleanup(manager.close)
        state = manager.store.load()
        state["accounts"]["qq"] = {"credential": OLD, "state": "valid"}
        manager._save(state)
        await manager.start_login("netease", "netease", "bot", "owner")
        with self.assertLogs("astrbot", "WARNING") as logs:
            await asyncio.wait_for(asyncio.shield(manager._pending["netease"].task), 2)
        netease.poll_login.assert_awaited_once()
        self.assertIn("8821", self.texts())
        self.assertIn("人工", self.texts())
        self.assertNotIn("untrusted-response-secret", self.texts() + str(logs.output))
        self.assertEqual(manager._state["accounts"]["qq"]["credential"], OLD)
        self.assertNotIn("netease", manager._state["accounts"])


if __name__ == "__main__":
    unittest.main()
