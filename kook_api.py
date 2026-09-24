"""
KOOK REST API 辅助工具。
用于发送/删除/更新卡片消息等操作。
"""

import asyncio
import copy
import email.utils
import hashlib
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass

import aiohttp

from .kook_events import enum_value, event_field

logger = logging.getLogger("astrbot")

KOOK_API_BASE = "https://www.kookapp.cn/api/v3"
_COUNTDOWN_START_OFFSET_MS = 500
_MIN_COUNTDOWN_DURATION_MS = 1000
_CARD_HTTP_TIMEOUT = 10
_CARD_RECEIPT_GRACE = 2
_MAX_PENDING_CARD_RECEIPTS = 128
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")

# 模块级共享 session（避免每次 API 调用都创建新连接）
_shared_session: aiohttp.ClientSession | None = None
_server_time_offset_ms = 0


@dataclass
class _CardReceipt:
    token_hash: bytes
    channel_id: str
    future: asyncio.Future
    request_task: asyncio.Task | None = None


@dataclass
class _CardAttempt:
    msg_id: str | None = None
    countdown_rejected: bool = False
    uncertain: bool = False


_pending_card_receipts: dict[str, _CardReceipt] = {}


def _safe_id(value) -> bool:
    return isinstance(value, str) and _SAFE_ID.fullmatch(value) is not None


def _token_hash(token):
    if (
        not isinstance(token, str)
        or not token
        or any(ord(char) < 32 or ord(char) == 127 for char in token)
    ):
        return None
    try:
        return hashlib.sha256(token.encode("utf-8")).digest()
    except UnicodeError:
        return None


def _log_api_failure(stage, *, http=None, data=None, error=None):
    code = data.get("code") if isinstance(data, dict) else None
    logger.warning(
        "[KookMusic] KOOK API stage=%s http=%s code=%s error=%s",
        stage,
        http if type(http) is int and 100 <= http <= 599 else "unknown",
        code if type(code) is int and -10000000 <= code <= 10000000 else "unknown",
        type(error).__name__ if error is not None else "none",
    )


def observe_card_receipt(token: str, bot_user_id: str, event) -> bool:
    """Correlate a self-authored gateway card with an outstanding HTTP attempt."""
    if event_field(event, "type") is None:
        signal = event_field(event, "s", event_field(event, "signal"))
        if enum_value(signal) == 0:
            event = event_field(event, "d", event_field(event, "data"))
    if (
        enum_value(event_field(event, "type")) != 10
        or enum_value(event_field(event, "channel_type")) != "GROUP"
        or not _safe_id(bot_user_id)
        or event_field(event, "author_id") != bot_user_id
    ):
        return False
    nonce = event_field(event, "nonce")
    if not isinstance(nonce, str):
        return False
    receipt = _pending_card_receipts.get(nonce)
    if (
        receipt is None
        or receipt.future.done()
        or receipt.token_hash != _token_hash(token)
        or event_field(event, "target_id") != receipt.channel_id
    ):
        return False
    msg_id = event_field(event, "msg_id")
    if not _safe_id(msg_id):
        return False
    receipt.future.set_result(msg_id)
    return True


def _register_card_receipt(token, channel_id):
    token_hash = _token_hash(token)
    if token_hash is None or not _safe_id(channel_id):
        _log_api_failure("CARD_INPUT")
        return None
    for nonce, receipt in tuple(_pending_card_receipts.items()):
        if receipt.future.done() or receipt.future.get_loop().is_closed():
            _pending_card_receipts.pop(nonce, None)
    if len(_pending_card_receipts) >= _MAX_PENDING_CARD_RECEIPTS:
        _log_api_failure("CARD_RECEIPT_CAPACITY")
        return None
    nonce = secrets.token_hex(16)
    while nonce in _pending_card_receipts:
        nonce = secrets.token_hex(16)
    receipt = _CardReceipt(
        token_hash, channel_id, asyncio.get_running_loop().create_future()
    )
    _pending_card_receipts[nonce] = receipt
    return nonce, receipt


async def _get_session() -> aiohttp.ClientSession:
    """获取共享 aiohttp session（懒初始化）

    如果 session 绑定的事件循环与当前不同（如插件热重载后），会自动重建。
    """
    global _shared_session
    if _shared_session is not None and not _shared_session.closed:
        # 检查事件循环匹配（防止热重载后 RuntimeError）
        try:
            session_loop = _shared_session._loop  # type: ignore[attr-defined]
            if session_loop is not asyncio.get_running_loop():
                await _shared_session.close()
                _shared_session = None
        except Exception:
            pass
    if _shared_session is None or _shared_session.closed:
        _shared_session = aiohttp.ClientSession()
    return _shared_session


