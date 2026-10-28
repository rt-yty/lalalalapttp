"""Public service flows for independently selected travel components.

The scripted interpreter isolates state/contract behavior from model wording.
Offers are synthetic and pass through the production normalizers.
"""
from copy import deepcopy
from datetime import date, timedelta
from uuid import uuid4

import pytest

from travel_agent.adapters.normalize import normalize_flight, normalize_hotel, normalize_rail
from travel_agent.adapters.replay import ReplayAdapter
from travel_agent.domain.models import TurnInput
from travel_agent.domain.planning import component_options_from_search
from travel_agent.interpretation import RuleBasedGateway
from travel_agent.service import AgentService

NOW = "2026-10-28T12:00:00+03:00"
REQUEST = {"origin": "Москва", "destination": "Санкт-Петербург", "departure_date": "2026-11-10",
           "return_date": "2026-11-17", "adults": 1, "children_ages": [], "constraints": {},
           "budget_minor": 3_000_000, "budget_scope": "trip", "products": ["avia", "hotel"]}


class ScriptedGateway(RuleBasedGateway):
    def __init__(self, request, decisions=None):
        self.decisions = {"start": {"intent": "plan", "patch": request}, **(decisions or {})}

    async def interpret(self, text, request, now, timezone, skills=None):
        if text in self.decisions:
            return deepcopy(self.decisions[text])
        return await super().interpret(text, request, now, timezone, skills)


class ComponentAdapter:
    def __init__(self, *, missing=(), overnight=False, hotel_error=False):
        self.calls = []
        self.missing, self.overnight, self.hotel_error = set(missing), overnight, hotel_error

    async def capabilities(self):
        return {"products": ["avia", "rail", "hotel"]}

    @staticmethod
    def flight(request, now, *, night=False):
        depart, returning = request["departure_date"], request["return_date"]
        arrival_day = str(date.fromisoformat(depart) + timedelta(days=1)) if night else depart
        departure_at, arrival_at = depart + "T20:00:00+03:00", arrival_day + ("T10:00:00+03:00" if night else "T22:00:00+03:00")
        return_departure, return_arrival = returning + "T12:00:00+03:00", returning + "T14:00:00+03:00"
        fare = {"price": {"amount": 10000 if not night else 9000, "currency": "RUB"},
                "service_class": "Y", "conditions": {"fare_family": "Day" if not night else "Night",
                 "baggage": {"kg": 23}, "refundable": True}}
        offer = {"carriers": ["Synthetic Airlines"], "departure_at": departure_at, "arrival_at": arrival_at,
            "return_departure_at": return_departure, "return_arrival_at": return_arrival,
            "legs": [{"segments": [{"from": request["origin"], "to": request["destination"],
                "voyage_no": "T100" if not night else "T200", "departure_at": departure_at, "arrival_at": arrival_at}]},
                {"segments": [{"from": request["destination"], "to": request["origin"], "voyage_no": "T101",
                    "departure_at": return_departure, "arrival_at": return_arrival}]}]}
        return normalize_flight(offer, fare, request, now, data_origin="synthetic")

    @staticmethod
    def hotel(request, now, hotel_id):
        detail = {"hotel": {"hotel_id": hotel_id, "name": "Hotel " + hotel_id}, "rooms": []}
        room = {"room_name": "Standard", "bed_type": "double"}
        rate = {"price": {"amount": 8000 if hotel_id == "h1" else 12000, "currency": "RUB"},
                "breakfast_included": True, "refundable": True}
        return normalize_hotel(detail, room, rate, request, now, data_origin="synthetic")

    @staticmethod
    def train(request, now):
        def ride(outward):
            day = request["departure_date" if outward else "return_date"]
            depart, arrive = day + "T08:00:00+03:00", day + "T12:00:00+03:00"
            return {"departure_at": depart, "arrival_at": arrive,
                "legs": [{"segments": [{"from": request["origin" if outward else "destination"],
                    "to": request["destination" if outward else "origin"], "departure_at": depart,
                    "arrival_at": arrive, "voyage_no": "001А" if outward else "002А"}]}]}
        fare = {"price": {"amount": 2000, "currency": "RUB"}, "seats_left": 4,
            "service_class": "2К", "seat_category": "COMPARTMENT", "conditions": {"refundable": True}}
        item = normalize_rail(ride(True), fare, ride(False), fare, request, now)
        item["data_origin"] = "synthetic"
        return item

    async def search(self, request, now):
        request = deepcopy(request)
        self.calls.append(request)
        result = {"flights": [], "trains": [], "hotels": [], "component_errors": {},
                  "coverage": [], "warnings": [], "error": None}
        products = request.get("products", ["avia", "hotel"])
        if "avia" in products and "avia" not in self.missing:
            result["flights"] = [self.flight(request, now)]
            if self.overnight:
                result["flights"].append(self.flight(request, now, night=True))
        if "rail" in products and "rail" not in self.missing:
            result["trains"] = [self.train(request, now)]
        if "hotel" in products and "hotel" not in self.missing:
            if self.hotel_error:
                result["component_errors"]["hotel"] = {"code": "timeout", "retryable": True,
                    "message": "Отели временно не ответили."}
            else:
                result["hotels"] = [self.hotel(request, now, hotel_id) for hotel_id in request.get("_hotel_ids", ["h1", "h2"])]
        for kind, key in (("avia", "flights"), ("rail", "trains"), ("hotel", "hotels")):
            if kind in products:
                result["coverage"].append({"product": kind, "count": len(result[key]), "complete": True})
        if result["component_errors"] and not any(result[k] for k in ("flights", "trains", "hotels")):
            result["error"] = next(iter(result["component_errors"].values()))
        return result

    async def refresh(self, components, request, now):
        return {"status": "unchanged", "components": deepcopy(components), "warnings": [], "error": None}


