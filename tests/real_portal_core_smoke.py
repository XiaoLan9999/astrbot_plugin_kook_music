"""Offline account-portal smoke against actual AstrBot routing and JWT validation.

Run in a fresh process with the AstrBot checkout and plugins parent in PYTHONPATH.
Both cwd and ASTRBOT_ROOT must be the dedicated ``core-smoke`` directory. This
does not initialize the music plugin, access an account store, or bind a listener.
Socket connections and DNS are forbidden, including during AstrBot imports.
"""

import asyncio
import json
import os
import socket
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

if not os.environ.get("ASTRBOT_ROOT"):
    raise RuntimeError("Set ASTRBOT_ROOT to an isolated test directory")
ROOT = Path(os.environ["ASTRBOT_ROOT"]).resolve()
if ROOT != Path.cwd().resolve() or ROOT.name != "core-smoke":
    raise RuntimeError("Run with cwd and ASTRBOT_ROOT equal to dedicated core-smoke")
os.environ["ASTRBOT_DISABLE_METRICS"] = "1"


def deny_network(*_args, **_kwargs):
    raise AssertionError("Network is forbidden in the portal real-core smoke")


async def smoke():
    import httpx
    import jwt
    from astrbot.api.web import PluginRequest, bind_request_context
    from astrbot.core.config import VERSION
    from astrbot.core.star.context import Context
    from astrbot.dashboard.api.plugins import dashboard_plugin_extension_route
    from astrbot.dashboard.responses import ApiError
    from astrbot.dashboard.services.auth_service import DASHBOARD_JWT_COOKIE_NAME
    from astrbot_plugin_kook_music.music_auth.verification_portal import (
        VerificationPortal,
    )
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from starlette.requests import Request

    checks = {"request": 0, "http": 0}

    def check(group, condition, label):
        if not condition:
            raise AssertionError(label)
        checks[group] += 1

    def make_portal():
        context = SimpleNamespace(registered_web_apis=[])
        context.register_web_api = lambda *args: Context.register_web_api(
            context, *args
        )
        calls = []

        async def importer(bot, user, cookie):
            calls.append((bot, user, cookie))
            return True, "synthetic safe result"

        portal = VerificationPortal(
            "https://bot.example",
            importer,
            lambda bot, user: (bot, user) == ("bot", "123"),
        )
        portal.register(context)
        return context, portal, calls

    context, portal, calls = make_portal()

    async def native_request(method, payload=None, username="admin", origin=None):
        raw = json.dumps(payload).encode() if payload is not None else b""

        async def receive():
            return {"type": "http.request", "body": raw, "more_body": False}

        path = urlsplit(portal.page_url).path
        scope = {
            "type": "http",
            "method": method,
            "path": path,
            "raw_path": path.encode(),
            "scheme": "https",
            "query_string": b"",
            "headers": [
                (b"origin", (origin or portal.origin).encode()),
                (b"content-type", b"application/json"),
                (b"content-length", str(len(raw)).encode()),
            ],
            "server": ("bot.example", 443),
            "client": ("192.0.2.1", 1234),
        }
        proxy = PluginRequest(
            Request(scope, receive),
            username=username,
            plugin_name="astrbot_plugin_kook_music",
        )
        with bind_request_context(proxy):
            return await context.registered_web_apis[0][1]()

    try:
        check(
            "request", len(context.registered_web_apis) == 1, "native API registration"
        )
        response = await native_request("GET", username=None)
        check(
            "request", response.status_code == 401, "missing request identity rejected"
        )
        response = await native_request("GET")
        check("request", response.status_code == 200, "native HTML request accepted")
        check(
            "request",
            "text/html" in response.headers["content-type"],
            "native HTML response",
        )
        check(
            "request",
            "connect-src 'self'" in response.headers["content-security-policy"],
            "native response retains restrictive CSP",
        )
        ticket = parse_qs(urlsplit(portal.issue_link("bot", "123")).fragment)["ticket"][
            0
        ]
        beginning = {"action": "begin", "ticket": ticket}
        response = await native_request(
            "POST", beginning, origin="https://evil.example"
        )
        check(
            "request",
            response.status_code == 403,
            "native cross-origin request rejected",
        )
        response = await native_request("POST", beginning)
        check("request", response.status_code == 200, "native one-use ticket exchange")
        state = json.loads(response.body)
        submission = {
            "action": "import",
            "session": state["session"],
            "csrf": state["csrf"],
            "cookie": "MUSIC_U=synthetic-only",
        }
        response = await native_request("POST", submission)
        check("request", response.status_code == 200, "native credential handoff")
        check(
            "request",
            calls == [("bot", "123", "MUSIC_U=synthetic-only")],
            "native handoff retains authorized bot and owner",
        )
        check(
            "request",
            b"synthetic-only" not in response.body,
            "native response redaction",
        )
        response = await native_request("POST", submission)
        check("request", response.status_code == 403, "native replay rejected")
    finally:
        await portal.close()
    check("request", not context.registered_web_apis, "native route unregister")

    context, portal, calls = make_portal()
    app = FastAPI()
    app.state.jwt_secret = "synthetic-test-signing-key-not-production-12345"
    app.state.core_lifecycle = SimpleNamespace(star_context=context)
    app.add_api_route(
        "/api/plug/{plugin_path:path}",
        dashboard_plugin_extension_route,
        methods=["GET", "POST"],
    )

    @app.exception_handler(ApiError)
    async def auth_error(_request, error):
        return JSONResponse({"message": error.message}, status_code=error.status_code)

    path = urlsplit(portal.page_url).path
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=portal.origin
        ) as client:
            response = await client.get(path)
            check(
                "http", response.status_code == 401, "GET requires real Dashboard JWT"
            )
            ticket = parse_qs(urlsplit(portal.issue_link("bot", "123")).fragment)[
                "ticket"
            ][0]
            beginning = {"action": "begin", "ticket": ticket}
            response = await client.post(
                path, json=beginning, headers={"Origin": portal.origin}
            )
            check(
                "http",
                response.status_code == 401,
                "ticket alone cannot bypass Dashboard JWT",
            )
            token = jwt.encode(
                {"username": "admin", "exp": int(time.time()) + 60},
                app.state.jwt_secret,
                algorithm="HS256",
            )
            client.cookies.set(DASHBOARD_JWT_COOKIE_NAME, token)
            response = await client.get(path)
            check(
                "http",
                response.status_code == 200,
                "real Dashboard JWT cookie accepted",
            )
            check(
                "http",
                "text/html" in response.headers["content-type"],
                "ASGI HTML response",
            )
            check(
                "http",
                "frame-ancestors 'none'" in response.headers["content-security-policy"],
                "ASGI response retains embedding protection",
            )
            response = await client.post(
                path, json=beginning, headers={"Origin": "https://evil.example"}
            )
            check(
                "http",
                response.status_code == 403,
                "JWT does not bypass Origin validation",
            )
            response = await client.post(
                path, json=beginning, headers={"Origin": portal.origin}
            )
            check(
                "http",
                response.status_code == 200,
                "real router exchanges one-use ticket",
            )
            state = response.json()
            submission = {
                "action": "import",
                "session": state["session"],
                "csrf": state["csrf"],
                "cookie": "MUSIC_U=synthetic-only",
            }
            response = await client.post(
                path, json=submission, headers={"Origin": portal.origin}
            )
            check(
                "http",
                response.status_code == 200,
                "real router imports synthetic credential",
            )
            check(
                "http",
                calls == [("bot", "123", "MUSIC_U=synthetic-only")],
                "real router retains authorized handoff identity",
            )
            check(
                "http", "synthetic-only" not in response.text, "ASGI response redaction"
            )
            response = await client.post(
                path, json=submission, headers={"Origin": portal.origin}
            )
            check(
                "http",
                response.status_code == 403,
                "real router rejects handoff replay",
            )
    finally:
        await portal.close()
    check(
        "http", not context.registered_web_apis, "real router handler removed on close"
    )
    print(
        f"PASS: {sum(checks.values())} portal real-core checks "
        f"(PluginRequest={checks['request']}, HTTP/JWT={checks['http']}); "
        f"AstrBot {VERSION}; no sockets, external requests, account files, or real credentials"
    )


async def guarded_smoke():
    with (
        patch.object(socket.socket, "connect", deny_network),
        patch.object(socket.socket, "connect_ex", deny_network),
        patch.object(socket, "getaddrinfo", deny_network),
        patch.object(socket, "create_connection", deny_network),
    ):
        await smoke()


if __name__ == "__main__":
    asyncio.run(guarded_smoke())