async def close_shared_session():
    """关闭共享 session（插件卸载时调用）"""
    global _shared_session
    tasks = []
    for receipt in tuple(_pending_card_receipts.values()):
        if not receipt.future.done():
            receipt.future.cancel()
        if receipt.request_task is not None:
            receipt.request_task.cancel()
            tasks.append(receipt.request_task)
    _pending_card_receipts.clear()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    if _shared_session and not _shared_session.closed:
        await _shared_session.close()
    _shared_session = None


def _is_countdown_validation_error(data: dict) -> bool:
    """判断 KOOK 是否因为 countdown 模块拒绝了卡片。"""
    if (
        not isinstance(data, dict)
        or type(data.get("code")) is not int
        or data["code"] == 0
    ):
        return False
    details = data.get("data", [])
    if isinstance(details, str):
        details = [details]
    return isinstance(details, list) and any(
        isinstance(item, str) and "countdown" in item.lower() for item in details
    )


def _sync_server_time_from_response(resp: aiohttp.ClientResponse):
    """用 KOOK 响应头 Date 粗略同步服务器时间，减少 countdown 校验误差。"""
    global _server_time_offset_ms
    date_header = resp.headers.get("Date")
    if not date_header:
        return
    try:
        server_dt = email.utils.parsedate_to_datetime(date_header)
        server_ms = int(server_dt.timestamp() * 1000)
        local_ms = int(time.time() * 1000)
        _server_time_offset_ms = server_ms - local_ms
    except Exception:
        pass


def _server_now_ms() -> int:
    return int(time.time() * 1000) + _server_time_offset_ms


def _normalize_countdown_modules(card_data: list[dict]) -> list[dict]:
    """发送前修正 countdown 时间，确保 startTime/endTime 是未来且 endTime 更大。"""
    normalized = copy.deepcopy(card_data)
    now_ms = _server_now_ms()
    start_floor_ms = now_ms + _COUNTDOWN_START_OFFSET_MS

    for card in normalized:
        if not isinstance(card, dict):
            continue
        modules = card.get("modules")
        if not isinstance(modules, list):
            continue
        for module in modules:
            if not isinstance(module, dict) or module.get("type") != "countdown":
                continue

            end_ms = _to_int(module.get("endTime"))
            start_ms = _to_int(module.get("startTime"))
            duration_ms = 0
            if start_ms is not None and end_ms is not None and end_ms > start_ms:
                duration_ms = end_ms - start_ms
            elif end_ms is not None and end_ms > now_ms:
                duration_ms = end_ms - now_ms
            if duration_ms <= 0:
                duration_ms = _MIN_COUNTDOWN_DURATION_MS

            if start_ms is None or start_ms < start_floor_ms:
                start_ms = start_floor_ms
            end_ms = start_ms + duration_ms

            module["mode"] = module.get("mode") or "second"
            module["startTime"] = start_ms
            module["endTime"] = end_ms

    return normalized


