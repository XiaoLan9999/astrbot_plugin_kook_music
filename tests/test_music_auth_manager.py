import asyncio
import copy
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

PLUGINS_DIR = Path(__file__).resolve().parents[2]
if str(PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGINS_DIR))

from astrbot_plugin_kook_music.music_auth.manager import AuthManager  # noqa: E402
from astrbot_plugin_kook_music.music_auth.store import (  # noqa: E402
    CredentialStore,
    CredentialStoreError,
)
from astrbot_plugin_kook_music.music_auth.types import (  # noqa: E402
    AudioResult,
    LoginChallenge,
    LoginPoll,
)

SECRET = "secret-cookie-DO-NOT-LOG-193793"
OLD = {"cookie": SECRET}
NEW = {"cookie": "new-secret-cookie"}


class FakeBackend:
    def __init__(self):
        self.begin_login = AsyncMock(
            return_value=LoginChallenge("qr-secret", b"qr-image", 180)
        )
        self.poll_login = AsyncMock(return_value=LoginPoll("pending"))
        self.cancel_login = AsyncMock()
        self.check_credentials = AsyncMock(return_value="valid")
        self.refresh_credentials = AsyncMock(return_value=None)
        self.resolve_audio = AsyncMock(
            return_value=AudioResult("resolved", "https://audio.example/song")
        )
        self.close = AsyncMock()


class CredentialStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "private"

    def account_state(self):
        return {
            "version": 1,
            "accounts": {"qq": {"credential": OLD, "state": "valid"}},
            "notices": {},
        }

    def test_encrypts_and_reopens_without_plaintext(self):
        store = CredentialStore(self.directory)
        store.save(self.account_state())
        for path in self.directory.iterdir():
            self.assertNotIn(SECRET.encode(), path.read_bytes())
        self.assertEqual(CredentialStore(self.directory).load(), self.account_state())
        self.assertNotIn(SECRET, repr(store))

    def test_load_returns_detached_copy(self):
        store = CredentialStore(self.directory)
        store.save(self.account_state())
        state = store.load()
        state["accounts"]["qq"]["credential"]["cookie"] = "tampered"
        self.assertEqual(store.load()["accounts"]["qq"]["credential"], OLD)

    def test_corrupt_ciphertext_is_not_overwritten(self):
        store = CredentialStore(self.directory)
        store.save(self.account_state())
        store.state_path.write_bytes(b"corrupt-existing-state")
        with self.assertRaises(CredentialStoreError):
            CredentialStore(self.directory)
        self.assertEqual(store.state_path.read_bytes(), b"corrupt-existing-state")

    def test_missing_key_is_not_regenerated_over_existing_state(self):
        store = CredentialStore(self.directory)
        store.save(self.account_state())
        previous = store.state_path.read_bytes()
        store.key_path.unlink()
        with self.assertRaises(CredentialStoreError):
            CredentialStore(self.directory)
        self.assertFalse(store.key_path.exists())
        self.assertEqual(store.state_path.read_bytes(), previous)

    def test_corrupt_key_is_preserved(self):
        store = CredentialStore(self.directory)
        store.key_path.write_bytes(b"bad key")
        with self.assertRaises(CredentialStoreError):
            CredentialStore(self.directory)
        self.assertEqual(store.key_path.read_bytes(), b"bad key")

    def test_runtime_external_change_is_not_overwritten(self):
        store = CredentialStore(self.directory)
        store.save(self.account_state())
        store.state_path.write_bytes(b"external-edit")
        with self.assertRaises(CredentialStoreError):
            store.save(self.account_state())
        self.assertEqual(store.state_path.read_bytes(), b"external-edit")

    def test_runtime_key_change_is_not_overwritten(self):
        store = CredentialStore(self.directory)
        store.save(self.account_state())
        previous = store.state_path.read_bytes()
        store.key_path.write_bytes(b"changed-key")
        with self.assertRaises(CredentialStoreError):
            store.save(self.account_state())
        self.assertEqual(store.state_path.read_bytes(), previous)

    def test_write_failure_keeps_previous_state_and_cleans_temporary(self):
        store = CredentialStore(self.directory)
        store.save(self.account_state())
        previous = store.state_path.read_bytes()
        updated = self.account_state()
        updated["accounts"]["qq"]["credential"] = NEW
        with patch("os.replace", side_effect=OSError("synthetic")):
            with self.assertRaises(CredentialStoreError):
                store.save(updated)
        self.assertEqual(store.state_path.read_bytes(), previous)
        self.assertEqual(list(self.directory.glob("*.tmp")), [])

    def test_malformed_state_rejected_before_disk_write(self):
        store = CredentialStore(self.directory)
        for state in (
            {},
            {"version": 1, "accounts": {}, "notices": {"key": float("nan")}},
            {"version": 1, "accounts": {"qq": {}}, "notices": {}},
        ):
            with self.subTest(state=state):
                with self.assertRaises(CredentialStoreError):
                    store.save(state)
        self.assertFalse(store.state_path.exists())

    def test_oversized_credential_does_not_create_state(self):
        store = CredentialStore(self.directory)
        state = self.account_state()
        state["accounts"]["qq"]["credential"] = {"cookie": "x" * store.MAX_BYTES}
        with self.assertRaises(CredentialStoreError):
            store.save(state)
        self.assertFalse(store.state_path.exists())

    def test_symlink_directory_rejected(self):
        target = Path(self.temporary.name) / "target"
        target.mkdir()
        try:
            self.directory.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("Host does not permit test symbolic links")
        with self.assertRaises(CredentialStoreError):
            CredentialStore(self.directory)
        self.assertEqual(list(target.iterdir()), [])

    def test_symlink_state_rejected(self):
        store = CredentialStore(self.directory)
        target = Path(self.temporary.name) / "target"
        target.write_bytes(b"preserve")
        try:
            store.state_path.symlink_to(target)
        except OSError:
            self.skipTest("Host does not permit test symbolic links")
        with self.assertRaises(CredentialStoreError):
            store.save(self.account_state())
        self.assertEqual(target.read_bytes(), b"preserve")

    @unittest.skipIf(os.name == "nt", "POSIX mode checks are not Windows ACL checks")
    def test_private_posix_file_modes(self):
        store = CredentialStore(self.directory)
        store.save(self.account_state())
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(store.key_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(store.state_path.stat().st_mode & 0o777, 0o600)


class AuthManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "auth"
        self.qq = FakeBackend()
        self.netease = FakeBackend()
        self.notify = AsyncMock()
        self.manager = self.new_manager()
        self.addAsyncCleanup(self.manager.close)

    def new_manager(self, **kwargs):
        return AuthManager(
            self.directory,
            {"qq": self.qq, "netease": self.netease},
            self.notify,
            {"admin", "admin2"},
            poll_interval=0.001,
            operation_timeout=0.1,
            **kwargs,
        )

    def seed(
        self, provider="qq", state="valid", credential=None, refresh_attempted=False
    ):
        stored = copy.deepcopy(self.manager._state)
        stored["accounts"][provider] = {
            "credential": copy.deepcopy(credential or OLD),
            "state": state,
            "refresh_attempted": refresh_attempted,
        }
        self.manager._save(stored)

    async def completed(self, provider="qq"):
        pending = self.manager._pending.get(provider)
        if pending:
            await asyncio.wait_for(asyncio.shield(pending.task), 1)

    async def wait_until(self, predicate):
        async with asyncio.timeout(1):
            while not predicate():
                await asyncio.sleep(0.001)

    async def login(self, provider="qq", method="qq", user="admin", bot="bot"):
        return await self.manager.start_login(provider, method, bot, user)

    def text_messages(self):
        return [str(call.args[2]) for call in self.notify.await_args_list]

    async def test_valid_explicit_login_is_stored_after_verification(self):
        self.qq.poll_login.return_value = LoginPoll("authorized", NEW)
        self.assertTrue((await self.login())[0])
        await self.completed()
        self.assertEqual(self.manager._state["accounts"]["qq"]["credential"], NEW)
        self.qq.check_credentials.assert_awaited_once_with(NEW)
        self.qq.cancel_login.assert_awaited_once()
        self.assertTrue(any("登录成功" in value for value in self.text_messages()))
        self.assertFalse(self.manager._pending)

    async def test_status_and_notifications_never_contain_credentials(self):
        self.qq.poll_login.return_value = LoginPoll("authorized", OLD, SECRET)
        await self.login()
        await self.completed()
        combined = (
            self.manager.status() + repr(self.manager) + "\n".join(self.text_messages())
        )
        self.assertNotIn(SECRET, combined)
        self.assertNotIn("qr-secret", combined)

    async def test_unauthorized_user_cannot_start_cancel_or_logout(self):
        self.seed()
        self.assertFalse((await self.login(user="other"))[0])
        self.assertFalse((await self.manager.cancel_login("qq", "bot", "other"))[0])
        self.assertFalse((await self.manager.logout("qq", "bot", "other"))[0])
        self.assertEqual(self.manager._state["accounts"]["qq"]["credential"], OLD)
        self.qq.begin_login.assert_not_called()

    async def test_invalid_bot_id_cannot_start(self):
        for bot in ("", None, 0):
            self.assertFalse((await self.login(bot=bot))[0])

    async def test_same_provider_cannot_be_claimed_by_another_admin_or_bot(self):
        self.assertTrue((await self.login())[0])
        self.assertFalse((await self.login(user="admin2"))[0])
        self.assertFalse((await self.login(bot="different"))[0])
        self.assertFalse((await self.manager.cancel_login("qq", "bot", "admin2"))[0])
        self.assertFalse(
            (await self.manager.cancel_login("qq", "different", "admin"))[0]
        )
        self.assertFalse((await self.manager.logout("qq", "bot", "admin2"))[0])
        self.assertTrue((await self.manager.cancel_login("qq", "bot", "admin"))[0])

    async def test_both_providers_can_login_independently(self):
        self.qq.poll_login.return_value = LoginPoll("authorized", OLD)
        self.netease.poll_login.return_value = LoginPoll("authorized", NEW)
        self.assertTrue((await self.login())[0])
        self.assertTrue((await self.login("netease", "qr", "admin2"))[0])
        await self.completed("qq")
        await self.completed("netease")
        self.assertEqual(set(self.manager._state["accounts"]), {"qq", "netease"})

    async def test_unknown_provider_rejected(self):
        self.assertFalse((await self.login("unknown"))[0])
        self.assertFalse((await self.manager.logout("unknown", "bot", "admin"))[0])

    async def test_denied_login_preserves_old_account(self):
        self.seed()
        self.qq.poll_login.return_value = LoginPoll("denied")
        await self.login()
        await self.completed()
        self.assertEqual(self.manager._state["accounts"]["qq"]["credential"], OLD)

    async def test_expired_login_preserves_old_account(self):
        self.seed()
        self.qq.poll_login.return_value = LoginPoll("expired")
        await self.login()
        await self.completed()
        self.assertEqual(self.manager._state["accounts"]["qq"]["credential"], OLD)
        self.qq.cancel_login.assert_awaited_once()

    async def test_unverified_authorization_does_not_replace_old_credentials(self):
        for validity in ("expired", "unknown"):
            with self.subTest(validity=validity):
                self.seed()
                self.qq.poll_login.return_value = LoginPoll("authorized", NEW)
                self.qq.check_credentials.return_value = validity
                await self.login()
                await self.completed()
                self.assertEqual(
                    self.manager._state["accounts"]["qq"]["credential"], OLD
                )

    async def test_malformed_authorized_result_is_rejected(self):
        self.qq.poll_login.return_value = LoginPoll("authorized", {})
        await self.login()
        await self.completed()
        self.assertNotIn("qq", self.manager._state["accounts"])

    async def test_timeout_cancels_qr_and_deletes_private_message(self):
        cleanup = AsyncMock()
        self.notify.return_value = cleanup
        await self.login()
        await self.wait_until(lambda: self.qq.poll_login.await_count > 0)
        self.manager._pending["qq"].deadline = time.monotonic() - 1
        await self.completed()
        self.qq.cancel_login.assert_awaited_once()
        cleanup.assert_awaited_once()
        self.assertTrue(any("超时" in value for value in self.text_messages()))

    async def test_timeout_before_qr_generation_has_no_private_qr_to_delete(self):
        cleanup = AsyncMock()
        self.notify.return_value = cleanup
        await self.login()
        self.manager._pending["qq"].deadline = time.monotonic() - 1
        await self.completed()
        self.qq.begin_login.assert_not_awaited()
        self.qq.cancel_login.assert_not_awaited()
        cleanup.assert_not_awaited()
        self.assertTrue(
            all(call.args[3] is None for call in self.notify.await_args_list)
        )
        self.assertTrue(any("超时" in value for value in self.text_messages()))

    async def test_success_deletes_qr_once(self):
        cleanup = AsyncMock()
        self.notify.return_value = cleanup
        self.qq.poll_login.return_value = LoginPoll("authorized", OLD)
        await self.login()
        await self.completed()
        cleanup.assert_awaited_once()

    async def test_expired_and_denied_cleanup_qr(self):
        for state in ("expired", "denied"):
            cleanup = AsyncMock()
            self.notify.return_value = cleanup
            self.qq.poll_login.return_value = LoginPoll(state)
            await self.login()
            await self.completed()
            cleanup.assert_awaited_once()

    async def test_cancel_deletes_qr_and_keeps_existing_account(self):
        self.seed()
        cleanup = AsyncMock()
        self.notify.return_value = cleanup
        await self.login()
        await self.wait_until(lambda: self.qq.poll_login.await_count > 0)
        await self.manager.cancel_login("qq", "bot", "admin")
        self.assertEqual(self.manager._state["accounts"]["qq"]["credential"], OLD)
        cleanup.assert_awaited_once()
        self.qq.cancel_login.assert_awaited_once()

    async def test_logout_cancels_pending_and_no_late_authorization_resurrection(self):
        entered = asyncio.Event()

        async def late_authorization(challenge):
            entered.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                return LoginPoll("authorized", NEW)

        self.seed()
        self.qq.poll_login.side_effect = late_authorization
        await self.login()
        await entered.wait()
        self.assertTrue((await self.manager.logout("qq", "bot", "admin"))[0])
        self.assertNotIn("qq", self.manager._state["accounts"])
        self.assertNotIn("qq", CredentialStore(self.directory).load()["accounts"])

    async def test_new_login_after_cancel_cannot_be_overwritten_by_old_poll(self):
        entered = asyncio.Event()

        async def late_authorization(challenge):
            entered.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                return LoginPoll("authorized", OLD)

        self.qq.poll_login.side_effect = late_authorization
        await self.login()
        await entered.wait()
        await self.manager.cancel_login("qq", "bot", "admin")
        self.qq.poll_login.side_effect = None
        self.qq.poll_login.return_value = LoginPoll("authorized", NEW)
        await self.login()
        await self.completed()
        self.assertEqual(self.manager._state["accounts"]["qq"]["credential"], NEW)

    async def test_notification_failure_stops_pending_qr_poll(self):
        self.notify.side_effect = RuntimeError(SECRET)
        await self.login()
        await self.completed()
        self.qq.poll_login.assert_not_called()
        self.qq.cancel_login.assert_awaited_once()

    async def test_backend_exception_is_not_leaked_to_admin(self):
        self.qq.poll_login.side_effect = RuntimeError(SECRET)
        await self.login()
        await self.completed()
        self.assertNotIn(SECRET, "\n".join(self.text_messages()))

    async def test_startup_notices_have_no_qr_and_repeated_start_is_deduplicated(self):
        await self.manager.start("bot")
        self.assertEqual(self.notify.await_count, 4)
        self.assertTrue(
            all(call.args[3] is None for call in self.notify.await_args_list)
        )
        self.qq.begin_login.assert_not_called()
        await self.manager.start("bot")
        self.assertEqual(self.notify.await_count, 4)

    async def test_notices_remain_deduplicated_after_restart(self):
        await self.manager.start("bot")
        await self.manager.close()
        restarted = self.new_manager()
        self.addAsyncCleanup(restarted.close)
        await restarted.start("bot")
        self.assertEqual(self.notify.await_count, 4)

    async def test_failed_notice_does_not_persist_cooldown(self):
        self.notify.side_effect = RuntimeError("delivery unavailable")
        await self.manager.start("bot")
        self.assertEqual(self.manager._state["notices"], {})
        self.notify.side_effect = None
        await self.manager.check_accounts()
        self.assertEqual(len(self.manager._state["notices"]), 4)

    async def test_distinct_bots_get_own_notices(self):
        await self.manager.start("bot")
        await self.manager.start("bot2")
        self.assertEqual(self.notify.await_count, 8)
        recipients = {
            (call.args[0], call.args[1]) for call in self.notify.await_args_list
        }
        self.assertEqual(
            recipients,
            {
                ("bot", "admin"),
                ("bot", "admin2"),
                ("bot2", "admin"),
                ("bot2", "admin2"),
            },
        )

    async def test_notice_cooldown_expiry_allows_reminder(self):
        await self.manager.start("bot")
        with patch("time.time", return_value=time.time() + 21601):
            await self.manager.check_accounts()
        self.assertEqual(self.notify.await_count, 8)

    async def test_network_unknown_does_not_expire_valid_account(self):
        self.seed()
        self.seed("netease")
        self.qq.check_credentials.return_value = "unknown"
        self.netease.check_credentials.side_effect = TimeoutError(SECRET)
        await self.manager.start("bot")
        self.assertEqual(self.manager._state["accounts"]["qq"]["state"], "valid")
        self.assertEqual(self.manager._state["accounts"]["netease"]["state"], "valid")
        self.qq.refresh_credentials.assert_not_called()
        self.notify.assert_not_called()

    async def test_expired_account_refreshes_once_then_notifies(self):
        self.seed()
        self.seed("netease")
        self.qq.check_credentials.return_value = "expired"
        await self.manager.start("bot")
        self.qq.refresh_credentials.assert_awaited_once_with(OLD)
        self.assertEqual(self.manager._state["accounts"]["qq"]["state"], "expired")
        self.assertEqual(self.manager._state["accounts"]["qq"]["credential"], OLD)
        self.assertEqual(self.notify.await_count, 2)
        await self.manager.check_accounts()
        self.qq.refresh_credentials.assert_awaited_once()
        self.assertEqual(self.notify.await_count, 2)

    async def test_successful_refresh_must_be_checked_before_saving(self):
        self.seed()
        self.seed("netease")
        self.qq.check_credentials.side_effect = ["expired", "valid"]
        self.qq.refresh_credentials.return_value = NEW
        await self.manager.start("bot")
        self.assertEqual(self.manager._state["accounts"]["qq"]["credential"], NEW)
        self.assertEqual(self.manager._state["accounts"]["qq"]["state"], "valid")
        self.notify.assert_not_called()

    async def test_bad_refreshed_credential_never_replaces_saved_account(self):
        self.seed()
        self.qq.check_credentials.side_effect = ["expired", "unknown"]
        self.qq.refresh_credentials.return_value = NEW
        await self.manager._check_provider("qq")
        self.assertEqual(self.manager._state["accounts"]["qq"]["credential"], OLD)
        self.assertEqual(self.manager._state["accounts"]["qq"]["state"], "expired")

    async def test_check_while_login_is_pending_cannot_overwrite_account(self):
        self.seed()
        await self.login()
        await self.manager._check_provider("qq")
        self.qq.check_credentials.assert_not_called()

    async def test_stale_background_check_cannot_undo_logout(self):
        self.seed()
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed_check(credential):
            entered.set()
            await release.wait()
            return "expired"

        self.qq.check_credentials.side_effect = delayed_check
        task = asyncio.create_task(self.manager._check_provider("qq"))
        await entered.wait()
        await self.manager.logout("qq", "bot", "admin")
        release.set()
        await task
        self.assertNotIn("qq", self.manager._state["accounts"])

    async def test_resolve_uses_exact_song_and_detached_credential(self):
        self.seed()
        song = SimpleNamespace(platform="qq", id="exact-mid")
        result = await self.manager.resolve_audio(song)
        self.assertEqual(result.status, "resolved")
        self.assertIs(self.qq.resolve_audio.await_args.args[0], song)
        self.assertEqual(self.qq.resolve_audio.await_args.args[1], OLD)
        self.assertIsNot(
            self.qq.resolve_audio.await_args.args[1],
            self.manager._state["accounts"]["qq"]["credential"],
        )

    async def test_missing_and_other_platforms_allow_anonymous_fallback(self):
        for provider in ("qq", "kugou", "bilibili"):
            result = await self.manager.resolve_audio(
                SimpleNamespace(platform=provider)
            )
            self.assertEqual(result.status, "unavailable")
        self.qq.resolve_audio.assert_not_called()

    async def test_network_audio_error_is_transient_not_expired(self):
        self.seed()
        self.qq.resolve_audio.side_effect = RuntimeError(SECRET)
        result = await self.manager.resolve_audio(SimpleNamespace(platform="qq"))
        self.assertEqual(result.status, "transient")
        self.assertNotIn(SECRET, result.reason)
        self.assertEqual(self.manager._state["accounts"]["qq"]["state"], "valid")
        self.qq.refresh_credentials.assert_not_called()

    async def test_audio_expiry_refreshes_then_retries_once(self):
        self.seed()
        self.qq.resolve_audio.side_effect = [
            AudioResult("expired"),
            AudioResult("resolved", "https://audio.example/refreshed"),
        ]
        self.qq.refresh_credentials.return_value = NEW
        result = await self.manager.resolve_audio(SimpleNamespace(platform="qq"))
        self.assertEqual(result.status, "resolved")
        self.assertEqual(self.qq.resolve_audio.await_count, 2)
        self.assertEqual(self.manager._state["accounts"]["qq"]["credential"], NEW)

    async def test_audio_expiry_without_refresh_marks_account_and_allows_fallback(self):
        self.seed()
        self.qq.resolve_audio.return_value = AudioResult("expired", reason=SECRET)
        result = await self.manager.resolve_audio(SimpleNamespace(platform="qq"))
        self.assertEqual(result.status, "expired")
        self.assertNotIn(SECRET, result.reason)
        self.assertEqual(self.manager._state["accounts"]["qq"]["state"], "expired")

    async def test_unavailable_and_bad_backend_audio_results_are_sanitized(self):
        self.seed()
        for status, expected in (
            ("unavailable", "unavailable"),
            ("garbage", "transient"),
            ("resolved", "transient"),
        ):
            self.qq.resolve_audio.return_value = AudioResult(
                status, "file:///private", SECRET
            )
            result = await self.manager.resolve_audio(SimpleNamespace(platform="qq"))
            self.assertEqual(result.status, expected)
            self.assertNotIn(SECRET, repr(result))

    async def test_storage_corruption_disables_account_module_without_overwrite(self):
        self.seed()
        self.manager.store.state_path.write_bytes(b"externally-corrupted")
        self.qq.check_credentials.return_value = "expired"
        await self.manager.check_accounts()
        self.assertTrue(self.manager._storage_failed)
        self.assertFalse((await self.login())[0])
        result = await self.manager.resolve_audio(SimpleNamespace(platform="qq"))
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(
            self.manager.store.state_path.read_bytes(), b"externally-corrupted"
        )

    async def test_close_cancels_poll_deletes_qr_and_closes_both_backends(self):
        cleanup = AsyncMock()
        self.notify.return_value = cleanup
        await self.login()
        await self.wait_until(lambda: self.qq.poll_login.await_count > 0)
        await self.manager.close()
        self.assertEqual(self.manager._pending, {})
        cleanup.assert_awaited_once()
        self.qq.close.assert_awaited_once()
        self.netease.close.assert_awaited_once()
        self.assertFalse((await self.login())[0])
        await self.manager.close()
        self.qq.close.assert_awaited_once()

    async def test_bad_cleanup_does_not_keep_login_pending(self):
        cleanup = AsyncMock(side_effect=RuntimeError(SECRET))
        self.notify.return_value = cleanup
        self.qq.cancel_login.side_effect = RuntimeError(SECRET)
        self.qq.poll_login.return_value = LoginPoll("authorized", OLD)
        await self.login()
        await self.completed()
        self.assertEqual(self.manager._pending, {})
        self.assertEqual(self.manager._state["accounts"]["qq"]["state"], "valid")

    async def test_late_poll_that_suppresses_timeout_cannot_save_credentials(self):
        entered = asyncio.Event()
        backend_tasks = []

        async def late_poll(challenge):
            backend_tasks.append(asyncio.current_task())
            entered.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                return LoginPoll("authorized", OLD)

        self.qq.poll_login.side_effect = late_poll
        await self.login()
        await entered.wait()
        self.manager._pending["qq"].deadline = time.monotonic() - 1
        backend_tasks[0].cancel()
        await self.completed()
        self.assertNotIn("qq", self.manager._state["accounts"])
        self.qq.cancel_login.assert_awaited_once()
        self.assertTrue(any("超时" in value for value in self.text_messages()))

    async def test_late_begin_login_is_cleaned_without_sending_expired_qr(self):
        entered = asyncio.Event()
        backend_tasks = []

        async def late_begin(method):
            backend_tasks.append(asyncio.current_task())
            entered.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                return LoginChallenge("late", b"late-qr", 180)

        self.qq.begin_login.side_effect = late_begin
        await self.login()
        await entered.wait()
        self.manager._pending["qq"].deadline = time.monotonic() - 1
        backend_tasks[0].cancel()
        await self.completed()
        self.qq.cancel_login.assert_awaited_once()
        self.assertTrue(
            all(call.args[3] is None for call in self.notify.await_args_list)
        )

    async def test_double_cancel_during_cleanup_still_deletes_qr_once(self):
        entered, release = asyncio.Event(), asyncio.Event()
        cleanup = AsyncMock()

        async def backend_cancel(challenge):
            entered.set()
            await release.wait()

        self.qq.cancel_login.side_effect = backend_cancel
        self.qq.poll_login.return_value = LoginPoll("expired")
        self.notify.return_value = cleanup
        await self.login()
        await entered.wait()
        cancellation = asyncio.create_task(
            self.manager.cancel_login("qq", "bot", "admin")
        )
        await asyncio.sleep(0)
        shutdown = asyncio.create_task(self.manager.close())
        await asyncio.sleep(0)
        release.set()
        await cancellation
        await shutdown
        cleanup.assert_awaited_once()
        self.assertFalse(self.manager._cleanup_tasks)

    async def test_login_commit_is_revoked_when_cancelled_during_final_validation(self):
        self.seed()
        entered = asyncio.Event()

        async def late_check(credential):
            entered.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                return "valid"

        self.qq.poll_login.return_value = LoginPoll("authorized", NEW)
        self.qq.check_credentials.side_effect = late_check
        await self.login()
        await entered.wait()
        await self.manager.cancel_login("qq", "bot", "admin")
        self.assertEqual(self.manager._state["accounts"]["qq"]["credential"], OLD)

    async def test_refresh_retry_expired_is_persisted_and_notified_once(self):
        self.seed()
        self.seed("netease")
        await self.manager.start("bot")
        self.qq.resolve_audio.return_value = AudioResult("expired")
        self.qq.refresh_credentials.return_value = NEW
        result = await self.manager.resolve_audio(SimpleNamespace(platform="qq"))
        self.assertEqual(result.status, "expired")
        self.assertEqual(self.manager._state["accounts"]["qq"]["state"], "expired")
        self.assertEqual(self.manager._state["accounts"]["qq"]["credential"], NEW)
        self.qq.refresh_credentials.assert_awaited_once()
        self.assertEqual(self.notify.await_count, 2)

    async def test_old_expired_result_does_not_refresh_newly_refreshed_credentials(
        self,
    ):
        self.seed()
        self.qq.refresh_credentials.return_value = NEW
        self.assertTrue(
            await self.manager._check_provider(
                "qq", force_expired=True, expected_credential=OLD
            )
        )
        self.assertTrue(
            await self.manager._check_provider(
                "qq", force_expired=True, expected_credential=OLD
            )
        )
        self.qq.refresh_credentials.assert_awaited_once()
        self.assertEqual(self.manager._state["accounts"]["qq"]["credential"], NEW)

    async def test_logout_only_removes_selected_provider(self):
        self.seed()
        self.seed("netease", credential=NEW)
        await self.manager.logout("qq", "bot", "admin")
        self.assertNotIn("qq", self.manager._state["accounts"])
        self.assertEqual(self.manager._state["accounts"]["netease"]["credential"], NEW)

    async def test_malformed_backend_audio_result_is_transient(self):
        self.seed()
        self.qq.resolve_audio.return_value = None
        result = await self.manager.resolve_audio(SimpleNamespace(platform="qq"))
        self.assertEqual(result.status, "transient")


if __name__ == "__main__":
    unittest.main()
