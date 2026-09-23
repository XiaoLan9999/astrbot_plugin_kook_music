"""Private QQ/WeChat authorization using a pinned QQMusicApi protocol.

Account credentials are only sent over HTTPS to explicitly listed Tencent
endpoints. No public music resolver is used by this module.
"""

import asyncio
import json
import logging
import re
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import timezone
from email.utils import parsedate_to_datetime
from http.cookies import CookieError, SimpleCookie
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

import aiohttp
from yarl import URL

from .types import AudioResult, AuthError, LoginChallenge, LoginPoll

SDK_REVISION = "ba95861ee9391f5b5f60f89caa8d4de4af160c8b"
_API_HOSTS = frozenset(
    {
        "ssl.ptlogin2.qq.com",
        "ssl.ptlogin2.graph.qq.com",
        "graph.qq.com",
        "open.weixin.qq.com",
        "lp.open.weixin.qq.com",
        "u.y.qq.com",
        "c6.y.qq.com",
    }
)
_EXPIRED_CODES = frozenset({1000, 104400, 104401})
_MID_RE = re.compile(r"[A-Za-z0-9]{8,32}\Z")
_DEFAULT_CDN = "https://dl.stream.qqmusic.qq.com/"
_LOGIN_TRACE = ContextVar("qq_music_login_trace", default=None)
_LOGGER = logging.getLogger(__name__)
_QQ_CALLBACK_RE = re.compile(r"ptuiCB\((.*?)\)", re.DOTALL)
_QQ_ARGUMENT_RE = re.compile(r"'((?:\\.|[^'])*)'")
_LOGIN_ERROR_CODES = (
    1000,
    104401,
    104400,
    20261,
    20271,
    20272,
    20274,
    20277,
    20278,
    20279,
    20450,
    104604,
)
_SAFE_ERROR_NAMES = frozenset(
    {
        "ApiDataError",
        "HTTPError",
        "NetworkError",
        "TimeoutNetworkError",
        "TimeoutError",
        "TransportError",
        "TransportTimeout",
        "ValueError",
        "ValidationError",
        "LoginError",
        "LoginAuthExpiredError",
        "LoginDeviceLimitError",
        "LoginAccountRestrictedError",
        "LoginRateLimitError",
        "GlobalApiError",
        "CgiApiException",
        "CredentialExpiredError",
    }
)


def _login_query(url, hosts, required):
    try:
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.hostname not in hosts
            or parts.port not in (None, 443)
            or parts.username
            or parts.password
            or parts.fragment
            or "\\" in url
            or any(ord(char) < 33 or ord(char) == 127 for char in url)
        ):
            raise ValueError
        query = parse_qs(parts.query, keep_blank_values=True, max_num_fields=64)
        if any(len(values) != 1 for values in query.values()):
            raise ValueError
        for key in required:
            value = query.get(key, [""])[0]
            if (
                not value
                or len(value) > 4096
                or any(ord(c) < 33 or ord(c) == 127 for c in value)
            ):
                raise ValueError
        return {key: values[0] for key, values in query.items()}
    except (TypeError, ValueError):
        raise AuthError(
            "protocol", "QQ Music returned an invalid authorization response"
        ) from None


def _safe_diagnostic(stage, error, trace):
    name = type(error).__name__
    if name not in _SAFE_ERROR_NAMES:
        name = "AuthError" if isinstance(error, AuthError) else "UnexpectedError"
    parts = [stage, name]
    status = trace.get("status", getattr(error, "status_code", None))
    if type(status) is int and 100 <= status <= 599:
        parts.append(f"HTTP{status}")
    code = getattr(error, "code", None)
    if type(code) is int and -1000000 <= code <= 1000000:
        parts.append(f"CODE{code}")
    return ":".join(parts)


def _official_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        return (
            parts.scheme == "https"
            and parts.hostname in _API_HOSTS
            and parts.port in (None, 443)
            and not parts.username
            and not parts.password
            and not parts.fragment
            and "\\" not in url
        )
    except (TypeError, ValueError):
        return False