def _replace_countdown_with_text(card_data: list[dict]) -> list[dict]:
    """将 countdown 替换成静态剩余时间文本，作为最后兜底。"""
    sanitized = copy.deepcopy(card_data)
    now_ms = _server_now_ms()
    for card in sanitized:
        if not isinstance(card, dict):
            continue
        modules = card.get("modules")
        if not isinstance(modules, list):
            continue
        new_modules = []
        for module in modules:
            if isinstance(module, dict) and module.get("type") == "countdown":
                end_ms = _to_int(module.get("endTime"))
                start_ms = _to_int(module.get("startTime"))
                if end_ms is not None and end_ms > now_ms:
                    remaining_ms = end_ms - now_ms
                elif end_ms is not None and start_ms is not None and end_ms > start_ms:
                    remaining_ms = end_ms - start_ms
                else:
                    remaining_ms = 0
                remaining_seconds = max(1, remaining_ms // 1000)
                new_modules.append(
                    {
                        "type": "section",
                        "text": {
                            "type": "kmarkdown",
                            "content": f"**歌曲剩余：** {remaining_seconds} 秒",
                        },
                    }
                )
            else:
                new_modules.append(module)
        card["modules"] = new_modules
    return sanitized


def _to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _repair_countdown_modules(card_data: list[dict]) -> list[dict]:
    """修复 countdown 时间戳，使用最新 KOOK 服务器时间重算。"""
    repaired = copy.deepcopy(card_data)
    now_ms = _server_now_ms()
    repaired_start_ms = now_ms + _COUNTDOWN_START_OFFSET_MS

    for card in repaired:
        if not isinstance(card, dict):
            continue
        modules = card.get("modules")
        if not isinstance(modules, list):
            continue
        for module in modules:
            if not isinstance(module, dict) or module.get("type") != "countdown":
                continue

            end_ms = _to_int(module.get("endTime"))
            start_ms = _to_int(module.get("startTime"))
            duration_ms = 0
            if end_ms is not None:
                if start_ms is not None and end_ms > start_ms:
                    duration_ms = end_ms - start_ms
                elif end_ms > now_ms:
                    duration_ms = end_ms - now_ms
            if duration_ms <= 0:
                duration_ms = _MIN_COUNTDOWN_DURATION_MS

            module["mode"] = module.get("mode") or "second"
            module["startTime"] = repaired_start_ms
            module["endTime"] = repaired_start_ms + duration_ms

    return repaired


async def send_text_message(
    token: str,
    channel_id: str,
    content: str,
) -> str | None:
    """
    发送文本消息（KMARKDOWN）并返回 msg_id。

    Args:
        token: Bot Token
        channel_id: 目标频道 ID
        content: 消息内容

    Returns:
        发送成功返回 msg_id，失败返回 None
    """
    headers = {"Authorization": f"Bot {token}"}
    payload = {
        "target_id": channel_id,
        "content": content,
        "type": 9,  # KMARKDOWN
    }

    try:
        session = await _get_session()
        async with session.post(
            f"{KOOK_API_BASE}/message/create",
            headers=headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=10),
            allow_redirects=False,
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("code") == 0:
                    return data.get("data", {}).get("msg_id", "")
                else:
                    _log_api_failure("TEXT_REJECTED", http=resp.status, data=data)
            else:
                _log_api_failure("TEXT_HTTP", http=resp.status)
    except Exception as exc:
        _log_api_failure("TEXT_SEND", error=exc)
    return None


async def send_card_message(
    token: str,
    channel_id: str,
    card_data: dict | list[dict],
) -> str | None:
    """Return the first authenticated HTTP/gateway receipt; never retry ambiguity."""
    try:
        if isinstance(card_data, dict):
            card_data = [card_data]
        if (
            not isinstance(card_data, list)
            or not card_data
            or not all(isinstance(card, dict) for card in card_data)
        ):
            _log_api_failure("CARD_INPUT")
            return None
        normalized = _normalize_countdown_modules(card_data)
        session = await _get_session()
        candidate = normalized
        for attempt in range(3):
            result = await _send_card_attempt(session, token, channel_id, candidate)
            if result.msg_id:
                return result.msg_id
            if not result.countdown_rejected:
                return None
            if attempt == 0:
                candidate = _repair_countdown_modules(normalized)
            elif attempt == 1:
                candidate = _replace_countdown_with_text(normalized)
        return None
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log_api_failure("CARD_SEND", error=exc)
        return None


async def _post_card_attempt(session, token, payload):
    try:
        async with asyncio.timeout(_CARD_HTTP_TIMEOUT):
            async with session.post(
                f"{KOOK_API_BASE}/message/create",
                headers={"Authorization": f"Bot {token}"},
                json=payload,
                timeout=aiohttp.ClientTimeout(total=_CARD_HTTP_TIMEOUT),
                allow_redirects=False,
            ) as resp:
                _sync_server_time_from_response(resp)
                if resp.status != 200:
                    _log_api_failure("CARD_HTTP", http=resp.status)
                    return _CardAttempt(uncertain=True)
                data = await resp.json()
                if not isinstance(data, dict) or type(data.get("code")) is not int:
                    _log_api_failure("CARD_RESPONSE", http=resp.status)
                    return _CardAttempt(uncertain=True)
                if data["code"] == 0:
                    result = data.get("data")
                    msg_id = result.get("msg_id") if isinstance(result, dict) else None
                    if _safe_id(msg_id):
                        return _CardAttempt(msg_id=msg_id)
                    _log_api_failure(
                        "CARD_RECEIPT_MISSING", http=resp.status, data=data
                    )
                    return _CardAttempt(uncertain=True)
                _log_api_failure("CARD_REJECTED", http=resp.status, data=data)
                return _CardAttempt(
                    countdown_rejected=_is_countdown_validation_error(data)
                )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log_api_failure("CARD_HTTP", error=exc)
        return _CardAttempt(uncertain=True)


async def _send_card_attempt(session, token, channel_id, card_data):
    registered = _register_card_receipt(token, channel_id)
    if registered is None:
        return _CardAttempt()
    nonce, receipt = registered
    request_task = None
    try:
        # nonce is correlation only: KOOK does not promise idempotent delivery.
        payload = {
            "target_id": channel_id,
            "content": json.dumps(card_data),
            "type": 10,
            "nonce": nonce,
        }
        request_task = asyncio.create_task(_post_card_attempt(session, token, payload))
        receipt.request_task = request_task
        await asyncio.wait(
            {request_task, receipt.future}, return_when=asyncio.FIRST_COMPLETED
        )
        if receipt.future.cancelled():
            raise asyncio.CancelledError
        if receipt.future.done():
            return _CardAttempt(msg_id=receipt.future.result())
        result = request_task.result()
        if result.uncertain:
            try:
                async with asyncio.timeout(_CARD_RECEIPT_GRACE):
                    msg_id = await asyncio.shield(receipt.future)
                return _CardAttempt(msg_id=msg_id)
            except TimeoutError:
                _log_api_failure("CARD_UNCONFIRMED")
        return result
    finally:
        if _pending_card_receipts.get(nonce) is receipt:
            _pending_card_receipts.pop(nonce, None)
        if not receipt.future.done():
            receipt.future.cancel()
        if request_task is not None:
            if not request_task.done():
                request_task.cancel()
            await asyncio.gather(request_task, return_exceptions=True)


async def delete_message(token: str, msg_id: str) -> bool:
    """
    删除消息。

    Args:
        token: Bot Token
        msg_id: 要删除的消息 ID

    Returns:
        删除成功返回 True
    """
    if not msg_id:
        return False

    headers = {"Authorization": f"Bot {token}"}
    payload = {"msg_id": msg_id}

    try:
        session = await _get_session()
        async with session.post(
            f"{KOOK_API_BASE}/message/delete",
            headers=headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=10),
            allow_redirects=False,
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                if (
                    isinstance(data, dict)
                    and type(data.get("code")) is int
                    and data["code"] == 0
                ):
                    return True
                else:
                    _log_api_failure("DELETE_REJECTED", http=resp.status, data=data)
            else:
                _log_api_failure("DELETE_HTTP", http=resp.status)
    except Exception as exc:
        _log_api_failure("DELETE_SEND", error=exc)
    return False


async def update_card_message(
    token: str,
    msg_id: str,
    card_data: dict | list[dict],
) -> bool:
    """
    更新卡片消息内容。

    Args:
        token: Bot Token
        msg_id: 要更新的消息 ID
        card_data: 新的卡片数据

    Returns:
        更新成功返回 True
    """
    if not msg_id:
        return False

    if isinstance(card_data, dict):
        card_data = [card_data]

    headers = {"Authorization": f"Bot {token}"}
    payload = {
        "msg_id": msg_id,
        "content": json.dumps(card_data),
    }

    try:
        session = await _get_session()
        async with session.post(
            f"{KOOK_API_BASE}/message/update",
            headers=headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=10),
            allow_redirects=False,
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("code") == 0:
                    return True
                else:
                    _log_api_failure("UPDATE_REJECTED", http=resp.status, data=data)
            else:
                _log_api_failure("UPDATE_HTTP", http=resp.status)
    except Exception as exc:
        _log_api_failure("UPDATE_SEND", error=exc)
    return False


async def _get_api_data(token, endpoint, params, stage):
    if _token_hash(token) is None:
        _log_api_failure(stage + "_INPUT")
        return None
    try:
        session = await _get_session()
        async with session.get(
            f"{KOOK_API_BASE}/{endpoint}",
            headers={"Authorization": f"Bot {token}"},
            params=params,
            timeout=aiohttp.ClientTimeout(total=10),
            allow_redirects=False,
        ) as resp:
            if resp.status != 200:
                _log_api_failure(stage + "_HTTP", http=resp.status)
                return None
            data = await resp.json()
            if (
                isinstance(data, dict)
                and type(data.get("code")) is int
                and data["code"] == 0
                and isinstance(data.get("data"), dict)
            ):
                return data["data"]
            _log_api_failure(stage + "_RESPONSE", http=resp.status, data=data)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log_api_failure(stage + "_REQUEST", error=exc)
    return None


async def get_channel_messages(
    token: str, channel_id: str, before: str = "", page_size: int = 50
) -> dict | None:
    """Fetch one bounded page; callers must enforce their own cleanup scope."""
    if (
        not _safe_id(channel_id)
        or before != ""
        and not _safe_id(before)
        or type(page_size) is not int
        or not 1 <= page_size <= 50
    ):
        _log_api_failure("HISTORY_INPUT")
        return None
    params = {"target_id": channel_id, "page_size": page_size}
    if before:
        params.update({"msg_id": before, "flag": "before"})
    return await _get_api_data(token, "message/list", params, "HISTORY")


async def get_bot_identity(token: str) -> dict | None:
    """Return the current token's user/me data without logging account details."""
    return await _get_api_data(token, "user/me", {}, "IDENTITY")
