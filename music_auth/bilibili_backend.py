"""First-party Bilibili web QR login with an isolated, bounded transport.

Protocol references:
https://github.com/bilibili-plugins/bilibili-api-collect/blob/master/docs/login/login_action/QR.md
https://github.com/public-clis/bilibili-cli/pull/27
"""

import asyncio
import io
import json
import math
import re
import secrets
import time
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
from http.cookies import CookieError, SimpleCookie
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

import aiohttp

from .bilibili_cookie import COOKIE_NAMES as _COOKIE_NAMES
from .types import AudioResult, AuthError, LoginChallenge, LoginPoll

_GENERATE = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
_NAV = "https://api.bilibili.com/x/web-interface/nav"
_EXCHANGE = "https://passport.biligame.com/x/passport-login/web/crossDomain"
_ENDPOINTS = frozenset({_GENERATE, _POLL, _NAV, _EXCHANGE})
_QR_KEY = re.compile(r"[0-9a-fA-F]{32}\Z")
_SAFE_TOKEN = re.compile(r"[\x21-\x7e]{1,16384}\Z")
_SAFE_TICKET = re.compile(r"[A-Za-z0-9._~+/=-]{8,4096}\Z")
_MAX_RESPONSE = 1024 * 1024
_QR_PATH = "/h5-app/passport/login/scan"
_QR_PATHS = {
    "passport.bilibili.com": frozenset({_QR_PATH}),
    "account.bilibili.com": frozenset({"/h5/account-h5/auth/scan-web"}),
}
_QR_CALLBACK_ID = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]{0,63}\Z")
_QR_CALLBACK_HOSTS = frozenset(
    {"bilibili.com", "www.bilibili.com", "passport.bilibili.com", "account.bilibili.com"}
)
_QR_CALLBACK_PATH = re.compile(r"/[A-Za-z0-9._/-]*\Z")
_LEGACY_CROSS_PATHS = {
    "passport.biligame.com": frozenset({"/crossDomain"}),
    "passport.bilibili.com": frozenset({"/crossDomain"}),
}
_CROSS_PATHS = {
    **_LEGACY_CROSS_PATHS,
    "passport.biligame.com": frozenset(
        {"/crossDomain", "/x/passport-login/web/crossDomain"}
    ),
}


def _cookie_value(name, value):
    if not isinstance(value, str) or not _SAFE_TOKEN.fullmatch(value):
        return ""
    if name == "SESSDATA":
        value = value.replace(",", "%2C")
    if any(char in value for char in ';,"\\'):
        return ""
    if name == "DedeUserID" and not re.fullmatch(r"[1-9][0-9]{0,19}", value):
        return ""
    return value


def _cookies(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    for name, token in value.items():
        if name in _COOKIE_NAMES and (safe := _cookie_value(name, token)):
            result[name] = safe
    return result


def _merge_cookies(*sources):
    result = {}
    for source in sources:
        for name, value in _cookies(source).items():
            if name in result and result[name] != value:
                raise AuthError(
                    "transient",
                    "Bilibili returned conflicting account credentials",
                    diagnostic="BILIBILI_COOKIE",
                )
            result[name] = value
    return result


def _safe_url(value, paths):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 65536
        or "\\" in value
        or any(ord(char) < 33 or ord(char) > 126 for char in value)
    ):
        return None
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
            or parsed.fragment
            or parsed.path not in paths.get(host, ())
        ):
            return None
        return parsed
    except (ValueError, UnicodeError):
        return None


def _qr_url(value, key):
    parsed = _safe_url(value, _QR_PATHS)
    if parsed is None or len(value) > 4096:
        return ""
    try:
        pairs = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=20)
    except (ValueError, UnicodeError):
        return ""
    keys = [item for name, item in pairs if name == "qrcode_key"]
    if keys != [key]:
        return ""
    if any(
        not name or len(name) > 64 or len(item) > 2048
        or any(ord(char) < 32 or ord(char) > 126 for char in name + item)
        for name, item in pairs
    ):
        return ""
    callbacks = [item for name, item in pairs if name == "callback"]
    if len(callbacks) > 1 or (callbacks and not _qr_callback(callbacks[0])):
        return ""
    return value


def _qr_callback(value):
    if _QR_CALLBACK_ID.fullmatch(value):
        return True
    if len(value) > 1024:
        return False
    try:
        parsed = urlsplit(value)
        path = parsed.path or "/"
        return (
            parsed.scheme == "https"
            and (parsed.hostname or "").lower() in _QR_CALLBACK_HOSTS
            and parsed.username is None
            and parsed.password is None
            and parsed.port in {None, 443}
            and not parsed.query
            and not parsed.fragment
            and _QR_CALLBACK_PATH.fullmatch(path) is not None
            and "//" not in path
            and not any(part in {".", ".."} for part in path.split("/"))
        )
    except (ValueError, UnicodeError):
        return False