@pytest.fixture
def make_service(tmp_path):
    opened = []
    def make(request=None, *, adapter=None, decisions=None, path=None):
        service = AgentService(db_path=path or tmp_path / (str(uuid4()) + ".sqlite"), data_mode="live",
            checkout_mode="disabled", gateway=ScriptedGateway(request or deepcopy(REQUEST), decisions),
            adapter=adapter or ComponentAdapter())
        opened.append(service)
        return service
    yield make
    for service in opened:
        service.close()


def turn(version=0, text="start", *, action="message", payload=None):
    return TurnInput(request_id=str(uuid4()), user_id="component-user", session_id="trip",
        expected_state_version=version, input=action, payload=payload or {"text": text}, now=NOW)


async def select(service, result, card):
    return await service.run_turn(turn(result.state_version, action="select_component",
        payload={"component_id": card["component_id"], "options_version": card["options_version"]}))


async def assembled(service):
    found = await service.run_turn(turn())
    assert found.status == "options_ready", found.model_dump()
    transport = await select(service, found, found.transport_options[0])
    assert transport.status == "plan_ready", transport.model_dump()
    complete = await select(service, transport, transport.hotel_options[0])
    assert complete.status == "plan_ready", complete.model_dump()
    assert complete.current_plan["complete"] is True
    return complete


async def test_hotel_only_works_without_origin(make_service):
    adapter = ComponentAdapter()
    service = make_service({**REQUEST, "products": ["hotel"], "origin": None}, adapter=adapter)
    found = await service.run_turn(turn())
    assert found.status == "options_ready" and found.hotel_options and not found.transport_options
    assert found.questions == []
    selected = await select(service, found, found.hotel_options[0])
    assert selected.current_plan["complete"] and selected.current_plan["total_minor"] == 800_000
    assert adapter.calls[0]["products"] == ["hotel"]


