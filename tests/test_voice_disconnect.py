import asyncio
import json
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp

PLUGINS_DIR = Path(__file__).resolve().parents[2]
if str(PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGINS_DIR))

from astrbot_plugin_kook_music.kook_voice.ffmpeg_player import (
    DirectFFmpegPlayer,
    RelayFFmpegPlayer,
)
from astrbot_plugin_kook_music.kook_voice.voice_client import VoiceClient
from astrbot_plugin_kook_music.kook_voice.voice_manager import GuildSession, VoiceManager
from astrbot_plugin_kook_music.music.model import Song


class FakeWebSocket:
    def __init__(self, payloads=()):
        self.payloads = list(payloads)
        self.closed = False
        self.sent = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.payloads:
            raise StopAsyncIteration
        return SimpleNamespace(
            type=aiohttp.WSMsgType.TEXT,
            data=json.dumps(self.payloads.pop(0)),
        )

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self):
        self.closed = True


class LocalVoiceClient(VoiceClient):
    def __init__(self):
        super().__init__("token")
        self.channel_id = "voice"
        self.rtp_url = "rtp://test"
        self.ssrc = 1
        self._connected.set()
        self._rtp_ready.set()
        self.reconnect_calls = 0

    @property
    def is_alive(self):
        return self.is_connected and not self.remote_removed

    async def reconnect(self, channel_id="", timeout=15.0):
        self.reconnect_calls += 1
        return not self.remote_removed


class BlockingDirectPlayer(DirectFFmpegPlayer):
    def __init__(self):
        self.started = asyncio.Event()
        self.done = asyncio.Event()
        self._playing = False
        self.stops = 0

    @property
    def is_playing(self):
        return self._playing

    async def play(self, *args, **kwargs):
        self._playing = True
        self.started.set()
        return True

    async def wait_until_done(self, timeout=None):
        await self.done.wait()
        return False

    async def stop(self):
        self.stops += 1
        self._playing = False
        self.done.set()


class BlockingRelayPlayer(BlockingDirectPlayer, RelayFFmpegPlayer):
    def __init__(self):
        super().__init__()
        self.relay_running = True

    @property
    def is_relay_running(self):
        return self.relay_running

    async def stop_relay(self):
        await self.stop()
        self.relay_running = False


class StartingRelayPlayer(BlockingRelayPlayer):
    def __init__(self):
        super().__init__()
        self.relay_running = False
        self.relay_starting = asyncio.Event()
        self.allow_start = asyncio.Event()

    async def start_relay(self, *args):
        self.relay_starting.set()
        await self.allow_start.wait()
        self.relay_running = True
        return True


class VoiceClientRemovalTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_notification_during_initial_handshake_is_terminal(self):
        client = VoiceClient("token")
        socket = FakeWebSocket([{"notification": True, "method": "disconnect"}])
        client._ws = socket
        callback = AsyncMock()
        client.on_remote_removed = callback

        task = asyncio.create_task(client._ws_message_handler())
        client._tasks.append(task)
        await asyncio.wait_for(task, 1)
        await asyncio.wait_for(client._removal_task, 1)

        self.assertTrue(client.remote_removed)
        self.assertFalse(client.is_alive)
        self.assertTrue(socket.closed)
        callback.assert_awaited_once_with(client)
        self.assertEqual(len(socket.sent), 1)
        self.assertFalse(await client.connect("voice"))
        self.assertFalse(await client.reconnect("voice"))

    async def test_disconnect_during_rtp_refresh_is_not_a_refresh_response(self):
        client = VoiceClient("token")
        client._refreshing = True
        client._ws = FakeWebSocket([
            {}, {}, {"data": {"id": "transport", "ip": "127.0.0.1", "port": 1}},
            {"data": {"id": "producer"}},
            {"notification": True, "method": "disconnect"},
        ])
        await client._ws_message_handler()
        await client._removal_task
        self.assertTrue(client.remote_removed)
        self.assertTrue(client._refresh_response.empty())
        self.assertFalse(client.is_rtp_ready)

    async def test_network_eof_is_not_a_remote_removal(self):
        client = VoiceClient("token")
        client._ws = FakeWebSocket()
        client.on_remote_removed = AsyncMock()
        await client._ws_message_handler()
        self.assertFalse(client.remote_removed)
        client.on_remote_removed.assert_not_called()
        await client.disconnect()

    async def test_host_clock_skew_cannot_hide_current_server_exit(self):
        client = VoiceClient("token")
        client.channel_id = "voice"
        server_now = 1_700_000_000_000
        with patch(
            "astrbot_plugin_kook_music.kook_voice.voice_client.time.time",
            return_value=(server_now + 30_000) / 1000,
        ):
            self.assertFalse(client.consume_expected_exit("voice", server_now))
            client.note_channel_joined("voice", server_now - 1000)
            self.assertFalse(client.consume_expected_exit("voice", server_now))

    async def test_server_join_timestamps_never_regress(self):
        client = VoiceClient("token")
        client.channel_id = "voice"
        client.note_channel_joined("voice", 3000)
        client.note_channel_joined("voice", 2000)
        client.note_channel_joined("voice", 3000)
        self.assertTrue(client.consume_expected_exit("voice", 2500))
        self.assertFalse(client.consume_expected_exit("voice", 4000))
        client.note_channel_joined("unrelated", 5000)
        self.assertFalse(client.consume_expected_exit("voice", 4000))

    async def test_disconnect_does_not_await_its_own_task(self):
        client = VoiceClient("token")

        async def close_from_handler():
            client._tasks = [asyncio.current_task()]
            await client.disconnect()

        await asyncio.wait_for(asyncio.create_task(close_from_handler()), 1)
        self.assertEqual(client._tasks, [])

    async def test_connect_fails_promptly_if_removed_before_rtp_is_ready(self):
        client = VoiceClient("token")
        socket = FakeWebSocket([{"notification": True, "method": "disconnect"}])
        http_session = SimpleNamespace(
            ws_connect=AsyncMock(return_value=socket), closed=False, close=AsyncMock()
        )
        with patch.object(client, "_get_gateway", AsyncMock(return_value="wss://test")):
            with patch(
                "astrbot_plugin_kook_music.kook_voice.voice_client.aiohttp.ClientSession",
                return_value=http_session,
            ):
                self.assertFalse(await asyncio.wait_for(client.connect("voice", 30), 1))
        await client._removal_task
        self.assertTrue(client.remote_removed)


