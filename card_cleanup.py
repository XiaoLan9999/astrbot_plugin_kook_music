"""Retry owned playing-card cleanup without inspecting unrelated conversations."""

import asyncio
import re
import time

from astrbot.api import logger

from .card_ledger import CardLedger, CardLedgerError, is_playing_card_message
from .kook_api import get_bot_identity, get_channel_messages


class CardCleanupMixin:
    def _init_card_cleanup(self):
        try:
            self._card_ledger = CardLedger(self.data_dir / "playing_cards.json")
        except (OSError, ValueError, CardLedgerError):
            logger.error("[KookMusic] Card ledger unavailable; original file preserved")
            self._card_ledger = CardLedger(None)
        self._card_reconcile_needed = set(self._card_ledger.scopes)
        self._card_last_history_check = {}
        self._card_cleanup_wake = asyncio.Event()
        self._card_cleanup_closing = False
        for msg_id, record in self._card_ledger.records.items():
            self._card_msg_ids.setdefault(record["guild_id"], []).append(msg_id)
        self._card_cleanup_task = asyncio.create_task(self._card_cleanup_loop())

    def _ledger_write(self, method, *args, **kwargs):
        ledger = getattr(self, "_card_ledger", None)
        if ledger is not None:
            try:
                return getattr(ledger, method)(*args, **kwargs)
            except (OSError, ValueError, CardLedgerError):
                logger.error(
                    "[KookMusic] Card ledger update failed; cleanup will retry"
                )
        return None

    def _remember_card_scope(self, guild_id, channel_id, token=None):
        token = self._kook_token if token is None else token
        if not token:
            return
        ledger = getattr(self, "_card_ledger", None)
        fresh = ledger is not None and guild_id not in ledger.scopes
        self._ledger_write("remember_scope", guild_id, channel_id, token)
        if fresh:
            self._request_card_reconcile(guild_id)

    def _request_card_reconcile(self, guild_id):
        if getattr(self, "_card_ledger", None) is not None:
            self._card_reconcile_needed.add(guild_id)
            self._card_cleanup_wake.set()

    def _retire_card_ids(self, msg_ids):
        self._ledger_write("mark_pending", msg_ids)
        wake = getattr(self, "_card_cleanup_wake", None)
        if wake is not None:
            wake.set()

    async def _reconcile_music_cards(self, guild_id, channel_id, *, pages=3):
        token = self._kook_token
        if not token or getattr(self, "_card_ledger", None) is None:
            return None
        digest = CardLedger.token_hash(token)
        identity = getattr(self, "_card_bot_identity", None)
        if not identity or identity[0] != digest:
            me = await get_bot_identity(token)
            if not isinstance(me, dict) or not me.get("id"):
                return None
            identity = self._card_bot_identity = (digest, str(me["id"]))
        lock = self._card_locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            owned = {}
            before = ""
            cursors = set()
            for _ in range(min(10, max(1, pages))):
                if self._kook_token != token:
                    return None
                data = await get_channel_messages(token, channel_id, before=before)
                if not isinstance(data, dict) or not isinstance(
                    data.get("items"), list
                ):
                    return None
                rows = [row for row in data["items"] if isinstance(row, dict)]
                if not rows:
                    break
                for row in rows:
                    msg_id = row.get("id")
                    if (
                        isinstance(msg_id, str)
                        and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", msg_id)
                        and is_playing_card_message(row, identity[1])
                    ):
                        owned[str(row["id"])] = row

                def created(row):
                    try:
                        return float(row.get("create_at", 0))
                    except (TypeError, ValueError):
                        return 0

                oldest = min(rows, key=created)
                before = str(oldest.get("id", ""))
                if len(rows) < 50 or not before or before in cursors:
                    break
                cursors.add(before)
            if self._kook_token != token:
                return None
            session = self.voice_manager.sessions.get(guild_id)
            playing = (
                session is not None
                and bool(session.playlist)
                and getattr(session, "text_channel_id", channel_id) == channel_id
            )
            protected = (
                {
                    key
                    for key, record in self._card_ledger.records.items()
                    if record["guild_id"] == guild_id
                    and record["channel_id"] == channel_id
                    and record["token_hash"] == digest
                    and not record["pending"]
                }
                if playing
                else set()
            )
            if playing and not protected and owned:
                protected.add(max(owned, key=lambda key: created(owned[key])))
            for msg_id in owned:
                self._ledger_write(
                    "track",
                    guild_id,
                    channel_id,
                    token,
                    msg_id,
                    pending=msg_id not in protected,
                )
                ids = self._card_msg_ids.setdefault(guild_id, [])
                if msg_id not in ids:
                    ids.append(msg_id)
            self._card_reconcile_needed.discard(guild_id)
            if not hasattr(self, "_card_last_history_check"):
                self._card_last_history_check = {}
            self._card_last_history_check[guild_id] = time.monotonic()
            return len(set(owned) - protected)

    async def _cleanup_pending_cards(self):
        ledger = getattr(self, "_card_ledger", None)
        if ledger is None or not self._kook_token:
            return 0
        digest = CardLedger.token_hash(self._kook_token)
        guilds = {
            r["guild_id"]
            for r in ledger.records.values()
            if r["pending"] and r["token_hash"] == digest
        }
        deleted = 0
        for guild_id in guilds:
            lock = self._card_locks.setdefault(guild_id, asyncio.Lock())
            async with lock:
                ids = [
                    key
                    for key, r in ledger.records.items()
                    if r["guild_id"] == guild_id
                    and r["pending"]
                    and r["token_hash"] == digest
                ][:10]
                failed = await self._delete_card_messages(ids)
                removed = set(ids) - set(failed)
                self._ledger_write("forget", removed)
                remaining = [
                    key
                    for key in self._card_msg_ids.get(guild_id, [])
                    if key not in removed
                ]
                if remaining:
                    self._card_msg_ids[guild_id] = remaining
                else:
                    self._card_msg_ids.pop(guild_id, None)
                deleted += len(removed)
        return deleted

    async def _card_cleanup_loop(self):
        while not self._card_cleanup_closing:
            self._card_cleanup_wake.clear()
            try:
                last_checks = getattr(self, "_card_last_history_check", {})
                if not self._card_reconcile_needed:
                    for guild_id, scope in self._card_ledger.scopes.items():
                        if (
                            self._kook_token
                            and scope["token_hash"]
                            == CardLedger.token_hash(self._kook_token)
                            and time.monotonic() - last_checks.get(guild_id, 0) >= 300
                        ):
                            self._card_reconcile_needed.add(guild_id)
                            break
                for guild_id in list(self._card_reconcile_needed)[:1]:
                    scope = self._card_ledger.scopes.get(guild_id)
                    if (
                        scope
                        and self._kook_token
                        and scope["token_hash"]
                        == CardLedger.token_hash(self._kook_token)
                    ):
                        await self._reconcile_music_cards(guild_id, scope["channel_id"])
                    elif self._kook_token:
                        self._card_reconcile_needed.discard(guild_id)
                await self._cleanup_pending_cards()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(
                    "[KookMusic] Card cleanup deferred (%s)", type(error).__name__
                )
            try:
                await asyncio.wait_for(self._card_cleanup_wake.wait(), 30)
            except TimeoutError:
                pass

    async def _close_card_cleanup(self):
        self._card_cleanup_closing = True
        task = getattr(self, "_card_cleanup_task", None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        ledger = getattr(self, "_card_ledger", None)
        if ledger is not None:
            self._ledger_write("mark_pending", list(ledger.records))