async def test_transport_only_is_complete_without_hotel(make_service):
    adapter = ComponentAdapter()
    service = make_service({**REQUEST, "products": ["avia"]}, adapter=adapter)
    found = await service.run_turn(turn())
    selected = await select(service, found, found.transport_options[0])
    assert selected.current_plan["complete"] and not selected.hotel_options
    assert selected.current_plan["total_minor"] == 1_000_000
    assert adapter.calls[0]["products"] == ["avia"]


async def test_independent_selection_builds_total_and_missing_component_state(make_service):
    service = make_service()
    found = await service.run_turn(turn())
    selected = await select(service, found, found.transport_options[0])
    assert selected.current_plan["missing_products"] == ["hotel"]
    assert selected.current_plan["complete"] is False
    complete = await select(service, selected, selected.hotel_options[0])
    assert complete.current_plan["complete"] and complete.current_plan["eligibility"] == "satisfied"
    assert complete.current_plan["total_minor"] == 1_800_000
    assert {c["product_type"] for c in complete.selected_components} == {"avia", "hotel"}


async def test_combined_budget_rejects_incompatible_second_component(make_service):
    service = make_service({**REQUEST, "budget_minor": 1_500_000})
    found = await service.run_turn(turn())
    assert found.transport_options and found.hotel_options
    selected = await select(service, found, found.transport_options[0])
    denied = await select(service, selected, selected.hotel_options[0])
    assert denied.status == "partial"
    state = await service.get_session("component-user", "trip")
    assert [c["product_type"] for c in state.selected_components] == ["avia"]
    assert state.current_plan["total_minor"] == 1_000_000
    assert state.request.budget_minor == 1_500_000


@pytest.mark.parametrize("missing,remaining", [("avia", "hotel_options"), ("hotel", "transport_options")])
async def test_missing_component_keeps_successful_results(make_service, missing, remaining):
    service = make_service(adapter=ComponentAdapter(missing=[missing]))
    found = await service.run_turn(turn())
    assert found.status == "partial", found.model_dump()
    assert getattr(found, remaining)
    selected = await select(service, found, getattr(found, remaining)[0])
    assert selected.current_plan and selected.current_plan["complete"] is False


async def test_dependency_failure_keeps_transport_and_explains_hotel_error(make_service):
    service = make_service(adapter=ComponentAdapter(hotel_error=True))
    found = await service.run_turn(turn())
    assert found.status == "partial" and found.transport_options and not found.hotel_options
    assert found.component_errors["hotel"]["code"] == "timeout"
    assert "поиск отелей" in found.message.lower() and "не успел завершить" in found.message.lower()
    assert "не нашлось" not in found.message.lower()


async def test_night_transport_fetches_hotel_for_actual_arrival(make_service):
    adapter = ComponentAdapter(overnight=True)
    service = make_service(adapter=adapter)
    found = await service.run_turn(turn())
    night = next(c for c in found.transport_options if c["arrival_at"].startswith("2026-11-11"))
    selected = await select(service, found, night)
    assert adapter.calls[-1]["products"] == ["hotel"]
    assert adapter.calls[-1]["departure_date"] == "2026-11-11"
    assert all(c["query"]["departure_date"] == "2026-11-11" for c in selected.hotel_options)
    complete = await select(service, selected, selected.hotel_options[0])
    assert complete.current_plan["eligibility"] == "satisfied" and complete.current_plan["complete"]


async def test_keep_transport_only_researches_hotels(make_service):
    adapter = ComponentAdapter()
    service = make_service(adapter=adapter, decisions={"breakfast": {"intent": "replan", "keep_transport": True,
        "patch": {"constraints": {"breakfast": True}}}})
    complete = await assembled(service)
    old = next(c for c in complete.selected_components if c["product_type"] == "avia")
    count = len(adapter.calls)
    updated = await service.run_turn(turn(complete.state_version, "breakfast"))
    assert len(adapter.calls) == count + 1 and adapter.calls[-1]["products"] == ["hotel"]
    kept = next(c for c in updated.selected_components if c["product_type"] == "avia")
    assert kept["provider_refs"] == old["provider_refs"] and kept["price_minor"] == old["price_minor"]


