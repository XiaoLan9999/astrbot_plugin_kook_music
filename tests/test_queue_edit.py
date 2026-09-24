import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_kook_music.kook_voice.voice_manager import (  # noqa: E402
    GuildSession,
    VoiceManager,
)
from astrbot_plugin_kook_music.music.model import Song  # noqa: E402
from test_voice_manager import (  # noqa: E402
    BlockingDirectPlayer,
    FakeVoiceClient,
    wait_for,
)


class QueueEditTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.files = []
        self.managers = []

    async def asyncTearDown(self):
        for manager in self.managers:
            await manager.leave_all()
        self.temp.cleanup()

    def ready(self, song):
        path = self.root / f"{len(self.files)}-{song.id}.mp3"
        path.write_bytes(b"synthetic audio")
        self.files.append(path)
        song.file_path = str(path)
        return song

    def song(self, name, *, owner="owner", ready=True):
        song = Song(id=name, name=name, platform="qq", requester_id=owner)
        return self.ready(song) if ready else song

    def manager(self, songs=None, *, loop=0):
        manager = VoiceManager(streaming_mode="direct", auto_leave_timeout=0)
        manager.PLAYBACK_START_DELAY = 0
        manager.DIRECT_RECONNECT_DELAY = 0
        player = BlockingDirectPlayer()
        player.stop = AsyncMock(wraps=player.stop)
        session = GuildSession(
            guild_id="guild",
            voice_channel_id="voice",
            text_channel_id="text",
            voice_client=FakeVoiceClient(),
            ffmpeg_player=player,
            playlist=list(
                songs if songs is not None else [self.song(name) for name in "abcd"]
            ),
            loop_mode=loop,
        )
        manager.sessions["guild"] = session
        self.managers.append(manager)
        return manager, session, player

    async def start(self, manager, player):
        manager._start_playback_loop("guild")
        await wait_for(lambda: len(player.played) == 1)

    async def edit(
        self,
        manager,
        action,
        position,
        *,
        destination=None,
        actor="owner",
        admin=False,
        **kwargs,
    ):
        return await manager.control(
            "guild",
            action,
            position=position,
            destination=destination,
            actor_id=actor,
            is_admin=admin,
            **kwargs,
        )

    async def test_remove_pending_item_cleans_file_without_touching_current_audio(self):
        manager, session, player = self.manager()
        await self.start(manager, player)
        current, removed = session.playlist[0], session.playlist[2]
        removed_file = Path(removed.file_path)
        stop_calls = player.stop.await_count
        ok, _ = await self.edit(manager, "remove", 3)
        self.assertTrue(ok)
        self.assertEqual([song.id for song in session.playlist], ["a", "b", "d"])
        self.assertIs(session.current_song, current)
        self.assertTrue(Path(current.file_path).exists())
        self.assertFalse(removed_file.exists())
        self.assertEqual(removed.file_path, "")
        self.assertTrue(player.is_playing)
        self.assertEqual(player.stop.await_count, stop_calls)
        self.assertEqual(len(player.played), 1)

    async def test_reorder_uses_final_position_forward_and_backward(self):
        manager, session, player = self.manager()
        await self.start(manager, player)
        stop_calls = player.stop.await_count
        self.assertTrue((await self.edit(manager, "reorder", 2, destination=4))[0])
        self.assertEqual([song.id for song in session.playlist], ["a", "c", "d", "b"])
        self.assertTrue((await self.edit(manager, "reorder", 4, destination=2))[0])
        self.assertEqual([song.id for song in session.playlist], list("abcd"))
        self.assertEqual(player.stop.await_count, stop_calls)
        self.assertTrue(all(Path(song.file_path).exists() for song in session.playlist))

    async def test_source_owner_can_edit_but_current_owner_cannot_edit_others(self):
        for action in ("remove", "reorder", "move"):
            with self.subTest(action=action):
                manager, session, _ = self.manager(
                    [
                        self.song("current", owner="current-owner"),
                        self.song("other", owner="other-owner"),
                        self.song("mine", owner="pending-owner"),
                    ]
                )
                before = list(session.playlist)
                self.assertFalse(
                    (
                        await self.edit(
                            manager, action, 3, destination=2, actor="current-owner"
                        )
                    )[0]
                )
                self.assertEqual(session.playlist, before)
                self.assertTrue(
                    (
                        await self.edit(
                            manager, action, 3, destination=2, actor="pending-owner"
                        )
                    )[0]
                )
                self.assertIs(session.current_song, before[0])

    async def test_admin_can_edit_any_pending_song_but_never_first_song(self):
        for action in ("remove", "reorder", "move"):
            manager, _, _ = self.manager()
            self.assertTrue(
                (
                    await self.edit(
                        manager, action, 3, destination=2, actor="admin", admin=True
                    )
                )[0]
            )
            self.assertFalse(
                (
                    await self.edit(
                        manager, action, 1, destination=2, actor="admin", admin=True
                    )
                )[0]
            )
        manager, _, _ = self.manager()
        self.assertFalse(
            (
                await self.edit(
                    manager, "reorder", 2, destination=1, actor="admin", admin=True
                )
            )[0]
        )

    async def test_skip_clear_stop_loop_still_use_current_song_owner(self):
        manager, _, _ = self.manager(
            [
                self.song("current", owner="current-owner"),
                self.song("mine", owner="pending-owner"),
            ]
        )
        for action in ("next", "clear", "stop", "loop"):
            self.assertFalse(
                (await self.edit(manager, action, None, actor="pending-owner"))[0]
            )

    async def test_missing_owner_empty_actor_and_truthy_non_boolean_admin_fail_closed(
        self,
    ):
        manager, session, _ = self.manager()
        session.playlist[1].requester_id = ""
        for actor, admin in (
            ("owner", False),
            ("", True),
            ("other", 1),
            ("other", "true"),
        ):
            self.assertFalse(
                (await self.edit(manager, "remove", 2, actor=actor, admin=admin))[0]
            )
        self.assertEqual(len(session.playlist), 4)

    async def test_invalid_positions_and_noop_leave_all_resources_untouched(self):
        manager, session, _ = self.manager()
        before = list(session.playlist)
        for value in (None, True, False, "2", 2.0, 0, -1, 5, 1):
            for action in ("remove", "reorder", "move"):
                self.assertFalse(
                    (await self.edit(manager, action, value, destination=2))[0]
                )
            self.assertFalse(
                (await self.edit(manager, "reorder", 2, destination=value))[0]
            )
        self.assertTrue((await self.edit(manager, "reorder", 2, destination=2))[0])
        self.assertEqual(session.playlist, before)
        self.assertTrue(all(Path(song.file_path).exists() for song in before))

    async def test_empty_missing_and_stale_session_are_rejected(self):
        manager, session, _ = self.manager([])
        self.assertFalse(
            (await self.edit(manager, "remove", 2, actor="admin", admin=True))[0]
        )
        manager.sessions.pop("guild")
        self.assertFalse(
            (await self.edit(manager, "remove", 2, actor="admin", admin=True))[0]
        )
        replacement, replacement_session, _ = self.manager()
        self.assertFalse(
            (await self.edit(replacement, "remove", 2, expected_session=session))[0]
        )
        self.assertEqual(len(replacement_session.playlist), 4)

    async def test_direct_edit_methods_also_reject_pending_skips(self):
        manager, session, _ = self.manager()
        session.pending_skips = 2
        before = list(session.playlist)
        for result in (
            await manager.remove_queued_song("guild", 2),
            await manager.reorder_queued_song("guild", 2, 3),
            await manager.move_to_next("guild", 3),
        ):
            self.assertFalse(result[0])
            self.assertIn("切换", result[1])
        for action in ("remove", "reorder", "move"):
            result = await self.edit(
                manager, action, 3, destination=2, actor="admin", admin=True
            )
            self.assertFalse(result[0])
            self.assertIn("切换", result[1])
        self.assertEqual(session.playlist, before)

    async def test_first_song_stays_protected_while_preparing(self):
        first = self.song("first", ready=False)
        manager, session, player = self.manager([first, self.song("next")])
        entered, release = asyncio.Event(), asyncio.Event()

        async def prepare(song):
            entered.set()
            await release.wait()
            return self.ready(song)

        manager.on_download_song = prepare
        manager._start_playback_loop("guild")
        await asyncio.wait_for(entered.wait(), 1)
        preparation = session.preparation_task
        self.assertFalse((await self.edit(manager, "remove", 1))[0])
        self.assertFalse((await self.edit(manager, "reorder", 2, destination=1))[0])
        self.assertTrue((await self.edit(manager, "remove", 2))[0])
        self.assertIs(session.preparation_task, preparation)
        self.assertFalse(preparation.cancelled())
        release.set()
        await wait_for(lambda: player.is_playing)
        self.assertEqual(session.current_song.id, "first")

    async def test_completed_prefetch_removed_and_next_target_prepared(self):
        manager, session, player = self.manager(
            [
                self.song("first"),
                self.song("second", ready=False),
                self.song("third", ready=False),
            ]
        )
        manager.on_download_song = AsyncMock(side_effect=self.ready)
        await self.start(manager, player)
        await wait_for(lambda: session.prefetch and session.prefetch.task.done())
        cached = Path(session.prefetch.task.result().file_path)
        self.assertTrue((await self.edit(manager, "remove", 2))[0])
        await wait_for(
            lambda: (
                session.prefetch
                and session.prefetch.original.id == "third"
                and session.prefetch.task.done()
            )
        )
        self.assertFalse(cached.exists())
        self.assertEqual(session.current_song.id, "first")
        self.assertTrue(player.is_playing)

    async def test_remove_late_cancellation_resistant_prefetch_never_resurrects_song(
        self,
    ):
        manager, session, player = self.manager(
            [
                self.song("first"),
                self.song("removed", ready=False),
                self.song("next"),
            ]
        )
        entered, release = asyncio.Event(), asyncio.Event()
        late_files = []

        async def prepare(song):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            result = self.ready(song)
            late_files.append(Path(result.file_path))
            return result

        manager.on_download_song = prepare
        await self.start(manager, player)
        await asyncio.wait_for(entered.wait(), 1)
        old = session.prefetch
        self.assertTrue((await self.edit(manager, "remove", 2))[0])
        self.assertTrue(old.discarded)
        release.set()
        await wait_for(lambda: not session.prefetch_tasks)
        self.assertTrue(late_files)
        self.assertTrue(all(not path.exists() for path in late_files))
        self.assertEqual([song.id for song in session.playlist], ["first", "next"])
        player.finish()
        await wait_for(lambda: len(player.played) == 2)
        self.assertEqual(session.current_song.id, "next")

    async def test_reorder_retargets_prefetch_and_preserves_current(self):
        manager, session, player = self.manager(
            [
                self.song("first"),
                self.song("second", ready=False),
                self.song("third", ready=False),
            ]
        )
        manager.on_download_song = AsyncMock(side_effect=self.ready)
        await self.start(manager, player)
        await wait_for(lambda: session.prefetch and session.prefetch.task.done())
        old_cached = Path(session.prefetch.task.result().file_path)
        self.assertTrue((await self.edit(manager, "reorder", 3, destination=2))[0])
        await wait_for(
            lambda: (
                session.prefetch
                and session.prefetch.original.id == "third"
                and session.prefetch.task.done()
            )
        )
        self.assertFalse(old_cached.exists())
        next_cached = session.prefetch.task.result().file_path
        player.finish()
        await wait_for(lambda: len(player.played) == 2)
        self.assertEqual(player.played[1], next_cached)
        self.assertEqual(session.current_song.id, "third")

    async def test_edit_away_from_next_preserves_existing_prefetch(self):
        manager, session, player = self.manager(
            [
                self.song("first"),
                self.song("second", ready=False),
                self.song("third"),
                self.song("fourth"),
            ]
        )
        manager.on_download_song = AsyncMock(side_effect=self.ready)
        await self.start(manager, player)
        await wait_for(lambda: session.prefetch and session.prefetch.task.done())
        original = session.prefetch
        self.assertTrue((await self.edit(manager, "reorder", 4, destination=3))[0])
        self.assertIs(session.prefetch, original)
        self.assertTrue((await self.edit(manager, "remove", 4))[0])
        self.assertIs(session.prefetch, original)
        self.assertFalse(original.discarded)

    async def test_natural_completion_uses_edited_order(self):
        manager, session, player = self.manager()
        await self.start(manager, player)
        await self.edit(manager, "reorder", 4, destination=2)
        await self.edit(manager, "remove", 3)
        player.finish()
        await wait_for(lambda: len(player.played) == 2)
        self.assertEqual([song.id for song in session.playlist], ["d", "c"])
        player.finish()
        await wait_for(lambda: len(player.played) == 3)
        self.assertEqual(session.current_song.id, "c")

    async def test_waiting_edit_rechecks_owner_after_natural_completion(self):
        manager, session, player = self.manager(
            [
                self.song("a", owner="a"),
                self.song("b", owner="b"),
                self.song("c", owner="c"),
            ]
        )
        await self.start(manager, player)
        lock = manager._guild_locks.setdefault("guild", asyncio.Lock())
        await lock.acquire()
        task = asyncio.create_task(self.edit(manager, "remove", 2, actor="b"))
        try:
            player.finish()
            await wait_for(
                lambda: session.current_song.id == "b" and len(player.played) == 2
            )
        finally:
            lock.release()
        self.assertFalse((await task)[0])
        self.assertEqual([song.id for song in session.playlist], ["b", "c"])

    async def test_rapid_skips_reject_edits_until_transition_finishes(self):
        manager, session, player = self.manager()
        await self.start(manager, player)
        self.assertTrue((await self.edit(manager, "next", None))[0])
        self.assertTrue((await self.edit(manager, "next", None))[0])
        self.assertEqual(session.pending_skips, 2)
        for action in ("remove", "reorder", "move"):
            self.assertFalse((await self.edit(manager, action, 3, destination=2))[0])
        await wait_for(
            lambda: session.current_song.id == "c" and session.pending_skips == 0
        )
        self.assertTrue((await self.edit(manager, "remove", 2))[0])
        self.assertEqual([song.id for song in session.playlist], ["c"])

    async def test_stop_then_queued_edit_cannot_recreate_queue(self):
        manager, session, player = self.manager()
        await self.start(manager, player)
        stop = asyncio.create_task(self.edit(manager, "stop", None))
        edit = asyncio.create_task(self.edit(manager, "remove", 2, admin=True))
        result, removed = await asyncio.gather(stop, edit)
        self.assertTrue(result[0])
        self.assertFalse(removed[0])
        self.assertEqual(session.playlist, [])
        self.assertFalse(player.is_playing)
        self.assertFalse(session.prefetch_tasks)

    async def test_list_loop_does_not_restore_removed_item(self):
        manager, session, player = self.manager(loop=2)
        await self.start(manager, player)
        await self.edit(manager, "remove", 2)
        await self.edit(manager, "reorder", 3, destination=2)
        player.finish()
        await wait_for(lambda: len(player.played) == 2)
        self.assertEqual([song.id for song in session.playlist], ["d", "c", "a"])
        self.assertTrue(all(song.id != "b" for song in session.playlist))

    async def test_shuffle_after_current_song_keeps_remaining_items_only(self):
        manager, session, player = self.manager(loop=3)
        await self.start(manager, player)
        await self.edit(manager, "remove", 3)
        await self.edit(manager, "reorder", 3, destination=2)
        with patch(
            "astrbot_plugin_kook_music.kook_voice.voice_manager.random.shuffle",
            side_effect=lambda items: items.reverse(),
        ) as shuffle:
            player.finish()
            await wait_for(lambda: len(player.played) == 2)
            shuffle.assert_called_once()
        self.assertEqual({song.id for song in session.playlist}, {"b", "d"})
        self.assertEqual(session.current_song.id, "b")

    async def test_concurrent_append_and_edit_are_serialized_without_losing_tracks(
        self,
    ):
        manager, session, player = self.manager()
        await self.start(manager, player)
        added = self.song("added", owner="another")
        result, reordered = await asyncio.gather(
            manager.add_song("guild", added),
            self.edit(manager, "reorder", 4, destination=2),
        )
        self.assertTrue(result[0])
        self.assertTrue(reordered[0])
        self.assertEqual(
            [song.id for song in session.playlist], ["a", "d", "b", "c", "added"]
        )

    async def test_duplicate_object_and_shared_cache_do_not_delete_current_resource(
        self,
    ):
        for same_object in (False, True):
            current = self.song("current")
            queued = current if same_object else self.song("other", ready=False)
            queued.file_path = current.file_path
            manager, session, player = self.manager([current, queued])
            await self.start(manager, player)
            path = Path(current.file_path)
            self.assertTrue((await self.edit(manager, "remove", 2))[0])
            self.assertEqual(session.playlist, [current])
            self.assertTrue(path.exists())
            self.assertEqual(current.file_path, str(path))
            self.assertTrue(player.is_playing)

    async def test_remove_last_pending_song_keeps_current_playing_then_finishes_normally(
        self,
    ):
        manager, session, player = self.manager(
            [self.song("current"), self.song("last")]
        )
        await self.start(manager, player)
        self.assertTrue((await self.edit(manager, "remove", 2))[0])
        self.assertTrue(player.is_playing)
        self.assertEqual(len(session.playlist), 1)
        player.finish()
        await wait_for(lambda: not session.is_playing)
        self.assertEqual(session.playlist, [])
        self.assertEqual(len(player.played), 1)

    async def test_clear_then_waiting_reorder_does_not_restore_removed_items(self):
        manager, session, player = self.manager()
        await self.start(manager, player)
        cleared, reordered = await asyncio.gather(
            self.edit(manager, "clear", None),
            self.edit(manager, "reorder", 4, destination=2),
        )
        self.assertTrue(cleared[0])
        self.assertFalse(reordered[0])
        self.assertEqual([song.id for song in session.playlist], ["a"])
        self.assertTrue(player.is_playing)

    async def test_own_song_can_cross_others_but_foreign_source_is_rejected(self):
        manager, session, player = self.manager(
            [
                self.song("current", owner="current"),
                self.song("mine", owner="me"),
                self.song("other-one", owner="one"),
                self.song("other-two", owner="two"),
            ]
        )
        await self.start(manager, player)
        self.assertTrue(
            (await self.edit(manager, "reorder", 2, destination=4, actor="me"))[0]
        )
        self.assertEqual(
            [song.id for song in session.playlist],
            ["current", "other-one", "other-two", "mine"],
        )
        self.assertFalse(
            (await self.edit(manager, "reorder", 2, destination=4, actor="me"))[0]
        )
        self.assertTrue(player.is_playing)


if __name__ == "__main__":
    unittest.main()