def _audio_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if (
            parts.scheme not in {"http", "https"}
            or not (
                host == "stream.qqmusic.qq.com"
                or host.endswith(".stream.qqmusic.qq.com")
            )
            or parts.port not in (None, 80, 443)
            or parts.username
            or parts.password
            or parts.fragment
            or "\\" in url
        ):
            return ""
        # Tencent's dispatch may advertise HTTP; credentials/audio tokens use TLS.
        return urlunsplit(("https", host, parts.path, parts.query, ""))
    except (TypeError, ValueError):
        return ""


@dataclass(repr=False)
class _Response:
    status_code: int
    url: str
    headers: Any
    cookies: dict
    content: bytes
    text: str

    def json(self):
        return json.loads(self.content)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("QQ Music HTTP request failed")


def _qq_check_sig_cookies(headers):
    """Keep cookie scopes until selecting the OAuth endpoint's cookie header."""
    source = URL("https://ssl.ptlogin2.graph.qq.com/check_sig")
    target = URL("https://graph.qq.com/oauth2.0/authorize")
    jar = aiohttp.CookieJar()
    if hasattr(headers, "getall"):
        values = headers.getall("Set-Cookie", [])
    else:
        value = headers.get("Set-Cookie")
        values = [value] if value else []
    for value in values:
        cookie = SimpleCookie()
        try:
            cookie.load(value)
        except CookieError:
            continue
        for item in cookie.values():
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
                        # aiohttp treats an Expires timestamp of zero as missing.
                        item["max-age"] = "0"
                except (TypeError, ValueError, OverflowError):
                    pass
        # A broad-domain deletion must not erase a same-named graph.qq.com cookie.
        jar.update_cookies(cookie, response_url=source)
    return {name: item.value for name, item in jar.filter_cookies(target).items()}


class _OfficialTransport:
    """SDK transport with no ambient cookies, proxies, redirects or raw logging."""

    def __init__(self, timeout: float = 20.0, session_factory=None):
        self.timeout = timeout
        self._session_factory = session_factory or aiohttp.ClientSession
        self._session = None
        self._closed = False

    async def request(self, request):
        from qqmusic_api.core.transport import TransportError, TransportTimeout

        if self._closed or not _official_url(request.url):
            raise TransportError("QQ Music transport rejected the request")
        allowed = {
            "params",
            "headers",
            "json",
            "data",
            "cookies",
            "timeout",
            "allow_redirects",
        }
        if set(request.kwargs) - allowed:
            raise TransportError("QQ Music transport rejected unsupported options")
        kwargs = dict(request.kwargs)
        kwargs.pop("timeout", None)
        kwargs["allow_redirects"] = False
        kwargs["timeout"] = aiohttp.ClientTimeout(total=self.timeout)
        if self._session is None:
            self._session = self._session_factory(
                cookie_jar=aiohttp.DummyCookieJar(), trust_env=False
            )
        try:
            async with self._session.request(
                request.method, request.url, **kwargs
            ) as response:
                trace = _LOGIN_TRACE.get()
                if trace is not None:
                    trace["status"] = response.status
                body = bytearray()
                async for block in response.content.iter_chunked(65536):
                    body.extend(block)
                    if len(body) > 2 * 1024 * 1024:
                        raise TransportError(
                            "QQ Music response exceeded the size limit"
                        )
                content = bytes(body)
                request_url = urlsplit(request.url)
                if (
                    request.method.upper() == "GET"
                    and request_url.hostname == "ssl.ptlogin2.graph.qq.com"
                    and request_url.path == "/check_sig"
                ):
                    cookies = _qq_check_sig_cookies(response.headers)
                else:
                    cookies = {
                        key: value.value for key, value in response.cookies.items()
                    }
                return _Response(
                    response.status,
                    str(response.url),
                    response.headers,
                    cookies,
                    content,
                    content.decode("utf-8", errors="replace"),
                )
        except asyncio.TimeoutError:
            raise TransportTimeout("QQ Music request timed out") from None
        except aiohttp.ClientError:
            raise TransportError("QQ Music connection failed") from None

    async def close(self):
        self._closed = True
        if self._session is not None:
            await self._session.close()