async def test_keep_hotel_only_researches_transport(make_service):
    adapter = ComponentAdapter()
    service = make_service(adapter=adapter, decisions={"baggage": {"intent": "replan", "keep_hotel": True,
        "patch": {"constraints": {"baggage_kg": 23}}}})
    complete = await assembled(service)
    old = next(c for c in complete.selected_components if c["product_type"] == "hotel")
    count = len(adapter.calls)
    updated = await service.run_turn(turn(complete.state_version, "baggage"))
    assert len(adapter.calls) == count + 1 and adapter.calls[-1]["products"] == ["avia"]
    kept = next(c for c in updated.selected_components if c["product_type"] == "hotel")
    assert kept["provider_refs"] == old["provider_refs"]


async def test_kept_hotel_is_addressed_by_id_when_dates_change(make_service):
    adapter = ComponentAdapter()
    service = make_service(adapter=adapter, decisions={"dates": {"intent": "replan", "keep_hotel": True,
        "patch": {"departure_date": "2026-11-12", "return_date": "2026-11-19"}}})
    complete = await assembled(service)
    old = next(c for c in complete.selected_components if c["product_type"] == "hotel")
    updated = await service.run_turn(turn(complete.state_version, "dates"))
    assert adapter.calls[-1]["_hotel_ids"] == [old["provider_refs"]["hotel_id"]]
    assert adapter.calls[-1]["departure_date"] == "2026-11-12"
    assert updated.hotel_options and all(c["provider_refs"]["hotel_id"] == old["provider_refs"]["hotel_id"] for c in updated.hotel_options)
    assert all(c["query"]["departure_date"] == "2026-11-12" for c in updated.hotel_options)
    assert all(c["query"]["departure_date"] != "2026-11-10" for c in updated.selected_components)


async def test_remove_hotel_preserves_transport_and_missing_product(make_service):
    service = make_service()
    complete = await assembled(service)
    removed = await service.run_turn(turn(complete.state_version, action="remove_component", payload={"product_type": "hotel"}))
    assert removed.status == "plan_ready"
    assert [c["product_type"] for c in removed.selected_components] == ["avia"]
    assert removed.current_plan["missing_products"] == ["hotel"]
    assert removed.current_plan["total_minor"] == 1_000_000
    assert removed.pending_action is None


async def test_restart_restores_component_ids_and_partial_selection(make_service, tmp_path):
    path = tmp_path / "restart.sqlite"
    service = make_service(path=path)
    found = await service.run_turn(turn())
    selected = await select(service, found, found.hotel_options[0])
    restarted = make_service(path=path)
    state = await restarted.get_session("component-user", "trip")
    assert state.selected_components == selected.selected_components
    assert state.transport_options == selected.transport_options
    complete = await select(restarted, selected, state.transport_options[0])
    assert complete.current_plan["complete"] and complete.current_plan["total_minor"] == 1_800_000


async def test_stale_component_version_cannot_replace_selected_component(make_service):
    service = make_service()
    complete = await assembled(service)
    state_before = await service.get_session("component-user", "trip")
    card = complete.hotel_options[-1]
    refused = await service.run_turn(turn(complete.state_version, action="select_component", payload={
        "component_id": card["component_id"], "options_version": card["options_version"] - 1}))
    assert refused.status == "error" and refused.error["code"] == "stale_options"
    state_after = await service.get_session("component-user", "trip")
    assert state_after.selected_components == state_before.selected_components
    assert state_after.current_plan == state_before.current_plan


async def test_rail_selection_never_silently_becomes_avia(make_service):
    adapter = ComponentAdapter()
    service = make_service({**REQUEST, "products": ["avia", "rail", "hotel"]}, adapter=adapter)
    found = await service.run_turn(turn())
    rail = next(c for c in found.transport_options if c["product_type"] == "rail")
    selected = await select(service, found, rail)
    complete = await select(service, selected, selected.hotel_options[0])
    assert {c["product_type"] for c in complete.selected_components} == {"rail", "hotel"}
    assert complete.current_plan["total_minor"] == 1_200_000
    assert complete.current_plan["missing_products"] == []
    assert all(c["product_type"] != "avia" for c in complete.current_plan["components"])


