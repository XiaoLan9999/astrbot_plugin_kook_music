"""Isolated first-party NetEase QR authentication and account playback.

Protocol reference: NeteaseCloudMusicApiEnhanced/api-enhanced (login_qr_*,
login_status, song_url_v1, util/crypto), and mos9527/pyncm login APIs.
Only protocol constants are shared; no SDK global session or public API proxy.
"""

import asyncio
import base64
import io
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import timezone
from email.utils import parsedate_to_datetime
from http.cookies import CookieError, SimpleCookie
from urllib.parse import urlencode, urlsplit, urlunsplit

import aiohttp
from yarl import URL

from .types import AudioResult, AuthError, LoginChallenge, LoginPoll

_ORIGIN = "https://music.163.com"
_PATHS = frozenset(
    {
        "/weapi/login/qrcode/unikey",
        "/weapi/login/qrcode/client/login",
        "/weapi/w/nuser/account/get",
        "/weapi/login/token/refresh",
        "/weapi/song/enhance/player/url/v1",
    }
)
_COOKIE_NAMES = frozenset(
    {
        "MUSIC_U",
        "MUSIC_A",
        "MUSIC_R_T",
        "__csrf",
        "NMTID",
        "_ntes_nuid",
        "_ntes_nnid",
        "WEVNSM",
    }
)
_PUBLIC_KEY = b"""-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDgtQn2JZ34ZC28NWYpAUd98iZ37BUrX/aKzmFbt7clFSs6sXqHauqKWqdtLkF2KexO40H1YTX8z2lSgBBOAxLsvaklV8k4cBFK9snQXE9/DDaFt6Rr7iVZMldczhC0JNgTz+SHXT6CBHuX3e9SdB1Ua44oncaTWz7OBGLbCiK45wIDAQAB
-----END PUBLIC KEY-----"""
_MAX_RESPONSE = 1024 * 1024
_SAFE_COOKIE = re.compile(r"^[\x21-\x7e]{1,16384}$")
_LOGGER = logging.getLogger(__name__)
_LOGIN_STAGES = frozenset(
    {
        "NETEASE_QR",
        "NETEASE_POLL",
        "NETEASE_COOKIE",
        "NETEASE_REQUEST",
        "NETEASE_VERIFY",
        "NETEASE_CHECK",
    }
)


def _diagnostic(stage, *, http=None, code=None, music_u=None):
    parts = [stage if stage in _LOGIN_STAGES else "NETEASE_REQUEST"]
    if type(http) is int and 100 <= http <= 599:
        parts.append(f"HTTP{http}")
    if type(code) is int and -1000000 <= code <= 1000000:
        parts.append(f"CODE{code}")
    if type(music_u) is bool:
        parts.append(f"MUSIC_U{int(music_u)}")
    return ":".join(parts)


def _cookies(value):
    if not isinstance(value, dict):
        return {}
    return {
        key: token
        for key, token in value.items()
        if key in _COOKIE_NAMES
        and isinstance(token, str)
        and _SAFE_COOKIE.fullmatch(token)
        and not any(char in token for char in ';,"\\')
    }


def _response_cookies(response, path):
    """Select account cookies without collapsing distinct Set-Cookie scopes."""
    source = URL(_ORIGIN + path)
    target = URL(_ORIGIN + "/weapi/w/nuser/account/get")
    jar = aiohttp.CookieJar()
    headers = getattr(response, "headers", None)
    if hasattr(headers, "getall"):
        values = headers.getall("Set-Cookie", [])
    elif headers is not None:
        value = headers.get("Set-Cookie")
        values = [value] if value else []
    else:
        # Compatibility with injected response doubles; real HTTP uses raw headers.
        values = [item.OutputString() for item in response.cookies.values()]
    for value in values:
        cookie = SimpleCookie()
        try:
            cookie.load(value)
        except CookieError:
            continue
        for name, item in list(cookie.items()):
            domain = item["domain"].lstrip(".").lower()
            if name not in _COOKIE_NAMES or domain not in {
                "",
                "music.163.com",
                "163.com",
            }:
                del cookie[name]
                continue
            if not item["expires"]:
                continue
            try:
                int(item["max-age"])
            except ValueError:
                try:
                    expires = parsedate_to_datetime(item["expires"])
                    if expires.tzinfo is None:
                        expires = expires.replace(tzinfo=timezone.utc)
                    if expires.timestamp() <= time.time():
                        # aiohttp treats epoch zero as an absent Expires timestamp.
                        item["max-age"] = "0"
                except (TypeError, ValueError, OverflowError):
                    pass
        jar.update_cookies(cookie, response_url=source)
    return _cookies(
        {name: item.value for name, item in jar.filter_cookies(target).items()}
    )


