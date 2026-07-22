import asyncio
import logging
import sys
import types
import unittest
from pathlib import Path


PLUGINS_DIR = Path(__file__).resolve().parents[2]
if str(PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGINS_DIR))


class _DummyEventMessageType:
    ALL = object()


class _DummyFilter:
    EventMessageType = _DummyEventMessageType

    @staticmethod
    def command(_name):
        return lambda function: function

    @staticmethod
    def event_message_type(_event_type):
        return lambda function: function


class _DummyStar:
    def __init__(self, context=None):
        self.context = context


class _Component:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


astrbot_module = types.ModuleType("astrbot")
api_module = types.ModuleType("astrbot.api")
api_module.logger = logging.getLogger("astrbot-test")
event_module = types.ModuleType("astrbot.api.event")
event_module.AstrMessageEvent = object
event_module.filter = _DummyFilter()
star_module = types.ModuleType("astrbot.api.star")
star_module.Context = object
star_module.Star = _DummyStar
core_module = types.ModuleType("astrbot.core")
message_module = types.ModuleType("astrbot.core.message")
components_module = types.ModuleType("astrbot.core.message.components")
components_module.Json = _Component
components_module.Plain = _Component

sys.modules.setdefault("astrbot", astrbot_module)
sys.modules.setdefault("astrbot.api", api_module)
sys.modules.setdefault("astrbot.api.event", event_module)
sys.modules.setdefault("astrbot.api.star", star_module)
sys.modules.setdefault("astrbot.core", core_module)
sys.modules.setdefault("astrbot.core.message", message_module)
sys.modules.setdefault("astrbot.core.message.components", components_module)

from astrbot_plugin_kook_music import main as main_module
from astrbot_plugin_kook_music.music.bilibili import BilibiliCollection
from astrbot_plugin_kook_music.music.model import Song


class _FakeEvent:
    def __init__(self):
        self.sent = []

    def get_sender_id(self):
        return "user"

    def get_sender_name(self):
        return "tester"

    def plain_result(self, content):
        return content

    def chain_result(self, content):
        return content

    async def send(self, result):
        self.sent.append(result)


class _FakeBilibili:
    async def materialize_collection_songs(self, songs):
        return songs


class _BlockingVoiceManager:
    def __init__(self):
        self.lookup_started = asyncio.Event()
        self.release_lookup = asyncio.Event()
        self.join_calls = 0

    def get_session(self, _guild_id):
        return None

    async def get_user_voice_channel(self, _token, _guild_id, _user_id):
        self.lookup_started.set()
        await self.release_lookup.wait()
        return "voice"

    async def join_and_play_many(self, *_args):
        self.join_calls += 1
        return True, "ok"


class BilibiliBatchRequestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        main_module._pending_playlist_ranges.clear()
        main_module._playlist_import_requests.clear()
        main_module._bilibili_play_requests.clear()

    async def asyncTearDown(self):
        main_module._pending_playlist_ranges.clear()
        main_module._playlist_import_requests.clear()
        main_module._bilibili_play_requests.clear()

    async def test_replaced_request_cannot_commit_after_voice_lookup(self):
        plugin = object.__new__(main_module.KookMusicPlugin)
        plugin.max_queue_size = 10
        plugin.playlist_range_timeout = 1
        plugin._kook_token = "token"
        plugin.bilibili = _FakeBilibili()
        plugin.voice_manager = _BlockingVoiceManager()
        plugin._get_channel_id = lambda _event: "text"

        event = _FakeEvent()
        songs = [
            Song(
                id=f"BV000000000{i}",
                name=f"part-{i}",
                platform="bilibili",
                provider_data={"bvid": f"BV000000000{i}", "page": 1},
            )
            for i in range(2)
        ]
        collection = BilibiliCollection(
            id="BV0000000000",
            title="parts",
            kind="分P视频",
            songs=songs,
        )
        key = "user_guild_text"
        old_marker = object()
        main_module._bilibili_play_requests[key] = old_marker

        task = asyncio.create_task(plugin._play_bilibili_collection(
            event,
            collection,
            "guild",
            key,
            old_marker,
        ))
        await plugin.voice_manager.lookup_started.wait()

        new_marker = object()
        async with main_module._playlist_request_commit_lock:
            async with main_module._pending_playlist_ranges_lock:
                main_module._bilibili_play_requests[key] = new_marker
                if main_module._playlist_import_requests.get(key) is old_marker:
                    main_module._playlist_import_requests.pop(key, None)

        plugin.voice_manager.release_lookup.set()
        await task

        self.assertEqual(plugin.voice_manager.join_calls, 0)
        self.assertTrue(any("已由更新" in str(message) for message in event.sent))


if __name__ == "__main__":
    unittest.main()
