"""Идентичность запроса и проверка прав на объекты Console."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from hmac import compare_digest
from secrets import token_urlsafe
from urllib.parse import parse_qs, urlencode

from nicegui import Client, core
from starlette.requests import HTTPConnection, Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from fleet_console.store.db import LOCAL_OWNER, Store

IDENTITY = "fleet_console.identity"
SHARE_PATH = re.compile(r"/measurements/shared/[A-Za-z0-9_-]{43}")


@dataclass(frozen=True)
class Identity:
    id: str
    name: str = ""


IdentityAdapter = Callable[[HTTPConnection], Identity | None]


class AccessDenied(LookupError):
    def __init__(self) -> None:
        super().__init__("Объект недоступен")


@dataclass(frozen=True)
class Access:
    store: Store
    identity: Identity

    @property
    def id(self) -> str:
        return self.identity.id

    @property
    def admin(self) -> bool:
        return self.id == LOCAL_OWNER or self.store.is_admin(self.id)

    def allows(self, item) -> bool:
        return item is not None and (self.admin or item.owner_id == self.id)

    def require(self, item):
        if not self.allows(item):
            raise AccessDenied()
        return item

    def require_admin(self) -> None:
        if not self.admin:
            raise AccessDenied()

    def run(self, run_id: str):
        return self.require(self.store.get_bench(run_id))

    def session(self, session_id: str):
        return self.require(self.store.get_session(session_id))

    def draft(self, draft_id: str) -> None:
        # Открытый черновик привязан к создавшему его клиенту даже для администраторов.
        if self.store.draft_owner(draft_id) != self.id:
            raise AccessDenied()


class TestIdentityAdapter:
    """Вход для изолированного стенда. Учётные данные задаёт оператор процесса."""

    cookie = "fleet_test_session"

    def __init__(self, accounts: dict[str, str]) -> None:
        self.accounts = accounts
        self.tokens = {name: token_urlsafe(32) for name in accounts}
        self.identities = {token: Identity(name) for name, token in self.tokens.items()}

    @classmethod
    def from_env(cls, value: str) -> TestIdentityAdapter:
        accounts = json.loads(value)
        if not isinstance(accounts, dict) or any(
            not isinstance(password, str) or not password or not name or name == LOCAL_OWNER
            for name, password in accounts.items()
        ):
            raise ValueError("FLEET_CONSOLE_TEST_USERS должен содержать JSON-словарь логинов и непустых паролей")
        return cls(accounts)

    def __call__(self, conn: HTTPConnection) -> Identity | None:
        return self.identities.get(conn.cookies.get(self.cookie, ""))

    async def login(self, request: Request):
        next_path = request.query_params.get("next", "")
        next_path = next_path if SHARE_PATH.fullmatch(next_path) else "/"
        if request.method == "POST":
            form = parse_qs((await request.body()).decode("utf-8", errors="replace"))
            user = form.get("user", [""])[0]
            password = form.get("password", [""])[0]
            expected = self.accounts.get(user)
            if expected is not None and compare_digest(password.encode(), expected.encode()):
                response = RedirectResponse(next_path, status_code=303, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
                response.set_cookie(self.cookie, self.tokens[user], httponly=True, samesite="lax", secure=request.url.scheme == "https")
                return response
        invalid = "<p>Проверьте логин и пароль.</p>" if request.method == "POST" else ""
        return HTMLResponse(
            '<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
            '<title>Вход в Fleet Console</title><main><h1>Fleet Console</h1>'
            f'{invalid}<form method="post" action="/test-login?{urlencode({"next": next_path})}">'
            '<p><label>Логин <input name="user" autocomplete="username" required></label></p>'
            '<p><label>Пароль <input name="password" type="password" autocomplete="current-password" required></label></p>'
            '<button>Войти</button></form></main></html>',
            status_code=401 if invalid else 200,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )


class IdentityMiddleware:
    def __init__(self, app, *, adapter: IdentityAdapter | None, store: Store) -> None:
        self.app, self.adapter, self.store = app, adapter, store

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in {"http", "websocket"} or (
            isinstance(self.adapter, TestIdentityAdapter) and scope["path"] == "/test-login"
        ):
            await self.app(scope, receive, send)
            return

        conn = HTTPConnection(scope)
        identity = self.adapter(conn) if self.adapter is not None else None
        socket = scope["path"].startswith("/_nicegui_ws/")
        if socket and (origin := conn.headers.get("origin")):
            expected = ("https" if conn.url.scheme in {"https", "wss"} else "http") + "://" + conn.url.netloc
            if origin != expected:
                identity = None
        if socket and (sid := conn.query_params.get("sid")):
            previous = _identity_id((core.sio.environ.get(sid) or {}).get("asgi.scope", {}))
            if identity is None or previous != identity.id:
                identity = None
        if identity is None or not identity.id.strip() or identity.id == LOCAL_OWNER:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                login_url = "/test-login"
                if SHARE_PATH.fullmatch(scope["path"]):
                    login_url += "?" + urlencode({"next": scope["path"]})
                response = (
                    RedirectResponse(login_url, status_code=303, headers={"Referrer-Policy": "no-referrer", "Cache-Control": "no-store"})
                    if isinstance(self.adapter, TestIdentityAdapter) and not socket and scope["method"] == "GET"
                    else PlainTextResponse("Требуется вход", status_code=401)
                )
                await response(scope, receive, send)
            return

        self.store.register_user(identity.id)
        scope[IDENTITY] = identity

        async def private_send(message):
            if message["type"] == "http.response.start":
                message["headers"] = [
                    (key, value) for key, value in message.get("headers", [])
                    if key.lower() not in {b"cache-control", b"referrer-policy"}
                ] + [(b"cache-control", b"no-store"), (b"referrer-policy", b"no-referrer")]
            await send(message)

        await self.app(scope, receive, private_send)


def _identity_id(scope: dict) -> str | None:
    identity = scope.get(IDENTITY)
    return identity.id if identity is not None else None


def socket_allowed(sid: str, client_id: str, *, joined: bool) -> bool:
    """Связать socket с личностью HTTP-страницы и её комнатой."""
    client = Client.instances.get(client_id)
    if client is None:
        return False
    actual = _identity_id((core.sio.get_environ(sid) or {}).get("asgi.scope", {}))
    expected = _identity_id(client.request.scope)
    return actual == expected and (not joined or client_id in core.sio.rooms(sid))


def install_socket_guard() -> None:
    """NiceGUI маршрутизирует события по client_id; в общем режиме проверяем отправителя."""
    handlers = core.sio.handlers["/"]
    if getattr(handlers["handshake"], "_fleet_guard", False):
        return

    def guard(handler, *, joined: bool):
        async def checked(sid, data):
            if not isinstance(data, dict) or not isinstance(data.get("client_id"), str):
                return False
            if not socket_allowed(sid, data["client_id"], joined=joined):
                return False
            if asyncio.iscoroutinefunction(handler):
                return await handler(sid, data)
            return handler(sid, data)

        checked._fleet_guard = True
        return checked

    original_connect = handlers["connect"]

    async def connect(sid, environ, data=None):
        query = parse_qs(environ.get("QUERY_STRING", ""))
        if query.get("implicit_handshake") == ["true"]:
            client_id = query.get("client_id", [""])[0]
            if not socket_allowed(sid, client_id, joined=False):
                return False
        return await original_connect(sid, environ, data)

    core.sio.on("connect", connect)
    for event in ("handshake", "event", "ack", "javascript_response", "log"):
        core.sio.on(event, guard(handlers[event], joined=event != "handshake"))
