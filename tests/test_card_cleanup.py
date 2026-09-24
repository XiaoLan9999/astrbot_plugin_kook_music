import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from test_main_bilibili_flow import main_module

# isort: split

from astrbot_plugin_kook_music import card_cleanup
from astrbot_plugin_kook_music.card_builder import (
    build_now_playing_card,
    build_queued_card,
)
from astrbot_plugin_kook_music.card_ledger import CardLedger
from astrbot_plugin_kook_music.music.model import Song

TOKEN = "synthetic-card-token"
OTHER_TOKEN = "synthetic-other-card-token"
BOT = "12345"
GUILD = "67890"
CHANNEL = "98765"


def row(message_id, created=100, *, author=BOT, queued=False):
    song = Song(id="synthetic-song", name="Synthetic Song", duration=100000)
    card = build_queued_card(song) if queued else build_now_playing_card(song)
    return {
        "id": message_id,
        "type": 10,
        "author": {"id": author},
        "create_at": created,
        "content": json.dumps([card]),
    }


class CardCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.plugins = []
        self.identity = AsyncMock(return_value={"id": BOT, "bot": True})
        self.history = AsyncMock(return_value={"items": []})
        self.delete = AsyncMock(return_value=True)
        self.sender = AsyncMock(return_value="fresh-card")
        for module, name, value in (
            (card_cleanup, "get_bot_identity", self.identity),
            (card_cleanup, "get_channel_messages", self.history),
            (main_module, "delete_message", self.delete),
            (main_module, "send_card_message", self.sender),
        ):
            replacement = patch.object(module, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)

    async def asyncTearDown(self):
        for plugin in self.plugins:
            await plugin._close_card_cleanup()

    def plugin(self, *, name="default", token=TOKEN, playing=False):
        plugin = object.__new__(main_module.KookMusicPlugin)
        plugin.data_dir = self.root / name
        plugin._kook_token = token
        plugin._card_msg_ids = {}
        plugin._card_locks = {}
        plugin.voice_manager = NS(
            sessions={}, control=AsyncMock(return_value=(True, "ok"))
        )
        if playing:
            song = Song(
                id="synthetic-song", name="Synthetic Song", requester_id="owner"
            )
            plugin.voice_manager.sessions[GUILD] = NS(
                playlist=[song],
                current_song=song,
                pending_skips=0,
                voice_client=NS(token=token),
                is_playing=True,
                text_channel_id=CHANNEL,
                playback_started_at=0,
                playback_offset_seconds=0,
            )

        async def parked_loop():
            await asyncio.Event().wait()

        plugin._card_cleanup_loop = parked_loop
        plugin._init_card_cleanup()
        self.plugins.append(plugin)
        return plugin

    def track(self, plugin, message_id="old-card", *, token=TOKEN, pending=True):
        plugin._card_ledger.track(GUILD, CHANNEL, token, message_id, pending=pending)
        plugin._card_msg_ids.setdefault(GUILD, []).append(message_id)

    async def test_restart_restores_pending_receipts_and_removes_confirmed_ids(self):
        path = self.root / "default" / "playing_cards.json"
        ledger = CardLedger(path)
        ledger.track(GUILD, CHANNEL, TOKEN, "before-restart", pending=False)
        plugin = self.plugin()
        self.assertEqual(plugin._card_msg_ids[GUILD], ["before-restart"])
        self.assertTrue(plugin._card_ledger.records["before-restart"]["pending"])
        self.assertIn(GUILD, plugin._card_reconcile_needed)
        self.assertEqual(await plugin._cleanup_pending_cards(), 1)
        self.delete.assert_awaited_once_with(TOKEN, "before-restart")
        self.assertFalse(CardLedger(path).records)
        self.assertNotIn(GUILD, plugin._card_msg_ids)

    async def test_none_send_requests_history_recovery_of_own_playing_cards_only(self):
        plugin = self.plugin()
        self.sender.return_value = None
        card = build_now_playing_card(Song(id="song", name="Synthetic"))
        self.assertIsNone(await plugin._send_card(CHANNEL, GUILD, card))
        self.assertIn(GUILD, plugin._card_reconcile_needed)
        self.history.return_value = {
            "items": [
                row("own-stale", 1),
                row("user-card", 2, author="other-user"),
                row("queued-card", 3, queued=True),
                {
                    "id": "plain",
                    "type": 9,
                    "author": {"id": BOT},
                    "content": "ordinary",
                },
            ]
        }
        self.assertEqual(await plugin._reconcile_music_cards(GUILD, CHANNEL), 1)
        self.assertEqual(await plugin._cleanup_pending_cards(), 1)
        self.delete.assert_awaited_once_with(TOKEN, "own-stale")
        self.assertNotIn(GUILD, plugin._card_reconcile_needed)

    async def test_untracked_current_playback_keeps_latest_matching_card(self):
        plugin = self.plugin(playing=True)
        self.history.return_value = {
            "items": [
                row("latest-own", 50),
                row("older-own", 10),
                row("user-latest", 100, author="other-user"),
                row("queued-latest", 200, queued=True),
            ]
        }
        self.assertEqual(await plugin._reconcile_music_cards(GUILD, CHANNEL), 1)
        self.assertFalse(plugin._card_ledger.records["latest-own"]["pending"])
        self.assertTrue(plugin._card_ledger.records["older-own"]["pending"])
        await plugin._cleanup_pending_cards()
        self.delete.assert_awaited_once_with(TOKEN, "older-own")
        self.assertEqual(set(plugin._card_ledger.records), {"latest-own"})

    async def test_explicit_current_record_is_protected_during_reconciliation(self):
        plugin = self.plugin(playing=True)
        self.track(plugin, "current-card", pending=False)
        self.history.return_value = {
            "items": [row("current-card", 20), row("stray-card", 30)]
        }
        self.assertEqual(await plugin._reconcile_music_cards(GUILD, CHANNEL), 1)
        await plugin._cleanup_pending_cards()
        self.delete.assert_awaited_once_with(TOKEN, "stray-card")
        self.assertFalse(plugin._card_ledger.records["current-card"]["pending"])

    async def test_foreign_token_active_record_cannot_hide_current_latest_card(self):
        plugin = self.plugin(playing=True)
        self.track(plugin, "foreign-card", token=OTHER_TOKEN, pending=False)
        self.history.return_value = {
            "items": [row("new-bot-current", 30), row("new-bot-old", 10)]
        }
        self.assertEqual(await plugin._reconcile_music_cards(GUILD, CHANNEL), 1)
        self.assertFalse(plugin._card_ledger.records["new-bot-current"]["pending"])
        await plugin._cleanup_pending_cards()
        self.delete.assert_awaited_once_with(TOKEN, "new-bot-old")
        self.assertIn("foreign-card", plugin._card_ledger.records)

    async def test_queue_and_user_messages_are_never_saved_as_pending_cards(self):
        plugin = self.plugin()
        self.history.return_value = {
            "items": [row("user", author="another"), row("queue", queued=True)]
        }
        self.assertEqual(await plugin._reconcile_music_cards(GUILD, CHANNEL), 0)
        self.assertEqual(await plugin._cleanup_pending_cards(), 0)
        self.assertFalse(plugin._card_ledger.records)
        self.delete.assert_not_awaited()

    async def test_playing_in_other_channel_does_not_protect_history_in_cleanup_channel(
        self,
    ):
        plugin = self.plugin(playing=True)
        self.track(plugin, "actual-current", pending=False)
        self.history.return_value = {
            "items": [row("other-channel-old", 10), row("other-channel-latest", 30)]
        }
        self.assertEqual(
            await plugin._reconcile_music_cards(GUILD, "different-channel"), 2
        )
        self.assertFalse(plugin._card_ledger.records["actual-current"]["pending"])
        self.assertEqual(await plugin._cleanup_pending_cards(), 2)
        self.assertEqual(set(plugin._card_ledger.records), {"actual-current"})

    async def test_failed_background_delete_stays_persisted_until_next_pass(self):
        plugin = self.plugin()
        self.track(plugin)
        plugin._delete_message_with_retry = AsyncMock(return_value=False)
        self.assertEqual(await plugin._cleanup_pending_cards(), 0)
        self.assertTrue(plugin._card_ledger.records["old-card"]["pending"])
        self.assertIn(
            "old-card", CardLedger(plugin.data_dir / "playing_cards.json").records
        )
        self.assertIn("old-card", plugin._card_msg_ids[GUILD])
        plugin._delete_message_with_retry.return_value = True
        self.assertEqual(await plugin._cleanup_pending_cards(), 1)
        self.assertFalse(plugin._card_ledger.records)
        self.assertEqual(plugin._delete_message_with_retry.await_count, 2)

    async def test_missing_token_is_not_treated_as_successful_deletion(self):
        plugin = self.plugin(token="")
        self.track(plugin)
        self.assertEqual(await plugin._delete_card_messages(["old-card"]), ["old-card"])
        self.assertEqual(await plugin._cleanup_pending_cards(), 0)
        self.assertIn("old-card", plugin._card_ledger.records)
        self.delete.assert_not_awaited()

    async def test_background_cleanup_never_uses_new_token_for_old_records(self):
        plugin = self.plugin()
        self.track(plugin, token=OTHER_TOKEN)
        self.assertEqual(await plugin._cleanup_pending_cards(), 0)
        self.assertIn("old-card", plugin._card_ledger.records)
        self.delete.assert_not_awaited()

    async def test_direct_card_delete_preserves_known_foreign_token_records(self):
        plugin = self.plugin()
        self.track(plugin, token=OTHER_TOKEN)
        self.assertEqual(await plugin._delete_card_messages(["old-card"]), ["old-card"])
        self.assertIn("old-card", plugin._card_ledger.records)
        self.delete.assert_not_awaited()

    async def test_new_send_does_not_delete_restored_foreign_token_card(self):
        path = self.root / "default" / "playing_cards.json"
        CardLedger(path).track(GUILD, CHANNEL, OTHER_TOKEN, "foreign-card")
        plugin = self.plugin()
        self.assertEqual(await plugin._send_card(CHANNEL, GUILD, {}), "fresh-card")
        self.delete.assert_not_awaited()
        self.assertIn("foreign-card", plugin._card_ledger.records)
        self.assertIn("fresh-card", plugin._card_ledger.records)

    async def test_token_change_during_send_preserves_original_token_receipt(self):
        plugin = self.plugin()

        async def change_binding(token, *_args):
            self.assertEqual(token, TOKEN)
            plugin._kook_token = OTHER_TOKEN
            return "old-token-late-card"

        self.sender.side_effect = change_binding
        self.assertIsNone(await plugin._send_card(CHANNEL, GUILD, {}))
        record = plugin._card_ledger.records["old-token-late-card"]
        self.assertEqual(record["token_hash"], CardLedger.token_hash(TOKEN))
        self.assertTrue(record["pending"])
        self.delete.assert_not_awaited()

    async def test_token_change_during_delete_stops_retry_with_new_identity(self):
        plugin = self.plugin()
        self.track(plugin)

        async def change_binding(token, _msg_id):
            self.assertEqual(token, TOKEN)
            plugin._kook_token = OTHER_TOKEN
            return False

        self.delete.side_effect = change_binding
        self.assertFalse(await plugin._delete_message_with_retry("old-card"))
        self.delete.assert_awaited_once_with(TOKEN, "old-card")
        self.assertIn("old-card", plugin._card_ledger.records)

    async def test_token_change_while_reading_history_prevents_tracking(self):
        plugin = self.plugin()
        plugin._remember_card_scope(GUILD, CHANNEL)

        async def change_binding(*_args, **_kwargs):
            plugin._kook_token = OTHER_TOKEN
            return {"items": [row("old-token-history")]}

        self.history.side_effect = change_binding
        self.assertIsNone(await plugin._reconcile_music_cards(GUILD, CHANNEL))
        self.assertFalse(plugin._card_ledger.records)
        self.assertIn(GUILD, plugin._card_reconcile_needed)
        self.delete.assert_not_awaited()

    async def test_history_pagination_uses_oldest_timestamp_not_response_order(self):
        plugin = self.plugin()
        rows = [{"id": f"row-{i}", "type": 9, "create_at": i} for i in range(50)]
        rows = rows[20:] + rows[:20]
        self.history.side_effect = [{"items": rows}, {"items": [row("old-card", -1)]}]
        self.assertEqual(await plugin._reconcile_music_cards(GUILD, CHANNEL), 1)
        self.assertEqual(self.history.await_args_list[0].kwargs["before"], "")
        self.assertEqual(self.history.await_args_list[1].kwargs["before"], "row-0")
        self.assertEqual(self.history.await_count, 2)

    async def test_history_scanning_has_hard_page_limit(self):
        plugin = self.plugin()
        counter = [0]

        async def page(*_args, **_kwargs):
            counter[0] += 1
            return {
                "items": [
                    {
                        "id": f"page-{counter[0]}-row-{i}",
                        "type": 9,
                        "create_at": 10000 - counter[0] * 100 + i,
                    }
                    for i in range(50)
                ]
            }

        self.history.side_effect = page
        self.assertEqual(
            await plugin._reconcile_music_cards(GUILD, CHANNEL, pages=999), 0
        )
        self.assertEqual(self.history.await_count, 10)

    async def test_repeated_history_cursor_stops_without_infinite_scan(self):
        plugin = self.plugin()
        self.history.return_value = {
            "items": [{"id": f"row-{i}", "type": 9, "create_at": i} for i in range(50)]
        }
        self.assertEqual(
            await plugin._reconcile_music_cards(GUILD, CHANNEL, pages=10), 0
        )
        self.assertEqual(self.history.await_count, 2)

    async def test_history_failure_keeps_reconcile_request_and_existing_receipts(self):
        plugin = self.plugin()
        self.track(plugin)
        plugin._request_card_reconcile(GUILD)
        self.history.return_value = None
        self.assertIsNone(await plugin._reconcile_music_cards(GUILD, CHANNEL))
        self.assertIn(GUILD, plugin._card_reconcile_needed)
        self.assertIn("old-card", plugin._card_ledger.records)
        self.delete.assert_not_awaited()

    async def test_malformed_history_message_id_does_not_abort_valid_recovery(self):
        plugin = self.plugin()
        missing_id = row("missing")
        del missing_id["id"]
        self.history.return_value = {
            "items": [missing_id, row("bad\nidentifier"), row("valid-card")]
        }
        self.assertEqual(await plugin._reconcile_music_cards(GUILD, CHANNEL), 1)
        self.assertEqual(set(plugin._card_ledger.records), {"valid-card"})

    async def test_skip_success_retires_only_pre_control_snapshot(self):
        plugin = self.plugin(playing=True)
        self.track(plugin, "old-card", pending=False)

        async def change_song(*_args, **_kwargs):
            await asyncio.sleep(0)
            self.track(plugin, "late-new-card", pending=False)
            return True, "skipped"

        plugin.voice_manager.control.side_effect = change_song
        self.assertTrue((await plugin._control_playback(GUILD, "owner", "next"))[0])
        self.assertTrue(plugin._card_ledger.records["old-card"]["pending"])
        self.assertFalse(plugin._card_ledger.records["late-new-card"]["pending"])
        await plugin._cleanup_pending_cards()
        self.delete.assert_awaited_once_with(TOKEN, "old-card")

    async def test_failed_skip_does_not_retire_current_card(self):
        plugin = self.plugin(playing=True)
        self.track(plugin, pending=False)
        plugin.voice_manager.control.return_value = (False, "denied")
        self.assertFalse((await plugin._control_playback(GUILD, "owner", "next"))[0])
        self.assertFalse(plugin._card_ledger.records["old-card"]["pending"])
        self.delete.assert_not_awaited()

    async def test_deleted_message_gateway_confirmation_forgets_known_receipt(self):
        plugin = self.plugin()
        self.track(plugin)
        event = {
            "type": 255,
            "extra": {
                "type": "deleted_message",
                "body": {"msg_id": "old-card", "channel_id": CHANNEL},
            },
        }
        await plugin._handle_kook_system_event(NS(bot_id=BOT), event, TOKEN)
        self.assertNotIn("old-card", plugin._card_ledger.records)
        self.assertNotIn("old-card", plugin._card_msg_ids.get(GUILD, []))
        self.delete.assert_not_awaited()

    async def test_foreign_token_deleted_event_does_not_forget_receipt(self):
        plugin = self.plugin()
        self.track(plugin)
        event = {
            "s": 0,
            "d": {
                "type": 255,
                "extra": {"type": "deleted_message", "body": {"msg_id": "old-card"}},
            },
        }
        await plugin._handle_kook_system_event(NS(bot_id=BOT), event, OTHER_TOKEN)
        self.assertIn("old-card", plugin._card_ledger.records)
        self.assertIn("old-card", plugin._card_msg_ids[GUILD])

    async def test_gateway_delete_receipt_confirms_http_failure_without_retry(self):
        plugin = self.plugin()
        self.track(plugin)

        async def confirmed_but_http_failed(_token, message_id):
            event = {
                "type": 255,
                "extra": {"type": "deleted_message", "body": {"msg_id": message_id}},
            }
            await plugin._handle_kook_system_event(NS(bot_id=BOT), event, TOKEN)
            return False

        self.delete.side_effect = confirmed_but_http_failed
        self.assertEqual(await plugin._delete_card_messages(["old-card"]), [])
        self.assertFalse(plugin._card_ledger.records)
        self.delete.assert_awaited_once_with(TOKEN, "old-card")

    async def test_background_deletion_batch_is_bounded_to_ten(self):
        plugin = self.plugin()
        for index in range(11):
            self.track(plugin, f"pending-{index}")
        self.assertEqual(await plugin._cleanup_pending_cards(), 10)
        self.assertEqual(len(plugin._card_ledger.records), 1)
        self.assertEqual(self.delete.await_count, 10)
        self.assertEqual(await plugin._cleanup_pending_cards(), 1)

    async def test_corrupt_ledger_is_preserved_and_memory_fallback_does_not_overwrite(
        self,
    ):
        path = self.root / "default" / "playing_cards.json"
        path.parent.mkdir()
        corrupt = b'{"synthetic-broken-ledger":'
        path.write_bytes(corrupt)
        plugin = self.plugin()
        self.assertIsNone(plugin._card_ledger.path)
        plugin._remember_card_scope(GUILD, CHANNEL)
        self.track(plugin, "memory-only")
        await plugin._cleanup_pending_cards()
        await plugin._close_card_cleanup()
        self.assertEqual(path.read_bytes(), corrupt)

    async def test_shutdown_marks_unfinished_active_cards_pending_for_restart(self):
        plugin = self.plugin()
        self.track(plugin, pending=False)
        await plugin._close_card_cleanup()
        self.assertTrue(plugin._card_ledger.records["old-card"]["pending"])
        self.assertTrue(plugin._card_cleanup_task.done())
        self.delete.assert_not_awaited()

    async def test_periodic_history_retry_skips_foreign_scope(self):
        plugin = self.plugin()
        plugin._card_ledger.remember_scope("foreign-guild", CHANNEL, OTHER_TOKEN)
        plugin._card_ledger.remember_scope(GUILD, CHANNEL, TOKEN)
        plugin._card_reconcile_needed.clear()
        plugin._reconcile_music_cards = AsyncMock(return_value=0)

        async def finish():
            plugin._card_cleanup_closing = True
            plugin._card_cleanup_wake.set()

        plugin._cleanup_pending_cards = AsyncMock(side_effect=finish)
        with patch.object(card_cleanup, "time", NS(monotonic=lambda: 1000)):
            await card_cleanup.CardCleanupMixin._card_cleanup_loop(plugin)
        plugin._reconcile_music_cards.assert_awaited_once_with(GUILD, CHANNEL)

    async def test_foreign_initial_scope_cannot_block_recovery_queue_forever(self):
        plugin = self.plugin()
        plugin._card_ledger.remember_scope("foreign-guild", CHANNEL, OTHER_TOKEN)
        plugin._card_reconcile_needed = {"foreign-guild"}

        async def finish():
            plugin._card_cleanup_closing = True
            plugin._card_cleanup_wake.set()

        plugin._cleanup_pending_cards = AsyncMock(side_effect=finish)
        await card_cleanup.CardCleanupMixin._card_cleanup_loop(plugin)
        self.assertFalse(plugin._card_reconcile_needed)
        self.history.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
