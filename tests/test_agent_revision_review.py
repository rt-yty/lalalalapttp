"""Independent regressions for partial choices, undo, and scoped constraints."""
from copy import deepcopy

import pytest

from test_component_flow import (
    ComponentAdapter, NOW, REQUEST, assembled, make_service, select, turn,
)
from travel_agent.domain.planning import component_options_from_search


async def test_undo_can_restore_the_previous_partial_choice(make_service):
    service = make_service()
    full = await assembled(service)
    undone = await service.run_turn(turn(full.state_version, action="undo", payload={}))
    assert undone.status == "plan_ready", undone.message
    assert undone.current_plan["missing_products"] == ["hotel"]
    assert [item["product_type"] for item in undone.selected_components] == ["avia"]


async def test_undo_does_not_leave_new_date_cards_selectable_for_restored_trip(make_service):
    service = make_service(decisions={"later": {"intent": "replan", "patch": {
        "departure_date": "2026-11-12", "return_date": "2026-11-19"}}})
    full = await assembled(service)
    later = await service.run_turn(turn(full.state_version, "later"))
    assert later.transport_options[0]["departure_at"].startswith("2026-11-12")
    undone = await service.run_turn(turn(later.state_version, action="undo", payload={}))
    assert undone.status == "plan_ready", undone.message
    assert undone.current_plan["request"]["departure_date"] == "2026-11-10"
    assert all(item["departure_at"].startswith("2026-11-10") for item in undone.transport_options), \
        "Undo must clear or regenerate cards for the restored dates"


async def test_old_transport_card_cannot_be_selected_while_new_dates_need_clarification(make_service):
    service = make_service(decisions={"new-date": {"intent": "replan", "reset_dates": True,
        "patch": {"departure_date": "2026-11-12"}}})
    found = await service.run_turn(turn())
    old_card = deepcopy(found.transport_options[0])
    waiting = await service.run_turn(turn(found.state_version, "new-date"))
    assert waiting.status == "needs_input"
    selected = await select(service, waiting, old_card)
    state = await service.get_session("component-user", "trip")
    assert not state.selected_components, "A partial plan must not accept an old-date transport card"
    assert selected.status in ("error", "needs_input", "partial")


@pytest.mark.parametrize("kind,unrelated", [("rail", "flight_refundable"), ("avia", "rail_refundable")])
async def test_product_specific_refund_condition_does_not_filter_another_transport(kind, unrelated):
    request = {**REQUEST, "products": [kind], "constraints": {unrelated: True}}
    adapter = ComponentAdapter()
    found = await adapter.search(request, NOW)
    key = "trains" if kind == "rail" else "flights"
    found[key][0]["conditions"]["refundable"] = False
    cards = component_options_from_search(found, request, 1)
    assert cards["transport_options"], f"{unrelated} must not become a hard condition of {kind}"
