import asyncio
import copy
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

PLUGINS_DIR = Path(__file__).resolve().parents[2]
if str(PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGINS_DIR))

from astrbot_plugin_kook_music.music_auth.manager import AuthManager
from astrbot_plugin_kook_music.music_auth.store import (
    CredentialStore,
    CredentialStoreError,
)
from astrbot_plugin_kook_music.music_auth.types import (
    AudioResult,
    AuthError,
    LoginChallenge,
    LoginPoll,
)

OLD = {"cookie": "synthetic-old-secret", "refresh_token": "synthetic-old-refresh"}
ROTATED = {
    "cookie": "synthetic-rotated-secret",
    "refresh_token": "synthetic-rotated-refresh",
}
LOGIN = {"cookie": "synthetic-new-login", "refresh_token": "synthetic-new-refresh"}
SECOND_ROTATION = {
    "cookie": "synthetic-second-rotation",
    "refresh_token": "synthetic-second-refresh",
}


class RefreshBackend:
    def __init__(self):
        self.refresh_due = Mock(return_value=False)
        self.check_credentials = AsyncMock(return_value="valid")
        self.refresh_credentials = AsyncMock(return_value=ROTATED)
        self.resolve_audio = AsyncMock(
            return_value=AudioResult("resolved", "https://audio.example/synthetic")
        )
        self.begin_login = AsyncMock(
            return_value=LoginChallenge("synthetic-challenge", b"synthetic-qr", 180)
        )
        self.poll_login = AsyncMock(return_value=LoginPoll("authorized", LOGIN))
        self.cancel_login = AsyncMock()
        self.close = AsyncMock()


class RefreshLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name) / "accounts"
        self.now = 1_800_000_000.0
        self.clock = patch(
            "astrbot_plugin_kook_music.music_auth.manager.time.time",
            side_effect=lambda: self.now,
        )
        self.clock.start()
        self.backend = RefreshBackend()
        self.notify = AsyncMock()
        self.manager = self.new_manager()

    async def asyncTearDown(self):
        await self.manager.close()
        self.clock.stop()
        self.temporary.cleanup()

    def new_manager(self):
        manager = AuthManager(
            self.directory,
            {"qq": self.backend},
            self.notify,
            {"owner"},
            operation_timeout=1,
            poll_interval=0.001,
        )
        manager._bots.add("bot")
        return manager

    def seed(self, state="valid", *, legacy=False, **fields):
        record = {
            "credential": copy.deepcopy(OLD),
            "state": state,
            "refresh_attempted": False,
        }
        if not legacy:
            record.update(
                renewal_version=1,
                refresh_rejected=False,
                refresh_failures=0,
                refresh_retry_at=0,
                last_refresh_at=self.now - 1000,
            )
        record.update(copy.deepcopy(fields))
        snapshot = copy.deepcopy(self.manager._state)
        snapshot["accounts"]["qq"] = record
        self.manager._save(snapshot)

    def account(self):
        return self.manager._state["accounts"]["qq"]

    async def restart(self):
        await self.manager.close()
        self.manager = self.new_manager()

    async def pending_rotation(self):
        self.seed()

        async def check(credential):
            return "expired" if credential == OLD else "unknown"

        self.backend.check_credentials.side_effect = check
        await self.manager._check_provider("qq")

    async def test_valid_account_renews_before_expiration(self):
        self.seed()
        self.backend.refresh_due.return_value = True
        self.assertTrue(await self.manager._check_provider("qq"))
        self.backend.refresh_credentials.assert_awaited_once_with(OLD)
        self.assertEqual(self.account()["credential"], ROTATED)
        self.assertEqual(self.account()["state"], "valid")
        self.assertEqual(self.account()["last_refresh_at"], self.now)
        self.notify.assert_not_awaited()

    async def test_successful_proactive_refresh_has_short_rate_limit(self):
        self.seed()
        self.backend.refresh_due.return_value = True
        await self.manager._check_provider("qq")
        await self.manager._check_provider("qq")
        self.backend.refresh_credentials.assert_awaited_once()
        self.assertEqual(self.account()["credential"], ROTATED)

    async def test_unknown_validity_does_not_expire_healthy_account(self):
        self.seed()
        self.backend.check_credentials.side_effect = TimeoutError("private-response")
        await self.manager._check_provider("qq")
        self.assertEqual(self.account()["credential"], OLD)
        self.assertEqual(self.account()["state"], "valid")
        self.backend.refresh_credentials.assert_not_awaited()
        self.notify.assert_not_awaited()

    async def test_unknown_expiry_anchors_then_maintains_every_twelve_hours(self):
        self.seed(legacy=True)
        self.backend.refresh_due.return_value = None
        await self.manager._check_provider("qq")
        self.backend.refresh_credentials.assert_not_awaited()
        self.assertEqual(self.account()["last_refresh_at"], self.now)
        self.now += 12 * 3600 - 1
        await self.manager._check_provider("qq")
        self.backend.refresh_credentials.assert_not_awaited()
        self.now += 1
        await self.manager._check_provider("qq")
        self.backend.refresh_credentials.assert_awaited_once_with(OLD)
        self.assertEqual(self.account()["credential"], ROTATED)

    async def test_transient_expired_refresh_retries_with_persisted_backoff(self):
        self.seed()
        self.backend.check_credentials.return_value = "expired"
        self.backend.refresh_credentials.side_effect = TimeoutError("private-secret")
        await self.manager._check_provider("qq")
        self.assertEqual(self.account()["state"], "unknown")
        self.assertEqual(self.account()["credential"], OLD)
        self.assertFalse(self.account()["refresh_attempted"])
        self.assertFalse(self.account()["refresh_rejected"])
        self.assertEqual(self.account()["refresh_failures"], 1)
        self.assertEqual(self.account()["refresh_retry_at"], self.now + 60)
        self.notify.assert_not_awaited()
        await self.restart()
        self.now += 59
        await self.manager._check_provider("qq")
        self.backend.refresh_credentials.assert_awaited_once()
        self.now += 1
        await self.manager._check_provider("qq")
        self.assertEqual(self.backend.refresh_credentials.await_count, 2)
        self.assertEqual(self.account()["refresh_failures"], 2)
        self.assertEqual(self.account()["refresh_retry_at"], self.now + 120)

    async def test_transient_proactive_refresh_keeps_still_valid_credentials(self):
        self.seed()
        self.backend.refresh_due.return_value = True
        self.backend.refresh_credentials.side_effect = AuthError(
            "transient", "synthetic-private-response"
        )
        await self.manager._check_provider("qq")
        self.assertEqual(self.account()["credential"], OLD)
        self.assertEqual(self.account()["state"], "valid")
        self.assertFalse(self.account()["refresh_attempted"])
        self.assertEqual(self.account()["refresh_failures"], 1)
        self.assertGreater(self.account()["refresh_retry_at"], self.now)
        self.notify.assert_not_awaited()

    async def test_successful_retry_resets_transient_failure_bookkeeping(self):
        self.seed()
        self.backend.check_credentials.side_effect = lambda value: (
            "expired" if value == OLD else "valid"
        )
        self.backend.refresh_credentials.side_effect = TimeoutError("synthetic-failure")
        await self.manager._check_provider("qq")
        self.now = self.account()["refresh_retry_at"]
        self.backend.refresh_credentials.side_effect = None
        self.assertTrue(await self.manager._check_provider("qq"))
        self.assertEqual(self.account()["credential"], ROTATED)
        self.assertEqual(self.account()["refresh_failures"], 0)
        self.assertEqual(self.account()["refresh_retry_at"], 0)
        self.assertFalse(self.account()["refresh_rejected"])
        self.assertFalse(self.account()["refresh_attempted"])
        self.assertNotIn("pending_credential", self.account())
        self.assertEqual(self.backend.refresh_credentials.await_count, 2)

    async def test_repeated_transient_refreshes_cap_backoff_at_one_hour(self):
        self.seed()
        self.backend.check_credentials.return_value = "expired"
        self.backend.refresh_credentials.side_effect = RuntimeError("synthetic-failure")
        for delay in (60, 120, 240, 480, 960, 1920, 3600, 3600):
            await self.manager._check_provider("qq")
            self.assertEqual(self.account()["refresh_retry_at"] - self.now, delay)
            self.now += delay
        self.assertEqual(self.backend.refresh_credentials.await_count, 8)
        self.assertFalse(self.account()["refresh_rejected"])
        self.notify.assert_not_awaited()

    async def test_rotated_credential_is_encrypted_before_verification_finishes(self):
        self.seed()
        entered, release = asyncio.Event(), asyncio.Event()

        async def check(credential):
            if credential == OLD:
                return "expired"
            entered.set()
            await release.wait()
            return "unknown"

        self.backend.check_credentials.side_effect = check
        task = asyncio.create_task(self.manager._check_provider("qq"))
        await asyncio.wait_for(entered.wait(), 1)
        try:
            persisted = CredentialStore(self.directory).load()["accounts"]["qq"]
            self.assertEqual(persisted["credential"], OLD)
            self.assertEqual(persisted["pending_credential"], ROTATED)
            for file in self.directory.iterdir():
                raw = file.read_bytes()
                for value in ROTATED.values():
                    self.assertNotIn(value.encode(), raw)
        finally:
            release.set()
            await task

    async def test_unknown_rotated_validation_retains_pending_without_relogin_notice(
        self,
    ):
        await self.pending_rotation()
        self.assertEqual(self.account()["credential"], OLD)
        self.assertEqual(self.account()["pending_credential"], ROTATED)
        self.assertEqual(self.account()["state"], "unknown")
        self.assertFalse(self.account()["refresh_attempted"])
        self.assertGreater(self.account()["refresh_retry_at"], self.now)
        self.notify.assert_not_awaited()

    async def test_restart_checks_pending_instead_of_refreshing_old_token_again(self):
        await self.pending_rotation()
        retry_at = self.account()["refresh_retry_at"]
        await self.restart()
        self.now = retry_at
        self.backend.check_credentials.reset_mock()
        self.backend.check_credentials.side_effect = None
        self.backend.check_credentials.return_value = "valid"
        self.backend.refresh_due.return_value = True
        self.assertTrue(await self.manager._check_provider("qq"))
        self.backend.check_credentials.assert_awaited_once_with(ROTATED)
        self.backend.refresh_credentials.assert_awaited_once_with(OLD)
        self.assertEqual(self.account()["credential"], ROTATED)
        self.assertNotIn("pending_credential", self.account())
        self.assertEqual(self.account()["refresh_failures"], 0)
        self.assertEqual(self.account()["refresh_retry_at"], 0)

    async def test_pending_validation_retries_obey_backoff_without_reissuing_refresh(
        self,
    ):
        await self.pending_rotation()
        self.backend.check_credentials.reset_mock()
        await self.manager._check_provider("qq")
        self.backend.check_credentials.assert_not_awaited()
        self.now = self.account()["refresh_retry_at"]
        await self.manager._check_provider("qq")
        self.backend.check_credentials.assert_awaited_once_with(ROTATED)
        self.backend.refresh_credentials.assert_awaited_once_with(OLD)
        self.assertEqual(self.account()["refresh_failures"], 2)
        self.assertEqual(self.account()["refresh_retry_at"], self.now + 120)

    async def test_cancellation_during_pending_validation_preserves_rotation_for_restart(
        self,
    ):
        self.seed()
        entered = asyncio.Event()

        async def check(credential):
            if credential == OLD:
                return "expired"
            entered.set()
            await asyncio.Event().wait()

        self.backend.check_credentials.side_effect = check
        task = asyncio.create_task(self.manager._check_provider("qq"))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.restart()
        self.backend.check_credentials.side_effect = None
        self.backend.check_credentials.return_value = "valid"
        self.now = max(self.now, self.account().get("refresh_retry_at", 0))
        await self.manager._check_provider("qq")
        self.assertEqual(self.account()["credential"], ROTATED)
        self.backend.refresh_credentials.assert_awaited_once_with(OLD)

    async def test_cancellation_while_pending_commit_waits_for_lock_keeps_rotated_tokens(
        self,
    ):
        self.seed()
        self.backend.check_credentials.return_value = "expired"
        entered, release = asyncio.Event(), asyncio.Event()

        async def refresh(credential):
            entered.set()
            await release.wait()
            return ROTATED

        self.backend.refresh_credentials.side_effect = refresh
        task = asyncio.create_task(self.manager._check_provider("qq"))
        await asyncio.wait_for(entered.wait(), 1)
        async with self.manager._lock:
            release.set()
            await asyncio.sleep(0.02)
            task.cancel()
            await asyncio.sleep(0)
        with self.assertRaises(asyncio.CancelledError):
            await task
        persisted = CredentialStore(self.directory).load()["accounts"]["qq"]
        self.assertEqual(persisted["credential"], OLD)
        self.assertEqual(persisted["pending_credential"], ROTATED)

    async def test_close_after_cancellation_waiting_for_commit_preserves_rotation(self):
        self.seed()
        self.backend.check_credentials.return_value = "expired"
        entered, release = asyncio.Event(), asyncio.Event()

        async def refresh(credential):
            entered.set()
            await release.wait()
            return ROTATED

        self.backend.refresh_credentials.side_effect = refresh
        task = asyncio.create_task(self.manager._check_provider("qq"))
        await asyncio.wait_for(entered.wait(), 1)
        async with self.manager._lock:
            release.set()
            await asyncio.sleep(0.02)
            task.cancel()
            await asyncio.sleep(0)
            closing = asyncio.create_task(self.manager.close())
            await asyncio.sleep(0)
        await closing
        with self.assertRaises(asyncio.CancelledError):
            await task
        persisted = CredentialStore(self.directory).load()["accounts"]["qq"]
        self.assertEqual(persisted["pending_credential"], ROTATED)

    async def test_cancellation_on_refresh_return_still_persists_rotated_tokens(self):
        self.seed()
        self.backend.check_credentials.return_value = "expired"

        async def refresh(credential):
            asyncio.get_running_loop().call_soon(task.cancel)
            return ROTATED

        self.backend.refresh_credentials.side_effect = refresh
        task = asyncio.create_task(self.manager._check_provider("qq"))
        with self.assertRaises(asyncio.CancelledError):
            await task
        persisted = CredentialStore(self.directory).load()["accounts"]["qq"]
        self.assertEqual(persisted["credential"], OLD)
        self.assertEqual(persisted["pending_credential"], ROTATED)

    async def test_explicit_refresh_rejection_requests_relogin_only_once(self):
        self.seed()
        self.backend.check_credentials.return_value = "expired"
        self.backend.refresh_credentials.return_value = None
        await self.manager._check_provider("qq")
        self.assertEqual(self.account()["state"], "expired")
        self.assertTrue(self.account()["refresh_rejected"])
        self.assertTrue(self.account()["refresh_attempted"])
        self.assertEqual(self.notify.await_count, 1)
        self.now += 3600
        await self.manager._check_provider("qq")
        self.backend.refresh_credentials.assert_awaited_once()
        self.assertEqual(self.notify.await_count, 1)

    async def test_explicit_pending_rejection_keeps_formal_credential_and_stops_refresh(
        self,
    ):
        await self.pending_rotation()
        self.now = self.account()["refresh_retry_at"]
        self.backend.check_credentials.side_effect = None
        self.backend.check_credentials.return_value = "expired"
        self.backend.refresh_credentials.return_value = None
        await self.manager._check_provider("qq")
        self.assertEqual(self.account()["credential"], OLD)
        self.assertEqual(self.account()["state"], "expired")
        self.assertTrue(self.account()["refresh_rejected"])
        self.assertEqual(
            [call.args[0] for call in self.backend.refresh_credentials.await_args_list],
            [OLD, ROTATED],
        )
        self.assertEqual(self.notify.await_count, 1)

    async def test_expired_pending_access_token_renews_using_pending_refresh_token(
        self,
    ):
        await self.pending_rotation()
        self.now = self.account()["refresh_retry_at"]
        self.backend.check_credentials.side_effect = lambda value: (
            "valid" if value == SECOND_ROTATION else "expired"
        )
        self.backend.refresh_credentials.return_value = SECOND_ROTATION
        self.assertTrue(await self.manager._check_provider("qq"))
        self.assertEqual(
            [call.args[0] for call in self.backend.refresh_credentials.await_args_list],
            [OLD, ROTATED],
        )
        self.assertEqual(self.account()["credential"], SECOND_ROTATION)
        self.assertNotIn("pending_credential", self.account())
        self.notify.assert_not_awaited()

    async def test_proactive_refresh_rejection_keeps_valid_account_until_access_expires(
        self,
    ):
        self.seed()
        self.backend.refresh_due.return_value = True
        self.backend.refresh_credentials.return_value = None
        self.assertTrue(await self.manager._check_provider("qq"))
        self.assertEqual(self.account()["credential"], OLD)
        self.assertEqual(self.account()["state"], "valid")
        self.assertTrue(self.account()["refresh_rejected"])
        self.notify.assert_not_awaited()
        self.now += 3600
        self.assertTrue(await self.manager._check_provider("qq"))
        self.backend.refresh_credentials.assert_awaited_once()
        self.backend.check_credentials.return_value = "expired"
        self.assertFalse(await self.manager._check_provider("qq"))
        self.assertEqual(self.account()["state"], "expired")
        self.backend.refresh_credentials.assert_awaited_once()
        self.assertEqual(self.notify.await_count, 1)

    async def test_legacy_terminal_refresh_failure_gets_one_recovery_attempt(self):
        self.seed("expired", legacy=True, refresh_attempted=True)
        self.backend.check_credentials.side_effect = lambda value: (
            "expired" if value == OLD else "valid"
        )
        self.assertTrue(await self.manager._check_provider("qq"))
        self.backend.refresh_credentials.assert_awaited_once_with(OLD)
        self.assertEqual(self.account()["credential"], ROTATED)
        self.assertEqual(self.account()["renewal_version"], 1)
        self.assertFalse(self.account()["refresh_rejected"])

    async def test_audio_expiry_with_transient_refresh_returns_retryable_failure(self):
        self.seed()
        self.backend.resolve_audio.return_value = AudioResult("expired")
        self.backend.refresh_credentials.side_effect = TimeoutError("private-secret")
        result = await self.manager.resolve_audio(SimpleNamespace(platform="qq"))
        self.assertEqual(result.status, "transient")
        self.assertNotIn("private-secret", result.reason)
        self.assertEqual(self.account()["state"], "unknown")
        self.notify.assert_not_awaited()

    async def test_audio_expiry_after_rotation_defers_validation_without_refreshing_again(
        self,
    ):
        self.seed()
        self.backend.resolve_audio.return_value = AudioResult("expired")
        result = await self.manager.resolve_audio(SimpleNamespace(platform="qq"))
        self.assertEqual(result.status, "transient")
        self.assertEqual(self.backend.resolve_audio.await_count, 2)
        self.backend.refresh_credentials.assert_awaited_once_with(OLD)
        self.assertEqual(self.account()["credential"], ROTATED)
        self.assertEqual(self.account()["state"], "unknown")
        self.assertFalse(self.account()["refresh_rejected"])
        self.assertFalse(self.account()["refresh_pending"])
        self.assertEqual(self.account()["refresh_retry_at"], self.now + 60)
        self.backend.check_credentials.reset_mock()
        self.now += 59
        self.assertFalse(await self.manager._check_provider("qq"))
        self.backend.check_credentials.assert_not_awaited()
        self.now += 1
        self.assertTrue(await self.manager._check_provider("qq"))
        self.backend.check_credentials.assert_awaited_once_with(ROTATED)
        self.backend.refresh_credentials.assert_awaited_once_with(OLD)
        self.assertEqual(self.account()["state"], "valid")
        self.assertEqual(self.account()["refresh_retry_at"], 0)
        self.backend.resolve_audio.return_value = AudioResult(
            "resolved", "https://audio.example/recovered"
        )
        result = await self.manager.resolve_audio(SimpleNamespace(platform="qq"))
        self.assertEqual(result.status, "resolved")
        self.notify.assert_not_awaited()

    async def test_audio_resolves_after_pending_rotation_is_verified(self):
        await self.pending_rotation()
        self.now = self.account()["refresh_retry_at"]
        self.backend.check_credentials.side_effect = None
        self.backend.check_credentials.return_value = "valid"
        result = await self.manager.resolve_audio(SimpleNamespace(platform="qq"))
        self.assertEqual(result.status, "resolved")
        self.backend.resolve_audio.assert_awaited_once()
        self.assertEqual(self.backend.resolve_audio.await_args.args[1], ROTATED)
        self.backend.refresh_credentials.assert_awaited_once_with(OLD)

    async def test_audio_failure_wakes_sleeping_background_check_for_retry(self):
        self.clock.stop()
        self.now = time.time()
        self.seed(last_refresh_at=self.now)
        self.manager.check_interval = 100
        self.manager.REFRESH_RETRY_BASE = 0.03
        await self.manager.start("bot")
        await asyncio.sleep(0.02)
        self.backend.check_credentials.side_effect = lambda value: (
            "expired" if value == OLD else "valid"
        )
        self.backend.resolve_audio.return_value = AudioResult("expired")
        self.backend.refresh_credentials.side_effect = [
            TimeoutError("synthetic"),
            ROTATED,
        ]
        result = await self.manager.resolve_audio(SimpleNamespace(platform="qq"))
        self.assertEqual(result.status, "transient")
        async with asyncio.timeout(1):
            while self.account()["credential"] != ROTATED:
                await asyncio.sleep(0.005)
        self.assertEqual(self.backend.refresh_credentials.await_count, 2)
        self.assertEqual(self.account()["state"], "valid")
        self.notify.assert_not_awaited()

    async def test_malformed_renewal_fields_preserve_existing_encrypted_state(self):
        self.seed()
        previous = self.manager.store.state_path.read_bytes()
        valid = self.manager.store.load()
        invalid_fields = {
            "pending_credential": ({}, [], "private-cookie"),
            "renewal_version": (True, 0, 2, "1"),
            "refresh_rejected": (0, "false"),
            "refresh_pending": (0, "false"),
            "refresh_failures": (True, 1.0, -1, 21),
            "refresh_retry_at": (
                True,
                -1,
                float("nan"),
                float("inf"),
                1e12 + 1,
                10**400,
                "123",
            ),
            "last_refresh_at": (
                True,
                -1,
                float("nan"),
                float("inf"),
                1e12 + 1,
                10**400,
                "123",
            ),
        }
        for field, values in invalid_fields.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    invalid = copy.deepcopy(valid)
                    invalid["accounts"]["qq"][field] = value
                    with self.assertRaises(CredentialStoreError):
                        self.manager.store.save(invalid)
                    self.assertEqual(
                        self.manager.store.state_path.read_bytes(), previous
                    )
                    self.assertEqual(self.manager.store.load(), valid)

    async def test_pending_qr_login_does_not_poll_overdue_refresh_in_busy_loop(self):
        self.seed("unknown", refresh_retry_at=self.now - 1, refresh_failures=1)
        self.manager.check_interval = 100
        self.manager.poll_interval = 0.2
        self.backend.poll_login.return_value = LoginPoll("pending")
        await self.manager.start_login("qq", "qq", "bot", "owner")
        self.manager.check_accounts = AsyncMock(wraps=self.manager.check_accounts)
        self.manager._loop_task = asyncio.create_task(self.manager._check_loop())
        await asyncio.sleep(0.08)
        self.assertLessEqual(self.manager.check_accounts.await_count, 1)
        self.backend.refresh_credentials.assert_not_awaited()

    async def test_due_refresh_with_unknown_validity_does_not_run_in_busy_loop(self):
        self.seed()
        self.backend.refresh_due.return_value = True
        self.backend.next_refresh_at = Mock(return_value=self.now - 1)
        self.backend.check_credentials.return_value = "unknown"
        self.manager.check_interval = 100
        await self.manager.start("bot")
        self.backend.check_credentials.reset_mock()
        await asyncio.sleep(0.08)
        self.assertLessEqual(self.backend.check_credentials.await_count, 1)
        self.backend.refresh_credentials.assert_not_awaited()
        self.assertEqual(self.account()["state"], "valid")

    async def test_simultaneous_expiry_checks_issue_only_one_rotation(self):
        self.seed()
        self.backend.check_credentials.side_effect = lambda value: (
            "expired" if value == OLD else "valid"
        )
        await asyncio.gather(
            self.manager._check_provider(
                "qq", force_expired=True, expected_credential=OLD
            ),
            self.manager._check_provider(
                "qq", force_expired=True, expected_credential=OLD
            ),
        )
        self.backend.refresh_credentials.assert_awaited_once_with(OLD)
        self.assertEqual(self.account()["credential"], ROTATED)

    async def test_logout_during_refresh_cannot_resurrect_old_account(self):
        self.seed()
        self.backend.check_credentials.return_value = "expired"
        entered, release = asyncio.Event(), asyncio.Event()

        async def refresh(credential):
            entered.set()
            await release.wait()
            return ROTATED

        self.backend.refresh_credentials.side_effect = refresh
        task = asyncio.create_task(self.manager._check_provider("qq"))
        await asyncio.wait_for(entered.wait(), 1)
        await self.manager.logout("qq", "bot", "owner")
        release.set()
        await task
        self.assertNotIn("qq", self.manager._state["accounts"])
        self.assertNotIn("qq", CredentialStore(self.directory).load()["accounts"])

    async def test_old_audio_expiry_waits_for_rotation_and_cannot_expire_new_tokens(
        self,
    ):
        self.seed()
        self.backend.check_credentials.side_effect = lambda value: (
            "expired" if value == OLD else "valid"
        )
        entered, release = asyncio.Event(), asyncio.Event()

        async def refresh(credential):
            entered.set()
            await release.wait()
            return ROTATED

        self.backend.refresh_credentials.side_effect = refresh
        generation = self.manager._generation["qq"]
        rotation = asyncio.create_task(self.manager._check_provider("qq"))
        await asyncio.wait_for(entered.wait(), 1)
        expiry = asyncio.create_task(self.manager._mark_expired("qq", generation, OLD))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(rotation, expiry)
        self.assertEqual(self.account()["credential"], ROTATED)
        self.assertEqual(self.account()["state"], "valid")
        self.assertNotIn("pending_credential", self.account())
        self.notify.assert_not_awaited()

    async def test_new_login_during_refresh_cannot_be_overwritten_by_old_rotation(self):
        self.seed()
        self.backend.check_credentials.side_effect = lambda value: (
            "expired" if value == OLD else "valid"
        )
        entered, release = asyncio.Event(), asyncio.Event()

        async def refresh(credential):
            entered.set()
            await release.wait()
            return ROTATED

        self.backend.refresh_credentials.side_effect = refresh
        task = asyncio.create_task(self.manager._check_provider("qq"))
        await asyncio.wait_for(entered.wait(), 1)
        self.assertTrue((await self.manager.start_login("qq", "qq", "bot", "owner"))[0])
        login_task = self.manager._pending["qq"].task
        await asyncio.wait_for(asyncio.shield(login_task), 1)
        release.set()
        await task
        self.assertEqual(self.account()["credential"], LOGIN)
        self.assertNotIn("pending_credential", self.account())

    async def test_cancelled_new_login_does_not_discard_inflight_rotation(self):
        self.seed()
        self.backend.check_credentials.side_effect = lambda value: (
            "expired" if value == OLD else "valid"
        )
        self.backend.poll_login.return_value = LoginPoll("pending")
        entered, release = asyncio.Event(), asyncio.Event()

        async def refresh(credential):
            entered.set()
            await release.wait()
            return ROTATED

        self.backend.refresh_credentials.side_effect = refresh
        task = asyncio.create_task(self.manager._check_provider("qq"))
        await asyncio.wait_for(entered.wait(), 1)
        await self.manager.start_login("qq", "qq", "bot", "owner")
        await self.manager.cancel_login("qq", "bot", "owner")
        release.set()
        await task
        self.assertEqual(self.account()["credential"], OLD)
        self.assertEqual(self.account()["pending_credential"], ROTATED)
        await self.manager._check_provider("qq")
        self.assertEqual(self.account()["credential"], ROTATED)

    async def test_failed_new_login_does_not_discard_inflight_rotation(self):
        self.seed()
        self.backend.check_credentials.side_effect = lambda value: (
            "expired" if value == OLD else "valid"
        )
        self.backend.poll_login.return_value = LoginPoll("expired")
        entered, release = asyncio.Event(), asyncio.Event()

        async def refresh(credential):
            entered.set()
            await release.wait()
            return ROTATED

        self.backend.refresh_credentials.side_effect = refresh
        task = asyncio.create_task(self.manager._check_provider("qq"))
        await asyncio.wait_for(entered.wait(), 1)
        await self.manager.start_login("qq", "qq", "bot", "owner")
        await asyncio.wait_for(asyncio.shield(self.manager._pending["qq"].task), 1)
        release.set()
        await task
        self.assertEqual(self.account()["credential"], OLD)
        self.assertEqual(self.account()["pending_credential"], ROTATED)
        await self.manager._check_provider("qq")
        self.assertEqual(self.account()["credential"], ROTATED)


if __name__ == "__main__":
    unittest.main()
