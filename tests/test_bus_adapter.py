"""Bus pricing, return pairing and selection are checked independently of LLMs."""
from copy import deepcopy
from datetime import datetime
from uuid import uuid4

import pytest

from travel_agent.adapters.normalize import normalize_bus
from travel_agent.domain.models import TripRequest, TurnInput
from travel_agent.domain.planning import component_options_from_search
from travel_agent.service import AgentService
from test_component_adapter import NOW, REQUEST, SchemaAdapter


def bus(direction, price=1725):
    outward = direction == "outbound"
    day = "2026-11-10" if outward else "2026-11-17"
    depart, arrive = day + "T10:00:00+03:00", day + "T17:00:00+03:00"
    return {"departure_at": depart, "arrival_at": arrive,
        "legs": [{"segments": [{"from": "Автовокзал A" if outward else "Автовокзал Б",
            "to": "Автовокзал Б" if outward else "Автовокзал A", "carrier": "Перевозчик",
            "departure_at": depart, "arrival_at": arrive}]}],
        "variants": [{"variant_id": direction, "price": {"amount": price, "currency": "RUB"}}]}


class BusAdapter(SchemaAdapter):
    def __init__(self, basis="party_total", malformed=False):
        super().__init__()
        self.basis, self.malformed, self.price = basis, malformed, 1725

    async def _rpc(self, method, params):
        if method == "tools/call" and params["name"] == "search_bus":
            args = params["arguments"]
            self.calls.append(("search_bus", deepcopy(args)))
            offer = bus("outbound" if args["origin"] == "Москва" else "inbound", self.price)
            if self.malformed:
                offer["legs"][0]["segments"][0]["from"] = None
            return {"offers": [offer], "meta": {"has_more": False, "pricing": {"basis": self.basis}}}
        return await super()._rpc(method, params)


@pytest.mark.parametrize("adults", [1, 2, 6, 8])
async def test_bus_price_is_already_party_total(adults):
    adapter = BusAdapter()
    result = await adapter.search({**REQUEST, "adults": adults, "products": ["bus"]}, NOW)
    assert result["error"] is None and len(result["buses"]) == 1
    item = result["buses"][0]
    assert item["price_minor"] == 345000
    assert item["conditions"]["passengers"] == adults
    assert item["conditions"]["baggage_kg"] is None
    assert item["conditions"]["refundable"] is None
    assert item["conditions"]["exact_seats_verified"] is False
    calls = [a for n, a in adapter.calls if n == "search_bus"]
    assert len(calls) == 2 and all(c["adults"] == adults for c in calls)
    assert calls[1]["departure_date"] == REQUEST["return_date"]
    assert calls[1]["destination"] == calls[0]["origin"]


@pytest.mark.parametrize("party", [{"adults": 9}, {"children_ages": [6]}])
async def test_bus_unsupported_party_preserves_hotels(party):
    adapter = BusAdapter()
    result = await adapter.search({**REQUEST, **party, "products": ["bus", "hotel"]}, NOW)
    assert not result["buses"]
    if party.get("children_ages"):
        assert result["hotels"] and result["error"] is None
    assert result["component_errors"]["bus"]["code"] == "unsupported_party"
    assert not any(n == "search_bus" for n, _ in adapter.calls)


@pytest.mark.parametrize("basis", [None, "per_seat", "unknown"])
async def test_bus_unconfirmed_price_basis_is_rejected(basis):
    result = await BusAdapter(basis).search({**REQUEST, "products": ["bus"]}, NOW)
    assert not result["buses"]
    assert result["component_errors"]["bus"]["code"] == "schema_changed"


async def test_bus_missing_stops_are_not_invented():
    result = await BusAdapter(malformed=True).search({**REQUEST, "products": ["bus"]}, NOW)
    assert not result["buses"]
    assert all(not c["complete"] for c in result["coverage"])


async def test_refresh_detects_bus_price_change():
    adapter = BusAdapter()
    found = await adapter.search({**REQUEST, "products": ["bus"]}, NOW)
    assert (await adapter.refresh(found["buses"], REQUEST, NOW))["status"] == "unchanged"
    adapter.price = 2000
    refreshed = await adapter.refresh(found["buses"], REQUEST, NOW)
    assert refreshed["status"] == "changed"
    assert refreshed["components"][0]["price_minor"] == 400000


@pytest.mark.parametrize("fault", ["timezone", "backwards", "date", "segment_time", "connection"])
def test_bus_bad_schedules_are_rejected(fault):
    out, back = bus("outbound"), bus("inbound")
    if fault == "timezone": out["departure_at"] = "2026-11-10T10:00:00"
    if fault == "backwards": out["arrival_at"] = "2026-11-10T09:00:00+03:00"
    if fault == "date": out["departure_at"] = "2026-11-11T10:00:00+03:00"
    if fault == "segment_time": out["legs"][0]["segments"][0]["departure_at"] = "wrong"
    if fault == "connection": out["legs"][0]["segments"] *= 2
    with pytest.raises(ValueError):
        normalize_bus(out, out["variants"][0], back, back["variants"][0], REQUEST, NOW)


class BusGateway:
    async def interpret(self, text, *args, **kwargs):
        if text == "Оформить":
            return {"intent": "checkout", "patch": {}}
        return {"intent": "plan", "patch": {**REQUEST, "products": ["bus"]}}


async def test_bus_end_to_end_selection_and_explicit_demo_confirmation(tmp_path):
    service = AgentService(db_path=tmp_path / "bus.sqlite", gateway=BusGateway(), adapter=BusAdapter(), data_mode="live")
    session, version = str(uuid4()), 0
    async def run(kind, payload):
        nonlocal version
        result = await service.run_turn(TurnInput(request_id=str(uuid4()), user_id="bus-test", session_id=session,
            expected_state_version=version, input=kind, payload=payload, now=datetime.fromisoformat(NOW)))
        version = result.state_version
        return result
    try:
        found = await run("message", {"text": "Автобус из Москвы в Санкт-Петербург с 10 по 17 ноября 2026, двое взрослых"})
        assert found.status == "options_ready" and found.transport_options
        card = found.transport_options[0]
        selected = await run("select_component", {"component_id": card["component_id"], "options_version": card["options_version"]})
        assert selected.current_plan["complete"] is True
        assert selected.current_plan["total_minor"] == 345000
        assert selected.selected_components[0]["product_type"] == "bus"
        assert selected.demo_order is None
        pending = await run("message", {"text": "Оформить"})
        assert pending.status == "needs_confirmation"
        action = {key: pending.pending_action[key] for key in ("action_id", "plan_id", "plan_version", "payload_hash")}
        ordered = await run("confirm_action", action)
        assert ordered.status == "completed_demo" and ordered.demo_order["total_minor"] == 345000
        removed = await run("remove_component", {"product_type": "bus"})
        assert not removed.selected_components
    finally:
        service.close()