def _load_sdk():
    try:
        from qqmusic_api import Client, Credential
        from qqmusic_api.core.versioning import Platform
        from qqmusic_api.models.login import QRCodeLoginEvents, QRLoginType
        from qqmusic_api.modules.song import SongFileInfo, SongFileType
    except ImportError:
        raise AuthError(
            "unavailable", "QQ Music account dependency is not installed"
        ) from None
    return SimpleNamespace(
        Client=Client,
        Credential=Credential,
        Platform=Platform,
        QRLoginType=QRLoginType,
        Events=QRCodeLoginEvents,
        SongFileInfo=SongFileInfo,
        SongFileType=SongFileType,
    )


@dataclass(repr=False)
class _Pending:
    qr: Any
    deadline: float
    method: str = "qq"
    uin: str = field(default="", repr=False)
    sigx: str = field(default="", repr=False)
    cookies: dict = field(default_factory=dict, repr=False)
    oauth_code: str = field(default="", repr=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    task: Any = None
    cancelled: bool = False


class QQBackend:
    def __init__(
        self, *, client_factory=None, sdk=None, timeout=20.0, qr_timeout=180, clock=None
    ):
        self.timeout = max(0.01, min(float(timeout), 60.0))
        self.qr_timeout = max(1, min(int(qr_timeout), 300))
        self._clock = clock or time.monotonic
        self._sdk = sdk
        self._client_factory = client_factory
        self._client = None
        self._closed = False
        self._pending = {}
        self._operations = set()

    def _get_client(self):
        if self._closed:
            raise AuthError("closed", "QQ Music account service is closed")
        if self._sdk is None:
            self._sdk = _load_sdk()
        if self._client is None:
            if self._client_factory:
                self._client = self._client_factory()
            else:
                self._client = self._sdk.Client(
                    platform=self._sdk.Platform.WEB,
                    max_concurrency=2,
                    transport=_OfficialTransport(self.timeout),
                )
        return self._client

    async def _call(self, awaitable):
        task = asyncio.ensure_future(awaitable)
        self._operations.add(task)
        try:
            return await asyncio.wait_for(task, timeout=self.timeout)
        finally:
            self._operations.discard(task)

    def _credential(self, credential: dict):
        if not isinstance(credential, dict):
            raise AuthError("expired", "QQ Music account credentials are invalid")
        try:
            result = self._sdk.Credential.model_validate(credential)
            if not result.musickey or result.musicid <= 0:
                raise ValueError
            if not result.str_musicid:
                result = result.model_copy(update={"str_musicid": str(result.musicid)})
            return result
        except Exception:
            raise AuthError(
                "expired", "QQ Music account credentials are invalid"
            ) from None

    @staticmethod
    def _expired(error):
        return getattr(error, "code", None) in _EXPIRED_CODES or (
            isinstance(error, AuthError) and error.kind == "expired"
        )

    async def begin_login(self, method: str) -> LoginChallenge:
        if method not in {"qq", "wechat"}:
            raise AuthError("invalid_method", "Choose QQ or WeChat login")
        try:
            client = self._get_client()
            kind = (
                self._sdk.QRLoginType.QQ if method == "qq" else self._sdk.QRLoginType.WX
            )
            qr = await self._call(client.login.get_qrcode(kind))
            if (
                not isinstance(qr.data, bytes)
                or not qr.data.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff"))
                or len(qr.data) > 2 * 1024 * 1024
            ):
                raise ValueError
            challenge_id = uuid.uuid4().hex
            self._pending[challenge_id] = _Pending(
                qr, self._clock() + self.qr_timeout, method
            )
            return LoginChallenge(challenge_id, qr.data, self.qr_timeout)
        except AuthError:
            raise
        except Exception:
            raise AuthError(
                "transient", "QQ Music QR service is temporarily unavailable"
            ) from None

    async def poll_login(self, challenge: LoginChallenge) -> LoginPoll:
        state = self._pending.get(challenge.challenge_id)
        if state is None or state.cancelled or self._clock() >= state.deadline:
            self._pending.pop(challenge.challenge_id, None)
            return LoginPoll("expired")
        async with state.lock:
            if (
                state.cancelled
                or self._pending.get(challenge.challenge_id) is not state
                or self._clock() >= state.deadline
            ):
                self._pending.pop(challenge.challenge_id, None)
                return LoginPoll("expired")
            state.task = asyncio.current_task()
            try:
                client = self._get_client()
                if state.method == "qq":
                    result = await self._poll_qq(client, state)
                else:
                    result = await self._login_stage(
                        state, "WX_POLL", client.login.check_qrcode(state.qr)
                    )
                events = self._sdk.Events
                status = {
                    events.SCAN: "pending",
                    events.CONF: "scanned",
                    events.TIMEOUT: "expired",
                    events.REFUSE: "denied",
                    events.DONE: "authorized",
                }.get(result.event)
                if not status:
                    raise ValueError
                if state.cancelled or self._clock() >= state.deadline:
                    status = "expired"
                credential = None
                if status == "authorized":
                    credential = self._credential(
                        result.credential.model_dump()
                    ).model_dump(mode="json")
                if status in {"authorized", "expired", "denied"}:
                    state.cancelled = True
                    self._pending.pop(challenge.challenge_id, None)
                return LoginPoll(status, credential)
            except Exception as error:
                if self._expired(error):
                    self._pending.pop(challenge.challenge_id, None)
                    return LoginPoll("expired")
                if isinstance(error, AuthError):
                    raise
                raise AuthError(
                    "transient", "QQ Music login status is temporarily unavailable"
                ) from None
            finally:
                state.task = None

    async def _login_stage(self, state, stage, awaitable):
        trace = {"stage": stage}
        token = _LOGIN_TRACE.set(trace)
        try:
            if state.cancelled or self._clock() >= state.deadline:
                if hasattr(awaitable, "close"):
                    awaitable.close()
                raise AuthError("expired", "QQ Music login has expired")
            return await self._call(awaitable)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            diagnostic = _safe_diagnostic(stage, error, trace)
            _LOGGER.warning("QQ Music login stage failed: %s", diagnostic)
            if isinstance(error, AuthError):
                error.diagnostic = diagnostic
                raise
            kind = "expired" if self._expired(error) else "transient"
            if getattr(error, "code", None) in set(_LOGIN_ERROR_CODES) - _EXPIRED_CODES:
                kind = "denied"
            raise AuthError(
                kind,
                "QQ Music login status is temporarily unavailable",
                diagnostic=diagnostic,
            ) from None
        finally:
            _LOGIN_TRACE.reset(token)

    async def _poll_qq(self, client, state):
        from qqmusic_api.models.login import QRLoginResult
        from qqmusic_api.utils import hash33

        if not state.uin:

            async def poll_callback():
                response = await client.login._build_http(
                    "GET",
                    "https://ssl.ptlogin2.qq.com/ptqrlogin",
                    params={
                        "u1": "https://graph.qq.com/oauth2.0/login_jump",
                        "ptqrtoken": str(hash33(state.qr.identifier)),
                        "ptredirect": "0",
                        "h": "1",
                        "t": "1",
                        "g": "1",
                        "from_ui": "1",
                        "ptlang": "2052",
                        "action": f"0-0-{time.time() * 1000}",
                        "js_ver": "20102616",
                        "js_type": "1",
                        "pt_uistyle": "40",
                        "aid": "716027609",
                        "daid": "383",
                        "pt_3rd_aid": "100497308",
                        "has_onekey": "1",
                    },
                    headers={"Referer": "https://xui.ptlogin2.qq.com/"},
                    cookies={"qrsig": state.qr.identifier},
                    raw=True,
                )
                match = _QQ_CALLBACK_RE.search(response.text)
                args = _QQ_ARGUMENT_RE.findall(match.group(1)) if match else []
                if not args or not args[0].isdigit():
                    raise AuthError(
                        "protocol", "QQ Music returned an invalid QR status"
                    )
                try:
                    event = self._sdk.Events.get_by_value(int(args[0]))
                except ValueError:
                    raise AuthError(
                        "protocol", "QQ Music returned an unknown QR status"
                    ) from None
                if event not in {
                    self._sdk.Events.SCAN,
                    self._sdk.Events.CONF,
                    self._sdk.Events.TIMEOUT,
                    self._sdk.Events.REFUSE,
                    self._sdk.Events.DONE,
                }:
                    raise AuthError(
                        "protocol", "QQ Music returned an unknown QR status"
                    )
                if event != self._sdk.Events.DONE:
                    return QRLoginResult(event=event)
                if len(args) < 3:
                    raise AuthError(
                        "protocol", "QQ Music returned an invalid QR confirmation"
                    )
                query = _login_query(
                    args[2],
                    {"graph.qq.com", "ssl.ptlogin2.graph.qq.com"},
                    ("uin", "ptsigx"),
                )
                if (
                    not query["uin"].isdigit()
                    or len(query["uin"]) > 20
                    or int(query["uin"]) <= 0
                ):
                    raise AuthError(
                        "protocol", "QQ Music returned an invalid login identity"
                    )
                state.uin, state.sigx = query["uin"], query["ptsigx"]
                return None

            result = await self._login_stage(state, "QQ_CALLBACK", poll_callback())
            if result is not None:
                return result

        if not state.cookies:

            async def check_signature():
                response = await client.login._build_http(
                    "GET",
                    "https://ssl.ptlogin2.graph.qq.com/check_sig",
                    params={
                        "uin": state.uin,
                        "pttype": "1",
                        "service": "ptqrlogin",
                        "nodirect": "0",
                        "ptsigx": state.sigx,
                        "s_url": "https://graph.qq.com/oauth2.0/login_jump",
                        "ptlang": "2052",
                        "ptredirect": "100",
                        "aid": "716027609",
                        "daid": "383",
                        "j_later": "0",
                        "low_login_hour": "0",
                        "regmaster": "0",
                        "pt_login_type": "3",
                        "pt_aid": "0",
                        "pt_aaid": "16",
                        "pt_light": "0",
                        "pt_3rd_aid": "100497308",
                    },
                    headers={"Referer": "https://xui.ptlogin2.qq.com/"},
                    cookies={},
                    raw=True,
                    allow_redirects=False,
                )
                cookies = dict(response.cookies)
                if not cookies.get("p_skey"):
                    raise AuthError(
                        "protocol", "QQ Music authorization cookie is missing"
                    )
                state.cookies = cookies

            await self._login_stage(state, "QQ_CHECK_SIG", check_signature())

        if not state.oauth_code:

            async def oauth_authorize():
                response = await client.login._build_http(
                    "POST",
                    "https://graph.qq.com/oauth2.0/authorize",
                    data={
                        "response_type": "code",
                        "client_id": "100497308",
                        "redirect_uri": "https://y.qq.com/portal/wx_redirect.html?login_type=1&surl=https://y.qq.com/",
                        "scope": "get_user_info,get_app_friends",
                        "state": "state",
                        "switch": "",
                        "from_ptlogin": "1",
                        "src": "1",
                        "update_auth": "1",
                        "openapi": "1010_1030",
                        "g_tk": hash33(state.cookies["p_skey"], 5381),
                        "auth_time": str(int(time.time()) * 1000),
                        "ui": str(uuid.uuid4()),
                    },
                    cookies=state.cookies,
                    raw=True,
                    allow_redirects=False,
                )
                query = _login_query(
                    response.headers.get("Location", ""), {"y.qq.com"}, ("code",)
                )
                if "error" in query:
                    raise AuthError("denied", "QQ Music authorization was refused")
                state.oauth_code = query["code"]

            await self._login_stage(state, "QQ_OAUTH", oauth_authorize())

        async def exchange_credential():
            response = await client.login._build_cgi(
                module="QQConnectLogin.LoginServer",
                method="QQLogin",
                param={"code": state.oauth_code},
                comm={"tmeLoginType": 2},
                allow_error_codes=_LOGIN_ERROR_CODES,
            )
            result = self._sdk.Credential.model_validate(
                client.login._validate_result(response)
            )
            self._credential(result.model_dump())
            return QRLoginResult(event=self._sdk.Events.DONE, credential=result)

        return await self._login_stage(state, "QQ_EXCHANGE", exchange_credential())

    async def cancel_login(self, challenge: LoginChallenge):
        state = self._pending.pop(challenge.challenge_id, None)
        if state is None:
            return
        state.cancelled = True
        task = state.task
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def check_credentials(self, credential: dict) -> str:
        try:
            client = self._get_client()
            target = self._credential(credential)
            # SDK check_expired(WEB) mistakes every nonzero code for expiry.
            response = await self._call(
                client.login._build_cgi(
                    module="music.UserInfo.userInfoServer",
                    method="GetLoginUserInfo",
                    credential=target,
                    comm=self._auth_comm(target),
                    allow_error_codes="all",
                )
            )
            code = response.get("code") if isinstance(response, dict) else None
            if type(code) is int and code == 0:
                return "valid"
            return "expired" if code in _EXPIRED_CODES else "unknown"
        except Exception as error:
            return "expired" if self._expired(error) else "unknown"

    async def refresh_credentials(self, credential: dict) -> dict | None:
        try:
            client = self._get_client()
            target = self._credential(credential)
            if not target.refresh_key or not target.refresh_token:
                return None
            result = await self._call(client.login.refresh_credential(target))
            return self._credential(result.model_dump()).model_dump(mode="json")
        except Exception as error:
            if self._expired(error):
                return None
            raise AuthError(
                "transient", "QQ Music credential refresh is temporarily unavailable"
            ) from None

    async def resolve_audio(self, song, credential: dict) -> AudioResult:
        mid = str(song.id)
        media_mid = str(song.provider_data.get("media_mid") or mid)
        if (
            song.platform != "qq"
            or not _MID_RE.fullmatch(mid)
            or not _MID_RE.fullmatch(media_mid)
        ):
            return AudioResult(
                "unavailable", reason="QQ Music song MID is missing or invalid"
            )
        try:
            client = self._get_client()
            target = self._credential(credential)
            info = self._sdk.SongFileInfo(mid=mid, media_mid=media_mid)
            request = client.song.get_song_urls(
                [info], file_type=self._sdk.SongFileType.MP3_128, credential=target
            )
            # WEB defaults only include uin/g_tk; add the actual login identity.
            request.comm = self._auth_comm(target)
            response = await self._call(request)
            items = [item for item in response.data if item.mid == mid]
            if len(items) != 1:
                return AudioResult(
                    "transient", reason="QQ Music returned an unexpected song identity"
                )
            item = items[0]
            if item.result in _EXPIRED_CODES:
                return AudioResult(
                    "expired", reason="QQ Music account authorization has expired"
                )
            if item.result in {104003, 104013}:
                return AudioResult(
                    "unavailable", reason="The QQ Music account cannot play this song"
                )
            if item.result != 0 or not item.purl:
                return AudioResult(
                    "transient", reason="QQ Music could not issue an audio URL"
                )
            expected = f"M500{media_mid}.mp3"
            parts = urlsplit(item.purl)
            if (
                parts.scheme
                or parts.netloc
                or item.filename != expected
                or parts.path.lstrip("/") != expected
                or parts.fragment
                or "\\" in item.purl
            ):
                return AudioResult(
                    "transient", reason="QQ Music returned an unexpected audio path"
                )
            url = _audio_url(urljoin(_DEFAULT_CDN, item.purl))
            if not url:
                return AudioResult(
                    "transient", reason="QQ Music returned an untrusted audio endpoint"
                )
            return AudioResult("resolved", url=url)
        except Exception as error:
            if self._expired(error):
                return AudioResult(
                    "expired", reason="QQ Music account authorization has expired"
                )
            return AudioResult(
                "transient",
                reason="QQ Music account audio service is temporarily unavailable",
            )

    @staticmethod
    def _auth_comm(credential):
        return {
            "qq": str(credential.musicid),
            "authst": credential.musickey,
            "tmeLoginType": credential.login_type,
        }

    async def close(self):
        self._closed = True
        for challenge_id in list(self._pending):
            await self.cancel_login(LoginChallenge(challenge_id, b""))
        tasks = list(self._operations)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._client is not None:
            try:
                await asyncio.wait_for(self._client.close(), timeout=self.timeout)
            except Exception:
                pass
