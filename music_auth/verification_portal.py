"""Dashboard-authenticated, one-use handoff for manually obtained account cookies."""

import asyncio
import hashlib
import ipaddress
import json
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
_ROUTE = "/astrbot_plugin_kook_music/account/netease"
_MAX_BODY = 16384
_MAX_COOKIE = 8192
_HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
}


class PortalError(ValueError):
    """Public messages are fixed strings and never contain submitted values."""


@dataclass(repr=False)
class _Grant:
    bot_id: str
    user_id: str
    expires_at: float
    username: str = ""
    csrf_digest: str = ""


@dataclass
class PortalReply:
    status: int
    payload: dict | str = field(repr=False)
    headers: dict = field(default_factory=lambda: dict(_HEADERS))


def _base_url(value):
    if not isinstance(value, str) or any(ord(char) < 33 for char in value):
        raise PortalError("请先配置 HTTPS 管理后台地址，或 HTTP 回环 SSH 隧道地址。")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        port = parsed.port
        if (
            not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or "\\" in value
            or (port is not None and not 1 <= port <= 65535)
            or not re.fullmatch(r"(?:/[A-Za-z0-9_-]+)*/?", parsed.path)
        ):
            raise ValueError
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
            raise ValueError
        host = host.encode("idna").decode("ascii").lower()
        if not re.fullmatch(r"[A-Za-z0-9.:-]+", host):
            raise ValueError
        netloc = f"[{host}]" if ":" in host else host
        if port is not None and port != (443 if parsed.scheme == "https" else 80):
            netloc += f":{port}"
        origin = urlunsplit((parsed.scheme, netloc, "", "", ""))
        return origin + parsed.path.rstrip("/"), origin
    except (TypeError, ValueError, UnicodeError):
        raise PortalError(
            "管理后台地址无效；仅支持 HTTPS，或字面回环 IP 的 HTTP SSH 隧道。"
        ) from None


def _digest(value):
    return hashlib.sha256(value.encode("ascii")).hexdigest()


