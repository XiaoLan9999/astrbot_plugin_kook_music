import asyncio
import copy
import json
import sys
import types
import unittest
from enum import Enum
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_kook_music import kook_api

TOKEN = "synthetic-token"
BOT = "12345"
CHANNEL = "67890"
CARD = {
    "type": "card",
    "modules": [
        {
            "type": "section",
            "text": {"type": "plain-text", "content": "synthetic-private-title"},
        }
    ],
}


class Response:
    def __init__(self, data=None, *, status=200, error=None, block=None, on_enter=None):
        self.data = (
            data if data is not None else {"code": 0, "data": {"msg_id": "http-id"}}
        )
        self.status = status
        self.headers = {}
        self.error = error
        self.block = block
        self.on_enter = on_enter
        self.closed = False
        self.cancelled = False

    async def __aenter__(self):
        if self.on_enter:
            self.on_enter()
        return self

    async def __aexit__(self, *_args):
        self.closed = True

    async def json(self):
        try:
            if self.block:
                await self.block.wait()
            if self.error:
                raise self.error
            return self.data
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, copy.deepcopy(kwargs)))
        return self.responses.pop(0)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, copy.deepcopy(kwargs)))
        return self.responses.pop(0)


def receipt(correlation, **changes):
    event = {
        "type": 10,
        "channel_type": "GROUP",
        "target_id": CHANNEL,
        "author_id": BOT,
        "msg_id": "gateway-id",
        "nonce": correlation,
    }
    event.update(changes)
    return event


async def wait_calls(session, count=1):
    async with asyncio.timeout(1):
        while len(session.calls) < count:
            await asyncio.sleep(0)


class DeliveryReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertFalse(kook_api._pending_card_receipts)
        self.session = Session([])
        self.patch_session = patch.object(
            kook_api, "_get_session", AsyncMock(return_value=self.session)
        )
        self.patch_session.start()
        self.addCleanup(self.patch_session.stop)
        for key, value in (("_CARD_HTTP_TIMEOUT", 0.2), ("_CARD_RECEIPT_GRACE", 0.03)):
            patched = patch.object(kook_api, key, value)
            patched.start()
            self.addCleanup(patched.stop)
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        await kook_api.close_shared_session()
        self.assertFalse(kook_api._pending_card_receipts)

    def send(self, token=TOKEN, channel=CHANNEL, card=None):
        task = asyncio.create_task(
            kook_api.send_card_message(token, channel, CARD if card is None else card)
        )
        self.tasks.append(task)
        return task

    def nonce(self, index=-1):
        return self.session.calls[index][2]["json"]["nonce"]

    async def test_http_success_returns_actual_id_and_clears_receipt(self):
        self.session.responses.append(Response())
        self.assertEqual(await self.send(), "http-id")
        payload = self.session.calls[0][2]["json"]
        self.assertEqual(payload["target_id"], CHANNEL)
        self.assertEqual(payload["type"], 10)
        self.assertRegex(payload["nonce"], r"^[a-f0-9]{32}$")
        self.assertFalse(self.session.calls[0][2]["allow_redirects"])
        self.assertFalse(kook_api._pending_card_receipts)

    async def test_receipt_registered_before_http_send_can_observe_immediately(self):
        def immediate_receipt():
            self.assertTrue(
                kook_api.observe_card_receipt(TOKEN, BOT, receipt(self.nonce()))
            )

        response = Response(block=asyncio.Event(), on_enter=immediate_receipt)
        self.session.responses.append(response)
        self.assertEqual(await asyncio.wait_for(self.send(), 0.1), "gateway-id")
        self.assertTrue(response.cancelled)
        self.assertTrue(response.closed)

    async def test_gateway_success_does_not_wait_for_http_timeout_or_resend(self):
        response = Response(block=asyncio.Event())
        self.session.responses.append(response)
        task = self.send()
        await wait_calls(self.session)
        self.assertTrue(
            kook_api.observe_card_receipt(TOKEN, BOT, receipt(self.nonce()))
        )
        self.assertEqual(await asyncio.wait_for(task, 0.1), "gateway-id")
        self.assertEqual(len(self.session.calls), 1)
        self.assertTrue(response.cancelled)

    async def test_http_first_ignores_late_gateway_duplicate(self):
        self.session.responses.append(Response())
        self.assertEqual(await self.send(), "http-id")
        self.assertFalse(
            kook_api.observe_card_receipt(TOKEN, BOT, receipt(self.nonce()))
        )

    async def test_gateway_wins_when_http_and_gateway_complete_same_turn(self):
        response = Response(
            on_enter=lambda: kook_api.observe_card_receipt(
                TOKEN, BOT, receipt(self.nonce())
            )
        )
        self.session.responses.append(response)
        self.assertEqual(await self.send(), "gateway-id")

    async def test_wrong_token_author_channel_type_or_message_id_is_ignored(self):
        self.session.responses.append(Response(block=asyncio.Event()))
        task = self.send()
        await wait_calls(self.session)
        nonce = self.nonce()
        for changes in (
            {"author_id": "other"},
            {"target_id": "other"},
            {"channel_type": "PERSON"},
            {"type": 9},
            {"type": 255},
            {"nonce": "wrong"},
            {"msg_id": ""},
            {"msg_id": "bad\nsecret"},
        ):
            self.assertFalse(
                kook_api.observe_card_receipt(TOKEN, BOT, receipt(nonce, **changes))
            )
        self.assertFalse(
            kook_api.observe_card_receipt("wrong-token", BOT, receipt(nonce))
        )
        self.assertFalse(
            kook_api.observe_card_receipt(TOKEN, "wrong-bot", receipt(nonce))
        )
        self.assertFalse(task.done())
        self.assertTrue(kook_api.observe_card_receipt(TOKEN, BOT, receipt(nonce)))
        self.assertEqual(await task, "gateway-id")

    async def test_typed_enum_and_raw_gateway_wrappers_are_supported(self):
        class Kind(Enum):
            CARD = 10
            GROUP = "GROUP"

        for wrap in (
            lambda x: {"s": 0, "d": x},
            lambda x: types.SimpleNamespace(signal=0, data=x),
            lambda x: x,
        ):
            self.session.responses.append(Response(block=asyncio.Event()))
            task = self.send()
            await wait_calls(self.session, len(self.session.calls) + 1)
            event = types.SimpleNamespace(
                **receipt(self.nonce(), type=Kind.CARD, channel_type=Kind.GROUP)
            )
            self.assertTrue(kook_api.observe_card_receipt(TOKEN, BOT, wrap(event)))
            self.assertEqual(await task, "gateway-id")

    async def test_http_timeout_can_be_recovered_by_late_receipt_during_grace(self):
        self.session.responses.append(
            Response(error=TimeoutError("synthetic-private-error"))
        )
        task = self.send()
        await wait_calls(self.session)
        await asyncio.sleep(0.005)
        self.assertTrue(
            kook_api.observe_card_receipt(TOKEN, BOT, receipt(self.nonce()))
        )
        self.assertEqual(await task, "gateway-id")
        self.assertEqual(len(self.session.calls), 1)

    async def test_unknown_timeout_never_blindly_retries_and_expires_receipt(self):
        self.session.responses.append(
            Response(error=TimeoutError("synthetic-private-error"))
        )
        self.assertIsNone(await self.send())
        self.assertEqual(len(self.session.calls), 1)
        self.assertFalse(kook_api._pending_card_receipts)
        self.assertFalse(
            kook_api.observe_card_receipt(TOKEN, BOT, receipt(self.nonce()))
        )

    async def test_real_timeout_budget_cancels_blocked_http(self):
        response = Response(block=asyncio.Event())
        self.session.responses.append(response)
        with patch.object(kook_api, "_CARD_HTTP_TIMEOUT", 0.01):
            self.assertIsNone(await asyncio.wait_for(self.send(), 0.15))
        self.assertTrue(response.cancelled)
        self.assertFalse(kook_api._pending_card_receipts)

    async def test_http_errors_and_malformed_success_never_resend(self):
        for response in (
            Response(status=502),
            Response(data=[]),
            Response(data={"code": 0, "data": {}}),
            Response(error=ValueError("synthetic-private-json")),
        ):
            self.session.responses.append(response)
            before = len(self.session.calls)
            self.assertIsNone(await self.send())
            self.assertEqual(len(self.session.calls), before + 1)

    async def test_non_countdown_business_rejection_does_not_retry(self):
        self.session.responses.append(
            Response({"code": 403, "message": "synthetic-private-title", "data": []})
        )
        self.assertIsNone(await self.send())
        self.assertEqual(len(self.session.calls), 1)

    async def test_countdown_repair_and_text_fallback_get_distinct_nonces(self):
        rejection = {"code": 40000, "data": ["countdown.endTime rejected"]}
        self.session.responses.extend(
            [
                Response(rejection),
                Response(rejection),
                Response({"code": 0, "data": {"msg_id": "fallback-id"}}),
            ]
        )
        card = {
            "type": "card",
            "modules": [{"type": "countdown", "startTime": 1, "endTime": 10001}],
        }
        original = copy.deepcopy(card)
        with patch.object(
            kook_api, "_server_now_ms", side_effect=[100000, 200000, 300000]
        ):
            self.assertEqual(await self.send(card=card), "fallback-id")
        self.assertEqual(len(self.session.calls), 3)
        self.assertEqual(len({self.nonce(i) for i in range(3)}), 3)
        cards = [
            json.loads(call[2]["json"]["content"])[0] for call in self.session.calls
        ]
        self.assertEqual(cards[0]["modules"][0]["startTime"], 100500)
        self.assertEqual(cards[1]["modules"][0]["startTime"], 200500)
        self.assertEqual(cards[2]["modules"][0]["type"], "section")
        self.assertEqual(card, original)
        self.assertFalse(kook_api._pending_card_receipts)

    async def test_timeout_after_rejected_countdown_does_not_attempt_text_send(self):
        self.session.responses.extend(
            [
                Response({"code": 40000, "data": "countdown rejected"}),
                Response(error=TimeoutError()),
            ]
        )
        self.assertIsNone(await self.send())
        self.assertEqual(len(self.session.calls), 2)

    async def test_late_rejected_attempt_cannot_confirm_fallback_nonce(self):
        self.session.responses.extend(
            [
                Response({"code": 40000, "data": ["countdown rejected"]}),
                Response(block=asyncio.Event()),
            ]
        )
        task = self.send()
        await wait_calls(self.session, 2)
        self.assertFalse(
            kook_api.observe_card_receipt(
                TOKEN, BOT, receipt(self.nonce(0), msg_id="old-id")
            )
        )
        self.assertTrue(
            kook_api.observe_card_receipt(
                TOKEN, BOT, receipt(self.nonce(1), msg_id="fallback-id")
            )
        )
        self.assertEqual(await task, "fallback-id")

    async def test_concurrent_cards_cannot_cross_tokens_or_channels(self):
        self.session.responses.extend(
            [Response(block=asyncio.Event()), Response(block=asyncio.Event())]
        )
        first = self.send()
        second = self.send(token="second-token", channel="second-channel")
        await wait_calls(self.session, 2)
        self.assertFalse(
            kook_api.observe_card_receipt(
                TOKEN, BOT, receipt(self.nonce(1), target_id="second-channel")
            )
        )
        self.assertTrue(
            kook_api.observe_card_receipt(
                "second-token",
                BOT,
                receipt(self.nonce(1), target_id="second-channel", msg_id="second-id"),
            )
        )
        self.assertTrue(
            kook_api.observe_card_receipt(
                TOKEN, BOT, receipt(self.nonce(0), msg_id="first-id")
            )
        )
        self.assertEqual(await asyncio.gather(first, second), ["first-id", "second-id"])

    async def test_cancelling_send_cleans_pending_receipt_and_http_task(self):
        response = Response(block=asyncio.Event())
        self.session.responses.append(response)
        task = self.send()
        await wait_calls(self.session)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(response.cancelled)
        self.assertTrue(response.closed)
        self.assertFalse(kook_api._pending_card_receipts)
        self.assertFalse(
            kook_api.observe_card_receipt(TOKEN, BOT, receipt(self.nonce()))
        )

    async def test_shared_session_close_cancels_all_card_attempts(self):
        response = Response(block=asyncio.Event())
        self.session.responses.append(response)
        task = self.send()
        await wait_calls(self.session)
        await kook_api.close_shared_session()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(response.cancelled)
        self.assertFalse(kook_api._pending_card_receipts)

    async def test_capacity_is_bounded_without_evicting_inflight_send(self):
        self.session.responses.append(Response(block=asyncio.Event()))
        with patch.object(kook_api, "_MAX_PENDING_CARD_RECEIPTS", 1):
            first = self.send()
            await wait_calls(self.session)
            self.assertIsNone(await self.send())
            self.assertEqual(len(self.session.calls), 1)
            self.assertTrue(
                kook_api.observe_card_receipt(TOKEN, BOT, receipt(self.nonce()))
            )
            self.assertEqual(await first, "gateway-id")

    async def test_card_failures_never_log_payload_token_or_exception_text(self):
        self.session.responses.extend(
            [
                Response(error=ValueError("synthetic-private-error")),
                Response(
                    {
                        "code": 403,
                        "message": "synthetic-private-title",
                        "data": {"Authorization": TOKEN},
                    }
                ),
            ]
        )
        with self.assertLogs("astrbot", level="WARNING") as captured:
            self.assertIsNone(await self.send())
            self.assertIsNone(await self.send())
        output = "\n".join(captured.output)
        for secret in (
            TOKEN,
            "synthetic-private-title",
            "synthetic-private-error",
            "Authorization",
        ):
            self.assertNotIn(secret, output)
        self.assertIn("ValueError", output)
        self.assertIn("code=403", output)

    async def test_invalid_card_token_or_channel_never_issues_http(self):
        for kwargs in (
            {"token": ""},
            {"token": "bad\r\nheader"},
            {"channel": ""},
            {"channel": "../elsewhere"},
            {"card": []},
            {"card": ["bad"]},
        ):
            self.assertIsNone(await self.send(**kwargs))
        self.assertFalse(self.session.calls)
        self.assertFalse(kook_api._pending_card_receipts)


class ReadApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = Session([])
        self.patch_session = patch.object(
            kook_api, "_get_session", AsyncMock(return_value=self.session)
        )
        self.patch_session.start()
        self.addCleanup(self.patch_session.stop)

    async def test_history_returns_data_with_before_reference_and_no_redirect(self):
        expected = {
            "items": [{"id": "history-id", "content": "synthetic-private-history"}]
        }
        self.session.responses.append(Response({"code": 0, "data": expected}))
        self.assertEqual(
            await kook_api.get_channel_messages(TOKEN, CHANNEL, "before-id", 25),
            expected,
        )
        method, url, options = self.session.calls[0]
        self.assertEqual(method, "GET")
        self.assertTrue(url.endswith("/message/list"))
        self.assertEqual(
            options["params"],
            {
                "target_id": CHANNEL,
                "page_size": 25,
                "msg_id": "before-id",
                "flag": "before",
            },
        )
        self.assertFalse(options["allow_redirects"])

    async def test_latest_history_has_no_reference_and_maximum_fifty(self):
        self.session.responses.append(Response({"code": 0, "data": {"items": []}}))
        self.assertEqual(
            await kook_api.get_channel_messages(TOKEN, CHANNEL), {"items": []}
        )
        self.assertEqual(
            self.session.calls[0][2]["params"], {"target_id": CHANNEL, "page_size": 50}
        )

    async def test_history_rejects_invalid_scope_or_unbounded_page_size(self):
        for args in (
            (TOKEN, ""),
            (TOKEN, CHANNEL, "../other"),
            (TOKEN, CHANNEL, "", 0),
            (TOKEN, CHANNEL, "", 51),
            (TOKEN, CHANNEL, "", True),
            ("", CHANNEL),
        ):
            self.assertIsNone(await kook_api.get_channel_messages(*args))
        self.assertFalse(self.session.calls)

    async def test_identity_uses_current_token_user_me_only(self):
        data = {"id": BOT, "bot": True, "username": "synthetic-private-name"}
        self.session.responses.append(Response({"code": 0, "data": data}))
        self.assertEqual(await kook_api.get_bot_identity(TOKEN), data)
        method, url, options = self.session.calls[0]
        self.assertEqual(method, "GET")
        self.assertTrue(url.endswith("/user/me"))
        self.assertEqual(options["params"], {})
        self.assertFalse(options["allow_redirects"])

    async def test_reads_fail_closed_without_logging_raw_data(self):
        self.session.responses.extend(
            [
                Response(
                    {
                        "code": 403,
                        "message": "synthetic-private-name",
                        "data": {"cookie": TOKEN},
                    }
                ),
                Response(status=302),
                Response(error=ValueError("synthetic-private-exception")),
                Response({"code": 0, "data": []}),
            ]
        )
        with self.assertLogs("astrbot", level="WARNING") as captured:
            for _ in range(4):
                self.assertIsNone(await kook_api.get_bot_identity(TOKEN))
        output = "\n".join(captured.output)
        self.assertNotIn("synthetic-private", output)
        self.assertNotIn(TOKEN, output)

    async def test_unknown_message_missing_error_is_not_assumed_idempotent(self):
        self.session.responses.extend(
            [
                Response({"code": 404, "message": "not found", "data": []}),
                Response({"code": 0, "data": []}),
            ]
        )
        self.assertFalse(await kook_api.delete_message(TOKEN, "missing-id"))
        self.assertTrue(await kook_api.delete_message(TOKEN, "present-id"))

    async def test_delete_text_update_failures_log_only_stage_and_numeric_codes(self):
        self.session.responses.extend(
            [
                Response(error=ValueError("synthetic-private-exception")),
                Response({"code": 403, "message": "synthetic-private-title"}),
                Response({"code": 403, "message": "synthetic-private-title"}),
            ]
        )
        with self.assertLogs("astrbot", level="WARNING") as captured:
            self.assertFalse(await kook_api.delete_message(TOKEN, "id"))
            self.assertIsNone(
                await kook_api.send_text_message(
                    TOKEN, CHANNEL, "synthetic-private-title"
                )
            )
            self.assertFalse(await kook_api.update_card_message(TOKEN, "id", CARD))
        output = "\n".join(captured.output)
        self.assertNotIn("synthetic-private", output)
        self.assertNotIn(TOKEN, output)


if __name__ == "__main__":
    unittest.main()
