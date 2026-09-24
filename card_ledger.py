"""Persist only owned playback-card identifiers for safe cleanup after reload."""

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from .card_builder import WATERMARK_TEXT

_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_MAX_BYTES = 1024 * 1024
_MAX_SCOPES = 256
_MAX_RECORDS = 4096
_MAX_CARD_BYTES = 64 * 1024
_PLAYING_HEADER = re.compile(r"^(?:🎵|🎬)?\s*正在播放[：:]")
_BUTTON_VALUES = {"kook_music_next", "kook_music_loop", "kook_music_clear"}
_MODULE_TYPES = {
    "header",
    "section",
    "container",
    "audio",
    "file",
    "countdown",
    "divider",
    "context",
    "action-group",
}


class CardLedgerError(RuntimeError):
    """Fixed diagnostics: never expose submitted tokens or corrupt file content."""


def _valid_id(value):
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("Invalid JSON constant")


def _decode(raw):
    return json.loads(
        raw,
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
    )


class CardLedger:
    def __init__(self, path: Path | None):
        self.path = Path(path) if path is not None else None
        self.scopes = {}
        self.records = {}
        if self.path is None:
            return
        try:
            if self.path.is_symlink():
                raise CardLedgerError("Playback card ledger must be a regular file")
            with self.path.open("rb") as stream:
                raw = stream.read(_MAX_BYTES + 1)
        except FileNotFoundError:
            return
        except CardLedgerError:
            raise
        except OSError:
            raise CardLedgerError("Unable to read playback card ledger") from None
        if len(raw) > _MAX_BYTES:
            raise CardLedgerError("Playback card ledger exceeds its size limit")
        try:
            data = _decode(raw.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError):
            raise CardLedgerError("Playback card ledger JSON is corrupt") from None
        scopes, records = self._validate(data)
        pending = {key: {**value, "pending": True} for key, value in records.items()}
        if pending != records:
            self._commit(scopes, pending)
        else:
            self.scopes, self.records = scopes, records

    @staticmethod
    def token_hash(token: str) -> str:
        if not isinstance(token, str):
            raise CardLedgerError("Playback card token must be text")
        try:
            return hashlib.sha256(token.encode("utf-8")).hexdigest()
        except UnicodeError:
            raise CardLedgerError("Playback card token is invalid") from None

    @staticmethod
    def _validate(data):
        if (
            not isinstance(data, dict)
            or set(data) != {"version", "scopes", "records"}
            or type(data["version"]) is not int
            or data["version"] != 1
            or not isinstance(data["scopes"], dict)
            or not isinstance(data["records"], dict)
            or len(data["scopes"]) > _MAX_SCOPES
            or len(data["records"]) > _MAX_RECORDS
        ):
            raise CardLedgerError("Playback card ledger schema is invalid")
        scopes, records = {}, {}
        for guild, value in data["scopes"].items():
            if (
                not _valid_id(guild)
                or not isinstance(value, dict)
                or set(value) != {"channel_id", "token_hash"}
                or not _valid_id(value["channel_id"])
                or not isinstance(value["token_hash"], str)
                or not _HASH.fullmatch(value["token_hash"])
            ):
                raise CardLedgerError("Playback card ledger scope is invalid")
            scopes[guild] = dict(value)
        for message_id, value in data["records"].items():
            if (
                not _valid_id(message_id)
                or not isinstance(value, dict)
                or set(value) != {"guild_id", "channel_id", "token_hash", "pending"}
                or not _valid_id(value["guild_id"])
                or value["guild_id"] not in scopes
                or not _valid_id(value["channel_id"])
                or not isinstance(value["token_hash"], str)
                or not _HASH.fullmatch(value["token_hash"])
                or type(value["pending"]) is not bool
            ):
                raise CardLedgerError("Playback card ledger record is invalid")
            records[message_id] = dict(value)
        return scopes, records

    def _commit(self, scopes, records):
        data = {"version": 1, "scopes": scopes, "records": records}
        scopes, records = self._validate(data)
        encoded = (
            json.dumps(data, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
            + "\n"
        ).encode("ascii")
        if len(encoded) > _MAX_BYTES:
            raise CardLedgerError("Playback card ledger exceeds its size limit")
        if self.path is not None:
            temporary = None
            try:
                if self.path.is_symlink():
                    raise CardLedgerError("Playback card ledger must be a regular file")
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                fd, temporary = tempfile.mkstemp(
                    prefix="." + self.path.name + ".",
                    suffix=".tmp",
                    dir=self.path.parent,
                )
                with os.fdopen(fd, "wb") as stream:
                    os.chmod(temporary, 0o600)
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
                temporary = None
            except CardLedgerError:
                raise
            except OSError:
                raise CardLedgerError("Unable to save playback card ledger") from None
            finally:
                if temporary is not None:
                    try:
                        os.unlink(temporary)
                    except OSError:
                        pass
        self.scopes, self.records = scopes, records

    @classmethod
    def _scope(cls, guild_id, channel_id, token):
        if (
            not _valid_id(guild_id)
            or not _valid_id(channel_id)
            or not isinstance(token, str)
            or not 1 <= len(token) <= 8192
            or not token.strip()
        ):
            raise CardLedgerError("Playback card scope arguments are invalid")
        return {"channel_id": channel_id, "token_hash": cls.token_hash(token)}

    @staticmethod
    def _ids(message_ids):
        if isinstance(message_ids, (str, bytes, dict)):
            raise CardLedgerError("Playback card message IDs must be a collection")
        try:
            iterator = iter(message_ids)
        except TypeError:
            raise CardLedgerError(
                "Playback card message IDs must be a collection"
            ) from None
        result = set()
        for index, value in enumerate(iterator):
            if index >= _MAX_RECORDS or not _valid_id(value):
                raise CardLedgerError("Playback card message IDs are invalid")
            result.add(value)
        return result

    def remember_scope(self, guild_id, channel_id, token):
        scope = self._scope(guild_id, channel_id, token)
        self._commit({**self.scopes, guild_id: scope}, self.records)

    def track(self, guild_id, channel_id, token, message_id, pending=False):
        scope = self._scope(guild_id, channel_id, token)
        if not _valid_id(message_id) or type(pending) is not bool:
            raise CardLedgerError("Playback card record arguments are invalid")
        value = {"guild_id": guild_id, **scope, "pending": pending}
        existing = self.records.get(message_id)
        if existing and any(
            existing[field] != value[field]
            for field in ("guild_id", "channel_id", "token_hash")
        ):
            raise CardLedgerError("Playback card message ID belongs to another scope")
        self._commit(
            {**self.scopes, guild_id: scope},
            {**self.records, message_id: value},
        )

    def mark_pending(self, message_ids):
        requested = self._ids(message_ids)
        self._commit(
            self.scopes,
            {
                key: {**value, "pending": True} if key in requested else value
                for key, value in self.records.items()
            },
        )

    def forget(self, message_ids):
        requested = self._ids(message_ids)
        self._commit(
            self.scopes,
            {key: value for key, value in self.records.items() if key not in requested},
        )


def _bounded_card_tree(value):
    pending = [(value, 0)]
    count = 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if count > 5000 or depth > 12:
            return False
        if isinstance(item, dict):
            if len(item) > 32 or any(
                not isinstance(key, str) or len(key) > 64 for key in item
            ):
                return False
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            if len(item) > 50:
                return False
            pending.extend((child, depth + 1) for child in item)
    return True


def is_playing_card_message(row, bot_id):
    """Match this bot's exact playback-card signature, never titles alone."""
    if (
        not isinstance(row, dict)
        or type(row.get("type")) is not int
        or row["type"] != 10
        or not _valid_id(bot_id)
        or not isinstance(row.get("author"), dict)
        or row["author"].get("id") != bot_id
        or not isinstance(row.get("content"), str)
    ):
        return False
    try:
        content = row["content"]
        if len(content.encode("utf-8")) > _MAX_CARD_BYTES:
            return False
        cards = _decode(content)
    except (ValueError, UnicodeError, RecursionError):
        return False
    if not isinstance(cards, list) or len(cards) != 1 or not _bounded_card_tree(cards):
        return False
    card = cards[0]
    if not isinstance(card, dict) or card.get("type") != "card":
        return False
    modules = card.get("modules")
    if not isinstance(modules, list) or not 1 <= len(modules) <= 50:
        return False
    if any(
        not isinstance(module, dict)
        or not isinstance(module.get("type"), str)
        or module["type"] not in _MODULE_TYPES
        for module in modules
    ):
        return False
    headers = [module.get("text") for module in modules if module["type"] == "header"]
    if (
        len(headers) != 1
        or not isinstance(headers[0], dict)
        or headers[0].get("type") != "plain-text"
        or not isinstance(headers[0].get("content"), str)
        or not _PLAYING_HEADER.match(headers[0]["content"])
    ):
        return False
    watermark = False
    buttons = set()
    for module in modules:
        if module["type"] not in {"context", "action-group"}:
            continue
        elements = module.get("elements")
        if not isinstance(elements, list) or len(elements) > 10:
            return False
        for element in elements:
            if not isinstance(element, dict):
                return False
            if module["type"] == "context":
                watermark |= (
                    isinstance(element.get("type"), str)
                    and element["type"] in {"plain-text", "kmarkdown"}
                    and isinstance(element.get("content"), str)
                    and element["content"] in {WATERMARK_TEXT, "Powered By XiaoLan9999"}
                )
            elif (
                element.get("type") == "button" and element.get("click") == "return-val"
            ):
                value = element.get("value")
                if isinstance(value, str):
                    buttons.add(value)
    return watermark and _BUTTON_VALUES.issubset(buttons)