def _redirect_cookies(value, received=None):
    authoritative = _cookies(received)
    if value in (None, ""):
        return authoritative
    parsed = _safe_url(value, _LEGACY_CROSS_PATHS)
    if parsed is None:
        raise AuthError(
            "transient",
            "Bilibili returned an unexpected account redirect",
            diagnostic="BILIBILI_COOKIE",
        )
    result = dict(authoritative)
    fields = parsed.query.split("&")
    if len(fields) > 64:
        raise AuthError("transient", "Bilibili returned an invalid account response")
    for field in fields:
        raw_name, separator, raw_value = field.partition("=")
        try:
            name = unquote(raw_name, errors="strict")
            decoded = unquote(raw_value, errors="strict")
        except (ValueError, UnicodeError):
            raise AuthError(
                "transient", "Bilibili returned invalid account encoding"
            ) from None
        if name not in _COOKIE_NAMES:
            continue
        if any(ord(char) < 32 or ord(char) == 127 for char in decoded):
            raise AuthError("transient", "Bilibili returned invalid account encoding")
        safe = _cookie_value(name, raw_value)
        if not separator or not safe:
            raise AuthError(
                "transient", "Bilibili returned invalid account credentials"
            )
        if name in authoritative:
            if authoritative[name] not in {safe, _cookie_value(name, decoded)}:
                raise AuthError(
                    "transient",
                    "Bilibili returned conflicting account credentials",
                    diagnostic="BILIBILI_COOKIE",
                )
            continue
        result = _merge_cookies(result, {name: safe})
    return result


def _ticket_params(value):
    parsed = _safe_url(
        value, {"passport.biligame.com": {"/x/passport-login/web/crossDomain"}}
    )
    if parsed is None or parsed.port is not None:
        raise AuthError(
            "transient",
            "Bilibili returned an invalid login ticket",
            diagnostic="BILIBILI_TICKET",
        )
    fields = parsed.query.split("&")
    if len(fields) > 16:
        raise AuthError("transient", "Bilibili returned an invalid login ticket")
    params = {}
    for field in fields:
        raw_name, separator, raw_value = field.partition("=")
        try:
            name = unquote(raw_name, errors="strict")
            value = unquote(raw_value, errors="strict")
        except (ValueError, UnicodeError):
            raise AuthError(
                "transient", "Bilibili returned invalid ticket encoding"
            ) from None
        if (
            not separator
            or name in params
            or name not in {"ticket", "gourl", "first_domain"}
        ):
            raise AuthError("transient", "Bilibili returned an invalid login ticket")
        params[name] = value
    ticket = params.get("ticket", "")
    if not _SAFE_TICKET.fullmatch(ticket):
        raise AuthError("transient", "Bilibili returned an invalid login ticket")
    # Navigation hints from the returned URL are never followed or reflected.
    return {
        "ticket": ticket,
        "gourl": "https://www.bilibili.com/",
        "first_domain": ".bilibili.com",
    }


def _response_cookies(response, *, ticket_exchange=False):
    headers = getattr(response, "headers", None)
    if hasattr(headers, "getall"):
        values = headers.getall("Set-Cookie", [])
    elif headers is not None:
        header = headers.get("Set-Cookie")
        values = [header] if header else []
    else:
        values = [item.OutputString() for item in response.cookies.values()]
    result = {}
    domains = {"", "bilibili.com", "passport.bilibili.com"}
    if ticket_exchange:
        domains.update({"biligame.com", "passport.biligame.com"})
    for header in values:
        cookies = SimpleCookie()
        try:
            seen = {}
            for part in header.split(";"):
                if part.partition("=")[0].strip() in _COOKIE_NAMES:
                    single = SimpleCookie()
                    single.load(part.strip())
                    seen = _merge_cookies(
                        seen, {name: item.value for name, item in single.items()}
                    )
            cookies.load(header)
        except CookieError:
            continue
        for name, item in cookies.items():
            if name not in _COOKIE_NAMES:
                continue
            domain = item["domain"].lstrip(".").lower()
            if domain not in domains:
                continue
            if item["path"] not in {"", "/"}:
                continue
            try:
                if item["max-age"] and int(item["max-age"]) <= 0:
                    continue
                if not item["max-age"] and item["expires"]:
                    expires = parsedate_to_datetime(item["expires"])
                    if expires.tzinfo is None:
                        expires = expires.replace(tzinfo=timezone.utc)
                    if expires.timestamp() <= time.time():
                        continue
            except (TypeError, ValueError, OverflowError):
                continue
            result = _merge_cookies(result, {name: item.value})
    return result


