"""Voice uploads are bounded, local HTTP requests that never submit a chat turn."""
from __future__ import annotations

import sqlite3
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from travel_agent.adapters.whisper_local import VoiceError
from travel_agent.web import create_app

MAX_AUDIO_BYTES = 8 * 1024 * 1024
VOICE_PATH = "/api/voice/transcribe"
TRANSCRIPT = "Хочу в Ереван на неделю, с завтраком и багажом."


class FakeTranscriber:
    def __init__(self):
        self.available = True
        self.calls = []
        self.error = None
        self.text = TRANSCRIPT

    def health(self):
        result = {"available": self.available, "engine": "whisper", "language": "ru", "max_seconds": 60, "max_bytes": MAX_AUDIO_BYTES}
        if not self.available:
            result["message"] = "Голосовой ввод пока недоступен."
        return result

    async def transcribe(self, audio: bytes, content_type: str):
        self.calls.append((audio, content_type))
        if not self.available:
            raise VoiceError("voice_unavailable", "Голосовой ввод пока недоступен.", 503)
        if self.error is not None:
            raise self.error
        return {"text": self.text, "language": "ru", "engine": "whisper"}


@pytest.fixture
def voice_env(tmp_path):
    transcriber = FakeTranscriber()
    db_path = tmp_path / "voice-web.sqlite"
    app = create_app(db_path=db_path, model_backend="rules", data_mode="replay", voice_transcriber=transcriber)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        from http_helpers import bootstrap
        bootstrap(client)
        yield SimpleNamespace(client=client, transcriber=transcriber, db_path=db_path)


def upload(env, audio=b"mock audio", *, content_type="audio/webm", headers=None):
    request_headers = dict(headers or {})
    if content_type is not None:
        request_headers["Content-Type"] = content_type
    return env.client.post(VOICE_PATH, content=audio, headers=request_headers)


def domain_snapshot(db_path):
    with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as connection:
        return {name: connection.execute("SELECT * FROM " + name + " ORDER BY rowid").fetchall()
                for name in ("sessions", "turns", "events", "demo_orders", "web_jobs")}


def test_bootstrap_exposes_transcriber_health_without_processing_audio(voice_env):
    response = voice_env.client.get("/api/bootstrap")
    assert response.json()["voice"] == voice_env.transcriber.health()
    assert voice_env.transcriber.calls == []
    voice_env.transcriber.available = False
    unavailable = voice_env.client.get("/api/bootstrap").json()["voice"]
    assert unavailable["available"] is False
    assert unavailable["language"] == "ru" and unavailable["engine"] == "whisper"
    assert unavailable["message"]


@pytest.mark.parametrize("content_type", ["audio/webm", "audio/webm;codecs=opus", "audio/ogg", "audio/mp4", "audio/wav"])
def test_supported_audio_returns_text_without_creating_a_chat_turn(voice_env, content_type):
    before = domain_snapshot(voice_env.db_path)
    audio = b"mock audio larger than the ordinary JSON body limit\0" * 2000
    assert 80_000 < len(audio) < MAX_AUDIO_BYTES
    response = upload(voice_env, audio, content_type=content_type)
    assert response.status_code == 200, response.text
    assert response.json() == {"text": TRANSCRIPT, "language": "ru", "engine": "whisper"}
    assert len(voice_env.transcriber.calls) == 1
    received, received_type = voice_env.transcriber.calls[0]
    assert received == audio
    assert received_type.split(";", 1)[0] == content_type.split(";", 1)[0]
    assert domain_snapshot(voice_env.db_path) == before


def test_chunked_audio_over_ordinary_api_limit_is_accepted(voice_env):
    chunks = [b"chunk-a" * 10_000, b"chunk-b" * 10_000]
    response = upload(voice_env, iter(chunks))
    assert "content-length" not in response.request.headers
    assert response.status_code == 200, response.text
    assert voice_env.transcriber.calls[0][0] == b"".join(chunks)


@pytest.mark.parametrize("token", [None, "wrong-bootstrap-token"])
def test_voice_requires_csrf_before_transcriber_is_called(voice_env, token):
    voice_env.client.headers.pop("X-Travel-Token", None)
    headers = {"X-Travel-Token": token} if token is not None else {}
    response = upload(voice_env, headers=headers)
    assert response.status_code == 403
    assert voice_env.transcriber.calls == []


@pytest.mark.parametrize("headers", [
    {"Origin": "https://unrelated.example"},
    {"Sec-Fetch-Site": "cross-site"},
])
def test_cross_origin_voice_is_rejected_even_with_valid_csrf(voice_env, headers):
    response = upload(voice_env, headers=headers)
    assert response.status_code == 403
    assert voice_env.transcriber.calls == []


def test_declared_oversized_audio_is_rejected_before_transcriber(voice_env):
    response = upload(voice_env, b"small body", headers={"Content-Length": str(MAX_AUDIO_BYTES + 1)})
    assert response.status_code == 413
    assert voice_env.transcriber.calls == []


@pytest.mark.parametrize("declared_length", [None, "1"])
def test_streamed_audio_cannot_bypass_size_limit_without_truthful_content_length(voice_env, declared_length):
    headers = {"Content-Length": declared_length} if declared_length is not None else {}
    chunks = iter([b"a" * (1024 * 1024)] * 8 + [b"x"])
    response = upload(voice_env, chunks, headers=headers)
    assert response.status_code == 413, response.text
    assert voice_env.transcriber.calls == []


