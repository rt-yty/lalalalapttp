"""A search timestamp is not a weather observation timestamp."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from travel_agent.research import answer_from_search, render_research_answer

@pytest.mark.parametrize("query", ["Погода в Казани сейчас", "Какая сейчас температура в Сочи?", "Kazan current weather"])
@pytest.mark.parametrize("page_status", ["read", "unavailable"])
async def test_current_weather_does_not_turn_search_time_into_an_observation(query, page_status):
    search = {"query": query, "fetched_at": "2026-10-28T12:45:00+03:00", "results": [
        {"id":"weather", "title":"Погода сейчас", "url":"https://weather.example/kazan", "snippet":"Прогноз на десять дней: от +1 до +10 градусов.",
         "page_status":page_status, "page_text":"Прогноз на 28 октября 2026. Сегодня +11 градусов."}]}
    raw = {"message":"Сейчас +11 градусов, два наблюдения на 12:45.", "source_ids":["weather"]}
    model = AsyncMock(return_value=raw)
    answer = await answer_from_search(SimpleNamespace(research=model), search, [], "2026-10-28T12:45:00+03:00")
    assert "не могу надёжно подтвердить" in answer["message"]
    assert not any(value in answer["message"] for value in ("+11", "12:45", "два наблюдения"))
    assert answer["source_ids"] == ["weather"]
    assert "не могу надёжно подтвердить" in render_research_answer(raw, search)["message"]
    model.assert_not_awaited()

async def test_original_current_weather_question_survives_a_rewritten_search_query():
    search={"query":"Казань погода 28 октября 2026", "results":[]}
    answer=await answer_from_search(object(), search, [{"role":"user", "text":"Какая погода в Казани сейчас?"}], "2026-10-28T12:45:00+03:00")
    assert "не могу надёжно подтвердить" in answer["message"]
    assert answer["source_ids"]==[]

@pytest.mark.parametrize("query", ["Прогноз погоды на завтра в Казани", "Погода в Сочи в ноябре", "Расписание концертов сейчас"])
async def test_forecast_and_non_weather_research_remain_available(query):
    search={"query":query, "results":[{"id":"source", "title":"Материал", "url":"https://travel.example/article", "snippet":"Подтверждённый материал", "page_status":"read", "page_text":"Подтверждённый материал"}]}
    model=AsyncMock(return_value={"message":"Ответ по найденным данным.", "source_ids":["source"]})
    answer=await answer_from_search(SimpleNamespace(research=model), search, [], "2026-10-28T12:45:00+03:00")
    assert answer["message"]=="Ответ по найденным данным."
    model.assert_awaited_once()
