"""Provider isolation and exact hotel refresh using the inspected MCP schemas."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from travel_agent.adapters.tutu_mcp import TutuAdapter, AdapterError
from travel_agent.adapters.replay import ReplayAdapter

NOW = "2026-10-28T12:00:00+03:00"
REQUEST = {"origin": "Москва", "destination": "Санкт-Петербург", "departure_date": "2026-11-10",
           "return_date": "2026-11-17", "adults": 2, "children_ages": [], "constraints": {}}


def flight():
    return {"carriers": ["Тест"], "departure_at": "2026-11-10T08:00:00+03:00",
        "arrival_at": "2026-11-10T10:00:00+03:00", "return_departure_at": "2026-11-17T12:00:00+03:00",
        "return_arrival_at": "2026-11-17T14:00:00+03:00", "legs": [],
        "variants": [{"price": {"amount": 10000, "currency": "RUB"}, "service_class": "Y",
                      "conditions": {"fare_family": "Base", "baggage": {"kg": 23}}}]}


def hotel(hotel_id):
    return {"hotel": {"hotel_id": hotel_id, "name": "Отель " + hotel_id},
        "rooms": [{"room_name": "Standard", "bed_type": "double", "rates": [
            {"price": {"amount": 8000, "currency": "RUB"}, "breakfast_included": True,
             "refundable": True}]}]}


class SchemaAdapter(TutuAdapter):
    def __init__(self, fail=None, pages=1):
        super().__init__(max_pages=pages)
        self.calls = []
        self.fail = fail or {}

    async def _rpc(self, method, params):
        if method == "tools/list":
            return json.loads((Path(__file__).parent / "fixtures/travel-tool-schemas.json").read_text())
        name, args = params["name"], params["arguments"]
        self.calls.append((name, deepcopy(args)))
        failure = self.fail.get((name, args.get("page"))) or self.fail.get((name, args.get("hotel_id"))) or self.fail.get(name)
        if failure:
            raise AdapterError("timeout", "Тестовый отказ", retryable=True)
        if name.endswith("_instructions"):
            return "inspected instructions"
        if name == "search_avia":
            return {"offers": [flight()], "meta": {"has_more": self.max_pages > 1}}
        if name == "search_hotels":
            return {"hotels": [{"hotel_id": "first"}, {"hotel_id": "second"}], "meta": {"has_more": False}}
        if name == "get_offer_details":
            return hotel(args["hotel_id"])
        raise AssertionError(name)


async def test_hotel_only_does_not_need_origin_or_call_transport():
    adapter = SchemaAdapter()
    result = await adapter.search({**REQUEST, "origin": None, "products": ["hotel"]}, NOW)
    assert len(result["hotels"]) == 2 and not result["flights"] and result["error"] is None
    assert not any(name in {"search_avia", "search_rail", "get_avia_instructions"} for name, _ in adapter.calls)


async def test_transport_only_never_fetches_hotels():
    adapter = SchemaAdapter()
    result = await adapter.search({**REQUEST, "products": ["avia"]}, NOW)
    assert result["flights"] and result["hotels"] == [] and result["error"] is None
    assert not any("hotel" in name for name, _ in adapter.calls)


@pytest.mark.parametrize("failed,remaining,component", [("search_avia", "hotels", "avia"), ("search_hotels", "flights", "hotel")])
async def test_product_failure_preserves_other_product(failed, remaining, component):
    result = await SchemaAdapter(fail={failed: True}).search(REQUEST, NOW)
    assert result[remaining] and result["error"] is None
    assert result["component_errors"][component]["code"] == "timeout"


async def test_one_broken_hotel_does_not_discard_other_details():
    result = await SchemaAdapter(fail={("get_offer_details", "first"): True}).search({**REQUEST, "products": ["hotel"]}, NOW)
    assert [h["provider_refs"]["hotel_id"] for h in result["hotels"]] == ["second"]
    assert result["component_errors"]["hotel"]["code"] == "timeout"
    assert result["error"] is None


async def test_page_two_failure_keeps_first_page_and_marks_coverage():
    result = await SchemaAdapter(fail={("search_avia", 2): True}, pages=2).search({**REQUEST, "products": ["avia"]}, NOW)
    assert result["flights"] and result["error"] is None
    assert result["coverage"][0]["pages"] == [1]
    assert result["coverage"][0]["complete"] is False
    assert result["component_errors"]["avia"]["code"] == "timeout"


async def test_selected_hotel_uses_id_and_new_dates_without_listing():
    adapter = SchemaAdapter()
    result = await adapter.search({**REQUEST, "products": ["hotel"], "_hotel_ids": ["not-in-list"],
                                   "departure_date": "2026-11-11"}, NOW)
    assert result["hotels"][0]["provider_refs"]["hotel_id"] == "not-in-list"
    assert result["hotels"][0]["query"]["departure_date"] == "2026-11-11"
    assert not any(name == "search_hotels" for name, _ in adapter.calls)


async def test_refresh_does_not_depend_on_hotel_search_order():
    adapter = SchemaAdapter()
    request = {**REQUEST, "products": ["hotel"], "_hotel_ids": ["chosen"]}
    selected = (await adapter.search(request, NOW))["hotels"]
    adapter.calls.clear()
    refreshed = await adapter.refresh(selected, request, NOW)
    assert refreshed["status"] == "unchanged"
    assert [a["hotel_id"] for n, a in adapter.calls if n == "get_offer_details"] == ["chosen"]
    assert not any(n in ("search_hotels", "search_avia") for n, _ in adapter.calls)


async def test_replay_filters_components_but_does_not_invent_rail():
    adapter = ReplayAdapter()
    request = {**adapter.fixture["request"], "products": ["hotel"], "origin": None}
    result = await adapter.search(request, NOW)
    assert result["hotels"] and not result["flights"] and result["error"] is None
    result = await adapter.search({**adapter.fixture["request"], "products": ["rail", "hotel"]}, NOW)
    assert result["hotels"] and not result["trains"] and result["error"] is None
    assert result["component_errors"]["rail"]["code"] == "fixture_missing"
