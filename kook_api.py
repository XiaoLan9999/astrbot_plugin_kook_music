"""
KOOK REST API 辅助工具。
用于发送/删除/更新卡片消息等操作。
"""
import asyncio
import copy
import email.utils
import json
import logging
import time

import aiohttp

logger = logging.getLogger("astrbot")

KOOK_API_BASE = "https://www.kookapp.cn/api/v3"
_COUNTDOWN_START_OFFSET_MS = 500
_MIN_COUNTDOWN_DURATION_MS = 1000

# 模块级共享 session（避免每次 API 调用都创建新连接）
_shared_session: aiohttp.ClientSession | None = None
_server_time_offset_ms = 0


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
    if _shared_session and not _shared_session.closed:
        await _shared_session.close()
    _shared_session = None


def _is_countdown_validation_error(data: dict) -> bool:
    """判断 KOOK 是否因为 countdown 模块拒绝了卡片。"""
    details = data.get("data", [])
    if isinstance(details, str):
        details = [details]
    return any("countdown" in str(item) for item in details)


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
                new_modules.append({
                    "type": "section",
                    "text": {
                        "type": "kmarkdown",
                        "content": f"**歌曲剩余：** {remaining_seconds} 秒",
                    },
                })
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
    start_ms = now_ms + _COUNTDOWN_START_OFFSET_MS

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
            module["startTime"] = start_ms
            module["endTime"] = start_ms + duration_ms

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
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("code") == 0:
                    return data.get("data", {}).get("msg_id", "")
                else:
                    logger.debug(f"[KookMusic] 发送文本消息失败: {data}")
            else:
                logger.debug(f"[KookMusic] 发送文本消息 HTTP 错误: {resp.status}")
    except Exception as e:
        logger.debug(f"[KookMusic] 发送文本消息异常: {e}")
    return None


async def send_card_message(
    token: str,
    channel_id: str,
    card_data: dict | list[dict],
) -> str | None:
    """
    发送卡片消息并返回 msg_id。

    Args:
        token: Bot Token
        channel_id: 目标频道 ID
        card_data: 卡片数据（单个 dict 或列表）

    Returns:
        发送成功返回 msg_id，失败返回 None
    """
    if isinstance(card_data, dict):
        card_data = [card_data]
    card_data = _normalize_countdown_modules(card_data)

    headers = {"Authorization": f"Bot {token}"}
    payload = {
        "target_id": channel_id,
        "content": json.dumps(card_data),
        "type": 10,  # CARD 类型
    }

    try:
        session = await _get_session()
        async with session.post(
            f"{KOOK_API_BASE}/message/create",
            headers=headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            _sync_server_time_from_response(resp)
            if resp.status == 200:
                data = await resp.json()
                if data.get("code") == 0:
                    msg_id = data.get("data", {}).get("msg_id", "")
                    return msg_id
                if _is_countdown_validation_error(data):
                    logger.warning(
                        f"[KookMusic] countdown 校验失败，修正倒计时时间戳后重发卡片: {data}"
                    )
                    fallback_payload = {
                        "target_id": channel_id,
                        "content": json.dumps(_repair_countdown_modules(card_data)),
                        "type": 10,
                    }
                    async with session.post(
                        f"{KOOK_API_BASE}/message/create",
                        headers=headers,
                        json=fallback_payload,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as retry_resp:
                        _sync_server_time_from_response(retry_resp)
                        if retry_resp.status == 200:
                            retry_data = await retry_resp.json()
                            if retry_data.get("code") == 0:
                                return retry_data.get("data", {}).get("msg_id", "")
                            if _is_countdown_validation_error(retry_data):
                                logger.warning(
                                    f"[KookMusic] 修正倒计时后仍校验失败，改用文本剩余时间后重发卡片: {retry_data}"
                                )
                                stripped_payload = {
                                    "target_id": channel_id,
                                    "content": json.dumps(_replace_countdown_with_text(card_data)),
                                    "type": 10,
                                }
                                async with session.post(
                                    f"{KOOK_API_BASE}/message/create",
                                    headers=headers,
                                    json=stripped_payload,
                                    timeout=aiohttp.ClientTimeout(total=10),
                                ) as stripped_resp:
                                    _sync_server_time_from_response(stripped_resp)
                                    if stripped_resp.status == 200:
                                        stripped_data = await stripped_resp.json()
                                        if stripped_data.get("code") == 0:
                                            return stripped_data.get("data", {}).get("msg_id", "")
                                        logger.error(f"[KookMusic] 文本剩余时间发送卡片失败: {stripped_data}")
                                    else:
                                        logger.error(
                                            f"[KookMusic] 文本剩余时间发送卡片 HTTP 错误: {stripped_resp.status}"
                                        )
                            else:
                                logger.error(f"[KookMusic] 修正倒计时后发送卡片失败: {retry_data}")
                        else:
                            logger.error(
                                f"[KookMusic] 修正倒计时后发送卡片 HTTP 错误: {retry_resp.status}"
                            )
                else:
                    logger.error(f"[KookMusic] 发送卡片失败: {data}")
            else:
                logger.error(f"[KookMusic] 发送卡片 HTTP 错误: {resp.status}")
    except Exception as e:
        logger.error(f"[KookMusic] 发送卡片异常: {e}")
    return None


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
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("code") == 0:
                    return True
                else:
                    logger.debug(f"[KookMusic] 删除消息失败: {data}")
            else:
                logger.debug(f"[KookMusic] 删除消息 HTTP 错误: {resp.status}")
    except Exception as e:
        logger.debug(f"[KookMusic] 删除消息异常: {e}")
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
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("code") == 0:
                    return True
                else:
                    logger.debug(f"[KookMusic] 更新消息失败: {data}")
            else:
                logger.debug(f"[KookMusic] 更新消息 HTTP 错误: {resp.status}")
    except Exception as e:
        logger.debug(f"[KookMusic] 更新消息异常: {e}")
    return False