def _weapi(data, secret=None):
    from cryptography.hazmat.primitives import padding, serialization
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    def encrypt(value, key):
        padder = padding.PKCS7(128).padder()
        plain = padder.update(value) + padder.finalize()
        cipher = Cipher(algorithms.AES(key), modes.CBC(b"0102030405060708")).encryptor()
        return base64.b64encode(cipher.update(plain) + cipher.finalize())

    secret = secret or secrets.token_hex(8).encode("ascii")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    first = encrypt(payload, b"0CoJUm6Qyw8W8jud")
    public = serialization.load_pem_public_key(_PUBLIC_KEY).public_numbers()
    encrypted_key = pow(int.from_bytes(secret[::-1], "big"), public.e, public.n)
    return {
        "params": encrypt(first, secret).decode(),
        "encSecKey": f"{encrypted_key:0256x}",
    }


def _audio_url(value):
    if (
        not isinstance(value, str)
        or any(ord(char) < 33 for char in value)
        or "\\" in value
    ):
        return ""
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme not in {"https", "http"}
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 80, 443}
            or parsed.fragment
            or not host.endswith(".music.126.net")
            or not parsed.path
        ):
            return ""
        return urlunsplit(("https", host, parsed.path, parsed.query, ""))
    except (ValueError, UnicodeError):
        return ""


@dataclass(repr=False)
class _ChallengeState:
    key: str
    cookies: dict = field(default_factory=dict)
    deadline: float = 0
    cancelled: bool = False
    qr_type: int = 3