@dataclass(repr=False)
class _ChallengeState:
    key: str
    deadline: float
    cancelled: bool = False
    consumed: bool = False
    polling: bool = False


class BilibiliBackend:
    """Injected transport: async (url, params, cookies) -> (body, cookies)."""

    def __init__(self, *, request=None, timeout=15, clock=time.monotonic):
        timeout = float(timeout)
        if not math.isfinite(timeout):
            raise ValueError("timeout must be finite")
        self._timeout = max(1, min(timeout, 60))
        self._request_override = request
        self._clock = clock
        self._session = None
        self._challenges = {}
        self._closed = False

    async def _request(self, url, params=None, cookies=None):
        if self._closed:
            raise AuthError("unavailable", "Bilibili backend is closed")
        if url not in _ENDPOINTS:
            raise AuthError("invalid", "Unsupported Bilibili endpoint")
        if url == _EXCHANGE and (
            not isinstance(params, dict)
            or params.keys() != {"ticket", "gourl", "first_domain"}
            or not isinstance(params.get("ticket"), str)
            or not _SAFE_TICKET.fullmatch(params["ticket"])
            or params.get("gourl") != "https://www.bilibili.com/"
            or params.get("first_domain") != ".bilibili.com"
        ):
            raise AuthError("invalid", "Unsupported Bilibili ticket exchange")
        safe_cookies = _cookies(cookies) if url == _NAV else {}
        try:
            async with asyncio.timeout(self._timeout):
                if self._request_override is not None:
                    body, received = await self._request_override(
                        url, dict(params or {}), safe_cookies
                    )
                    if url == _EXCHANGE:
                        return {}, _cookies(received)
                    if not isinstance(body, dict):
                        raise AuthError(
                            "transient", "Bilibili returned an invalid response"
                        )
                    return body, _cookies(received)
                if self._session is None:
                    self._session = aiohttp.ClientSession(
                        cookie_jar=aiohttp.DummyCookieJar(),
                        trust_env=False,
                        timeout=aiohttp.ClientTimeout(total=self._timeout),
                        headers={
                            "Referer": "https://passport.bilibili.com/",
                            "User-Agent": (
                                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/130.0.0.0 Safari/537.36"
                            ),
                        },
                    )
                async with self._session.get(
                    url,
                    params=params or {},
                    cookies=safe_cookies,
                    allow_redirects=False,
                ) as response:
                    if url == _EXCHANGE and response.status in {200, 302, 303}:
                        return {}, _response_cookies(response, ticket_exchange=True)
                    if response.status != 200:
                        raise AuthError(
                            "transient", "Bilibili service is temporarily unavailable"
                        )
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        raw.extend(chunk)
                        if len(raw) > _MAX_RESPONSE:
                            raise AuthError(
                                "transient", "Bilibili returned an oversized response"
                            )
                    body = json.loads(raw)
                    if not isinstance(body, dict):
                        raise AuthError(
                            "transient", "Bilibili returned an invalid response"
                        )
                    return body, _response_cookies(response)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Transport exceptions can contain the QR key or Cookie request header.
            raise AuthError(
                "transient",
                "Bilibili request failed; saved credentials were retained",
                diagnostic="BILIBILI_REQUEST",
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
            stale = self._challenges.pop(challenge.challenge_id, None)
            if stale is not None:
                stale.cancelled = True
                stale.key = ""
            return None
        return state

    async def begin_login(self, method="bilibili"):
        if method not in {"bilibili", "qr", "bilibili_web"}:
            raise AuthError("invalid", "Use the Bilibili app to scan this QR code")
        for challenge_id, state in list(self._challenges.items()):
            if state.cancelled or self._clock() >= state.deadline:
                state.key = ""
                self._challenges.pop(challenge_id, None)
        if len(self._challenges) >= 8:
            raise AuthError("busy", "Too many pending Bilibili logins")
        body, _ = await self._request(_GENERATE)
        data = body.get("data")
        data = data if isinstance(data, dict) else {}
        key = data.get("qrcode_key")
        url = _qr_url(data.get("url"), key)
        if (
            type(body.get("code")) is not int
            or body["code"] != 0
            or (not isinstance(key, str) or not _QR_KEY.fullmatch(key) or not url)
        ):
            raise AuthError(
                "transient",
                "Bilibili could not create a login QR code",
                diagnostic="BILIBILI_QR",
            )
        try:
            import qrcode

            output = io.BytesIO()
            qrcode.make(url).save(output, format="PNG")
        except ImportError:
            raise AuthError(
                "dependency", "Install qrcode[pil] for account login"
            ) from None
        if self._closed:
            raise AuthError("unavailable", "Bilibili backend is closed")
        if len(self._challenges) >= 8:
            raise AuthError("busy", "Too many pending Bilibili logins")
        challenge = LoginChallenge(secrets.token_urlsafe(24), output.getvalue(), 180)
        self._challenges[challenge.challenge_id] = _ChallengeState(
            key, self._clock() + challenge.expires_in
        )
        return challenge

    async def poll_login(self, challenge):
        state = self._live_state(challenge)
        if state is None or state.consumed:
            return LoginPoll("expired")
        if state.polling:
            return LoginPoll("pending")
        state.polling = True
        try:
            return await self._poll_login(challenge, state)
        finally:
            state.polling = False

    async def _poll_login(self, challenge, state):
        body, received = await self._request(_POLL, {"qrcode_key": state.key})
        if self._live_state(challenge) is not state:
            return LoginPoll("expired")
        data = body.get("data")
        if (
            type(body.get("code")) is not int
            or body["code"] != 0
            or not isinstance(data, dict)
        ):
            raise AuthError(
                "transient",
                "Bilibili QR status is temporarily unavailable",
                diagnostic="BILIBILI_POLL",
            )
        code = data.get("code")
        if type(code) is not int:
            raise AuthError(
                "transient",
                "Bilibili returned an invalid QR status",
                diagnostic="BILIBILI_POLL",
            )
        if code in {86101, 86090}:
            return LoginPoll("pending" if code == 86101 else "scanned")
        if code == 86038:
            await self.cancel_login(challenge)
            return LoginPoll("expired")
        if code != 0:
            raise AuthError(
                "transient",
                "Bilibili QR status is temporarily unavailable",
                diagnostic="BILIBILI_POLL",
            )
        # A successful poll consumes the QR key; retrying it cannot recover a ticket.
        state.consumed = True
        state.key = ""
        try:
            required = ("SESSDATA", "bili_jct", "DedeUserID")
            value = data.get("url")
            parsed = _safe_url(value, _CROSS_PATHS) if value else None
            ticket_params = None
            if (
                parsed is not None
                and parsed.path == "/x/passport-login/web/crossDomain"
            ):
                ticket_params = _ticket_params(value)
                cookies = _cookies(received)
            else:
                cookies = _redirect_cookies(value, received)
            if not all(cookies.get(name) for name in required) and ticket_params:
                try:
                    _, exchanged = await self._request(_EXCHANGE, ticket_params)
                    cookies = _merge_cookies(cookies, exchanged)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    raise AuthError(
                        "transient",
                        "Bilibili ticket exchange failed; start a new QR login",
                        diagnostic="BILIBILI_TICKET",
                    ) from None
            if self._live_state(challenge) is not state:
                return LoginPoll("expired")
            if not all(cookies.get(name) for name in required):
                raise AuthError(
                    "transient",
                    "Bilibili did not return complete account credentials; start a new QR login",
                    diagnostic="BILIBILI_TICKET"
                    if ticket_params
                    else "BILIBILI_COOKIE",
                )
            credential = {"cookies": cookies}
            token = data.get("refresh_token")
            if isinstance(token, str) and _SAFE_TOKEN.fullmatch(token):
                credential["refresh_token"] = token
            return LoginPoll("authorized", credential)
        finally:
            await self.cancel_login(challenge)

    async def cancel_login(self, challenge):
        state = self._challenges.pop(getattr(challenge, "challenge_id", ""), None)
        if state is not None:
            state.cancelled = True
            state.key = ""

    @staticmethod
    def _credential_cookies(credential):
        return (
            _cookies(credential.get("cookies")) if isinstance(credential, dict) else {}
        )

    async def check_credentials(self, credential):
        cookies = self._credential_cookies(credential)
        if not cookies.get("SESSDATA"):
            return "expired"
        try:
            body, _ = await self._request(_NAV, cookies=cookies)
        except AuthError:
            return "unknown"
        code = body.get("code")
        if type(code) is not int:
            return "unknown"
        if code == -101:
            return "expired"
        if code != 0:
            return "unknown"
        data = body.get("data")
        if not isinstance(data, dict):
            return "unknown"
        if data.get("isLogin") is False:
            return "expired"
        mid = data.get("mid")
        if type(mid) not in {int, str} or not re.fullmatch(
            r"[1-9][0-9]{0,19}", str(mid)
        ):
            return "unknown"
        if data.get("isLogin") is not True:
            return "unknown"
        if cookies.get("DedeUserID") not in {None, str(mid)}:
            return "expired"
        return "valid"

    async def refresh_credentials(self, credential):
        # A complete refresh requires the official refresh_csrf confirmation flow.
        return None

    async def resolve_audio(self, song, credential):
        return AudioResult(
            "unavailable", reason="Bilibili audio is resolved by the video extractor"
        )

    async def close(self):
        self._closed = True
        for state in self._challenges.values():
            state.cancelled = True
            state.key = ""
        self._challenges.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