def test_exact_audio_size_limit_is_accepted(voice_env):
    audio = b"a" * MAX_AUDIO_BYTES
    response = upload(voice_env, audio)
    assert response.status_code == 200, response.text
    assert len(voice_env.transcriber.calls[0][0]) == MAX_AUDIO_BYTES


@pytest.mark.parametrize("content_type", [None, "application/json", "text/plain", "application/octet-stream", "multipart/form-data; boundary=unused"])
def test_missing_or_unsupported_mime_is_rejected_before_transcriber(voice_env, content_type):
    response = upload(voice_env, content_type=content_type)
    assert response.status_code == 415, response.text
    assert voice_env.transcriber.calls == []


def test_empty_audio_is_rejected_before_transcriber(voice_env):
    response = upload(voice_env, b"")
    assert response.status_code == 422, response.text
    assert voice_env.transcriber.calls == []


def test_voice_limit_does_not_raise_limit_for_other_api_routes(voice_env):
    response = voice_env.client.post("/api/turns", content=b" " * 80_001, headers={"Content-Type": "application/json"})
    assert response.status_code == 413
    assert voice_env.transcriber.calls == []
    assert all(not rows for rows in domain_snapshot(voice_env.db_path).values())


def test_unavailable_recognizer_returns_503_with_safe_message(voice_env):
    voice_env.transcriber.available = False
    response = upload(voice_env)
    assert response.status_code == 503, response.text
    assert "недоступен" in response.text.lower()


@pytest.mark.parametrize("code,message,status", [
    ("voice_busy", "Предыдущая запись ещё распознаётся.", 409),
    ("voice_too_long", "Запись длиннее 60 секунд.", 413),
    ("voice_invalid_audio", "Не удалось прочитать запись.", 422),
    ("voice_timeout", "Распознавание заняло слишком много времени.", 504),
])
def test_expected_voice_errors_keep_safe_message_and_status(voice_env, code, message, status):
    voice_env.transcriber.error = VoiceError(code, message, status)
    response = upload(voice_env)
    assert response.status_code == status, response.text
    assert message in response.text
    assert code in response.text
    assert len(voice_env.transcriber.calls) == 1


def test_unexpected_recognizer_exception_is_redacted_as_502(voice_env):
    voice_env.transcriber.error = RuntimeError("secret-api-token mock-secret /private/recording.wav")
    response = upload(voice_env)
    assert response.status_code == 502, response.text
    assert "secret" not in response.text and "/private/" not in response.text and "RuntimeError" not in response.text
    assert response.json()


def test_transcript_is_a_draft_and_cannot_mutate_existing_trip_or_jobs(voice_env):
    session_id = str(uuid4())
    submitted = voice_env.client.post("/api/turns", json={
        "session_id": session_id, "request_id": str(uuid4()), "expected_state_version": 0,
        "input": "message", "payload": {"text": "Хочу поехать из Москвы"},
    })
    assert submitted.status_code == 202
    job_id = submitted.json()["job_id"]
    deadline = time.monotonic() + 5
    while True:
        job = voice_env.client.get("/api/jobs/" + job_id).json()
        if job["status"] != "running":
            break
        assert time.monotonic() < deadline, "The deterministic chat turn did not finish"
        time.sleep(0.01)
    assert job["status"] == "completed"
    before = domain_snapshot(voice_env.db_path)
    session_before = voice_env.client.get("/api/sessions/" + session_id).json()
    voice_env.transcriber.text = "Подтверждаю, купи билеты и оформи заказ."
    response = upload(voice_env)
    assert response.status_code == 200
    assert response.json()["text"] == voice_env.transcriber.text
    assert domain_snapshot(voice_env.db_path) == before
    assert voice_env.client.get("/api/sessions/" + session_id).json() == session_before
    voice_env.transcriber.error = VoiceError("voice_invalid_audio", "Не удалось прочитать запись.")
    assert upload(voice_env).status_code == 422
    assert domain_snapshot(voice_env.db_path) == before
    assert voice_env.client.get("/api/sessions/" + session_id).json() == session_before


def test_browser_disconnect_cancels_recognition_without_creating_a_job(voice_env):
    import asyncio

    before = domain_snapshot(voice_env.db_path)

    async def simulate_disconnect():
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def slow_transcribe(audio, content_type):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        voice_env.transcriber.transcribe = slow_transcribe
        body_sent = False
        disconnect_delivered = False
        sent = []

        async def receive():
            nonlocal body_sent, disconnect_delivered
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": b"mock audio", "more_body": False}
            await started.wait()
            disconnect_delivered = True
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "POST", "scheme": "http", "path": VOICE_PATH,
            "raw_path": VOICE_PATH.encode(), "query_string": b"", "root_path": "",
            "headers": [(b"host", b"127.0.0.1"), (b"content-type", b"audio/webm"),
                        (b"cookie", "; ".join(name + "=" + value for name, value in voice_env.client.cookies.items()).encode()),
                        (b"x-travel-token", voice_env.client.headers["X-Travel-Token"].encode())],
            "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 80),
        }
        request_task = asyncio.create_task(voice_env.client.app(scope, receive, send))
        try:
            try:
                await asyncio.wait_for(cancelled.wait(), timeout=2)
            except TimeoutError:
                raise AssertionError(f"Recognition did not stop: started={started.is_set()}, disconnect_delivered={disconnect_delivered}") from None
            await asyncio.wait_for(request_task, timeout=2)
            assert started.is_set()
        finally:
            if not request_task.done():
                request_task.cancel()
            await asyncio.gather(request_task, return_exceptions=True)

    voice_env.client.portal.call(simulate_disconnect)
    assert domain_snapshot(voice_env.db_path) == before
