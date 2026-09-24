import copy
import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_kook_music import card_ledger  # noqa: E402
from astrbot_plugin_kook_music.card_builder import (  # noqa: E402
    WATERMARK_TEXT,
    build_bilibili_playing_card,
    build_now_playing_card,
    build_queue_card,
    build_queued_card,
    build_search_result_card,
)
from astrbot_plugin_kook_music.card_ledger import (  # noqa: E402
    CardLedger,
    CardLedgerError,
    is_playing_card_message,
)
from astrbot_plugin_kook_music.music.model import Song  # noqa: E402


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "private-state" / "cards.json"

    def tearDown(self):
        self.temp.cleanup()

    def seed(self):
        ledger = CardLedger(self.path)
        ledger.track("guild", "channel", "synthetic-token", "message")
        return ledger

    def test_missing_and_memory_only_ledgers_start_empty(self):
        for path in (None, self.path):
            ledger = CardLedger(path)
            self.assertEqual(ledger.scopes, {})
            self.assertEqual(ledger.records, {})
        self.assertFalse(self.path.exists())
        memory = CardLedger(None)
        memory.track("guild", "channel", "synthetic-token", "message")
        self.assertIn("message", memory.records)
        self.assertFalse(self.path.exists())

    def test_track_persists_scope_hash_and_identifier_only(self):
        ledger = self.seed()
        data = json.loads(self.path.read_text())
        self.assertEqual(set(data), {"version", "scopes", "records"})
        self.assertEqual(
            set(data["records"]["message"]),
            {"guild_id", "channel_id", "token_hash", "pending"},
        )
        self.assertEqual(data["scopes"], ledger.scopes)
        self.assertFalse(ledger.records["message"]["pending"])
        self.assertNotIn("synthetic-token", self.path.read_text())
        self.assertEqual(
            ledger.token_hash("synthetic-token"),
            hashlib.sha256(b"synthetic-token").hexdigest(),
        )

    def test_reloaded_records_all_become_pending_and_are_written_back(self):
        ledger = self.seed()
        ledger.track("guild", "channel", "synthetic-token", "pending", pending=True)
        loaded = CardLedger(self.path)
        self.assertTrue(all(item["pending"] for item in loaded.records.values()))
        self.assertTrue(
            all(
                item["pending"]
                for item in json.loads(self.path.read_text())["records"].values()
            )
        )
        self.assertEqual(loaded.scopes, ledger.scopes)

    def test_mark_pending_and_forget_are_scoped_and_persistent(self):
        ledger = self.seed()
        ledger.track("guild", "channel", "synthetic-token", "keep")
        ledger.mark_pending(["message", "unknown"])
        self.assertTrue(ledger.records["message"]["pending"])
        self.assertFalse(ledger.records["keep"]["pending"])
        ledger.forget(["message", "unknown"])
        self.assertEqual(set(ledger.records), {"keep"})
        self.assertEqual(set(json.loads(self.path.read_text())["records"]), {"keep"})
        self.assertIn("guild", ledger.scopes)

    def test_scope_changes_do_not_reassign_old_records(self):
        ledger = self.seed()
        original = dict(ledger.records["message"])
        ledger.remember_scope("guild", "replacement-channel", "replacement-token")
        ledger.track("guild", "replacement-channel", "replacement-token", "new-message")
        self.assertEqual(ledger.records["message"], original)
        self.assertEqual(ledger.scopes["guild"]["channel_id"], "replacement-channel")

    def test_existing_message_cannot_be_reassigned_to_different_scope(self):
        ledger = self.seed()
        before = self.path.read_bytes()
        for arguments in (
            ("other-guild", "channel", "synthetic-token"),
            ("guild", "other-channel", "synthetic-token"),
            ("guild", "channel", "other-token"),
        ):
            with self.assertRaises(CardLedgerError):
                ledger.track(*arguments, "message")
        self.assertEqual(self.path.read_bytes(), before)

    def test_bad_arguments_do_not_mutate_file_or_memory(self):
        ledger = self.seed()
        before = self.path.read_bytes()
        for arguments in (
            ("", "channel", "synthetic-token", "message"),
            ("guild", "../channel", "synthetic-token", "message"),
            ("guild", "channel", "", "message"),
            ("guild", "channel", "synthetic-token", "x" * 129),
        ):
            with self.assertRaises(CardLedgerError):
                ledger.track(*arguments)
        for pending in (1, "true", None):
            with self.assertRaises(CardLedgerError):
                ledger.track(
                    "guild", "channel", "synthetic-token", "new", pending=pending
                )
        for identifiers in (
            None,
            "message",
            {"message": True},
            ["invalid/message"],
            [False],
        ):
            with self.assertRaises(CardLedgerError):
                ledger.forget(identifiers)
            with self.assertRaises(CardLedgerError):
                ledger.mark_pending(identifiers)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(set(ledger.records), {"message"})

    def test_corrupt_and_duplicate_key_json_preserve_original_bytes(self):
        self.path.parent.mkdir()
        for raw in (
            b"{broken",
            b"\xff",
            b'{"version":1,"version":1,"scopes":{},"records":{}}',
            b'{"version":NaN,"scopes":{},"records":{}}',
        ):
            self.path.write_bytes(raw)
            with self.assertRaises(CardLedgerError) as raised:
                CardLedger(self.path)
            self.assertEqual(self.path.read_bytes(), raw)
            self.assertNotIn("{broken", str(raised.exception))

    def test_schema_extra_secret_fields_invalid_hash_and_types_fail_closed(self):
        self.seed()
        original = json.loads(self.path.read_text())
        variants = []
        for value in (True, 2, "1"):
            data = copy.deepcopy(original)
            data["version"] = value
            variants.append(data)
        data = copy.deepcopy(original)
        data["records"]["message"]["cookie"] = "do-not-store"
        variants.append(data)
        data = copy.deepcopy(original)
        data["records"]["message"]["token_hash"] = "synthetic-raw-token"
        variants.append(data)
        data = copy.deepcopy(original)
        data["records"]["message"]["pending"] = "false"
        variants.append(data)
        data = copy.deepcopy(original)
        data["records"]["message"]["guild_id"] = "unknown"
        variants.append(data)
        for data in variants:
            raw = json.dumps(data).encode()
            self.path.write_bytes(raw)
            with self.assertRaises(CardLedgerError):
                CardLedger(self.path)
            self.assertEqual(self.path.read_bytes(), raw)

    def test_file_record_scope_and_input_id_count_limits(self):
        ledger = self.seed()
        before = self.path.read_bytes()
        with patch.object(card_ledger, "_MAX_SCOPES", 1):
            with self.assertRaises(CardLedgerError):
                ledger.remember_scope("another", "channel", "synthetic-token")
        with patch.object(card_ledger, "_MAX_RECORDS", 1):
            with self.assertRaises(CardLedgerError):
                ledger.track("guild", "channel", "synthetic-token", "second")
            with self.assertRaises(CardLedgerError):
                ledger.forget(["message", "second"])
        with patch.object(card_ledger, "_MAX_BYTES", 8):
            with self.assertRaises(CardLedgerError):
                ledger.mark_pending(["message"])
            with self.assertRaises(CardLedgerError):
                CardLedger(self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_failed_replace_keeps_original_and_cleans_temporary_file(self):
        ledger = self.seed()
        before = self.path.read_bytes()
        with patch.object(
            card_ledger.os, "replace", side_effect=PermissionError("synthetic-secret")
        ):
            with self.assertRaises(CardLedgerError) as raised:
                ledger.mark_pending(["message"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(ledger.records["message"]["pending"])
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        self.assertNotIn("synthetic-secret", str(raised.exception))

    def test_load_pending_conversion_write_failure_keeps_original(self):
        self.seed()
        before = self.path.read_bytes()
        with patch.object(card_ledger.os, "replace", side_effect=OSError):
            with self.assertRaises(CardLedgerError):
                CardLedger(self.path)
        self.assertEqual(self.path.read_bytes(), before)

    @unittest.skipIf(os.name == "nt", "POSIX mode bits are not available on Windows")
    def test_each_write_creates_private_0600_file(self):
        ledger = self.seed()
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        ledger.mark_pending(["message"])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        ledger.forget(["message"])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_atomic_replace_never_exposes_partial_json(self):
        ledger = self.seed()
        before = self.path.read_bytes()
        original_replace = os.replace
        observed = []

        def replacing(source, destination):
            observed.append(json.loads(Path(source).read_text()))
            self.assertEqual(self.path.read_bytes(), before)
            original_replace(source, destination)

        with patch.object(card_ledger.os, "replace", side_effect=replacing):
            ledger.mark_pending(["message"])
        self.assertTrue(observed[0]["records"]["message"]["pending"])
        self.assertTrue(
            json.loads(self.path.read_text())["records"]["message"]["pending"]
        )


class CardMatcherTests(unittest.TestCase):
    def setUp(self):
        self.song = Song(
            id="synthetic",
            name="synthetic song",
            artists="synthetic artist",
            platform="qq",
        )

    def row(self, card=None, **changes):
        row = {
            "type": 10,
            "author": {"id": "bot-123"},
            "content": json.dumps([card or build_now_playing_card(self.song)]),
        }
        row.update(changes)
        return row

    def test_actual_music_and_bilibili_playing_cards_match(self):
        for card in (
            build_now_playing_card(self.song),
            build_bilibili_playing_card(self.song),
        ):
            self.assertTrue(is_playing_card_message(self.row(card), "bot-123"))

    def test_other_authors_wrong_message_types_and_missing_identity_rejected(self):
        for changes in (
            {"type": 9},
            {"type": "10"},
            {"type": True},
            {"author": {"id": "someone-else"}},
            {"author": {}},
            {"author": "bot-123"},
        ):
            self.assertFalse(is_playing_card_message(self.row(**changes), "bot-123"))
        self.assertFalse(is_playing_card_message(self.row(), ""))
        self.assertFalse(is_playing_card_message(None, "bot-123"))

    def test_queued_search_and_queue_cards_are_not_playback_cards(self):
        cards = (
            build_queued_card(self.song),
            build_search_result_card([self.song], "query"),
            build_queue_card([self.song]),
        )
        for card in cards:
            self.assertFalse(is_playing_card_message(self.row(card), "bot-123"))

    def test_header_buttons_and_watermark_must_all_be_present(self):
        card = build_now_playing_card(self.song)
        for kind in ("header", "action-group"):
            copy_card = copy.deepcopy(card)
            copy_card["modules"] = [
                item for item in copy_card["modules"] if item["type"] != kind
            ]
            self.assertFalse(is_playing_card_message(self.row(copy_card), "bot-123"))
        copy_card = copy.deepcopy(card)
        copy_card["modules"] = [
            item
            for item in copy_card["modules"]
            if not any(
                element.get("content") == WATERMARK_TEXT
                for element in item.get("elements", [])
            )
        ]
        self.assertFalse(is_playing_card_message(self.row(copy_card), "bot-123"))

    def test_each_required_button_must_be_return_value_button(self):
        for name in ("kook_music_next", "kook_music_loop", "kook_music_clear"):
            card = build_now_playing_card(self.song)
            buttons = next(
                item["elements"]
                for item in card["modules"]
                if item["type"] == "action-group"
            )
            next(item for item in buttons if item["value"] == name)["click"] = "link"
            self.assertFalse(is_playing_card_message(self.row(card), "bot-123"))

    def test_plain_messages_or_song_titles_cannot_supply_the_signature(self):
        card = build_queued_card(
            Song(
                id="synthetic-title",
                name="正在播放：Powered By XiaoLan9999 kook_music_next",
            )
        )
        self.assertFalse(is_playing_card_message(self.row(card), "bot-123"))
        card = build_now_playing_card(self.song)
        card["modules"][0]["text"]["content"] = "其他插件正在播放：song"
        self.assertFalse(is_playing_card_message(self.row(card), "bot-123"))

    def test_invalid_content_structures_and_unhashable_fields_never_raise(self):
        for content in (
            None,
            {},
            [],
            "broken",
            "{}",
            "[]",
            "[null]",
            '[{"type":"card","modules":[{"type":[]}]}]',
            "[" * 1000 + "]" * 1000,
        ):
            self.assertFalse(
                is_playing_card_message(self.row(content=content), "bot-123")
            )
        card = build_now_playing_card(self.song)
        context = next(item for item in card["modules"] if item["type"] == "context")
        context["elements"] = [{"type": [], "content": {}}]
        self.assertIsInstance(is_playing_card_message(self.row(card), "bot-123"), bool)

    def test_mixed_multiple_cards_rejected_to_avoid_deleting_unrelated_content(self):
        content = json.dumps(
            [build_now_playing_card(self.song), build_queued_card(self.song)]
        )
        self.assertFalse(is_playing_card_message(self.row(content=content), "bot-123"))

    def test_size_depth_and_module_count_bounds(self):
        card = build_now_playing_card(self.song)
        card["modules"][0]["text"]["content"] += "x" * (64 * 1024)
        self.assertFalse(is_playing_card_message(self.row(card), "bot-123"))
        card = build_now_playing_card(self.song)
        card["modules"] += [{"type": "divider"}] * 51
        self.assertFalse(is_playing_card_message(self.row(card), "bot-123"))
        card = build_now_playing_card(self.song)
        extra = card
        for _ in range(20):
            extra["nested"] = {}
            extra = extra["nested"]
        self.assertFalse(is_playing_card_message(self.row(card), "bot-123"))


class ConvertedAudioCardTests(unittest.TestCase):
    def test_server_file_module_conversion_keeps_owned_playing_signature(self):
        song = Song(
            id="synthetic", name="Synthetic", audio_url="https://audio.example/test.mp3"
        )
        card = build_now_playing_card(song)
        audio = next(module for module in card["modules"] if module["type"] == "audio")
        audio["type"] = "file"
        message = {
            "id": "synthetic-message",
            "type": 10,
            "author": {"id": "bot"},
            "content": json.dumps([card]),
        }
        self.assertTrue(is_playing_card_message(message, "bot"))
        self.assertFalse(is_playing_card_message(message, "other-bot"))


if __name__ == "__main__":
    unittest.main()
