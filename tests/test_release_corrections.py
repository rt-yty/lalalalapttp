"""User-reported gaps: real map facts and a saved plan without fictitious orders."""
from copy import deepcopy
import pytest
from travel_agent.adapters.normalize import normalize_hotel
from travel_agent.service import AgentService
from test_component_flow import ComponentAdapter, ScriptedGateway, REQUEST, NOW, assembled, turn


@pytest.mark.parametrize("lat,lng", [(40.1949, 44.54219), (0, 0), (-90, 180)])
def test_hotel_coordinates_are_preserved_from_actual_data(lat, lng):
    detail = {"hotel": {"hotel_id": "h", "name": "Hotel", "location": {"lat": lat, "lng": lng}, "address": "Address"}}
    card = normalize_hotel(detail, {"room_name": "Room"}, {"price": {"amount": 100, "currency": "RUB"}}, REQUEST, NOW)
    assert card["location"] == {"latitude": lat, "longitude": lng, "address": "Address"}


@pytest.mark.parametrize("location", [{}, {"lat": 91, "lng": 10}, {"lat": 10, "lng": 181},
    {"lat": float("nan"), "lng": 10}, {"lat": 10, "lng": float("inf")}, {"lat": True, "lng": 10},
    {"lat": "40.1", "lng": "44.5"}])
def test_invalid_coordinates_are_not_replaced_with_invented_markers(location):
    detail = {"hotel": {"hotel_id": "h", "location": location}}
    card = normalize_hotel(detail, {}, {"price": {"amount": 100, "currency": "RUB"}}, REQUEST, NOW)
    assert "location" not in card


async def test_finish_plan_does_not_create_order_or_confirmation(tmp_path):
    service = AgentService(db_path=tmp_path / "plan.sqlite", data_mode="live", checkout_mode="disabled",
        gateway=ScriptedGateway(deepcopy(REQUEST)), adapter=ComponentAdapter())
    try:
        selected = await assembled(service)
        result = await service.run_turn(turn(selected.state_version, "Оформить"))
        assert result.status == "plan_ready"
        assert result.current_plan["total_minor"] == selected.current_plan["total_minor"]
        assert result.demo_order is None and result.pending_action is None and result.handoff is None
        assert "демо" not in result.message.casefold() and "забронирован" not in result.message.casefold()
        assert not service.repo.conn.execute("SELECT * FROM demo_orders").fetchall()
        again = await service.run_turn(turn(result.state_version, "Оформить"))
        assert again.current_plan == result.current_plan
        assert again.demo_order is None and again.pending_action is None
    finally:
        service.close()


def test_hotel_keeps_verified_address_when_coordinates_are_absent():
    detail = {"hotel": {"hotel_id": "h", "name": "Hotel", "address": "23 Avenue de la Porte de Châtillon, Paris"}}
    card = normalize_hotel(detail, {}, {"price": {"amount": 100, "currency": "RUB"}}, REQUEST, NOW)
    assert card["location"] == {"address": detail["hotel"]["address"]}