class VoiceManagerRemovalTests(unittest.IsolatedAsyncioTestCase):
    def make_manager(self, relay=False, prepared=True):
        manager = VoiceManager(auto_leave_timeout=0)
        manager.PLAYBACK_START_DELAY = 0
        player = BlockingRelayPlayer() if relay else BlockingDirectPlayer()
        client = LocalVoiceClient()
        songs = [Song(id="first", name="first", file_path="first" if prepared else "")]
        songs.append(Song(id="next", name="next", stream_url="https://cdn.test/next"))
        session = GuildSession("guild", "voice", "text", client, player, playlist=songs)
        manager.sessions["guild"] = session
        manager.on_playback_finished = AsyncMock()
        client.on_remote_removed = lambda removed: manager.handle_voice_removed(
            "voice", expected_voice_client=removed
        )
        return manager, session, player, client

    async def test_gateway_removal_during_relay_start_cancels_unpublished_session(self):
        manager = VoiceManager(auto_leave_timeout=0)
        manager.on_playback_finished = AsyncMock()
        client = LocalVoiceClient()
        player = StartingRelayPlayer()
        with patch(
            "astrbot_plugin_kook_music.kook_voice.voice_manager.VoiceClient",
            return_value=client,
        ), patch(
            "astrbot_plugin_kook_music.kook_voice.voice_manager.create_player",
            return_value=player,
        ), patch.object(client, "connect", AsyncMock(return_value=True)):
            joining = asyncio.create_task(manager.join_and_play(
                "token", "guild", "voice", "text", Song(id="one", name="one")
            ))
            await asyncio.wait_for(player.relay_starting.wait(), 1)
            self.assertFalse(manager.sessions)
            pending_session = manager.iter_voice_sessions()[0]
            self.assertIs(pending_session.voice_client, client)
            self.assertTrue(await asyncio.wait_for(manager.handle_voice_removed(
                "voice", expected_voice_client=client, occurred_at=time.time() * 1000
            ), 1))
            succeeded, message = await asyncio.wait_for(joining, 1)
        await client._removal_task
        self.assertFalse(succeeded)
        self.assertIn("\u79fb\u51fa", message)
        self.assertFalse(player.is_relay_running)
        self.assertFalse(player.started.is_set())
        self.assertFalse(manager.iter_voice_sessions())
        self.assertFalse(manager._initialization_tasks)
        self.assertEqual(pending_session.playlist, [])
        manager.on_playback_finished.assert_awaited_once_with("guild")

    async def test_gateway_removal_during_initial_connect_returns_failure(self):
        manager = VoiceManager(auto_leave_timeout=0)
        client = LocalVoiceClient()
        player = BlockingDirectPlayer()
        connecting = asyncio.Event()

        async def connect(channel_id):
            connecting.set()
            await asyncio.Event().wait()

        with patch(
            "astrbot_plugin_kook_music.kook_voice.voice_manager.VoiceClient",
            return_value=client,
        ), patch(
            "astrbot_plugin_kook_music.kook_voice.voice_manager.create_player",
            return_value=player,
        ), patch.object(client, "connect", side_effect=connect):
            joining = asyncio.create_task(manager.join_and_play(
                "token", "guild", "voice", "text", Song(id="one", name="one")
            ))
            await asyncio.wait_for(connecting.wait(), 1)
            self.assertTrue(await asyncio.wait_for(
                manager.handle_voice_removed("voice", client), 1
            ))
            self.assertFalse((await joining)[0])
        await client._removal_task
        self.assertFalse(manager.iter_voice_sessions())
        self.assertFalse(player.started.is_set())

    async def test_removal_before_initialization_task_starts_still_cleans_up(self):
        manager = VoiceManager(auto_leave_timeout=0)
        client = LocalVoiceClient()
        player = BlockingDirectPlayer()

        def create_player(**kwargs):
            client.mark_remote_removed()
            return player

        with patch(
            "astrbot_plugin_kook_music.kook_voice.voice_manager.VoiceClient",
            return_value=client,
        ), patch(
            "astrbot_plugin_kook_music.kook_voice.voice_manager.create_player",
            side_effect=create_player,
        ):
            succeeded, _ = await asyncio.wait_for(manager.join_and_play(
                "token", "guild", "voice", "text", Song(id="one", name="one")
            ), 1)
        await client._removal_task
        self.assertFalse(succeeded)
        self.assertFalse(manager.iter_voice_sessions())
        self.assertGreater(player.stops, 0)

    async def test_unload_cancels_pending_relay_start_without_leaking_session(self):
        manager = VoiceManager(auto_leave_timeout=0)
        client = LocalVoiceClient()
        player = StartingRelayPlayer()
        with patch(
            "astrbot_plugin_kook_music.kook_voice.voice_manager.VoiceClient",
            return_value=client,
        ), patch(
            "astrbot_plugin_kook_music.kook_voice.voice_manager.create_player",
            return_value=player,
        ), patch.object(client, "connect", AsyncMock(return_value=True)):
            joining = asyncio.create_task(manager.join_and_play(
                "token", "guild", "voice", "text", Song(id="one", name="one")
            ))
            await asyncio.wait_for(player.relay_starting.wait(), 1)
            await asyncio.wait_for(manager.leave_all(), 1)
            with self.assertRaises(asyncio.CancelledError):
                await joining
        self.assertFalse(manager.iter_voice_sessions())
        self.assertFalse(player.is_relay_running)
        self.assertFalse(client.is_alive)

    async def test_unload_rejects_other_guild_waiting_for_creation_lock(self):
        manager = VoiceManager(auto_leave_timeout=0, streaming_mode="direct")
        connecting = asyncio.Event()
        client = LocalVoiceClient()

        async def blocked_connect(_channel_id):
            connecting.set()
            await asyncio.Event().wait()

        with patch(
            "astrbot_plugin_kook_music.kook_voice.voice_manager.VoiceClient",
            return_value=client,
        ) as factory, patch(
            "astrbot_plugin_kook_music.kook_voice.voice_manager.create_player",
            side_effect=lambda **_kwargs: BlockingDirectPlayer(),
        ), patch.object(client, "connect", side_effect=blocked_connect):
            first = asyncio.create_task(manager.join_and_play(
                "token", "guild-a", "voice-a", "text-a", Song(id="one", name="one")
            ))
            await asyncio.wait_for(connecting.wait(), 1)
            second = asyncio.create_task(manager.join_and_play(
                "token", "guild-b", "voice-b", "text-b", Song(id="two", name="two")
            ))
            await asyncio.sleep(0)
            await asyncio.wait_for(manager.leave_all(), 1)
            results = await asyncio.wait_for(
                asyncio.gather(first, second, return_exceptions=True), 1
            )
            self.assertIsInstance(results[0], asyncio.CancelledError)
            self.assertFalse(results[1][0])
            self.assertEqual(factory.call_count, 1)
            self.assertFalse((await manager.join_and_play(
                "token", "guild-c", "voice-c", "text-c", Song(id="three", name="three")
            ))[0])
        self.assertFalse(manager.iter_voice_sessions())
        self.assertFalse(manager._initialization_tasks)
        self.assertFalse(manager._playback_tasks)

    async def test_explicit_removal_stops_direct_and_relay_without_retry(self):
        for relay in (False, True):
            with self.subTest(relay=relay):
                manager, session, player, client = self.make_manager(relay=relay)
                manager._start_playback_loop("guild")
                await asyncio.wait_for(player.started.wait(), 1)
                client.mark_remote_removed()
                await asyncio.wait_for(client._removal_task, 1)
                self.assertFalse(player.is_playing)
                if relay:
                    self.assertFalse(player.is_relay_running)
                self.assertEqual(session.playlist, [])
                self.assertFalse(session.is_playing)
                self.assertNotIn("guild", manager.sessions)
                self.assertNotIn("guild", manager._playback_tasks)
                self.assertNotIn("guild", manager._retry_tasks)
                self.assertEqual(client.reconnect_calls, 0)
                manager.on_playback_finished.assert_awaited_once_with("guild")

    async def test_removal_cancels_inflight_preparation(self):
        manager, session, player, client = self.make_manager(prepared=False)
        preparing = asyncio.Event()
        cancelled = asyncio.Event()

        async def prepare(song):
            preparing.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        manager.on_download_song = prepare
        manager._start_playback_loop("guild")
        await asyncio.wait_for(preparing.wait(), 1)
        self.assertTrue(await manager.handle_voice_removed("voice"))
        await client._removal_task
        self.assertTrue(cancelled.is_set())
        self.assertFalse(player.started.is_set())
        self.assertIsNone(session.preparation_task)
        self.assertEqual(session.playlist, [])
        manager.on_playback_finished.assert_awaited_once_with("guild")

    async def test_removal_cancels_scheduled_retry(self):
        manager, session, player, client = self.make_manager()
        manager.PLAYBACK_RETRY_DELAY = 10
        session.playback_retry_count = 1
        manager._schedule_playback_retry("guild", session)
        retry = manager._retry_tasks["guild"]
        await manager.handle_voice_removed("voice")
        await client._removal_task
        self.assertTrue(retry.done())
        self.assertFalse(player.started.is_set())
        self.assertNotIn("guild", manager._retry_tasks)

    async def test_removal_of_idle_session_stops_relay_and_notifies(self):
        manager, session, player, client = self.make_manager(relay=True)
        session.playlist.clear()
        self.assertTrue(await manager.handle_voice_removed("voice", client))
        await client._removal_task
        self.assertFalse(player.is_relay_running)
        self.assertNotIn("guild", manager.sessions)
        manager.on_playback_finished.assert_awaited_once_with("guild")

    async def test_direct_song_transition_exit_does_not_stop_second_song(self):
        manager, session, player, client = self.make_manager()
        manager.DIRECT_RECONNECT_DELAY = 0
        second_started = asyncio.Event()
        waits = 0
        original_disconnect = client.disconnect

        async def disconnect_for_transition():
            await original_disconnect()
            ignored = await manager.handle_voice_removed(
                "voice", client, occurred_at=time.time() * 1000
            )
            self.assertFalse(ignored)

        async def connect_for_transition(channel_id):
            client.channel_id = channel_id
            client._is_exit = False
            client._connected.set()
            client._rtp_ready.set()
            client.note_channel_joined(channel_id, time.time() * 1000)
            return True

        async def wait_for_song(timeout=None):
            nonlocal waits
            waits += 1
            if waits == 1:
                return True
            await asyncio.Event().wait()

        async def song_started(guild_id, song, *args):
            if song.id == "next":
                second_started.set()

        with patch.object(client, "disconnect", side_effect=disconnect_for_transition):
            with patch.object(client, "connect", side_effect=connect_for_transition):
                with patch.object(player, "wait_until_done", side_effect=wait_for_song):
                    manager.on_song_started = song_started
                    manager._start_playback_loop("guild")
                    await asyncio.wait_for(second_started.wait(), 1)
                    self.assertIs(manager.sessions["guild"], session)
                    self.assertFalse(client.remote_removed)
                    self.assertTrue(player.is_playing)
        await manager.leave_all()

    async def test_old_client_notification_does_not_remove_replacement_session(self):
        manager, session, _, client = self.make_manager()
        old_client = LocalVoiceClient()
        self.assertFalse(await manager.handle_voice_removed("voice", old_client))
        self.assertIs(manager.sessions["guild"], session)
        self.assertFalse(client.remote_removed)
        await manager.leave_all()

    async def test_own_exit_and_late_exit_do_not_end_new_connection(self):
        manager, session, _, client = self.make_manager()
        await client.disconnect()
        self.assertFalse(await manager.handle_voice_removed("voice", client))
        client.channel_id = "voice"
        client._is_exit = False
        server_joined_at = time.time() * 1000
        client.note_channel_joined("voice", server_joined_at)
        self.assertFalse(await manager.handle_voice_removed(
            "voice", client, occurred_at=server_joined_at - 1000
        ))
        self.assertIs(manager.sessions["guild"], session)
        self.assertTrue(await manager.handle_voice_removed(
            "voice", client, occurred_at=server_joined_at + 1000
        ))
        await client._removal_task
        manager.on_playback_finished.assert_awaited_once_with("guild")

    async def test_duplicate_expected_exit_during_reconnect_is_ignored(self):
        manager, session, _, client = self.make_manager()
        await client.disconnect()
        timestamp = time.time() * 1000
        for _ in range(2):
            self.assertFalse(await manager.handle_voice_removed(
                "voice", client, occurred_at=timestamp
            ))
        self.assertIs(manager.sessions["guild"], session)
        self.assertFalse(client.remote_removed)
        await manager.leave_all()

    async def test_duplicate_events_cleanup_and_notify_only_once(self):
        manager, _, _, client = self.make_manager()
        results = await asyncio.gather(
            manager.handle_voice_removed("voice", client),
            manager.handle_voice_removed("voice", client),
        )
        await client._removal_task
        self.assertEqual(results.count(True), 1)
        manager.on_playback_finished.assert_awaited_once_with("guild")

    async def test_cleanup_called_from_playback_task_does_not_self_await(self):
        manager, session, _, _ = self.make_manager()

        async def playback_cleanup():
            manager._playback_tasks["guild"] = asyncio.current_task()
            await manager._cleanup_session("guild", expected_session=session)

        await asyncio.wait_for(asyncio.create_task(playback_cleanup()), 1)
        self.assertNotIn("guild", manager.sessions)


if __name__ == "__main__":
    unittest.main()
