import copy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

PLUGINS_DIR = Path(__file__).resolve().parents[2]
if str(PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGINS_DIR))

from astrbot_plugin_kook_music.music_auth.qq_backend import QQBackend  # noqa: E402
from astrbot_plugin_kook_music.music_auth.types import AuthError  # noqa: E402


CREATED = 1_700_000_000
CREDENTIAL = {
    "musicid": 123,
    "musickey": "SYNTHETIC_MUSIC_KEY",
    "str_musicid": "123",
    "refresh_key": "SYNTHETIC_REFRESH_KEY",
    "refresh_token": "SYNTHETIC_REFRESH_TOKEN",
    "login_type": 2,
    "musickey_create_time": CREATED,
    "key_expires_in": 86400,
}


class QQRefreshPolicyTests(unittest.TestCase):
    def setUp(self):
        self.backend = QQBackend()

    def test_music_key_deadline_and_default_lead(self):
        self.assertFalse(self.backend.refresh_due(CREDENTIAL, now=CREATED + 82799))
        self.assertTrue(self.backend.refresh_due(CREDENTIAL, now=CREATED + 82800))
        self.assertTrue(self.backend.refresh_due(CREDENTIAL, now=CREATED + 86400))
        self.assertIsNone(self.backend._client)

    def test_camel_fields_and_consistent_aliases(self):
        credential = {**CREDENTIAL, "musickeyCreateTime": str(CREATED), "keyExpiresIn": "86400"}
        self.assertTrue(self.backend.refresh_due(credential, now=CREATED + 82800))
        credential.pop("musickey_create_time")
        credential.pop("key_expires_in")
        self.assertTrue(self.backend.refresh_due(credential, now=CREATED + 82800))

    def test_conflicting_or_bad_duplicate_aliases_are_unknown(self):
        for changes in (
            {"musickeyCreateTime": CREATED + 1},
            {"keyExpiresIn": 86401},
            {"musickeyCreateTime": True},
            {"keyExpiresIn": None},
        ):
            with self.subTest(changes=changes):
                self.assertIsNone(self.backend.refresh_due({**CREDENTIAL, **changes}, now=CREATED))

    def test_no_refresh_material_is_not_refreshable(self):
        for field in ("refresh_key", "refresh_token"):
            for value in (None, "", "  ", False, 42):
                with self.subTest(field=field, value=value):
                    self.assertFalse(self.backend.refresh_due({**CREDENTIAL, field: value}, now=CREATED))
        self.assertFalse(self.backend.refresh_due(None, now=CREATED))

    def test_missing_music_timing_does_not_use_oauth_expiry(self):
        credential = {key: value for key, value in CREDENTIAL.items() if key not in {"musickey_create_time", "key_expires_in"}}
        credential.update(expired_at=1, need_refresh_key_in=1, needRefreshKeyIn=1)
        self.assertIsNone(self.backend.refresh_due(credential, now=CREATED))

    def test_malformed_timing_is_unknown(self):
        for field in ("musickey_create_time", "key_expires_in"):
            for value in (True, False, None, -1, 0, "bad", "nan", "inf", float("nan"), float("inf"), {}, 10**1000):
                with self.subTest(field=field, value=type(value).__name__):
                    self.assertIsNone(self.backend.refresh_due({**CREDENTIAL, field: value}, now=CREATED))

    def test_overflowed_sum_is_unknown(self):
        credential = {**CREDENTIAL, "musickey_create_time": 1e308, "key_expires_in": 1e308}
        self.assertIsNone(self.backend.refresh_due(credential, now=CREATED))

    def test_invalid_now_or_lead_is_unknown(self):
        for value in (True, None, -1, "nan", "inf", 10**1000):
            with self.subTest(argument="now", value=type(value).__name__):
                self.assertIsNone(self.backend.refresh_due(CREDENTIAL, now=value))
            with self.subTest(argument="lead", value=type(value).__name__):
                self.assertIsNone(self.backend.refresh_due(CREDENTIAL, now=CREATED, lead_seconds=value))

    def test_zero_lead_waits_until_exact_expiry(self):
        self.assertFalse(self.backend.refresh_due(CREDENTIAL, now=CREATED + 86399, lead_seconds=0))
        self.assertTrue(self.backend.refresh_due(CREDENTIAL, now=CREATED + 86400, lead_seconds=0))

    def test_short_lifetime_lead_is_capped_at_quarter(self):
        credential = {**CREDENTIAL, "key_expires_in": 100}
        self.assertFalse(self.backend.refresh_due(credential, now=CREATED))
        self.assertFalse(self.backend.refresh_due(credential, now=CREATED + 74))
        self.assertTrue(self.backend.refresh_due(credential, now=CREATED + 75))

    def test_both_login_methods_share_timing_and_input_is_not_mutated(self):
        for login_type in (1, 2):
            credential = {**CREDENTIAL, "login_type": login_type}
            original = copy.deepcopy(credential)
            self.assertTrue(self.backend.refresh_due(credential, now=CREATED + 82800))
            self.assertEqual(credential, original)

    def test_next_refresh_at_keeps_deadline_before_and_after_due_time(self):
        for now in (CREATED, CREATED + 82799, CREATED + 82800, CREATED + 90000):
            self.assertEqual(
                self.backend.next_refresh_at(CREDENTIAL, now=now), CREATED + 82800
            )

    def test_next_refresh_at_short_lifetime_and_zero_lead(self):
        credential = {**CREDENTIAL, "key_expires_in": 100}
        self.assertEqual(
            self.backend.next_refresh_at(credential, now=CREATED), CREATED + 75
        )
        self.assertEqual(
            self.backend.next_refresh_at(credential, now=CREATED, lead_seconds=0),
            CREATED + 100,
        )

    def test_next_refresh_at_accepts_camel_timing(self):
        credential = {key: value for key, value in CREDENTIAL.items() if key not in {"musickey_create_time", "key_expires_in"}}
        credential.update(musickeyCreateTime=CREATED, keyExpiresIn=86400)
        self.assertEqual(
            self.backend.next_refresh_at(credential, now=CREATED), CREATED + 82800
        )

    def test_next_refresh_at_unknown_or_unrefreshable_is_none(self):
        for credential in (
            None,
            {**CREDENTIAL, "refresh_token": ""},
            {**CREDENTIAL, "key_expires_in": 0},
            {**CREDENTIAL, "musickeyCreateTime": CREATED + 1},
        ):
            self.assertIsNone(self.backend.next_refresh_at(credential, now=CREATED))
        self.assertIsNone(self.backend.next_refresh_at(CREDENTIAL, now=True))
        self.assertIsNone(self.backend.next_refresh_at(CREDENTIAL, now=float("nan")))