async def test_real_replay_adapter_supports_independent_hotel_selection(make_service):
    adapter = ReplayAdapter()
    request = {**REQUEST, **adapter.fixture["request"], "products": ["hotel"], "origin": None,
               "budget_minor": 20_000_000}
    service = make_service(request, adapter=adapter)
    found = await service.run_turn(turn())
    assert found.status == "options_ready" and found.hotel_options and not found.transport_options
    selected = await select(service, found, found.hotel_options[0])
    assert selected.current_plan["complete"]
    assert selected.current_plan["components"][0]["data_origin"] == "recorded_fixture"


async def test_keep_hotel_before_hotel_selection_does_not_crash_or_erase_transport(make_service):
    service = make_service(decisions={"keep-unselected": {"intent": "replan", "keep_hotel": True,
        "patch": {"constraints": {"baggage_kg": 23}}}})
    found = await service.run_turn(turn())
    selected = await select(service, found, found.transport_options[0])
    changed = await service.run_turn(turn(selected.state_version, "keep-unselected"))
    assert changed.status != "error", changed.model_dump()
    state = await service.get_session("component-user", "trip")
    assert state.request.constraints.get("baggage_kg") == 23
    assert any(c["product_type"] == "avia" for c in state.selected_components)


async def test_failed_search_for_new_dates_does_not_keep_old_quotes_selected(make_service):
    adapter = ComponentAdapter()
    service = make_service(adapter=adapter, decisions={"new-dates": {"intent": "replan",
        "patch": {"departure_date": "2026-11-12", "return_date": "2026-11-19"}}})
    complete = await assembled(service)
    adapter.missing = {"avia"}
    adapter.hotel_error = True
    failed = await service.run_turn(turn(complete.state_version, "new-dates"))
    assert failed.status == "partial" and failed.error
    state = await service.get_session("component-user", "trip")
    assert str(state.request.departure_date) == "2026-11-12"
    assert state.current_plan is None
    assert state.selected_components == [], "Old-date fares must not remain selected after a failed search for new dates"


def test_hotel_cards_prioritize_distinct_hotels_then_keep_alternative_tariffs():
    request = {**REQUEST, "products": ["hotel"]}
    hotels = []
    # Many inexpensive rooms in one hotel must not hide other properties.
    # Two properties deliberately share a display name: hotel_id is identity.
    for hotel_id, amounts in (("a", range(100, 1100, 100)), ("b", [2100, 2000]), ("c", [3000])):
        for amount in amounts:
            hotels.append(normalize_hotel(
                {"hotel": {"hotel_id": hotel_id, "name": "Same hotel name"}, "rooms": []},
                {"room_name": f"Room {amount}", "bed_type": "double"},
                {"price": {"amount": amount, "currency": "RUB"}, "refundable": True},
                request, NOW, data_origin="synthetic"))
    # Unsorted input and a repeated provider row must not alter ranking/dedup.
    hotels.reverse()
    hotels.append(deepcopy(hotels[-1]))
    original = deepcopy(hotels)
    cards = component_options_from_search({"hotels": hotels}, request, 7)["hotel_options"]
    assert len(cards) == 8
    assert [c["provider_refs"]["hotel_id"] for c in cards[:3]] == ["a", "b", "c"]
    assert [c["price_minor"] for c in cards[:3]] == [10_000, 200_000, 300_000]
    assert [c["price_minor"] for c in cards[3:]] == [20_000, 30_000, 40_000, 50_000, 60_000]
    assert all(c["provider_refs"]["hotel_id"] == "a" for c in cards[3:])
    assert len({c["component_id"] for c in cards}) == len(cards)
    assert all(c["options_version"] == 7 for c in cards)
    assert hotels == original
