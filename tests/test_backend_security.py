"""Real HTTP isolation, request limits and explicitly saved guest preferences."""
from fastapi.testclient import TestClient
from travel_agent.web import create_app, COOKIE_NAME
from test_web import submit, turn_body
from test_web import TRIP
from http_helpers import bootstrap


def test_account_cannot_read_or_mutate_other_account_trip(tmp_path):
    app = create_app(tmp_path / "guests.sqlite", model_backend="rules")
    with TestClient(app, base_url="http://127.0.0.1") as first:
        bootstrap(first)
        trip = turn_body(session="shared-id", request_id="same-id")
        job = submit(first, trip)
        first_state = first.get("/api/sessions/shared-id").json()
        cookies = dict(first.cookies)
        # Change the browser cookie jar while keeping the same live server.
        first.cookies.clear()
        bootstrap(first)
        assert first.get("/api/jobs/" + job["job_id"]).status_code == 404
        assert first.get("/api/sessions/shared-id").json()["session"] is None
        assert first.get("/api/sessions").json() == {"sessions": []}
        own = submit(first, turn_body(session="shared-id", request_id="same-id", text="Привет"))
        assert own["job_id"] != job["job_id"]
        assert first.get("/api/sessions/shared-id").json()["session"]["user_id"] != first_state["session"]["user_id"]
        first.cookies.clear()
        first.cookies.update(cookies)
        bootstrap(first)
        assert first.get("/api/sessions/shared-id").json() == first_state


def test_signed_cookie_tampering_and_cross_guest_csrf_fail(tmp_path):
    app = create_app(tmp_path / "cookie.sqlite", model_backend="rules")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = bootstrap(client)
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "SameSite=lax" in response.headers["set-cookie"]
        old_token = client.headers["X-Travel-Token"]
        cookie = client.cookies[COOKIE_NAME]
        client.cookies.clear()
        client.cookies.set(COOKIE_NAME, "0" * 32 + cookie[32:])
        assert client.get("/api/sessions").status_code == 401
        assert client.post("/api/turns", json=turn_body()).status_code == 401
        client.cookies.clear()
        bootstrap(client)
        client.headers["X-Travel-Token"] = old_token
        assert client.post("/api/turns", json=turn_body()).status_code == 403


def test_profiles_are_explicit_private_and_survive_restart(tmp_path):
    path = tmp_path / "profile.sqlite"
    app = create_app(path, model_backend="rules")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        bootstrap(client)
        assert client.get("/api/profile").json() == {"origin": "", "preferences": []}
        value = {"origin": "Нижний Новгород", "preferences": ["Без пересадок", "Тихий номер"]}
        assert client.patch("/api/profile", json=value).json() == value
        assert client.patch("/api/profile", json={"origin": "x" * 121}).status_code == 422
        assert client.patch("/api/profile", json={"user_id": "other"}).status_code == 422
        cookies = dict(client.cookies)
    with TestClient(create_app(path, model_backend="rules"), base_url="http://127.0.0.1") as client:
        client.cookies.update(cookies)
        bootstrap(client)
        assert client.get("/api/profile").json() == value
        client.cookies.clear()
        bootstrap(client)
        assert client.get("/api/profile").json() == {"origin": "", "preferences": []}


def test_chunked_json_cannot_bypass_limit_and_config_is_admin_only(tmp_path):
    with TestClient(create_app(tmp_path / "limits.sqlite", model_backend="rules"), base_url="http://127.0.0.1") as client:
        bootstrap(client)
        response = client.post("/api/turns", content=iter([b"x" * 40001, b"x" * 40001]), headers={"Content-Type": "application/json"})
        assert response.status_code == 413
        assert client.post("/api/turns", json=turn_body(text="Москва\x00Ереван")).status_code == 422
        assert client.patch("/api/profile", json={"origin": "Москва\x00"}).status_code == 422
        assert client.post("/api/search/config", json={"api_key": "test", "folder_id": "test"}).status_code == 403
        assert client.get("/api/health").json()["status"] == "ok"


def test_explicit_profile_defaults_new_trip_and_current_request_overrides(tmp_path):
    from travel_agent.interpretation import RuleBasedGateway

    class RecordingGateway(RuleBasedGateway):
        contexts = []

        async def interpret(self, text, request, *args, **kwargs):
            self.contexts.append(request)
            return await super().interpret(text, request, *args, **kwargs)

    gateway = RecordingGateway()
    with TestClient(create_app(tmp_path / "profile-defaults.sqlite", model_backend="rules", gateway=gateway), base_url="http://127.0.0.1") as client:
        bootstrap(client)
        client.patch("/api/profile", json={"origin": "Москва", "preferences": ["Тихий номер"]})
        submit(client, turn_body(session="profile-trip", text=TRIP.replace("Из Москвы в", "В")))
        saved = client.get("/api/sessions/profile-trip").json()["session"]
        assert saved["request"]["origin"] == "Москва"
        assert gateway.contexts[-1]["_profile"]["preferences"] == ["Тихий номер"]
        client.patch("/api/profile", json={"origin": "Казань"})
        submit(client, turn_body(session="override-trip", text=TRIP))
        overridden = client.get("/api/sessions/override-trip").json()["session"]
        assert overridden["request"]["origin"] == "Москва"
        assert client.get("/api/sessions/profile-trip").json()["session"]["request"] == saved["request"]


def test_legacy_public_prose_is_filtered_without_rewriting_receipts(tmp_path):
    import json
    import sqlite3

    database = tmp_path / "legacy-voice.sqlite"
    with TestClient(create_app(database, model_backend="rules"), base_url="http://127.0.0.1") as client:
        bootstrap(client)
        user_text = "На Туту и Яндексе такая же цена?"
        job = submit(client, turn_body(session="legacy", text=user_text))
        raw = job["result"] | {"message": "Яндекс вернул результаты. Я нашёл варианты для поездки.",
            "warnings": ["Инструмент вернул ошибку. Я проверю условия ещё раз."],
            "evidence": ["https://tutu.ru/receipt"], "provider_id": "tutu_123"}
        encoded = json.dumps(raw, ensure_ascii=False)
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE web_jobs SET result=? WHERE job_id=?", (encoded, job["job_id"]))
            connection.execute("UPDATE turns SET result=? WHERE session_id='legacy'", (encoded,))
        public_job = client.get('/api/jobs/' + job['job_id']).json()['result']
        snapshot = client.get('/api/sessions/legacy').json()
        assert public_job['message'] == snapshot['result']['message'] == 'Я нашёл варианты для поездки.'
        assert public_job['warnings'] == ['Я проверю условия ещё раз.']
        assert public_job['evidence'] == ['https://tutu.ru/receipt']
        assert public_job['provider_id'] == 'tutu_123'
        assert snapshot['messages'][0]['text'] == user_text
        assert snapshot['messages'][-1]['text'] == 'Я нашёл варианты для поездки.'
        with sqlite3.connect(database) as connection:
            assert connection.execute('SELECT result FROM web_jobs WHERE job_id=?', (job['job_id'],)).fetchone()[0] == encoded
            assert connection.execute("SELECT result FROM turns WHERE session_id='legacy'").fetchone()[0] == encoded