class SyntheticCredential:
    def __init__(self, payload):
        self.payload = copy.deepcopy(payload)
        self.musicid = payload.get("musicid", 0)
        self.musickey = payload.get("musickey", "")
        self.str_musicid = payload.get("str_musicid", "")
        self.refresh_key = payload.get("refresh_key", "")
        self.refresh_token = payload.get("refresh_token", "")
        self.login_type = payload.get("login_type", 2)

    @classmethod
    def model_validate(cls, payload):
        if not isinstance(payload, dict):
            raise ValueError("Synthetic invalid payload")
        return cls(payload)

    def model_copy(self, *, update):
        return type(self)({**self.payload, **update})

    def model_dump(self, **kwargs):
        return copy.deepcopy(self.payload)


class QQRefreshOutcomeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.refresh = AsyncMock()
        client = SimpleNamespace(login=SimpleNamespace(refresh_credential=self.refresh))
        self.backend = QQBackend(client_factory=lambda: client, sdk=SimpleNamespace(Credential=SyntheticCredential), timeout=0.01)

    async def test_qq_and_wechat_refresh_keep_identity_and_rotate_material(self):
        for login_type in (1, 2):
            credential = {**CREDENTIAL, "login_type": login_type}
            updated = {**credential, "musickey": "SYNTHETIC_ROTATED_KEY", "refresh_token": "SYNTHETIC_ROTATED_TOKEN"}
            self.refresh.return_value = SyntheticCredential(updated)
            result = await self.backend.refresh_credentials(credential)
            self.assertEqual(result, updated)
            self.assertEqual(credential["refresh_token"], CREDENTIAL["refresh_token"])

    async def test_missing_material_or_invalid_input_is_not_refreshable(self):
        for credential in (None, {}, {**CREDENTIAL, "refresh_key": ""}, {**CREDENTIAL, "refresh_token": ""}):
            self.assertIsNone(await self.backend.refresh_credentials(credential))
        self.refresh.assert_not_called()

    async def test_confirmed_expiry_is_not_refreshable(self):
        for code in (1000, 104400, 104401):
            error = RuntimeError("SYNTHETIC_PRIVATE_ERROR")
            error.code = code
            self.refresh.side_effect = error
            self.assertIsNone(await self.backend.refresh_credentials(CREDENTIAL))

    async def test_network_and_unconfirmed_errors_remain_transient(self):
        for error in (TimeoutError("SYNTHETIC_PRIVATE_ERROR"), OSError("SYNTHETIC_PRIVATE_ERROR"), AuthError("transient", "SYNTHETIC_PRIVATE_ERROR")):
            self.refresh.side_effect = error
            with self.assertRaises(AuthError) as caught:
                await self.backend.refresh_credentials(CREDENTIAL)
            self.assertEqual(caught.exception.kind, "transient")
            self.assertNotIn("SYNTHETIC", str(caught.exception))

    async def test_unconfirmed_service_codes_remain_transient(self):
        for code in (429, 500, 104604):
            error = RuntimeError("SYNTHETIC_PRIVATE_ERROR")
            error.code = code
            self.refresh.side_effect = error
            with self.assertRaises(AuthError) as caught:
                await self.backend.refresh_credentials(CREDENTIAL)
            self.assertEqual(caught.exception.kind, "transient")
            self.assertNotIn("SYNTHETIC", str(caught.exception))

    async def test_real_timeout_cancels_refresh_and_remains_transient(self):
        import asyncio

        cancelled = asyncio.Event()

        async def wait(credential):
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        self.refresh.side_effect = wait
        with self.assertRaises(AuthError) as caught:
            await self.backend.refresh_credentials(CREDENTIAL)
        self.assertEqual(caught.exception.kind, "transient")
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.backend._operations, set())

    async def test_malformed_success_is_transient_instead_of_permanent_expiry(self):
        for result in (None, SyntheticCredential({}), SyntheticCredential({**CREDENTIAL, "musickey": ""})):
            self.refresh.return_value = result
            with self.assertRaises(AuthError) as caught:
                await self.backend.refresh_credentials(CREDENTIAL)
            self.assertEqual(caught.exception.kind, "transient")

    async def test_account_or_login_method_switch_is_rejected(self):
        for changes in ({"musicid": 456}, {"login_type": 1}):
            self.refresh.return_value = SyntheticCredential({**CREDENTIAL, **changes})
            with self.assertRaises(AuthError) as caught:
                await self.backend.refresh_credentials(CREDENTIAL)
            self.assertEqual(caught.exception.kind, "transient")

    async def test_cancellation_is_propagated(self):
        import asyncio

        self.refresh.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.backend.refresh_credentials(CREDENTIAL)


if __name__ == "__main__":
    unittest.main()
