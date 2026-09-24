import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock


PLUGINS_DIR = Path(__file__).resolve().parents[2]
if str(PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGINS_DIR))

from astrbot_plugin_kook_music.kook_voice.voice_manager import (
    GuildSession,
    VoiceManager,
)
from astrbot_plugin_kook_music.music.model import Song


class VoiceControlTests(unittest.IsolatedAsyncioTestCase):
    def make_manager(self, requesters=("alice", "bob", "carol")):
        manager = VoiceManager()
        session = GuildSession(
            guild_id="guild",
            voice_channel_id="voice",
            text_channel_id="text",
            voice_client=SimpleNamespace(disconnect=AsyncMock()),
            ffmpeg_player=SimpleNamespace(stop=AsyncMock()),
            playlist=[
                Song(id=str(index), name=str(index), requester_id=requester)
                for index, requester in enumerate(requesters)
            ],
            is_playing=True,
        )
        manager.sessions["guild"] = session
        return manager, session

    def assert_unchanged(self, manager, session, songs):
        self.assertIs(manager.sessions["guild"], session)
        self.assertEqual(session.playlist, songs)
        self.assertEqual(session.pending_skips, 0)
        self.assertEqual(session.loop_mode, 0)
        session.ffmpeg_player.stop.assert_not_awaited()
        session.voice_client.disconnect.assert_not_awaited()

    def test_control_song_tracks_pending_skips(self):
        manager, session = self.make_manager()
        for index in range(len(session.playlist)):
            session.pending_skips = index
            self.assertIs(manager.control_song(session), session.playlist[index])
        for index in (-1, len(session.playlist), len(session.playlist) + 1):
            session.pending_skips = index
            self.assertIsNone(manager.control_song(session))
        session.playlist.clear()
        session.pending_skips = 0
        self.assertIsNone(manager.control_song(session))

    async def test_all_actions_reject_another_requester_without_mutating_state(self):
        for action in ("next", "loop", "clear", "move", "leave"):
            with self.subTest(action=action):
                manager, session = self.make_manager()
                songs = list(session.playlist)
                ok, _ = await manager.control(
                    "guild", action, actor_id="bob", position=3
                )
                self.assertFalse(ok)
                self.assert_unchanged(manager, session, songs)

    async def test_requester_can_execute_each_control(self):
        for action in ("next", "loop", "clear", "move", "leave"):
            with self.subTest(action=action):
                manager, session = self.make_manager()
                ok, _ = await asyncio.wait_for(
                    manager.control(
                        "guild",
                        action,
                        actor_id="carol" if action == "move" else "alice",
                        position=3,
                    ),
                    timeout=1,
                )
                self.assertTrue(ok)
                if action == "next":
                    self.assertEqual(session.pending_skips, 1)
                    session.ffmpeg_player.stop.assert_awaited_once()
                elif action == "loop":
                    self.assertEqual(session.loop_mode, 1)
                elif action == "clear":
                    self.assertEqual([song.id for song in session.playlist], ["0"])
                elif action == "move":
                    self.assertEqual(
                        [song.id for song in session.playlist], ["0", "2", "1"]
                    )
                else:
                    self.assertNotIn("guild", manager.sessions)
                    session.voice_client.disconnect.assert_awaited_once()

    async def test_administrator_can_execute_each_control(self):
        for action in ("next", "loop", "clear", "move", "leave"):
            with self.subTest(action=action):
                manager, _ = self.make_manager()
                ok, _ = await asyncio.wait_for(
                    manager.control(
                        "guild", action, actor_id="moderator", is_admin=True, position=3
                    ),
                    timeout=1,
                )
                self.assertTrue(ok)

    async def test_repeated_skip_cannot_cross_into_another_requester(self):
        manager, session = self.make_manager()
        self.assertTrue((await manager.control("guild", "next", actor_id="alice"))[0])
        self.assertFalse((await manager.control("guild", "next", actor_id="alice"))[0])
        self.assertEqual(session.pending_skips, 1)
        self.assertTrue((await manager.control("guild", "next", actor_id="bob"))[0])
        self.assertEqual(session.pending_skips, 2)
        self.assertFalse((await manager.control("guild", "clear", actor_id="bob"))[0])
        self.assertEqual(len(session.playlist), 3)
        self.assertTrue((await manager.control("guild", "next", actor_id="carol"))[0])
        self.assertFalse((await manager.control("guild", "loop", actor_id="carol"))[0])
        self.assertEqual(session.pending_skips, 3)

    async def test_repeated_skip_allows_consecutive_songs_of_same_requester(self):
        manager, session = self.make_manager(("alice", "alice", "bob"))
        results = await asyncio.gather(
            *(manager.control("guild", "next", actor_id="alice") for _ in range(3))
        )
        self.assertEqual([result[0] for result in results], [True, True, False])
        self.assertEqual(session.pending_skips, 2)

    async def test_administrator_can_skip_consecutively_across_requesters(self):
        manager, session = self.make_manager()
        for _ in session.playlist:
            result = await manager.control(
                "guild", "next", actor_id="moderator", is_admin=True
            )
            self.assertTrue(result[0])
        result = await manager.control(
            "guild", "next", actor_id="moderator", is_admin=True
        )
        self.assertFalse(result[0])
        self.assertEqual(session.pending_skips, 3)

    async def test_move_cannot_insert_another_requester_into_pending_skip_prefix(self):
        for is_admin in (False, True):
            with self.subTest(is_admin=is_admin):
                manager, session = self.make_manager(("alice", "alice", "alice", "bob"))
                songs = list(session.playlist)
                for _ in range(2):
                    self.assertTrue(
                        (await manager.control("guild", "next", actor_id="alice"))[0]
                    )
                result = await manager.control(
                    "guild", "move", actor_id="alice", is_admin=is_admin, position=4
                )
                self.assertFalse(result[0])
                self.assertEqual(result[1], "正在切换歌曲，请稍后再调整队列")
                self.assertEqual(session.playlist, songs)
                self.assertEqual(session.pending_skips, 2)
                for _ in range(2):
                    self.assertTrue(manager._consume_pending_skip(session))
                self.assertEqual(session.playlist, songs[2:])
                self.assertEqual(session.playlist[-1].requester_id, "bob")

    async def test_clear_cannot_remove_effective_current_song_during_pending_skip(self):
        for is_admin in (False, True):
            with self.subTest(is_admin=is_admin):
                manager, session = self.make_manager(("alice", "alice", "bob"))
                songs = list(session.playlist)
                self.assertTrue(
                    (await manager.control("guild", "next", actor_id="alice"))[0]
                )
                result = await manager.control(
                    "guild", "clear", actor_id="alice", is_admin=is_admin
                )
                self.assertFalse(result[0])
                self.assertEqual(result[1], "正在切换歌曲，请稍后再调整队列")
                self.assertEqual(session.playlist, songs)
                self.assertEqual(session.pending_skips, 1)
                self.assertTrue(manager._consume_pending_skip(session))
                self.assertIs(manager.control_song(session), songs[1])
                self.assertEqual(session.playlist, songs[1:])

    async def test_unknown_actor_is_denied_even_if_admin(self):
        for actor in ("", "  ", None):
            for is_admin in (False, True):
                with self.subTest(actor=actor, is_admin=is_admin):
                    manager, session = self.make_manager()
                    songs = list(session.playlist)
                    result = await manager.control(
                        "guild", "clear", actor_id=actor, is_admin=is_admin
                    )
                    self.assertFalse(result[0])
                    self.assert_unchanged(manager, session, songs)

    async def test_unknown_requester_requires_administrator(self):
        for requester in ("", "  ", None):
            with self.subTest(requester=requester):
                manager, session = self.make_manager((requester, "bob"))
                self.assertFalse(
                    (await manager.control("guild", "clear", actor_id="bob"))[0]
                )
                self.assertEqual(len(session.playlist), 2)
                result = await manager.control(
                    "guild", "clear", actor_id="moderator", is_admin=True
                )
                self.assertTrue(result[0])

    async def test_empty_queue_requires_administrator(self):
        manager, session = self.make_manager(())
        for action in ("loop", "clear", "leave"):
            with self.subTest(action=action):
                self.assertFalse(
                    (await manager.control("guild", action, actor_id="alice"))[0]
                )
        self.assertIs(manager.sessions["guild"], session)
        result = await asyncio.wait_for(
            manager.control("guild", "leave", actor_id="moderator", is_admin=True),
            timeout=1,
        )
        self.assertTrue(result[0])
        self.assertNotIn("guild", manager.sessions)

    async def test_authorization_rechecks_song_after_waiting_for_lock(self):
        manager, session = self.make_manager()
        lock = manager._guild_locks.setdefault("guild", asyncio.Lock())
        await lock.acquire()
        task = asyncio.create_task(
            manager.control("guild", "next", actor_id="alice", expected_session=session)
        )
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        session.playlist.pop(0)
        lock.release()
        self.assertFalse((await task)[0])
        self.assertEqual(session.pending_skips, 0)
        session.ffmpeg_player.stop.assert_not_awaited()

    async def test_authorization_rechecks_pending_skip_after_waiting_for_lock(self):
        manager, session = self.make_manager()
        lock = manager._guild_locks.setdefault("guild", asyncio.Lock())
        await lock.acquire()
        task = asyncio.create_task(
            manager.control("guild", "loop", actor_id="alice", expected_session=session)
        )
        await asyncio.sleep(0)
        session.pending_skips = 1
        lock.release()
        self.assertFalse((await task)[0])
        self.assertEqual(session.loop_mode, 0)

    async def test_replaced_session_rejected_even_for_administrator(self):
        manager, session = self.make_manager()
        lock = manager._guild_locks.setdefault("guild", asyncio.Lock())
        await lock.acquire()
        task = asyncio.create_task(
            manager.control(
                "guild",
                "leave",
                actor_id="moderator",
                is_admin=True,
                expected_session=session,
            )
        )
        await asyncio.sleep(0)
        _, replacement = self.make_manager()
        manager.sessions["guild"] = replacement
        lock.release()
        self.assertFalse((await task)[0])
        self.assertIs(manager.sessions["guild"], replacement)
        replacement.voice_client.disconnect.assert_not_awaited()
        session.voice_client.disconnect.assert_not_awaited()

    async def test_removed_session_rejected_while_waiting_for_lock(self):
        manager, session = self.make_manager()
        lock = manager._guild_locks.setdefault("guild", asyncio.Lock())
        await lock.acquire()
        task = asyncio.create_task(
            manager.control(
                "guild", "clear", actor_id="alice", expected_session=session
            )
        )
        await asyncio.sleep(0)
        manager.sessions.pop("guild")
        lock.release()
        self.assertFalse((await task)[0])
        self.assertEqual(len(session.playlist), 3)

    async def test_unknown_action_and_invalid_position_do_not_mutate_state(self):
        manager, session = self.make_manager()
        songs = list(session.playlist)
        self.assertFalse(
            (await manager.control("guild", "unknown", actor_id="alice"))[0]
        )
        for position in (None, "3", True, 0, 4):
            with self.subTest(position=position):
                result = await manager.control(
                    "guild", "move", actor_id="alice", position=position
                )
                self.assertFalse(result[0])
                self.assert_unchanged(manager, session, songs)


if __name__ == "__main__":
    unittest.main()