class NeteaseBackend:
    """Injected request transport: async (path, payload, cookies) -> (body, cookies)."""

    def __init__(self, *, request=None, timeout=15, clock=time.monotonic, qr_type=3):
        if type(qr_type) is not int or qr_type not in (1, 3):
            raise ValueError("qr_type must be 1 or 3")
        self._request_override = request
        self._qr_type = qr_type
        self._timeout = max(1, min(float(timeout), 60))
        self._clock = clock
        self._session = None
        self._challenges = {}
        self._closed = False

    async def _request(self, path, payload, cookies=None):
        if self._closed:
            raise AuthError("unavailable", "NetEase backend is closed")
        if path not in _PATHS:
            raise AuthError("invalid", "Unsupported NetEase endpoint")
        safe_cookies = _cookies(cookies)
        status = None
        code = None
        music_u = None
        try:
            if self._request_override is not None:
                body, received = await self._request_override(
                    path, dict(payload), safe_cookies
                )
                if not isinstance(body, dict):
                    raise AuthError("transient", "NetEase returned an invalid response")
                return body, _cookies(received)
            if self._session is None:
                self._session = aiohttp.ClientSession(
                    cookie_jar=aiohttp.DummyCookieJar(),
                    trust_env=False,
                    timeout=aiohttp.ClientTimeout(total=self._timeout),
                    headers={
                        "Referer": _ORIGIN + "/",
                        "Origin": _ORIGIN,
                        "User-Agent": "Mozilla/5.0",
                    },
                )
            data = _weapi({**payload, "csrf_token": safe_cookies.get("__csrf", "")})
            async with self._session.post(
                _ORIGIN + path,
                data=data,
                cookies=safe_cookies,
                allow_redirects=False,
            ) as response:
                status = response.status
                if response.status != 200:
                    raise AuthError(
                        "transient", "NetEase service is temporarily unavailable"
                    )
                raw = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw) > _MAX_RESPONSE:
                        raise AuthError(
                            "transient", "NetEase returned an oversized response"
                        )
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise AuthError("transient", "NetEase returned an invalid response")
                code = body.get("code")
                received = _response_cookies(response, path)
                music_u = bool(received.get("MUSIC_U"))
                return body, received
        except asyncio.CancelledError:
            raise
        except AuthError as error:
            diagnostic = _diagnostic(
                "NETEASE_REQUEST", http=status, code=code, music_u=music_u
            )
            _LOGGER.warning("NetEase request failed: %s", diagnostic)
            raise AuthError(
                error.kind,
                "NetEase request failed; credentials were not discarded",
                diagnostic=diagnostic,
            ) from None
        except ImportError:
            raise AuthError(
                "dependency",
                "Install cryptography and qrcode[pil] for account login",
                diagnostic="NETEASE_REQUEST",
            ) from None
        except Exception:
            diagnostic = _diagnostic(
                "NETEASE_REQUEST", http=status, code=code, music_u=music_u
            )
            _LOGGER.warning("NetEase request failed: %s", diagnostic)
            raise AuthError(
                "transient",
                "NetEase request failed; credentials were not discarded",
                diagnostic=diagnostic,
            ) from None

    def _live_state(self, challenge):
        if not isinstance(challenge, LoginChallenge):
            return None
        state = self._challenges.get(challenge.challenge_id)
        if (
            state is None
            or state.cancelled
            or self._closed
            or self._clock() >= state.deadline
        ):
            self._challenges.pop(challenge.challenge_id, None)
            return None
        return state

    async def begin_login(self, method="netease"):
        if method not in {"netease", "qr", "netease_web"}:
            raise AuthError(
                "invalid", "Use the NetEase Cloud Music app to scan this QR code"
            )
        for key, state in list(self._challenges.items()):
            if state.cancelled or self._clock() >= state.deadline:
                self._challenges.pop(key, None)
        if len(self._challenges) >= 8:
            raise AuthError("busy", "Too many pending NetEase logins")
        qr_type = 1 if method == "netease_web" else self._qr_type
        body, cookies = await self._request(
            "/weapi/login/qrcode/unikey",
            {"type": qr_type, "noCheckToken": True},
        )
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        key = data.get("unikey", "")
        if (
            body.get("code") != 200
            or not isinstance(key, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{8,256}", key)
        ):
            diagnostic = _diagnostic("NETEASE_QR", http=200, code=body.get("code"))
            _LOGGER.warning("NetEase login failed: %s", diagnostic)
            raise AuthError(
                "transient",
                "NetEase could not create a login QR code",
                diagnostic=diagnostic,
            )
        try:
            import qrcode

            image = qrcode.make(_ORIGIN + "/login?" + urlencode({"codekey": key}))
            output = io.BytesIO()
            image.save(output, format="PNG")
        except ImportError:
            raise AuthError(
                "dependency", "Install qrcode[pil] for account login"
            ) from None
        if self._closed:
            raise AuthError("unavailable", "NetEase backend is closed")
        challenge = LoginChallenge(secrets.token_urlsafe(24), output.getvalue(), 180)
        self._challenges[challenge.challenge_id] = _ChallengeState(
            key,
            cookies,
            self._clock() + challenge.expires_in,
            qr_type=qr_type,
        )
        return challenge

    async def poll_login(self, challenge):
        state = self._live_state(challenge)
        if state is None:
            return LoginPoll("expired")
        body, received = await self._request(
            "/weapi/login/qrcode/client/login",
            {"key": state.key, "type": state.qr_type, "noCheckToken": True},
            state.cookies,
        )
        if self._live_state(challenge) is not state:
            return LoginPoll("expired")
        state.cookies.update(received)
        code = body.get("code")
        _LOGGER.debug(
            "NetEase QR status: %s",
            _diagnostic(
                "NETEASE_POLL",
                http=200,
                code=code,
                music_u=bool(received.get("MUSIC_U")),
            ),
        )
        if code == 801:
            return LoginPoll("pending")
        if code == 802:
            return LoginPoll("scanned")
        if code in (800, 804):
            await self.cancel_login(challenge)
            return LoginPoll("expired" if code == 800 else "denied")
        if code == 8821:
            diagnostic = _diagnostic(
                "NETEASE_VERIFY",
                http=200,
                code=code,
                music_u=bool(received.get("MUSIC_U")),
            )
            await self.cancel_login(challenge)
            _LOGGER.warning("NetEase login failed: %s", diagnostic)
            raise AuthError(
                "verification_required",
                "Complete the account security verification in the official NetEase client",
                diagnostic=diagnostic,
            )
        if code != 803:
            diagnostic = _diagnostic(
                "NETEASE_POLL",
                http=200,
                code=code,
                music_u=bool(received.get("MUSIC_U")),
            )
            _LOGGER.warning("NetEase login failed: %s", diagnostic)
            raise AuthError(
                "transient",
                "NetEase QR status is temporarily unavailable",
                diagnostic=diagnostic,
            )
        if not state.cookies.get("MUSIC_U"):
            diagnostic = _diagnostic(
                "NETEASE_COOKIE", http=200, code=code, music_u=False
            )
            _LOGGER.warning("NetEase login failed: %s", diagnostic)
            raise AuthError(
                "transient",
                "NetEase did not return account credentials",
                diagnostic=diagnostic,
            )
        # Capture only cookies from first-party response headers, never body cookie text.
        credential = {"cookies": dict(state.cookies)}
        await self.cancel_login(challenge)
        return LoginPoll("authorized", credential)

    async def cancel_login(self, challenge):
        state = self._challenges.pop(getattr(challenge, "challenge_id", ""), None)
        if state is not None:
            state.cancelled = True
            state.cookies.clear()
            state.key = ""

    @staticmethod
    def _credential_cookies(credential):
        return (
            _cookies(credential.get("cookies")) if isinstance(credential, dict) else {}
        )

    async def check_credentials(self, credential):
        cookies = self._credential_cookies(credential)
        if not cookies.get("MUSIC_U"):
            return "expired"
        try:
            body, _ = await self._request("/weapi/w/nuser/account/get", {}, cookies)
        except AuthError:
            return "unknown"
        account = body.get("account")
        valid_account = isinstance(account, dict) and bool(
            re.fullmatch(r"[1-9][0-9]*", str(account.get("id", "")))
        )
        if body.get("code") != 200 or not valid_account:
            _LOGGER.warning(
                "NetEase credential check failed: %s ACCOUNT_VALID%d PROFILE_PRESENT%d",
                _diagnostic("NETEASE_CHECK", http=200, code=body.get("code")),
                int(valid_account),
                int(isinstance(body.get("profile"), dict)),
            )
        if body.get("code") == 301:
            return "expired"
        if body.get("code") != 200:
            return "unknown"
        if valid_account:
            return "valid"
        if "account" in body and account is None and body.get("profile") is None:
            return "expired"
        return "unknown"

    async def refresh_credentials(self, credential):
        cookies = self._credential_cookies(credential)
        if not cookies.get("MUSIC_U"):
            return None
        try:
            body, received = await self._request(
                "/weapi/login/token/refresh", {}, cookies
            )
        except AuthError:
            return None
        if body.get("code") != 200:
            return None
        cookies.update(received)
        return {"cookies": cookies}

    async def resolve_audio(self, song, credential):
        if getattr(song, "platform", "") not in {"netease", "163", "wy", ""}:
            return AudioResult("unavailable", reason="Song is not from NetEase")
        song_id = str(getattr(song, "id", ""))
        if not re.fullmatch(r"[1-9][0-9]{0,19}", song_id):
            return AudioResult("unavailable", reason="Invalid NetEase song ID")
        cookies = self._credential_cookies(credential)
        if not cookies.get("MUSIC_U"):
            return AudioResult("expired", reason="NetEase account login is required")
        try:
            body, _ = await self._request(
                "/weapi/song/enhance/player/url/v1",
                {
                    "ids": json.dumps([int(song_id)]),
                    "level": "exhigh",
                    "encodeType": "mp3",
                },
                cookies,
            )
        except AuthError:
            return AudioResult(
                "transient", reason="NetEase account service is temporarily unavailable"
            )
        if body.get("code") == 301:
            return AudioResult("expired", reason="NetEase account login expired")
        if body.get("code") != 200:
            return AudioResult(
                "transient", reason="NetEase account service is temporarily unavailable"
            )
        rows = body.get("data")
        if not isinstance(rows, list):
            return AudioResult(
                "transient", reason="NetEase returned an invalid audio response"
            )
        row = next(
            (
                item
                for item in rows
                if isinstance(item, dict) and str(item.get("id")) == song_id
            ),
            None,
        )
        if row is None:
            return AudioResult(
                "unavailable", reason="NetEase returned a different song ID"
            )
        if row.get("freeTrialInfo") not in (None, False, "", "null"):
            return AudioResult(
                "unavailable",
                reason="Account only permits a trial excerpt of this song",
            )
        if row.get("code") not in (None, 200) or not row.get("url"):
            return AudioResult(
                "unavailable", reason="Account cannot play this song in full"
            )
        url = _audio_url(row["url"])
        if not url:
            return AudioResult(
                "unavailable", reason="NetEase returned an untrusted audio address"
            )
        return AudioResult("resolved", url)

    async def close(self):
        self._closed = True
        for state in self._challenges.values():
            state.cancelled = True
            state.cookies.clear()
            state.key = ""
        self._challenges.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
