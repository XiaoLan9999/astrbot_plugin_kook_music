import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

PLUGINS_DIR = Path(__file__).resolve().parents[2]
if str(PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGINS_DIR))

from astrbot_plugin_kook_music import kook_permissions


def guild_data(**overrides):
    return {
        "id": "100",
        "user_id": "200",
        "roles": [
            {"role_id": 10, "permissions": 1, "name": "ordinary name"},
            {"role_id": 20, "permissions": 2, "name": "Administrator"},
        ],
        **overrides,
    }


def user_data(**overrides):
    return {"id": "300", "roles": [10], **overrides}


class FakeResponse:
    def __init__(self, data=None, status=200, payload=None, error=None):
        self.status = status
        self.payload = {"code": 0, "data": data} if payload is None else payload
        self.error = error

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        return self.payload


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("Unexpected API request")
        return self.responses.pop(0)


class KookPermissionTests(unittest.IsolatedAsyncioTestCase):
    async def check(self, guild=None, user=None, **kwargs):
        session = FakeSession(
            FakeResponse(data=guild if guild is not None else guild_data()),
            FakeResponse(data=user if user is not None else user_data()),
        )
        with patch.object(kook_permissions, "_get_session", AsyncMock(return_value=session)):
            allowed = await kook_permissions.is_guild_admin(
                kwargs.get("token", "test-token"), kwargs.get("guild_id", "100"), kwargs.get("user_id", "300")
            )
        return allowed, session

    async def test_guild_owner_is_allowed_without_role_lookup(self):
        allowed, session = await self.check(user_id="200")
        self.assertTrue(allowed)
        self.assertEqual(len(session.calls), 1)

    async def test_owner_uses_user_id_not_master_id(self):
        allowed, _ = await self.check(guild=guild_data(master_id="300"), user=user_data(roles=[]))
        self.assertFalse(allowed)

    async def test_administrator_bit_is_allowed_and_queries_are_scoped(self):
        allowed, session = await self.check()
        self.assertTrue(allowed)
        self.assertEqual(session.calls[0][0], f"{kook_permissions.KOOK_API_BASE}/guild/view")
        self.assertEqual(session.calls[0][1]["params"], {"guild_id": "100"})
        self.assertEqual(session.calls[1][0], f"{kook_permissions.KOOK_API_BASE}/user/view")
        self.assertEqual(session.calls[1][1]["params"], {"guild_id": "100", "user_id": "300"})
        for _, kwargs in session.calls:
            self.assertEqual(kwargs["headers"], {"Authorization": "Bot test-token"})
            self.assertEqual(kwargs["timeout"].total, 5)
            self.assertFalse(kwargs["allow_redirects"])

    async def test_ordinary_member_and_manage_guild_role_are_denied(self):
        for roles in ([], [20], [999]):
            with self.subTest(roles=roles):
                allowed, _ = await self.check(user=user_data(roles=roles))
                self.assertFalse(allowed)

    async def test_role_ids_must_belong_to_requested_guild(self):
        allowed, _ = await self.check(guild=guild_data(roles=[{"role_id": 999, "permissions": 1}]))
        self.assertFalse(allowed)

    async def test_everyone_role_permissions_apply_to_verified_guild_members(self):
        allowed, _ = await self.check(
            guild=guild_data(roles=[{"role_id": 0, "permissions": 1}]), user=user_data(roles=[])
        )
        self.assertTrue(allowed)
        for user in (user_data(roles=None), user_data(id=None)):
            with self.subTest(user=user):
                allowed, _ = await self.check(
                    guild=guild_data(roles=[{"role_id": 0, "permissions": 1}]), user=user
                )
                self.assertFalse(allowed)

    async def test_string_ids_and_decimal_permission_values_work(self):
        allowed, _ = await self.check(
            guild=guild_data(roles=[{"role_id": "10", "permissions": "3"}]), user=user_data(roles=["10"])
        )
        self.assertTrue(allowed)

    async def test_wrong_guild_or_user_identity_is_denied(self):
        for guild, user in [
            (guild_data(id="101"), user_data()),
            (guild_data(id=None), user_data()),
            (guild_data(), user_data(id="301")),
            (guild_data(), user_data(id=None)),
            (guild_data(user_id=None), user_data()),
        ]:
            with self.subTest(guild=guild, user=user):
                allowed, _ = await self.check(guild=guild, user=user)
                self.assertFalse(allowed)

    async def test_malformed_role_data_is_denied(self):
        for roles in (None, {}, [None], [{"role_id": 10}], [{"permissions": 1}],
                      [{"role_id": 10, "permissions": True}], [{"role_id": 10, "permissions": -1}],
                      [{"role_id": 10, "permissions": "admin"}], [{"role_id": True, "permissions": 1}],
                      [{"id": 0, "permissions": 1}], [{"role_id": "-1", "permissions": 1}],
                      [{"role_id": 10.0, "permissions": 1}], [{"role_id": "10.0", "permissions": 1}],
                      [{"role_id": 10, "permissions": 1}, {"role_id": 10, "permissions": 0}]):
            with self.subTest(roles=roles):
                allowed, _ = await self.check(guild=guild_data(roles=roles))
                self.assertFalse(allowed)
        for roles in (None, {}, "10", [True], [10.0], [None], [{"role_id": 10}], [10, None], ["-1"], ["10.0"]):
            with self.subTest(member_roles=roles):
                allowed, _ = await self.check(user=user_data(roles=roles))
                self.assertFalse(allowed)

    async def test_missing_arguments_do_not_call_network(self):
        for token, guild_id, user_id in [("", "100", "300"), ("test-token", "", "300"),
                                        ("test-token", "100", ""), (None, "100", "300"),
                                        ("test-token", "100", " ")]:
            with self.subTest(args=(token, guild_id, user_id)):
                with patch.object(kook_permissions, "_get_session", AsyncMock()) as get_session:
                    self.assertFalse(await kook_permissions.is_guild_admin(token, guild_id, user_id))
                    get_session.assert_not_called()

    async def test_http_and_api_failures_at_either_step_are_denied(self):
        bad_responses = [
            lambda: FakeResponse(status=403),
            lambda: FakeResponse(status=302),
            lambda: FakeResponse(payload=[]),
            lambda: FakeResponse(payload={}),
            lambda: FakeResponse(payload={"code": "0", "data": guild_data()}),
            lambda: FakeResponse(payload={"code": False, "data": guild_data()}),
            lambda: FakeResponse(payload={"code": 1, "data": guild_data()}),
            lambda: FakeResponse(payload={"code": 0, "data": []}),
            lambda: FakeResponse(error=asyncio.TimeoutError("test-token should not leak")),
            lambda: FakeResponse(error=ValueError("test-token should not leak")),
        ]
        for at_user in (False, True):
            for make_response in bad_responses:
                with self.subTest(at_user=at_user, response=make_response):
                    responses = ([FakeResponse(data=guild_data())] if at_user else []) + [make_response()]
                    session = FakeSession(*responses)
                    with patch.object(kook_permissions, "_get_session", AsyncMock(return_value=session)):
                        self.assertFalse(await kook_permissions.is_guild_admin("test-token", "100", "300"))

    async def test_exception_logs_do_not_expose_tokens(self):
        session = FakeSession(FakeResponse(error=ValueError("secret-token")))
        with patch.object(kook_permissions, "_get_session", AsyncMock(return_value=session)):
            with self.assertLogs("astrbot", level="DEBUG") as captured:
                self.assertFalse(await kook_permissions.is_guild_admin("secret-token", "100", "300"))
        self.assertNotIn("secret-token", " ".join(captured.output))

    async def test_revocation_and_different_bot_tokens_are_not_cached(self):
        session = FakeSession(
            FakeResponse(data=guild_data()), FakeResponse(data=user_data()),
            FakeResponse(data=guild_data()), FakeResponse(data=user_data(roles=[])),
            FakeResponse(data=guild_data()), FakeResponse(data=user_data()),
        )
        with patch.object(kook_permissions, "_get_session", AsyncMock(return_value=session)):
            self.assertTrue(await kook_permissions.is_guild_admin("bot-a", "100", "300"))
            self.assertFalse(await kook_permissions.is_guild_admin("bot-a", "100", "300"))
            self.assertTrue(await kook_permissions.is_guild_admin("bot-b", "100", "300"))
        self.assertEqual([call[1]["headers"]["Authorization"] for call in session.calls],
                         ["Bot bot-a"] * 4 + ["Bot bot-b"] * 2)

    async def test_cancellation_propagates(self):
        with patch.object(kook_permissions, "_get_session", AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await kook_permissions.is_guild_admin("test-token", "100", "300")


if __name__ == "__main__":
    unittest.main()