class VerificationPortal:
    """No listener or authentication bypass: every route uses AstrBot's Dashboard JWT.

    ``authorized(bot_id, user_id)`` must check the current KOOK adapter and admin
    allowlist, not just the values captured at construction. ``import_callback``
    receives those IDs and the manually submitted cookie text; it must validate
    against the first-party service before atomically replacing encrypted state.
    """

    def __init__(
        self,
        base_url,
        import_callback,
        authorized,
        *,
        ttl=600,
        operation_timeout=45,
        clock=time.monotonic,
    ):
        self.base_url, self.origin = _base_url(base_url)
        self.page_url = self.base_url + "/api/plug" + _ROUTE
        self.import_callback = import_callback
        self.authorized = authorized
        self.ttl = max(30, min(600, float(ttl)))
        self.operation_timeout = max(1, min(120, float(operation_timeout)))
        self.clock = clock
        self._tickets = {}
        self._sessions = {}
        self._limits = {}
        self._operations = set()
        self._closed = False
        self._context = None
        self._handler = self._web_handler

    def _allowed(self, bot_id, user_id):
        if self._closed or not isinstance(bot_id, str) or not isinstance(user_id, str):
            return False
        try:
            return self.authorized(bot_id, user_id) is True
        except Exception:
            return False

    def _prune(self):
        now = self.clock()
        for storage in (self._tickets, self._sessions):
            for key, grant in list(storage.items()):
                if now >= grant.expires_at or not self._allowed(
                    grant.bot_id, grant.user_id
                ):
                    storage.pop(key, None)
        for key, calls in list(self._limits.items()):
            while calls and calls[0] <= now - 60:
                calls.popleft()
            if not calls:
                self._limits.pop(key, None)

    def _rate_limit(self, key, maximum):
        self._prune()
        calls = self._limits.get(key)
        if calls is None:
            if len(self._limits) >= 64:
                return False
            calls = self._limits[key] = deque()
        if len(calls) >= maximum:
            return False
        calls.append(self.clock())
        return True

    def issue_link(self, bot_id, user_id):
        if not self._allowed(bot_id, user_id):
            raise PortalError("只有当前指定 KOOK 机器人的白名单管理员可以接入账号。")
        if not self._rate_limit(("issue", bot_id, user_id), 5):
            raise PortalError("接入链接生成过于频繁，请一分钟后重试。")
        self.revoke(bot_id, user_id)
        if len(self._tickets) + len(self._sessions) >= 16:
            raise PortalError("当前接入流程过多，请稍后重试。")
        token = secrets.token_urlsafe(32)
        self._tickets[_digest(token)] = _Grant(bot_id, user_id, self.clock() + self.ttl)
        return self.page_url + "#ticket=" + token

    def revoke(self, bot_id, user_id=None):
        for storage in (self._tickets, self._sessions):
            for key, grant in list(storage.items()):
                if grant.bot_id == bot_id and (
                    user_id is None or grant.user_id == user_id
                ):
                    storage.pop(key, None)

    def register(self, context):
        if self._closed:
            raise PortalError("账号接入页面已经关闭。")
        if self._context is context:
            return
        if self._context is not None:
            raise PortalError("账号接入页面已绑定其他管理后台。")
        for route, handler, methods, _ in getattr(context, "registered_web_apis", []):
            if (
                route == _ROUTE
                and set(methods) & {"GET", "POST"}
                and handler is not self._handler
            ):
                raise PortalError("账号接入页面路由已被占用，请先停止旧实例。")
        context.register_web_api(
            _ROUTE, self._handler, ["GET", "POST"], "音乐账号手动登录态接入"
        )
        self._context = context

    async def close(self):
        self._closed = True
        self._tickets.clear()
        self._sessions.clear()
        self._limits.clear()
        if self._context is not None:
            routes = getattr(self._context, "registered_web_apis", None)
            if isinstance(routes, list):
                routes[:] = [entry for entry in routes if entry[1] is not self._handler]
            self._context = None
        operations = list(self._operations)
        for task in operations:
            task.cancel()
        if operations:
            await asyncio.gather(*operations, return_exceptions=True)

    async def _web_handler(self):
        from astrbot.api.web import request
        from starlette.responses import HTMLResponse, JSONResponse

        reply = await self.handle(request)
        response_type = HTMLResponse if isinstance(reply.payload, str) else JSONResponse
        return response_type(
            reply.payload, status_code=reply.status, headers=reply.headers
        )

    @staticmethod
    def _error(status, message):
        return PortalReply(status, {"ok": False, "message": message})

    async def _body(self, request):
        raw_length = request.headers.get("content-length")
        try:
            if raw_length is not None and not 0 <= int(raw_length) <= _MAX_BODY:
                raise PortalError("请求过大。")
        except (TypeError, ValueError):
            raise PortalError("请求大小无效。") from None
        native = getattr(request, "_request", None)
        if native is not None and callable(getattr(native, "stream", None)):
            parts, length = [], 0
            async for part in native.stream():
                length += len(part)
                if length > _MAX_BODY:
                    raise PortalError("请求过大。")
                parts.append(part)
            raw = b"".join(parts)
        else:
            if raw_length is None:
                raise PortalError("请求缺少大小信息。")
            raw = await request.body()
        if len(raw) > _MAX_BODY:
            raise PortalError("请求过大。")
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise PortalError("请求格式无效。") from None
        if not isinstance(value, dict):
            raise PortalError("请求格式无效。")
        return value

    async def handle(self, request):
        if self._closed:
            return self._error(410, "接入页面已关闭，请重新向机器人获取链接。")
        username = getattr(request, "username", None)
        if not isinstance(username, str) or not username or len(username) > 256:
            return self._error(
                401, "请先在同一浏览器登录 AstrBot 管理后台，再打开私聊链接。"
            )
        if not self._rate_limit(("request", username), 20):
            return self._error(429, "请求过于频繁，请一分钟后重试。")
        if request.method == "GET":
            return self._page()
        if request.method != "POST":
            return self._error(405, "不支持的请求方式。")
        if (
            request.headers.get("origin") != self.origin
            or request.headers.get("sec-fetch-site", "same-origin") != "same-origin"
        ):
            return self._error(403, "请求来源不匹配，请使用私聊中的完整管理后台链接。")
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip()
            != "application/json"
        ):
            return self._error(415, "仅支持页面内的安全提交。")
        try:
            value = await asyncio.wait_for(self._body(request), timeout=15)
        except (PortalError, TimeoutError):
            return self._error(400, "请求内容无效或过大，请重新打开接入页面。")
        if value.get("action") == "begin" and set(value) == {"action", "ticket"}:
            return self._begin(username, value["ticket"])
        if value.get("action") == "import" and set(value) == {
            "action",
            "session",
            "csrf",
            "cookie",
        }:
            return await self._import(username, value)
        return self._error(400, "请求格式无效。")

    def _begin(self, username, ticket):
        if not isinstance(ticket, str) or not _TOKEN.fullmatch(ticket):
            return self._error(
                403, "接入链接无效、已使用或已过期，请在 KOOK 私聊重新获取。"
            )
        grant = self._tickets.pop(_digest(ticket), None)
        if (
            grant is None
            or self.clock() >= grant.expires_at
            or not self._allowed(grant.bot_id, grant.user_id)
        ):
            return self._error(
                403, "接入链接无效、已使用或已过期，请在 KOOK 私聊重新获取。"
            )
        session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        grant.username = username
        grant.csrf_digest = _digest(csrf)
        self._sessions[_digest(session)] = grant
        return PortalReply(
            200,
            {
                "ok": True,
                "session": session,
                "csrf": csrf,
                "expires_in": max(0, int(grant.expires_at - self.clock())),
                "message": "请先在网易云官网完成登录和人工安全验证，再提交你自己的登录 Cookie。",
            },
        )

    async def _import(self, username, value):
        session, csrf, cookie = value["session"], value["csrf"], value["cookie"]
        if (
            not isinstance(session, str)
            or not _TOKEN.fullmatch(session)
            or not isinstance(csrf, str)
            or not _TOKEN.fullmatch(csrf)
        ):
            return self._error(403, "接入会话无效或已过期。")
        digest = _digest(session)
        grant = self._sessions.get(digest)
        if (
            grant is None
            or self.clock() >= grant.expires_at
            or grant.username != username
            or not secrets.compare_digest(grant.csrf_digest, _digest(csrf))
            or not self._allowed(grant.bot_id, grant.user_id)
        ):
            return self._error(403, "接入会话无效或已过期。")
        try:
            cookie_size = len(cookie.encode("utf-8")) if isinstance(cookie, str) else 0
        except UnicodeError:
            cookie_size = _MAX_COOKIE + 1
        if (
            not isinstance(cookie, str)
            or not cookie.strip()
            or cookie_size > _MAX_COOKIE
            or any(char in cookie for char in "\r\n\x00")
        ):
            return self._error(
                400, "请填写长度有效的 Cookie 请求头内容，不要提交密码或文件。"
            )
        self._sessions.pop(digest, None)
        task = asyncio.create_task(
            self.import_callback(grant.bot_id, grant.user_id, cookie)
        )
        self._operations.add(task)
        try:
            result = await asyncio.wait_for(task, timeout=self.operation_timeout)
            success = (
                isinstance(result, tuple) and len(result) == 2 and result[0] is True
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            success = False
        finally:
            self._operations.discard(task)
        if not success:
            return self._error(
                400,
                "接入未完成，原账号未更改。请检查官网登录状态，再从 KOOK 获取新链接重试。",
            )
        return PortalReply(
            200,
            {
                "ok": True,
                "message": "网易云账号已通过官方校验并加密保存。请回到 KOOK 查看音乐账号状态或点歌。",
            },
        )

    @staticmethod
    def _page():
        nonce = secrets.token_urlsafe(24)
        headers = dict(_HEADERS)
        headers["Content-Security-Policy"] = (
            "default-src 'none'; "
            f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
            "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        )
        return PortalReply(200, _PAGE.replace("__CSP_NONCE__", nonce), headers)


_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><title>网易云手动登录态接入</title>
<style nonce="__CSP_NONCE__">body{font:16px/1.6 system-ui,sans-serif;max-width:760px;margin:40px auto;padding:0 20px;color:#172033}textarea{box-sizing:border-box;width:100%;min-height:110px;padding:12px}button{padding:10px 18px;margin:12px 12px 12px 0}#status{padding:12px;background:#eef3fa;white-space:pre-wrap}.notice{border-left:4px solid #be5c16;padding-left:14px}code{background:#eef3fa;padding:2px 4px}</style>
</head><body><h1>网易云手动登录态接入</h1>
<p class="notice">这不是网易云验证码页面，也不会绕过 8821 安全验证。本页属于你自己的 AstrBot 服务器，仅用于接入你已在官方网页登录的账号。</p>
<ol><li>在同一浏览器登录 AstrBot 管理后台；本页必须同时持有 KOOK 管理员私聊发出的一次性链接。</li>
<li><a href="https://music.163.com/" target="_blank" rel="noopener noreferrer">打开网易云音乐官方网站</a>，登录你的账号，并在官方页面按提示完成人工安全验证。官网首页不是当前二维码的验证码挑战地址；若该二维码已失效，不要反复扫码。</li>
<li>官网成功登录后，在浏览器开发者工具的 Network 中查看发送到 <code>music.163.com</code> 的已登录请求，复制其 <code>Cookie</code> 请求头值，或在 Application / Storage 的 Cookies 中仅取 <code>MUSIC_U</code>（可加 <code>__csrf</code>），按 <code>MUSIC_U=值; __csrf=值</code> 格式填写。</li>
<li>不要填写密码、整个请求、其他网站 Cookie，也不要把 Cookie 发到 KOOK、群聊或第三方解析站。本页只把它提交到当前 AstrBot 后台；后台仅向网易云官方校验，成功后加密保存，失败不替换原账号。</li></ol>
<button id="begin" type="button">开始一次性接入</button>
<label for="cookie">网易云 Cookie 请求头值</label><textarea id="cookie" autocomplete="off" spellcheck="false" maxlength="8192" disabled placeholder="MUSIC_U=...; __csrf=..."></textarea>
<button id="submit" type="button" disabled>校验并加密保存</button>
<p>链接最多 10 分钟有效，不能转发；关闭插件、取消登录、重新发链接或退出账号后应重新获取。页面不读取本机浏览器 Cookie，不采集手机号或密码。</p>
<p id="status" role="status">请先在网易云官网完成人工登录，再点击“开始一次性接入”。</p>
<script nonce="__CSP_NONCE__">
(() => {
  const params = new URLSearchParams(location.hash.slice(1));
  let ticket = params.get('ticket') || '', session = '', csrf = '';
  history.replaceState(null, '', location.pathname);
  const begin = document.getElementById('begin'), submit = document.getElementById('submit');
  const cookie = document.getElementById('cookie'), status = document.getElementById('status');
  if (!ticket) { begin.disabled = true; status.textContent = '缺少一次性票据，请重新打开 KOOK 私聊中的完整链接。'; }
  async function post(payload) {
    const response = await fetch(location.pathname, {method:'POST', credentials:'same-origin', cache:'no-store', redirect:'error', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
    const data = await response.json();
    if (!response.ok || data.ok !== true) throw new Error(data.message || '管理后台未授权或操作失败，请重新获取接入链接。');
    return data;
  }
  begin.addEventListener('click', async () => {
    begin.disabled = true;
    const current = ticket; ticket = '';
    try {
      const data = await post({action:'begin', ticket:current});
      session = data.session; csrf = data.csrf;
      status.textContent = data.message + ' 剩余约 ' + data.expires_in + ' 秒。';
      cookie.disabled = false; submit.disabled = false;
    } catch (error) { status.textContent = error.message || '连接失败，请重新获取接入链接。'; }
  });
  submit.addEventListener('click', async () => {
    if (!cookie.value.trim()) { status.textContent = '请先填写你自己的网易云 Cookie 请求头值。'; return; }
    submit.disabled = true; cookie.disabled = true;
    const payload = {action:'import', session:session, csrf:csrf, cookie:cookie.value};
    cookie.value = ''; session = ''; csrf = '';
    status.textContent = '正在向网易云官方校验，请稍候，不要重复提交。';
    try { const data = await post(payload); status.textContent = data.message; }
    catch (error) { status.textContent = error.message || '连接失败，请重新获取接入链接。'; }
    finally { payload.cookie = ''; }
  });
  addEventListener('pagehide', () => { cookie.value = ''; ticket = ''; session = ''; csrf = ''; });
})();
</script></body></html>"""
