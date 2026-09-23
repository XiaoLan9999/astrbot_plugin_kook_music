"""Dedicated private-only auth notifications; never falls back to channel messages."""

import asyncio
from urllib.parse import urlsplit

import aiohttp

from ..kook_api import KOOK_API_BASE, _get_session


class PrivateDeliveryError(Exception):
    pass


async def _post(session, token, endpoint, **kwargs):
    try:
        async with session.post(
            f"{KOOK_API_BASE}/{endpoint}",
            headers={"Authorization": f"Bot {token}"},
            timeout=aiohttp.ClientTimeout(total=10),
            allow_redirects=False,
            **kwargs,
        ) as response:
            if response.status != 200:
                raise PrivateDeliveryError("KOOK private delivery failed")
            payload = await response.json()
            if not isinstance(payload, dict) or payload.get("code") != 0:
                raise PrivateDeliveryError("KOOK private delivery failed")
            return payload.get("data") or {}
    except asyncio.CancelledError:
        raise
    except Exception:
        raise PrivateDeliveryError("KOOK private delivery failed") from None


async def send_private_auth(
    token: str, user_id: str, text: str, qr_bytes: bytes | None = None
):
    if not token or not user_id.isdigit():
        raise PrivateDeliveryError("Invalid private delivery target")
    session = await _get_session()
    uploaded = ""
    if qr_bytes is not None:
        if not isinstance(qr_bytes, bytes) or len(qr_bytes) > 2 * 1024 * 1024:
            raise PrivateDeliveryError("Invalid login image")
        if not qr_bytes.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")):
            raise PrivateDeliveryError("Invalid login image")
        form = aiohttp.FormData()
        jpeg = qr_bytes.startswith(b"\xff\xd8\xff")
        form.add_field(
            "file",
            qr_bytes,
            filename="music-login.jpg" if jpeg else "music-login.png",
            content_type="image/jpeg" if jpeg else "image/png",
        )
        result = await _post(session, token, "asset/create", data=form)
        uploaded = str(result.get("url", ""))
        parsed = urlsplit(uploaded)
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or not (host.endswith(".kookapp.cn") or host.endswith(".kaiheila.cn"))
        ):
            raise PrivateDeliveryError("Invalid KOOK image response")
    await _post(
        session,
        token,
        "direct-message/create",
        json={
            "type": 1,
            "target_id": user_id,
            "content": str(text)[:3000],
        },
    )
    if not uploaded:
        return None
    result = await _post(
        session,
        token,
        "direct-message/create",
        json={
            "type": 2,
            "target_id": user_id,
            "content": uploaded,
        },
    )
    message_id = str(result.get("msg_id", ""))
    if not message_id:
        raise PrivateDeliveryError("Missing private image receipt")

    async def cleanup():
        # Closing a QR session attempts to remove only the image sent by this call.
        if not session.closed:
            await _post(
                session, token, "direct-message/delete", json={"msg_id": message_id}
            )

    return cleanup
