"""Live KOOK guild administrator checks for playback controls."""

import logging

import aiohttp

from .kook_api import KOOK_API_BASE, _get_session

logger = logging.getLogger("astrbot")


def _identifier(value) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    if type(value) is int and value >= 0:
        return str(value)
    return None


def _permission_bits(value) -> int | None:
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        return int(value)
    return None


def _role_identifier(value) -> str | None:
    role_id = _permission_bits(value)
    return str(role_id) if role_id is not None else None


async def _fetch_data(session, token: str, endpoint: str, params: dict) -> dict | None:
    async with session.get(
        f"{KOOK_API_BASE}/{endpoint}",
        headers={"Authorization": f"Bot {token}"},
        params=params,
        timeout=aiohttp.ClientTimeout(total=5),
        allow_redirects=False,
    ) as response:
        if response.status != 200:
            logger.debug("[KookMusic] Permission check HTTP failure: %s", response.status)
            return None
        result = await response.json()
        if not isinstance(result, dict) or type(result.get("code")) is not int or result["code"] != 0:
            logger.debug("[KookMusic] Permission check rejected API response")
            return None
        data = result.get("data")
        return data if isinstance(data, dict) else None


async def is_guild_admin(token: str, guild_id: str, user_id: str) -> bool:
    """Fail closed unless current API data proves ownership or Administrator."""
    if not all(isinstance(value, str) and value.strip() for value in (token, guild_id, user_id)):
        return False
    guild_id, user_id = guild_id.strip(), user_id.strip()
    try:
        session = await _get_session()
        guild = await _fetch_data(session, token, "guild/view", {"guild_id": guild_id})
        if guild is None or _identifier(guild.get("id")) != guild_id:
            return False
        owner_id = _identifier(guild.get("user_id"))
        if owner_id is None:
            return False
        if owner_id == user_id:
            return True

        roles = guild.get("roles")
        if not isinstance(roles, list):
            return False
        admin_roles = set()
        seen_roles = set()
        for role in roles:
            if not isinstance(role, dict):
                return False
            role_id = _role_identifier(role.get("role_id"))
            permissions = _permission_bits(role.get("permissions"))
            if role_id is None or permissions is None or role_id in seen_roles:
                return False
            seen_roles.add(role_id)
            if permissions & 1:
                admin_roles.add(role_id)

        user = await _fetch_data(
            session, token, "user/view", {"guild_id": guild_id, "user_id": user_id}
        )
        if user is None or _identifier(user.get("id")) != user_id:
            return False
        member_roles = user.get("roles")
        if not isinstance(member_roles, list):
            return False
        role_ids = [_role_identifier(role_id) for role_id in member_roles]
        if any(role_id is None for role_id in role_ids):
            return False
        # All verified guild members inherit the explicit everyone role (0).
        return bool(admin_roles.intersection({"0", *role_ids}))
    except Exception as exc:
        # Network exception text may contain request data; log its type only.
        logger.debug("[KookMusic] Permission check failed: %s", type(exc).__name__)
        return False
