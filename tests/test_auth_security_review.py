"""Independent regression cases for adapter and account lifecycle revocation."""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_kook_music.music.model import Song  # noqa: E402
from astrbot_plugin_kook_music.music.searcher import MusicSearcher  # noqa: E402
from astrbot_plugin_kook_music.music_auth.integration import (
    MusicAuthMixin,  # noqa: E402
)
from astrbot_plugin_kook_music.music_auth.manager import AuthManager  # noqa: E402
from astrbot_plugin_kook_music.music_auth.netease_backend import (
    _audio_url,  # noqa: E402
)
from astrbot_plugin_kook_music.music_auth.types import (  # noqa: E402
    AudioResult,
    LoginChallenge,
    LoginPoll,
)


def platform(identifier):
    return SimpleNamespace(
        meta=lambda: SimpleNamespace(name="kook", id=identifier),
        config={"kook_bot_token": "synthetic-token"},
        client=object(),
    )


class Plugin(MusicAuthMixin):
    def __init__(self, platforms):
        self.config = {"music_auth_admin_ids": ["123"]}
        self.context = SimpleNamespace(
            platform_manager=SimpleNamespace(platform_insts=platforms),
        )
        self._configure_music_auth()


class SecurityReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_adapter_revokes_existing_login_manager(self):
        plugin = Plugin([])
        previous = SimpleNamespace(close=AsyncMock())
        plugin._music_auth = previous
        plugin._music_auth_binding = ("previous", "synthetic-token")
        self.addAsyncCleanup(plugin._close_music_auth)
        self.assertIsNone(await plugin._ensure_music_auth())
        previous.close.assert_awaited_once()
        self.assertIsNone(plugin._music_auth)
        self.assertIsNone(plugin._music_auth_binding)

    async def test_ambiguous_adapters_revoke_existing_login_manager(self):
        plugin = Plugin([platform("one"), platform("two")])
        previous = SimpleNamespace(close=AsyncMock())
        plugin._music_auth = previous
        plugin._music_auth_binding = ("one", "synthetic-token")
        self.addAsyncCleanup(plugin._close_music_auth)
        self.assertIsNone(await plugin._ensure_music_auth())
        previous.close.assert_awaited_once()
        self.assertIsNone(plugin._music_auth)

    def manager(self, backend, notify=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        manager = AuthManager(
            credentials_dir=Path(temporary.name),
            backends={"netease": backend},
            notify=notify or AsyncMock(),
            admin_ids={"123"},
            poll_interval=0.001,
            operation_timeout=1,
        )
        self.addAsyncCleanup(manager.close)
        return manager

    async def test_logout_rejects_late_account_audio(self):
        started, finish = asyncio.Event(), asyncio.Event()

        async def resolve(song, credential):
            started.set()
            await finish.wait()
            return AudioResult("resolved", "https://m7.music.126.net/private.mp3")

        backend = SimpleNamespace(resolve_audio=resolve, close=AsyncMock())
        manager = self.manager(backend)
        state = manager.store.load()
        state["accounts"]["netease"] = {
            "credential": {"cookies": {"MUSIC_U": "synthetic"}},
            "state": "valid",
        }
        manager._save(state)
        task = asyncio.create_task(
            manager.resolve_audio(Song(id="123", platform="netease"))
        )
        await started.wait()
        logged_out, _ = await manager.logout("netease", "bot", "123")
        self.assertTrue(logged_out)
        finish.set()
        result = await task
        self.assertNotEqual(result.status, "resolved")
        self.assertFalse(result.url)

    async def test_close_rejects_late_account_audio(self):
        started, finish = asyncio.Event(), asyncio.Event()

        async def resolve(song, credential):
            started.set()
            await finish.wait()
            return AudioResult("resolved", "https://m7.music.126.net/private.mp3")

        backend = SimpleNamespace(resolve_audio=resolve, close=AsyncMock())
        manager = self.manager(backend)
        state = manager.store.load()
        state["accounts"]["netease"] = {
            "credential": {"cookies": {"MUSIC_U": "synthetic"}},
            "state": "valid",
        }
        manager._save(state)
        task = asyncio.create_task(
            manager.resolve_audio(Song(id="123", platform="netease"))
        )
        await started.wait()
        await manager.close()
        finish.set()
        result = await task
        self.assertNotEqual(result.status, "resolved")
        self.assertFalse(result.url)

    async def test_cancel_during_backend_cleanup_still_removes_private_qr(self):
        cleanup_started, finish_cleanup = asyncio.Event(), asyncio.Event()
        cleanup = AsyncMock()

        async def cancel_backend(_challenge):
            cleanup_started.set()
            await finish_cleanup.wait()

        async def notify(bot, user, text, qr):
            return cleanup if qr else None

        backend = SimpleNamespace(
            begin_login=AsyncMock(return_value=LoginChallenge("fake", b"qr", 180)),
            poll_login=AsyncMock(return_value=LoginPoll("expired")),
            cancel_login=cancel_backend,
            close=AsyncMock(),
        )
        manager = self.manager(backend, notify)
        await manager.start_login("netease", "netease", "bot", "123")
        await cleanup_started.wait()
        cancel_task = asyncio.create_task(manager.cancel_login("netease", "bot", "123"))
        await asyncio.sleep(0)
        finish_cleanup.set()
        await cancel_task
        cleanup.assert_awaited_once()

    def test_netease_backend_cdn_urls_pass_searcher_validation(self):
        for url in (
            "http://m701.music.126.net/a.mp3?auth=synthetic",
            "https://m7.music.126.net/path/audio.m4a",
            "https://M10.MUSIC.126.NET/path/song.flac",
        ):
            normalized = _audio_url(url)
            self.assertTrue(normalized)
            self.assertTrue(MusicSearcher._is_netease_audio_url(normalized))


if __name__ == "__main__":
    unittest.main()
