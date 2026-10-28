"""Account-required HTTP backend with legacy guest-data migration.

Web jobs retain the original TurnInput, including its server clock, so network
retries and browser reloads cannot create a different idempotency fingerprint.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from importlib.resources import files
import json
import logging
import hashlib
import hmac
import os
from pathlib import Path
import secrets
from typing import Annotated, Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints, SecretStr, field_validator, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .adapters.whisper_local import MAX_AUDIO_BYTES, SUPPORTED_AUDIO_TYPES, VoiceError, WhisperTranscriber
from .cli import DEMO_TEXT
from .config import make_gateway
from .domain.models import SessionSnapshot, TurnInput
from .graph.suggestions import build_suggestions
from .dialogue import bound_dialogue
from .response_policy import public_result, public_text
from .service import AgentService
from .storage.sqlite import fingerprint
from .home_recommendations import RecommendationStore
from .auth import AUTH_COOKIE, SESSION_SECONDS, DUMMY_HASH, AuthConflict, AuthRateLimit, AuthStore, hash_password, verify_password

WEB_USER = "local-web"
COOKIE_NAME = "travel_visitor"
Identifier = Annotated[str, StringConstraints(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")]


class MessagePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=12000)


class SelectPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    option_id: str = Field(min_length=1, max_length=150)
    options_version: int = Field(ge=1, strict=True)


class SelectComponentPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    component_id: str = Field(min_length=1, max_length=150)
    options_version: int = Field(ge=1, strict=True)


class RemoveComponentPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_type: Literal["avia", "rail", "bus", "hotel"]


class SelectEventPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(min_length=1, max_length=150)
    event_choices_version: int = Field(ge=1, strict=True)


class ConfirmPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action_id: str = Field(min_length=1, max_length=150)
    plan_id: str = Field(min_length=1, max_length=150)
    plan_version: int = Field(ge=1, strict=True)
    payload_hash: str = Field(min_length=1, max_length=150)


class EmptyPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr = Field(max_length=254)
    password: SecretStr = Field(min_length=12, max_length=128)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value):
        return value.casefold()

    @field_validator("password")
    @classmethod
    def valid_password(cls, value):
        text = value.get_secret_value()
        if "\x00" in text:
            raise ValueError("Пароль содержит недопустимый символ")
        text.encode("utf-8")
        return value


class SignupPayload(LoginPayload):
    name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=80)] = ""

    @field_validator("name")
    @classmethod
    def valid_name(cls, value):
        if "\x00" in value or any(ord(char) < 32 for char in value):
            raise ValueError("Имя содержит недопустимый символ")
        value.encode("utf-8")
        return value


class ProfilePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origin: str | None = Field(default=None, max_length=120)
    preferences: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]] | None = Field(default=None, max_length=10)

    @model_validator(mode="after")
    def text_is_storable(self):
        for value in [self.origin or "", *(self.preferences or [])]:
            if "\x00" in value:
                raise ValueError("Текст содержит недопустимый символ")
            value.encode("utf-8")
        return self


class YandexConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_key: SecretStr = Field(min_length=1, max_length=4096)
    folder_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50, pattern=r"^[A-Za-z0-9_-]+$")]


class WebTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: Identifier
    request_id: Identifier
    expected_state_version: int = Field(ge=0, strict=True)
    input: Literal["message", "select_option", "select_component", "remove_component", "select_event", "confirm_action", "reject_action", "undo"]
    payload: dict[str, Any]

    @model_validator(mode="after")
    def valid_action(self):
        payload_type = {"message": MessagePayload, "select_option": SelectPayload, "select_component": SelectComponentPayload, "remove_component": RemoveComponentPayload, "select_event": SelectEventPayload,
                        "confirm_action": ConfirmPayload, "reject_action": EmptyPayload,
                        "undo": EmptyPayload}[self.input]
        payload_type.model_validate(self.payload)
        for value in self.payload.values():
            if isinstance(value, str) and "\x00" in value:
                raise ValueError("Текст содержит недопустимый символ")
        if self.input == "message" and not self.payload["text"].strip():
            raise ValueError("Введите сообщение")
        return self


def create_app(db_path="data/web-agent.sqlite", *, model_backend="codex", data_mode="replay", checkout_mode="demo", gateway=None, adapter=None, web_search_adapter=None, voice_transcriber=None, allowed_hosts=None, secure_cookies=None, static_dir=None, allow_local_configuration=None):
    tasks: dict[str, asyncio.Task] = {}
    concurrency = asyncio.Semaphore(3)
    auth_concurrency = asyncio.Semaphore(2)
    auth_pending = 0
    secure_cookies = secure_cookies if secure_cookies is not None else os.getenv("COOKIE_SECURE", "false").lower() == "true"
    allow_local_configuration = allow_local_configuration if allow_local_configuration is not None else os.getenv("ALLOW_LOCAL_CONFIGURATION", "false").lower() == "true"

    @asynccontextmanager
    async def lifespan(app):
        # SQLite and service belong to the same loop/thread as all API handlers.
        service = AgentService(db_path=db_path, data_mode=data_mode, checkout_mode=checkout_mode,
                               gateway=gateway or make_gateway(model_backend), adapter=adapter, history_loader=load_history, web_search_adapter=web_search_adapter, profile_loader=load_profile)
        app.state.service = service
        app.state.voice = voice_transcriber if voice_transcriber is not None else WhisperTranscriber()
        db = service.repo.conn
        postgres = getattr(service.repo, "dialect", "sqlite") == "postgres"
        if postgres and not db.execute("SELECT pg_try_advisory_lock(1869767286)").fetchone()[0]:
            service.close()
            raise RuntimeError("Для этой базы уже запущен сервер. Используйте один процесс приложения.")
        if not postgres:
            columns = {row[1] for row in db.execute("PRAGMA table_info(web_jobs)").fetchall()}
            if columns and "user_id" not in columns:
                # Existing local-only history stays archived; it cannot be
                # assigned to an arbitrary first visitor on a public server.
                db.execute("ALTER TABLE web_jobs RENAME TO legacy_web_jobs")
        sequence = "sequence BIGSERIAL UNIQUE," if postgres else ""
        db.execute(f'''CREATE TABLE IF NOT EXISTS web_jobs (
            {sequence}
            job_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, request_id TEXT NOT NULL,
            body_hash TEXT NOT NULL, turn_json TEXT NOT NULL, user_text TEXT NOT NULL,
            status TEXT NOT NULL, result TEXT, error TEXT, events TEXT NOT NULL,
            created_at TEXT NOT NULL, user_id TEXT NOT NULL, UNIQUE(user_id, session_id, request_id))''')
        db.execute("CREATE INDEX IF NOT EXISTS web_jobs_owner_session ON web_jobs(user_id,session_id,created_at)")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS web_jobs_one_active ON web_jobs(user_id,session_id) WHERE status='running'")
        db.execute("CREATE TABLE IF NOT EXISTS web_settings (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT OR IGNORE INTO web_settings VALUES(?,?)", ("cookie_secret", secrets.token_hex(32)))
        app.state.cookie_secret = db.execute("SELECT value FROM web_settings WHERE name=?", ("cookie_secret",)).fetchone()[0].encode()
        db.execute("CREATE TABLE IF NOT EXISTS profiles (user_id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        app.state.auth = AuthStore(service.repo)
        app.state.recommendations = RecommendationStore(service.repo)
        # Reconcile a crash after domain commit, without repeating an action.
        for row in db.execute("SELECT * FROM web_jobs WHERE status='running'").fetchall():
            receipt = service.repo.turn(row["user_id"], row["session_id"], row["request_id"])
            if receipt:
                db.execute("UPDATE web_jobs SET status='completed', result=? WHERE job_id=?", (json.dumps(receipt[1], ensure_ascii=False), row["job_id"]))
            else:
                db.execute("UPDATE web_jobs SET status='failed', error=? WHERE job_id=?", ("Приложение перезапустилось до завершения запроса. Отправьте сообщение ещё раз; сохранённая поездка восстановлена.", row["job_id"]))
        try:
            yield
        finally:
            pending = list(tasks.values())
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            service.close()

    app = FastAPI(title="О!дин запрос", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts or os.getenv("ALLOWED_HOSTS", "127.0.0.1,localhost,[::1]").split(","))

    def signature(value):
        return hmac.new(app.state.cookie_secret, value.encode(), hashlib.sha256).hexdigest()

    def cookie_secure(request):
        return secure_cookies or request.url.scheme == "https"

    def auth_cookie_name(request):
        return "__Host-" + AUTH_COOKIE if cookie_secure(request) else AUTH_COOKIE

    def auth_token(request):
        return request.cookies.get(auth_cookie_name(request))

    def guest_identity(request):
        cookie = request.cookies.get(COOKIE_NAME, "")
        visitor, separator, supplied = cookie.partition(".")
        if not separator or len(visitor) != 32 or len(supplied) != 64 or not all(c in "0123456789abcdef" for c in visitor + supplied) or not hmac.compare_digest(signature(visitor), supplied):
            return None
        return None if app.state.auth.guest_retired(visitor) else visitor

    def account_identity(request):
        return app.state.auth.session(auth_token(request))

    def identity(request):
        account = account_identity(request)
        if account:
            return account["id"]
        # An expired account session must not silently authenticate as a guest.
        if auth_token(request):
            return None
        return guest_identity(request)

    def csrf_token(request, *, guest=None, account_token=None):
        account_token = account_token or (auth_token(request) if account_identity(request) else None)
        return signature("csrf:account:" + account_token) if account_token else signature("csrf:" + (guest or guest_identity(request) or ""))

    def owner(request):
        account = account_identity(request)
        if not account:
            raise HTTPException(401, "Войдите в аккаунт, чтобы продолжить.")
        return account["id"]

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        if request.url.path.startswith("/api/auth/"):
            # Passwords, email input and arbitrary extra fields are never echoed.
            return JSONResponse({"detail": "Проверьте email и пароль: от 12 до 128 символов. Имя — до 80 символов."}, status_code=422)
        if request.url.path.rstrip("/") == "/api/search/config":
            # Default validation details can echo input values, extra-field names
            # and malformed request bodies. Never return those for credentials.
            return JSONResponse({"detail": "Проверьте данные Яндекса: непустой API-ключ до 4096 символов и ID каталога до 50 символов из букв, цифр, дефиса и подчёркивания."}, status_code=422)
        try:
            return await request_validation_exception_handler(request, error)
        except UnicodeEncodeError:
            # Keep the normal validation contract, but never echo text that
            # cannot be encoded (for example a lone UTF-16 surrogate).
            return JSONResponse({"detail": [{"type": "string_unicode", "loc": ["body"],
                "msg": "Сообщение содержит повреждённый символ. Введите его заново."}]}, status_code=422)

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        path = request.url.path.rstrip("/")
        if path == "/api" or path.startswith("/api/"):
            public = {"/api/bootstrap", "/api/health", "/api/auth/signup",
                      "/api/auth/login", "/api/auth/logout", "/api/auth/me"}
            # The account boundary precedes body parsing and every business
            # handler, including future/legacy routes and expensive voice work.
            if path not in public and not account_identity(request):
                return JSONResponse({"detail": "Войдите в аккаунт, чтобы продолжить."},
                                    status_code=401, headers={"Cache-Control": "no-store"})
            origin = request.headers.get("origin")
            expected = str(request.base_url).rstrip("/")
            if (origin and origin.rstrip("/") != expected) or request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "Откройте интерфейс по адресу приложения."}, status_code=403)
            if request.method not in ("GET", "HEAD"):
                supplied = request.headers.get("x-travel-token", "")
                visitor = identity(request)
                if not visitor or not supplied.isascii() or not secrets.compare_digest(supplied, csrf_token(request)):
                    return JSONResponse({"detail": "Обновите страницу перед отправкой запроса."}, status_code=403)
                try:
                    length = int(request.headers.get("content-length", "0"))
                except ValueError:
                    return JSONResponse({"detail": "Некорректный запрос."}, status_code=400)
                voice_upload = request.url.path.rstrip("/") == "/api/voice/transcribe"
                limit = MAX_AUDIO_BYTES if voice_upload else 80000
                if length < 0:
                    return JSONResponse({"detail": "Некорректный запрос."}, status_code=400)
                if length > limit:
                    message = "Запись слишком большая. Максимальный размер — 8 МБ." if voice_upload else "Сообщение слишком длинное."
                    return JSONResponse({"detail": message}, status_code=413)
                if not voice_upload:
                    chunks = bytearray()
                    try:
                        async with asyncio.timeout(15):
                            async for chunk in request.stream():
                                if len(chunks) + len(chunk) > limit:
                                    return JSONResponse({"detail": "Сообщение слишком длинное."}, status_code=413)
                                chunks.extend(chunk)
                    except TimeoutError:
                        return JSONResponse({"detail": "Не удалось загрузить сообщение вовремя."}, status_code=408)
                    request._body = bytes(chunks)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; connect-src 'self'; frame-src https://www.google.com https://maps.google.com; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        return response

    def db():
        return app.state.service.repo.conn

    def load_profile(user_id):
        row = db().execute("SELECT body FROM profiles WHERE user_id=?", (user_id,)).fetchone()
        return json.loads(row[0]) if row else {"origin": "", "preferences": []}

    def load_history(user_id, session_id):
        rows = db().execute("SELECT user_text,result FROM (SELECT rowid AS idx,user_text,result FROM web_jobs WHERE user_id=? AND session_id=? AND status='completed' AND result IS NOT NULL ORDER BY rowid DESC LIMIT 12) AS history ORDER BY idx", (user_id, session_id)).fetchall()
        history = []
        for row in rows:
            result = json.loads(row["result"])
            history.extend([{"role": "user", "text": row["user_text"]},
                            {"role": "assistant", "text": result.get("message", "")}])
        return bound_dialogue(history)

    def get_job(job_id, user_id):
        row = db().execute("SELECT * FROM web_jobs WHERE job_id=? AND user_id=?", (job_id, user_id)).fetchone()
        if row is None:
            raise HTTPException(404, "Запрос не найден")
        return row

    async def execute_job(job_id, turn):
        events = []
        async def on_event(event):
            events.append(event)
            db().execute("UPDATE web_jobs SET events=? WHERE job_id=?", (json.dumps(events, ensure_ascii=False), job_id))
        try:
            async with concurrency:
                result = await app.state.service.run_turn(turn, on_event=on_event)
            db().execute("UPDATE web_jobs SET status='completed',result=? WHERE job_id=?", (result.model_dump_json(), job_id))
            try:
                row = db().execute("SELECT * FROM web_jobs WHERE job_id=?", (job_id,)).fetchone()
                app.state.recommendations.record_job(row)
            except Exception:
                # The trip's committed result must remain usable if the optional
                # feed is unavailable. Its next read retries missing signals.
                logging.getLogger(__name__).exception("Could not update homepage recommendation signals")
        except asyncio.CancelledError:
            # Leave a durable running marker; next startup checks the domain receipt.
            raise
        except Exception:
            db().execute("UPDATE web_jobs SET status='failed',error=? WHERE job_id=?", ("Не удалось завершить запрос. Сохранённая поездка не потеряна. Попробуйте ещё раз.", job_id))
        finally:
            tasks.pop(job_id, None)

    def user_text(body, user_id):
        if body.input == "message":
            return body.payload["text"].strip()
        if body.input == "select_option":
            session = app.state.service.repo.session(user_id, body.session_id)
            option = next((o for o in (session or {}).get("options", []) if o["option_id"] == body.payload["option_id"]), None)
            return "Выбираю: " + option["title"] if option else "Выбираю вариант поездки"
        if body.input == "select_component":
            session = app.state.service.repo.session(user_id, body.session_id) or {}
            cards = session.get("transport_options", []) + session.get("hotel_options", [])
            component = next((c for c in cards if c["component_id"] == body.payload["component_id"]), None)
            return "Выбираю: " + component["title"] if component else "Выбираю компонент поездки"
        if body.input == "remove_component":
            return "Убираю выбранное проживание" if body.payload["product_type"] == "hotel" else "Убираю выбранный транспорт"
        if body.input == "select_event":
            state = app.state.service.repo.session(user_id, body.session_id) or {}
            event = next((item for item in state.get("event_choices", []) if item.get("event_id") == body.payload["event_id"]), None)
            if not event:
                return "Выбираю событие"
            from .dialogue import event_selection_text
            return event_selection_text(event)
        return {"confirm_action": "Подтверждаю демонстрационный заказ", "reject_action": "Отменяю подтверждение", "undo": "Вернуть предыдущий вариант"}[body.input]

    @app.get("/api/bootstrap")
    async def bootstrap(request: Request):
        account = account_identity(request)
        visitor = guest_identity(request) or uuid4().hex
        response = JSONResponse({"model_backend": model_backend, "data_mode": data_mode, "checkout_mode": checkout_mode,
                "voice": app.state.voice.health(),
                "demo_prompt": DEMO_TEXT, "csrf_token": csrf_token(request, guest=visitor), "user": account,
                "auth_required": True,
                "maps": {"embed_api_key": os.getenv("GOOGLE_MAPS_EMBED_API_KEY") or None},
                "app": "ostrovok-travel-agent", "web_search": {"provider": "yandex", "configured": bool(getattr(app.state.service.web_search_adapter, "configured", False))}})
        response.set_cookie(COOKIE_NAME, visitor + "." + signature(visitor), httponly=True, secure=cookie_secure(request), samesite="lax", max_age=60 * 60 * 24 * 365)
        if auth_token(request) and not account:
            response.delete_cookie(auth_cookie_name(request), httponly=True, secure=cookie_secure(request), samesite="lax")
        return response

    @app.get("/api/auth/me")
    async def auth_me(request: Request):
        account = account_identity(request)
        if not account and not guest_identity(request):
            raise HTTPException(401, "Обновите страницу, чтобы продолжить.")
        return {"user": account, "csrf_token": csrf_token(request)}

    def auth_response(request, token, user, migrated):
        visitor = uuid4().hex
        response = JSONResponse({"user": user, "csrf_token": csrf_token(request, account_token=token), "guest_migration": migrated})
        response.set_cookie(auth_cookie_name(request), token, httponly=True, secure=cookie_secure(request), samesite="lax", max_age=SESSION_SECONDS)
        response.set_cookie(COOKIE_NAME, visitor + "." + signature(visitor), httponly=True, secure=cookie_secure(request), samesite="lax", max_age=60 * 60 * 24 * 365)
        return response

    async def password_work(function, *args):
        nonlocal auth_pending
        if auth_pending >= 8:
            raise HTTPException(429, "Слишком много попыток входа. Попробуйте через минуту.", headers={"Retry-After": "60"})
        auth_pending += 1
        try:
            async with auth_concurrency:
                work = asyncio.create_task(asyncio.to_thread(function, *args))
                try:
                    return await asyncio.shield(work)
                except asyncio.CancelledError:
                    # Thread cancellation cannot stop scrypt. Retain its slot
                    # until hashing finishes, including on client disconnect.
                    await work
                    raise
        finally:
            auth_pending -= 1

    def auth_limits(request, email, *, signup=False):
        address = request.client.host if request.client else "unknown"
        try:
            app.state.auth.rate_limit(signature("auth:ip:" + address), limit=30, seconds=900)
            app.state.auth.rate_limit(signature("auth:email:" + email), limit=10, seconds=900)
            if signup:
                app.state.auth.rate_limit(signature("auth:signup:" + address), limit=10, seconds=3600)
        except AuthRateLimit as error:
            raise HTTPException(429, "Слишком много попыток входа. Попробуйте позже.", headers={"Retry-After": str(error.retry_after)}) from None

    def finish_login(request, account_id, *, signup=None):
        try:
            guest = guest_identity(request) if not account_identity(request) else None
            token, user, migrated = app.state.auth.issue(account_id, guest=guest, old_token=auth_token(request), signup=signup)
        except AuthConflict as error:
            raise HTTPException(409, str(error)) from None
        return auth_response(request, token, user, migrated)

    @app.post("/api/auth/signup", status_code=201)
    async def signup(body: SignupPayload, request: Request):
        auth_limits(request, body.email, signup=True)
        encoded = await password_work(hash_password, body.password.get_secret_value())
        response = finish_login(request, "acct_" + uuid4().hex, signup={"email": body.email, "password_hash": encoded, "name": body.name})
        response.status_code = 201
        return response

    @app.post("/api/auth/login")
    async def login(body: LoginPayload, request: Request):
        auth_limits(request, body.email)
        account = app.state.auth.account_by_email(body.email)
        valid = await password_work(verify_password, body.password.get_secret_value(), account["password_hash"] if account else DUMMY_HASH)
        if not valid or not account:
            raise HTTPException(401, "Неверный email или пароль.")
        return finish_login(request, account["account_id"])

    @app.post("/api/auth/logout")
    async def logout(body: EmptyPayload, request: Request):
        app.state.auth.revoke(auth_token(request))
        visitor = uuid4().hex
        response = JSONResponse({"user": None, "csrf_token": signature("csrf:" + visitor), "guest_migration": {"moved_sessions": 0, "session_id_map": {}}})
        response.delete_cookie(auth_cookie_name(request), httponly=True, secure=cookie_secure(request), samesite="lax")
        response.set_cookie(COOKIE_NAME, visitor + "." + signature(visitor), httponly=True, secure=cookie_secure(request), samesite="lax", max_age=60 * 60 * 24 * 365)
        return response

    @app.get("/api/health")
    async def health():
        try:
            db().execute("SELECT 1").fetchone()
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return {"status": "ok", "storage": getattr(app.state.service.repo, "dialect", "sqlite")}

    @app.get("/api/profile")
    async def profile(request: Request):
        return load_profile(owner(request))

    @app.patch("/api/profile")
    async def save_profile(body: ProfilePatch, request: Request):
        user_id = owner(request)
        current = await profile(request)
        for key, value in body.model_dump(exclude_unset=True).items():
            current[key] = (value or "").strip() if key == "origin" else (value or [])
        db().execute("INSERT INTO profiles VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET body=excluded.body", (user_id, json.dumps(current, ensure_ascii=False)))
        return current

    @app.get("/api/recommendations")
    async def recommendations(request: Request):
        user_id = owner(request)
        return app.state.recommendations.feed(user_id, load_profile(user_id))

    @app.post("/api/recommendations/{recommendation_id}/dismiss")
    async def dismiss_recommendation(recommendation_id: Annotated[str, StringConstraints(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_:-]+$")], request: Request):
        app.state.recommendations.dismiss(owner(request), recommendation_id)
        return {"ok": True}

    @app.get("/api/sessions")
    async def list_sessions(request: Request):
        user_id = owner(request)
        rows = db().execute("SELECT session_id,MAX(created_at) AS updated_at FROM web_jobs WHERE user_id=? GROUP BY session_id ORDER BY updated_at DESC LIMIT 100", (user_id,)).fetchall()
        items = []
        for row in rows:
            state = app.state.service.repo.session(user_id, row["session_id"]) or {}
            destination = (state.get("request") or {}).get("destination")
            first = db().execute("SELECT user_text FROM web_jobs WHERE user_id=? AND session_id=? ORDER BY rowid LIMIT 1", (user_id, row["session_id"])).fetchone()
            title = destination or (first[0][:80] if first else "Новая поездка")
            items.append({"session_id": row["session_id"], "title": title, "destination": destination, "updated_at": row["updated_at"], "state_version": state.get("state_version", 0), "status": "completed_demo" if state.get("demo_order") else "planning"})
        return {"sessions": items}

    @app.post("/api/voice/transcribe")
    async def transcribe_voice(request: Request):
        # Raw audio avoids multipart buffering. Check both declared and actual
        # length: streamed uploads may omit Content-Length entirely.
        content_type = request.headers.get("content-type", "")
        mime = content_type.split(";", 1)[0].strip().lower()
        work = disconnected = None
        try:
            if mime not in SUPPORTED_AUDIO_TYPES:
                raise VoiceError("voice_unsupported_format", "Этот формат записи не поддерживается. Используйте WebM, Ogg, MP4, MP3 или WAV.", 415)
            health = app.state.voice.health()
            if not health.get("available"):
                raise VoiceError("voice_unavailable", health.get("message", "Голосовой ввод пока недоступен."), 503)
            audio = bytearray()
            try:
                async with asyncio.timeout(15):
                    async for chunk in request.stream():
                        if len(audio) + len(chunk) > MAX_AUDIO_BYTES:
                            raise VoiceError("voice_too_large", "Запись слишком большая. Максимальный размер — 8 МБ.", 413)
                        audio.extend(chunk)
            except TimeoutError:
                raise VoiceError("voice_upload_timeout", "Не удалось загрузить запись вовремя. Попробуйте ещё раз.", 408) from None
            if not audio:
                raise VoiceError("voice_empty", "Запись пустая. Запишите голосовое сообщение.")

            async def wait_for_disconnect():
                # The body is fully consumed; the next receive waits for the
                # browser disconnect. No concurrent reader competes for audio.
                while True:
                    message = await request.receive()
                    if message["type"] == "http.disconnect":
                        return

            work = asyncio.create_task(app.state.voice.transcribe(bytes(audio), content_type))
            audio.clear()
            disconnected = asyncio.create_task(wait_for_disconnect())
            # Stop recognition on browser cancellation, releasing its lock and
            # temporary audio immediately instead of waiting for inference.
            async with asyncio.timeout(90):
                done, _ = await asyncio.wait({work, disconnected}, return_when=asyncio.FIRST_COMPLETED)
                if work not in done:
                    raise VoiceError("voice_cancelled", "Запись отменена.", 499)
                result = await work
            text = result.get("text") if isinstance(result, dict) else None
            if not isinstance(text, str) or not text.strip() or len(text) > 12000:
                raise VoiceError("voice_invalid_result", "Не удалось получить текст записи. Попробуйте ещё раз.", 502)
            # A transcript is a draft only; no turn, job or session is created.
            return {"text": text.strip(), "language": "ru", "engine": "whisper"}
        except VoiceError as error:
            return JSONResponse({"detail": error.message, "code": error.code}, status_code=error.http_status)
        except TimeoutError:
            return JSONResponse({"detail": "Распознавание заняло слишком много времени. Попробуйте запись короче.", "code": "voice_timeout"}, status_code=504)
        except Exception:
            return JSONResponse({"detail": "Не удалось распознать запись. Попробуйте ещё раз.", "code": "voice_failed"}, status_code=502)
        finally:
            pending = [task for task in (work, disconnected) if task is not None]
            for task in pending:
                if not task.done():
                    task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    @app.post("/api/search/config")
    async def configure_search(body: YandexConnection, request: Request):
        if not allow_local_configuration:
            raise HTTPException(403, "Подключение поиска настраивается владельцем приложения.")
        from .adapters.search_yandex import YandexSearchAdapter
        # Validate a candidate before replacing a working connection. Credentials
        # stay in this process, never in turns, SQLite or model context.
        try:
            candidate = YandexSearchAdapter(api_key=body.api_key.get_secret_value(), folder_id=body.folder_id)
        except (ValueError, TypeError):
            raise HTTPException(422, "Проверьте формат API-ключа и ID каталога Яндекса.") from None
        if not candidate.configured:
            raise HTTPException(422, "Ключ или ID каталога имеют неподдерживаемый формат. Проверьте введённые данные; предыдущее подключение сохранено.")
        app.state.service.web_search_adapter = candidate
        return {"provider": "yandex", "configured": True}

    @app.post("/api/sessions/{session_id}/continue")
    async def continue_session(session_id: Identifier, request: Request):
        user_id = owner(request)
        source = app.state.service.repo.session(user_id, session_id)
        if not source:
            raise HTTPException(404, "Сохранённая поездка не найдена")
        if source["data_mode"] == data_mode and source["checkout_mode"] == checkout_mode:
            return {"session_id": session_id, "data_mode": data_mode}
        if db().execute("SELECT 1 FROM web_jobs WHERE user_id=? AND session_id=? AND status='running'", (user_id, session_id)).fetchone():
            raise HTTPException(409, "Дождитесь ответа агента перед сменой режима поиска.")
        new_id = fingerprint([user_id, session_id, source["state_version"], data_mode, checkout_mode])[:32]
        history = source.get("dialogue") or load_history(user_id, session_id)
        rows = db().execute("SELECT * FROM (SELECT rowid AS idx,* FROM web_jobs WHERE user_id=? AND session_id=? AND status='completed' AND result IS NOT NULL ORDER BY rowid DESC LIMIT 12) AS history ORDER BY idx", (user_id, session_id)).fetchall()
        messages = list(source.get("continued_messages", []))
        for row in rows:
            answer = json.loads(row["result"])
            messages.extend([{"role": "user", "text": row["user_text"]}, {"role": "assistant", "text": answer["message"], "web_search": answer.get("web_search")}])
        if not messages:
            messages = history
        fresh = SessionSnapshot(user_id=user_id, session_id=new_id, data_mode=data_mode, checkout_mode=checkout_mode,
                                request=source["request"], dialogue=bound_dialogue(history),
                                profile_context=source.get("profile_context", {}),
                                concert_intake=source.get("concert_intake", {}),
                                last_question=source.get("last_question"), last_web_search=source.get("last_web_search"),
                                event_choices=source.get("event_choices", []), event_choices_version=source.get("event_choices_version", 0), selected_event=source.get("selected_event"),
                                continued_messages=messages[-24:])
        db().execute("INSERT OR IGNORE INTO sessions VALUES(?,?,?,?)", (user_id, new_id, 0, fresh.model_dump_json()))
        return {"session_id": new_id, "data_mode": data_mode}

    @app.post("/api/turns", status_code=202)
    async def submit(body: WebTurn, request: Request):
        user_id = owner(request)
        digest = fingerprint(body.model_dump(mode="json"))
        existing = db().execute("SELECT * FROM web_jobs WHERE user_id=? AND session_id=? AND request_id=?", (user_id, body.session_id, body.request_id)).fetchone()
        if existing:
            if existing["body_hash"] != digest:
                raise HTTPException(409, "Этот запрос уже отправлен с другим содержимым.")
            if existing["status"] == "failed":
                # A retry must reuse the original clock and full domain input.
                # The action may already have committed before HTTP finalization
                # failed; reconcile that receipt before considering execution.
                receipt = app.state.service.repo.turn(user_id, body.session_id, body.request_id)
                if receipt:
                    db().execute("UPDATE web_jobs SET status='completed',result=?,error=NULL WHERE job_id=?", (json.dumps(receipt[1], ensure_ascii=False), existing["job_id"]))
                else:
                    active = db().execute("SELECT job_id FROM web_jobs WHERE user_id=? AND session_id=? AND status='running'", (user_id, body.session_id)).fetchone()
                    if active:
                        raise HTTPException(409, "Агент ещё обрабатывает предыдущий запрос этой поездки.")
                    if len(tasks) >= 8:
                        raise HTTPException(429, "Сейчас обрабатывается несколько запросов. Попробуйте через минуту.")
                    original = TurnInput.model_validate_json(existing["turn_json"])
                    db().execute("UPDATE web_jobs SET status='running',result=NULL,error=NULL,events='[]' WHERE job_id=?", (existing["job_id"],))
                    tasks[existing["job_id"]] = asyncio.create_task(execute_job(existing["job_id"], original))
            return {"job_id": existing["job_id"]}
        busy = db().execute("SELECT job_id FROM web_jobs WHERE user_id=? AND session_id=? AND status='running'", (user_id, body.session_id)).fetchone()
        if busy:
            raise HTTPException(409, "Агент ещё обрабатывает предыдущий запрос этой поездки.")
        if len(tasks) >= 8:
            raise HTTPException(429, "Сейчас обрабатывается несколько запросов. Попробуйте через минуту.")
        turn = TurnInput(**body.model_dump(), user_id=user_id, now=datetime.now(ZoneInfo("Europe/Moscow")), timezone="Europe/Moscow")
        job_id = uuid4().hex
        db().execute("INSERT INTO web_jobs(job_id,session_id,request_id,body_hash,turn_json,user_text,status,result,error,events,created_at,user_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (job_id, body.session_id, body.request_id, digest, turn.model_dump_json(), user_text(body, user_id), "running", None, None, "[]", turn.now.isoformat(), user_id))
        tasks[job_id] = asyncio.create_task(execute_job(job_id, turn))
        return {"job_id": job_id}

    @app.get("/api/jobs/{job_id}")
    async def job_status(job_id: Identifier, request: Request):
        row = get_job(job_id, owner(request))
        return {"job_id": job_id, "status": row["status"], "events": json.loads(row["events"]),
                "result": public_result(json.loads(row["result"])) if row["result"] else None, "error": public_text(row["error"]) if row["error"] else None}

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: Identifier, request: Request):
        user_id = owner(request)
        state = app.state.service.repo.session(user_id, session_id)
        if state:
            state["options"] = [o for o in state.get("options", []) if o.get("eligibility") == "satisfied"]
        rows = db().execute("SELECT * FROM (SELECT rowid AS idx,* FROM web_jobs WHERE user_id=? AND session_id=? ORDER BY rowid DESC LIMIT 100) AS history ORDER BY idx", (user_id, session_id)).fetchall()
        messages, last_result, active_job_id = list((state or {}).get("continued_messages", [])), None, None
        for row in rows:
            messages.append({"id": row["job_id"] + "-user", "role": "user", "text": row["user_text"]})
            if row["result"]:
                last_result = json.loads(row["result"])
                messages.append({"id": row["job_id"] + "-assistant", "role": "assistant", "text": last_result["message"], "web_search": last_result.get("web_search")})
            elif row["status"] == "failed":
                messages.append({"id": row["job_id"] + "-assistant", "role": "assistant", "text": row["error"]})
            if row["status"] == "running":
                active_job_id = row["job_id"]
        if state and last_result:
            if (not active_job_id and last_result.get("state_version") == state["state_version"]
                    and state.get("current_plan") and last_result.get("status") == "partial"
                    and (last_result.get("error") or {}).get("code") == "stale_offer"):
                old_message = last_result["message"]
                last_result["message"] = "Выбранный тариф не удалось подтвердить. Обновим варианты?"
                last_result["questions"] = ["Обновим варианты?"]
                last_result["suggestions_version"] = 0
                state["current_plan"]["verification_status"] = "unverified"
                if messages and messages[-1].get("role") == "assistant" and messages[-1].get("text") == old_message:
                    messages[-1]["text"] = last_result["message"]
            # Older receipts had coverage but hid the missing component behind
            # a generic answer. Repair only the latest presentation from that
            # saved evidence; durable turn receipts stay immutable.
            if (not active_job_id and last_result.get("state_version") == state["state_version"]
                    and last_result.get("status") == "partial" and not state.get("options")
                    and not last_result.get("error") and not state.get("search_diagnostics")
                    and last_result.get("message", "").startswith("Подходящих вариантов среди проверенных предложений не нашлось.")):
                from .domain.search_diagnostics import legacy_search_diagnostics, format_search_diagnostics
                diagnostic = legacy_search_diagnostics(state.get("search_coverage", []), state["request"])
                if diagnostic and diagnostic.get("code") in {"missing_flights", "missing_hotels", "missing_both"}:
                    explained = format_search_diagnostics(diagnostic, followup=True, data_mode=data_mode)
                    old_message = last_result["message"]
                    last_result["message"] = explained["message"]
                    last_result["questions"] = [explained["question"]] if explained["question"] else []
                    last_result["search_diagnostics"] = state["search_diagnostics"] = diagnostic
                    if messages and messages[-1].get("role") == "assistant" and messages[-1].get("text") == old_message:
                        messages[-1]["text"] = explained["message"]
            if not state.get("dialogue"):
                state["dialogue"] = load_history(user_id, session_id)
            # A current partial answer can be a specific factual follow-up.
            # Only the known historical template needs presentation migration;
            # never replace a completed answer merely because no cards exist.
            from .domain.search_diagnostics import is_legacy_search_explanation
            if (not active_job_id and last_result.get("state_version") == state["state_version"]
                    and last_result.get("status") == "partial" and not state.get("options")
                    and state.get("search_diagnostics") and not last_result.get("web_search")
                    and is_legacy_search_explanation(last_result.get("message", ""))):
                from .domain.search_diagnostics import format_search_diagnostics
                explained = format_search_diagnostics(state["search_diagnostics"],
                    followup=not bool(last_result.get("questions")), data_mode=data_mode)
                old_message = last_result["message"]
                last_result["message"] = explained["message"]
                last_result["questions"] = [explained["question"]] if explained["question"] else []
                state["last_question"] = explained["question"]
                if state["dialogue"] and state["dialogue"][-1].get("role") == "assistant" and state["dialogue"][-1].get("text") == old_message:
                    state["dialogue"][-1]["text"] = explained["message"]
                if messages and messages[-1].get("role") == "assistant" and messages[-1].get("text") == old_message:
                    messages[-1]["text"] = explained["message"]
                if old_message != explained["message"]:
                    last_result["suggestions_version"] = 0
            if (not active_job_id and last_result.get("state_version") == state["state_version"]
                    and last_result.get("status") == "needs_input"
                    and last_result.get("message") == state.get("last_question")):
                from .domain.clarifications import normalize_session_question
                old_question = state["last_question"]
                state = normalize_session_question(state)
                if state["last_question"] != old_question:
                    last_result["message"] = state["last_question"]
                    last_result["questions"] = [state["last_question"]]
                    # Old chips answered another part of a compound question.
                    last_result["suggestions_version"] = 0
                    if messages and messages[-1].get("role") == "assistant" and messages[-1].get("text") == old_question:
                        messages[-1]["text"] = state["last_question"]
            if last_result.get("state_version") != state["state_version"]:
                last_result["suggestions"] = []
            elif "suggestions" not in last_result or last_result.get("suggestions_version", 1) < 6:
                # Present pre-feature history with the same contextual rules.
                # Saved receipts remain immutable for idempotent retries.
                last_result["suggestions"] = build_suggestions(SessionSnapshot.model_validate(state), last_result)
                last_result["suggestions_version"] = 6
            for field in ("state_version", "options", "transport_options", "hotel_options", "selected_components", "component_errors", "current_plan", "pending_action", "demo_order", "search_coverage", "search_diagnostics", "event_choices", "event_choices_version", "selected_event"):
                last_result[field] = state.get(field)
        messages = [{**message, "text": public_text(message["text"])}
                    if message.get("role") == "assistant" else message for message in messages]
        return {"session": state, "messages": messages, "result": public_result(last_result) if last_result else None, "active_job_id": active_job_id}

    static_dir = Path(static_dir or os.getenv("FRONTEND_DIST", "frontend/dist"))
    if not (static_dir / "index.html").is_file():
        bundled = Path(str(files("travel_agent").joinpath("web_dist")))
        static_dir = bundled if (bundled / "index.html").is_file() else Path(str(files("travel_agent").joinpath("web_static")))
    app.mount("/", StaticFiles(directory=static_dir, html=True, check_dir=False), name="interface")
    return app


def serve(*, db_path="data/web-agent.sqlite", model_backend="codex", data_mode="replay", checkout_mode="demo", port=8765, open_browser=False):
    import uvicorn
    import webbrowser
    import threading
    url = f"http://127.0.0.1:{port}"
    print(f"\nО!дин запрос: {url}\nОставьте это окно открытым, пока пользуетесь интерфейсом.\n", flush=True)
    if open_browser:
        timer = threading.Timer(1.2, webbrowser.open, args=[url])
        timer.daemon = True
        timer.start()
    uvicorn.run(create_app(db_path=db_path, model_backend=model_backend, data_mode=data_mode, checkout_mode=checkout_mode),
                host="127.0.0.1", port=port, access_log=False, log_level="warning")
