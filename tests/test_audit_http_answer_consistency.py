"""A factual follow-up must survive poll, session read and browser reload."""
import pytest
from fastapi.testclient import TestClient
from travel_agent.web import create_app
from test_component_flow import REQUEST, ScriptedGateway, ComponentAdapter
from test_web import submit, turn_body
from http_helpers import bootstrap

@pytest.mark.parametrize("requirement,question", [
    ("Обязательно доступный вход без ступенек для человека на коляске.", "Подтверждён доступный вход для коляски?"),
    ("Обязательно тихий номер подальше от лифта.", "Подтверждено, что номера тихие и не рядом с лифтом?"),
])
def test_factual_answer_is_identical_in_job_history_and_reopened_session(tmp_path,requirement,question):
    database=tmp_path/"consistent.sqlite"
    text="Нужен только отель. "+requirement
    trip={**REQUEST,"products":["hotel"]}
    gateway=ScriptedGateway(trip,{text:{"intent":"plan","patch":trip},question:{"intent":"question","patch":{},"question_text":question}})
    def app():
        return create_app(db_path=database,model_backend="rules",data_mode="replay",checkout_mode="disabled",gateway=gateway,adapter=ComponentAdapter())
    with TestClient(app(),base_url="http://127.0.0.1") as client:
        bootstrap(client)
        first=submit(client,turn_body(session="hotel",text=text))["result"]
        assert first["status"]=="partial"
        job=submit(client,turn_body(session="hotel",text=question,version=first["state_version"]))
        expected=job["result"]["message"]
        assert expected!=first["message"]
        assert "не подтвердил" in expected
        saved=client.get("/api/sessions/hotel").json()
        assert saved["result"]["message"]==expected
        assert saved["messages"][-1]["text"]==expected
        assert saved["session"]["dialogue"][-1]["text"]==expected
        assert saved["result"]["suggestions"]==job["result"]["suggestions"]
        cookies=dict(client.cookies)
    with TestClient(app(),base_url="http://127.0.0.1") as client:
        for name,value in cookies.items():client.cookies.set(name,value,domain="127.0.0.1",path="/")
        reloaded=client.get("/api/sessions/hotel").json()
        assert reloaded["result"]["message"]==expected
        assert reloaded["messages"][-1]["text"]==expected
        assert client.get("/api/jobs/"+job["job_id"]).json()["result"]["message"]==expected
