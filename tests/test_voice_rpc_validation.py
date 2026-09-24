import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_kook_music.kook_voice.voice_client import VoiceClient  # noqa: E402


class RPCWebSocket:
    closed = False

    def __init__(self, client, transform=None, *, noise=False):
        self.client = client
        self.transform = transform
        self.noise = noise
        self.sent = []
        self.incoming = asyncio.Queue()
        self.serial = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.incoming.get()
        if message is None:
            raise StopAsyncIteration
        return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(message))

    async def send_json(self, payload):
        self.sent.append(payload)
        self.serial += 1
        response = {"response": True, "id": payload["id"], "ok": True, "data": {}}
        if payload["method"] == "createPlainTransport":
            response["data"] = {
                "id": "transport-" + str(self.serial),
                "ip": "192.0.2.1",
                "port": 20000,
                "rtcpPort": 20001,
            }
        if payload["method"] == "produce":
            response["data"] = {"id": "producer-" + str(self.serial)}
        if self.noise:
            await self.incoming.put(
                {
                    "notification": True,
                    "method": "newPeer",
                    "id": payload["id"],
                    "response": True,
                    "ok": True,
                    "data": {"id": "noise"},
                }
            )
            await self.incoming.put(
                {"response": True, "id": -1, "ok": False, "errorCode": 500}
            )
        if self.transform:
            response = self.transform(payload, response, self.client._refreshing)
        if response is not None:
            await self.incoming.put(response)

    async def close(self):
        self.closed = True
        await self.incoming.put(None)


class VoiceRPCValidationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            await client.disconnect()

    def client(self, transform=None, *, noise=False):
        client = VoiceClient("synthetic-token")
        client.channel_id = "synthetic-voice"
        client._connected.set()
        client._ws = RPCWebSocket(client, transform, noise=noise)
        client._tasks = [asyncio.create_task(client._ws_message_handler())]
        self.clients.append(client)
        return client

    async def ready(self, **kwargs):
        client = self.client(**kwargs)
        await asyncio.wait_for(client._rtp_ready.wait(), 1)
        return client

    async def test_initial_handshake_ignores_notifications_and_wrong_ids(self):
        client = await self.ready(noise=True)
        self.assertEqual(
            [message["method"] for message in client._ws.sent],
            ["getRouterRtpCapabilities", "join", "createPlainTransport", "produce"],
        )
        self.assertEqual(client.rtp_url, "rtp://192.0.2.1:20000?rtcpport=20001")
        self.assertEqual(client._producer_id, "producer-4")
        self.assertFalse(client._handshake_failed.is_set())

    async def test_initial_rejected_rpc_never_marks_rtp_ready(self):
        for stage in (
            "getRouterRtpCapabilities",
            "join",
            "createPlainTransport",
            "produce",
        ):
            with self.subTest(stage=stage):

                def reject(payload, response, refreshing):
                    if payload["method"] == stage:
                        response.update(ok=False, errorCode=500)
                    return response

                client = self.client(reject)
                await asyncio.wait_for(client._handshake_failed.wait(), 1)
                self.assertFalse(client.is_rtp_ready)
                self.assertEqual(client.rtp_url, "")
                self.assertEqual(client._ws.sent[-1]["method"], stage)

    async def test_initial_transport_must_have_valid_id_ip_and_ports(self):
        for field, value in (
            ("id", ""),
            ("ip", "invalid-host"),
            ("port", 0),
            ("port", True),
            ("rtcpPort", 65536),
            ("rtcpPort", None),
        ):
            with self.subTest(field=field, value=value):

                def malformed(payload, response, refreshing):
                    if payload["method"] == "createPlainTransport":
                        response["data"][field] = value
                    return response

                client = self.client(malformed)
                await asyncio.wait_for(client._handshake_failed.wait(), 1)
                self.assertFalse(client.is_rtp_ready)
                self.assertEqual(client._ws.sent[-1]["method"], "createPlainTransport")

    async def test_initial_producer_id_is_required(self):
        def malformed(payload, response, refreshing):
            if payload["method"] == "produce":
                response["data"] = {}
            return response

        client = self.client(malformed)
        await asyncio.wait_for(client._handshake_failed.wait(), 1)
        self.assertFalse(client.is_rtp_ready)
        self.assertEqual(client._producer_id, "")

    async def test_refresh_uses_matching_rpc_responses_in_correct_order(self):
        client = await self.ready(noise=True)
        old_transport = client._transport_id
        self.assertTrue(await client.refresh_rtp(timeout=0.2))
        self.assertEqual(
            [message["method"] for message in client._ws.sent[4:]],
            ["closeProducer", "closeTransport", "createPlainTransport", "produce"],
        )
        self.assertNotEqual(client._transport_id, old_transport)
        self.assertEqual(client._producer_id, "producer-8")
        self.assertTrue(client.is_rtp_ready)

    async def test_refresh_produce_rejection_regression_never_reports_success(self):
        def reject(payload, response, refreshing):
            if refreshing and payload["method"] == "produce":
                return {
                    "response": True,
                    "id": payload["id"],
                    "ok": False,
                    "errorCode": 500,
                }
            return response

        client = await self.ready(transform=reject)
        self.assertFalse(await client.refresh_rtp(timeout=0.2))
        self.assertFalse(client.is_rtp_ready)
        self.assertFalse(client.is_alive)
        self.assertEqual(client.rtp_url, "")
        self.assertEqual(client._producer_id, "")

    async def test_failed_close_preserves_unconfirmed_resource_ids_and_aborts_refresh(
        self,
    ):
        for stage in ("closeProducer", "closeTransport"):
            with self.subTest(stage=stage):

                def reject(payload, response, refreshing):
                    if payload["method"] == stage:
                        response.update(ok=False, errorCode=500)
                    return response

                client = await self.ready(transform=reject)
                old_transport, old_producer = client._transport_id, client._producer_id
                self.assertFalse(await client.refresh_rtp(timeout=0.2))
                self.assertEqual(client._transport_id, old_transport)
                self.assertEqual(
                    client._producer_id,
                    old_producer if stage == "closeProducer" else "",
                )
                self.assertEqual(client._ws.sent[-1]["method"], stage)
                self.assertFalse(client.is_rtp_ready)
                self.assertEqual(client.rtp_url, "")

    async def test_refresh_timeout_does_not_restore_closed_old_transport(self):
        def missing(payload, response, refreshing):
            return None if refreshing and payload["method"] == "produce" else response

        client = await self.ready(transform=missing)
        self.assertFalse(await client.refresh_rtp(timeout=0.02))
        self.assertFalse(client.is_rtp_ready)
        self.assertEqual(client.rtp_url, "")

    async def test_refresh_invalid_transport_and_producer_are_rejected(self):
        for stage in ("createPlainTransport", "produce"):
            with self.subTest(stage=stage):

                def malformed(payload, response, refreshing):
                    if refreshing and payload["method"] == stage:
                        response["data"] = {}
                    return response

                client = await self.ready(transform=malformed)
                self.assertFalse(await client.refresh_rtp(timeout=0.2))
                self.assertFalse(client.is_rtp_ready)
                self.assertEqual(client.rtp_url, "")

    async def test_refresh_only_wrong_id_responses_timeout_without_advancing(self):
        def wrong(payload, response, refreshing):
            if refreshing:
                response["id"] = -1
            return response

        client = await self.ready(transform=wrong)
        old_transport, old_producer = client._transport_id, client._producer_id
        self.assertFalse(await client.refresh_rtp(timeout=0.02))
        self.assertEqual(client._transport_id, old_transport)
        self.assertEqual(client._producer_id, old_producer)
        self.assertEqual(client._ws.sent[-1]["method"], "closeProducer")
        self.assertFalse(client.is_rtp_ready)

    async def test_numeric_ok_and_explicit_error_are_not_success(self):
        for changes in ({"ok": 1}, {"ok": True, "errorCode": 500}):
            with self.subTest(changes=changes):

                def malformed(payload, response, refreshing):
                    if refreshing and payload["method"] == "closeProducer":
                        response.update(changes)
                    return response

                client = await self.ready(transform=malformed)
                self.assertFalse(await client.refresh_rtp(timeout=0.2))
                self.assertFalse(client.is_rtp_ready)

    async def test_ipv6_transport_builds_valid_bracketed_rtp_url(self):
        def ipv6(payload, response, refreshing):
            if payload["method"] == "createPlainTransport":
                response["data"]["ip"] = "2001:db8::1"
            return response

        client = await self.ready(transform=ipv6)
        self.assertEqual(client.rtp_url, "rtp://[2001:db8::1]:20000?rtcpport=20001")

    async def test_initial_eof_fails_connect_promptly_without_remote_removal(self):
        client = VoiceClient("synthetic-token")
        websocket = RPCWebSocket(client, transform=lambda *_: None)
        await websocket.incoming.put(None)
        http = SimpleNamespace(
            ws_connect=AsyncMock(return_value=websocket),
            closed=False,
            close=AsyncMock(),
        )
        self.clients.append(client)
        with (
            patch.object(
                client,
                "_get_gateway",
                AsyncMock(return_value="wss://synthetic.invalid"),
            ),
            patch(
                "astrbot_plugin_kook_music.kook_voice.voice_client.aiohttp.ClientSession",
                return_value=http,
            ),
        ):
            self.assertFalse(
                await asyncio.wait_for(
                    client.connect("synthetic-voice", timeout=30), 0.5
                )
            )
        self.assertTrue(client._handshake_failed.is_set())
        self.assertFalse(client.remote_removed)
        self.assertFalse(client.is_rtp_ready)
        self.assertTrue(websocket.closed)

    async def test_disconnect_clears_all_transport_and_producer_state(self):
        client = await self.ready()
        self.assertTrue(client._transport_id)
        self.assertTrue(client._producer_id)
        await client.disconnect()
        self.assertEqual(client._transport_id, "")
        self.assertEqual(client._producer_id, "")
        self.assertEqual(client._rtp_ip, "")
        self.assertEqual(client._rtp_port, 0)
        self.assertEqual(client._rtcp_port, 0)
        self.assertEqual(client.ssrc, 0)
        self.assertFalse(client.is_rtp_ready)

    async def test_refresh_is_rejected_until_initial_produce_is_ready(self):
        client = self.client(
            transform=lambda payload, response, _: (
                None if payload["method"] == "produce" else response
            )
        )
        await asyncio.sleep(0)
        self.assertEqual(client._ws.sent[-1]["method"], "produce")
        self.assertFalse(client.is_rtp_ready)
        count = len(client._ws.sent)
        self.assertFalse(await client.refresh_rtp(timeout=0.02))
        self.assertEqual(len(client._ws.sent), count)
        self.assertFalse(client._refreshing)

    async def test_late_refresh_response_cannot_revive_disconnected_transport(self):
        entered = asyncio.Event()
        produce_request = []

        def delay(payload, response, refreshing):
            if refreshing and payload["method"] == "produce":
                produce_request.append(payload)
                entered.set()
                return None
            return response

        client = await self.ready(transform=delay)
        task = asyncio.create_task(client.refresh_rtp(timeout=0.2))
        await asyncio.wait_for(entered.wait(), 1)
        await client.disconnect()
        await client._refresh_response.put(
            {
                "response": True,
                "ok": True,
                "id": produce_request[0]["id"],
                "data": {"id": "late-producer"},
            }
        )
        self.assertFalse(await asyncio.wait_for(task, 1))
        self.assertFalse(client.is_rtp_ready)
        self.assertEqual(client.rtp_url, "")
        self.assertEqual(client._producer_id, "")


if __name__ == "__main__":
    unittest.main()
