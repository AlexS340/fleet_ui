"""Идентичность запроса и проверка прав на объекты Console."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import pbkdf2_hmac
from html import escape
from hmac import compare_digest
from secrets import token_bytes, token_urlsafe
from urllib.parse import parse_qs, urlencode

from nicegui import Client, core
from starlette.requests import HTTPConnection, Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from fleet_console.store.db import LOCAL_OWNER, BenchRun, OwnRun, Store

IDENTITY = "fleet_console.identity"
SESSION_VALID = "fleet_console.session_valid"
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
        if item is None:
            return False
        if self.id == LOCAL_OWNER:
            return True
        user = self.store.user(self.id)
        if user and not user.enabled:
            return False
        if user and user.role == "admin":
            return True
        if isinstance(item, (BenchRun, OwnRun)):
            return self.store.has_run_access(item.id if isinstance(item, BenchRun) else item.run_id, self.id)
        return item.owner_id == self.id

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
        user = self.store.user(self.id)
        if (user and not user.enabled) or self.store.draft_owner(draft_id) != self.id:
            raise AccessDenied()


def _password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt if salt is not None else token_bytes(16)
    digest = pbkdf2_hmac("sha256", password.encode(), salt, 600_000)
    return f"{salt.hex()}:{digest.hex()}"


async def hash_new_password(password: str) -> str:
    if not 12 <= len(password) <= 256 or not password.strip():
        raise ValueError("Введите новый пароль длиной от 12 до 256 символов")
    return await asyncio.to_thread(_password_hash, password)


def _login_destination(request: Request) -> str:
    path = request.query_params.get("next", "")
    return path if SHARE_PATH.fullmatch(path) else "/"


def _account_form(next_path: str, *, register: bool = False, message: str = "", status: int = 200, user: str = "") -> HTMLResponse:
    query = urlencode({"next": next_path})
    if register:
        page, action, link = "Регистрация", "Зарегистрироваться", "Вернуться ко входу"
        route, other = "/test-register", "/test-login"
        hint = "<p>После регистрации администратор должен одобрить вашу учётную запись.</p>"
        password_attrs = 'autocomplete="new-password" minlength="12" maxlength="256"'
        password_hint = "<small>От 12 до 256 символов</small>"
    else:
        page, action, link = "Вход в Fleet Console", "Войти", "Зарегистрироваться"
        route, other = "/test-login", "/test-register"
        hint = password_hint = ""
        password_attrs = 'autocomplete="current-password"'
    notice = f'<p role="status">{escape(message)}</p>' if message else ""
    return HTMLResponse(
        '<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{page}</title><style>:root{{color-scheme:light dark;font:1rem/1.5 system-ui,sans-serif}}'
        'main{max-width:24rem;margin:8vh auto;padding:0 1rem} h1{font-size:1.6rem} '
        'input,button{font:inherit;padding:.4rem .6rem} input{display:block;width:100%;box-sizing:border-box} '
        'small{display:block} button{cursor:pointer}</style>'
        f'<main><h1>{page}</h1>{hint}{notice}'
        f'<form method="post" action="{route}?{query}"><p><label>Логин '
        f'<input name="user" autocomplete="username" value="{escape(user, quote=True)}" required></label></p>'
        f'<p><label>Пароль <input name="password" type="password" {password_attrs} required></label>'
        f'{password_hint}</p><button>{action}</button></form><p><a href="{other}?{query}">{link}</a></p></main></html>',
        status_code=status, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


class TestIdentityAdapter:
    """Вход для стенда: исходные пароли задаёт оператор, сброс сохраняется в SQLite."""

    cookie = "fleet_test_session"

    def __init__(self, accounts: dict[str, str]) -> None:
        self._seed = dict(accounts)
        self.sessions: dict[str, tuple[str, int]] = {}

    def bind(self, store: Store) -> None:
        self.store = store
        for user, password in self._seed.items():
            if store.password_hash(user) is None:
                store.seed_password(user, _password_hash(password))
        self._seed.clear()

    @classmethod
    def from_env(cls, value: str) -> TestIdentityAdapter:
        accounts = json.loads(value)
        if not isinstance(accounts, dict) or any(
            not isinstance(password, str) or not password or not name.strip() or name == LOCAL_OWNER
            for name, password in accounts.items()
        ):
            raise ValueError("FLEET_CONSOLE_TEST_USERS должен содержать JSON-словарь логинов и непустых паролей")
        return cls(accounts)

    def __call__(self, conn: HTTPConnection) -> Identity | None:
        session = self.sessions.get(conn.cookies.get(self.cookie, ""))
        if session is None:
            return None
        user = self.store.user(session[0])
        return Identity(user.id) if user and user.enabled and user.session_version == session[1] else None

    async def login(self, request: Request):
        next_path = _login_destination(request)
        if request.method == "POST":
            form = parse_qs((await request.body()).decode("utf-8", errors="replace"))
            user = form.get("user", [""])[0]
            password = form.get("password", [""])[0]
            account = self.store.user(user)
            expected = self.store.password_hash(user)
            if account and account.enabled and expected:
                actual = await asyncio.to_thread(_password_hash, password, bytes.fromhex(expected.split(":", 1)[0]))
                current = self.store.user(user)
                if current and current.enabled and current.session_version == account.session_version and compare_digest(actual, expected):
                    self.sessions.pop(request.cookies.get(self.cookie, ""), None)
                    token = token_urlsafe(32)
                    self.sessions[token] = (user, current.session_version)
                    response = RedirectResponse(next_path, status_code=303, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
                    response.set_cookie(self.cookie, token, httponly=True, samesite="lax", secure=request.url.scheme == "https")
                    return response
            return _account_form(next_path, message="Проверьте логин и пароль.", status=401, user=user)
        message = "Заявка отправлена. Вы сможете войти после одобрения администратором." if "registered" in request.query_params else ""
        return _account_form(next_path, message=message)

    async def register(self, request: Request) -> Response:
        next_path = _login_destination(request)
        if request.method == "GET":
            return _account_form(next_path, register=True)
        form = parse_qs((await request.body()).decode("utf-8", errors="replace"))
        user = form.get("user", [""])[0]
        if not re.fullmatch(r"[\w.@-]{1,100}", user) or user == LOCAL_OWNER:
            return _account_form(next_path, register=True, status=400, user=user,
                                 message="Выберите другой логин. До 100 символов: буквы, цифры или . _ @ -")
        try:
            digest = await hash_new_password(form.get("password", [""])[0])
        except ValueError as exc:
            return _account_form(next_path, register=True, status=400, user=user, message=str(exc))
        if not self.store.request_registration(user, digest):
            return _account_form(next_path, register=True, status=409, user=user, message="Этот логин уже занят.")
        return RedirectResponse(
            "/test-login?" + urlencode({"next": next_path, "registered": "1"}), status_code=303,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    async def logout(self, request: Request) -> Response:
        # Обычная форма с другого сайта не может добавить этот заголовок.
        if request.headers.get("X-Fleet-Logout") != "1":
            return PlainTextResponse("Недопустимый запрос на выход", status_code=403)
        self.sessions.pop(request.cookies.get(self.cookie, ""), None)
        response = Response(status_code=204, headers={"Cache-Control": "no-store"})
        response.delete_cookie(self.cookie, httponly=True, samesite="lax", secure=request.url.scheme == "https")
        return response


class IdentityMiddleware:
    def __init__(self, app, *, adapter: IdentityAdapter | None, store: Store) -> None:
        self.app, self.adapter, self.store = app, adapter, store

    async def __call__(self, scope, receive, send) -> None:
        test_adapter = self.adapter if isinstance(self.adapter, TestIdentityAdapter) else None
        if scope["type"] not in {"http", "websocket"} or (
            test_adapter is not None and scope["path"] in {"/test-login", "/test-register", "/test-logout"}
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
        account = None
        if identity is not None and identity.id.strip() and identity.id != LOCAL_OWNER:
            self.store.register_user(identity.id)
            account = self.store.user(identity.id)
        if account is None or not account.enabled:
            identity = None
        if identity is None:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                login_url = "/test-login"
                if SHARE_PATH.fullmatch(scope["path"]):
                    login_url += "?" + urlencode({"next": scope["path"]})
                response = (
                    RedirectResponse(login_url, status_code=303, headers={"Referrer-Policy": "no-referrer", "Cache-Control": "no-store"})
                    if test_adapter is not None and not socket and scope["method"] == "GET"
                    else PlainTextResponse("Требуется вход", status_code=401)
                )
                await response(scope, receive, send)
            return

        scope[IDENTITY] = identity
        version = account.session_version

        def session_valid() -> bool:
            if test_adapter is not None:
                return test_adapter(conn) == identity
            current = self.store.user(identity.id)
            return current is not None and current.enabled and current.session_version == version

        scope[SESSION_VALID] = session_valid

        async def private_send(message):
            if message["type"] == "http.response.start":
                message["headers"] = [
                    (key, value) for key, value in message.get("headers", [])
                    if key.lower() not in {b"cache-control", b"referrer-policy"}
                ] + [(b"cache-control", b"no-store"), (b"referrer-policy", b"no-referrer")]
            await send(message)

        await self.app(scope, receive, private_send)


def scope_identity(scope: dict) -> Identity | None:
    valid = scope.get(SESSION_VALID)
    return scope.get(IDENTITY) if valid is None or valid() else None


def _identity_id(scope: dict) -> str | None:
    identity = scope_identity(scope)
    return identity.id if identity is not None else None


def socket_allowed(sid: str, client_id: str, *, joined: bool) -> bool:
    """Связать socket с личностью HTTP-страницы и её комнатой."""
    client = Client.instances.get(client_id)
    if client is None:
        return False
    scope = (core.sio.get_environ(sid) or {}).get("asgi.scope", {})
    actual = _identity_id(scope)
    expected = _identity_id(client.request.scope)
    if actual is None and (IDENTITY in scope or IDENTITY in client.request.scope):
        return False
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
