"""Replay integration scenarios against the public AgentService contract.

The deterministic gateway exercises state and money logic without a paid model.
These checks do not claim to validate natural-language understanding of a live model.
"""

from uuid import uuid4

import pytest

from travel_agent.domain.models import TurnInput
from travel_agent.interpretation import RuleBasedGateway
from travel_agent.service import AgentService

NOW = "2026-10-27T12:00:00+03:00"
FULL_TRIP = (
    "Из Москвы в Ереван с 24 ноября по 1 декабря 2026, один взрослый, "
    "общий бюджет 80 тысяч рублей на перелёт и отель"
)


def service_at(path, **kwargs):
    return AgentService(db_path=path, data_mode="replay", checkout_mode="demo",
                        gateway=RuleBasedGateway(), **kwargs)


@pytest.fixture
def service(tmp_path):
    return service_at(tmp_path / "agent.sqlite")


def make_turn(version=0, text=FULL_TRIP, *, kind="message", payload=None,
              user="alice", session="trip-1", request_id=None, now=NOW):
    return TurnInput(
        request_id=request_id or str(uuid4()), user_id=user, session_id=session,
        expected_state_version=version, input=kind,
        payload=payload if payload is not None else {"text": text},
        now=now, timezone="Europe/Moscow",
    )


async def ready_plan(service):
    options = await service.run_turn(make_turn())
    assert options.status == "options_ready", options.model_dump()
    assert options.options
    option = next((o for o in options.options if o["total_minor"] == 6_792_000), options.options[0])
    selected = await service.run_turn(make_turn(options.state_version, kind="select_option", payload={
        "option_id": option["option_id"], "options_version": option["options_version"],
    }))
    assert selected.status == "plan_ready", selected.model_dump()
    return selected


async def pending_plan(service):
    selected = await ready_plan(service)
    pending = await service.run_turn(make_turn(selected.state_version, "Оформить"))
    assert pending.status == "needs_confirmation", pending.model_dump()
    assert pending.pending_action
    return pending


def confirmation_payload(pending):
    return {key: pending.pending_action[key] for key in
            ("action_id", "plan_id", "plan_version", "payload_hash")}


async def test_full_request_searches_without_reasking_known_fields(service):
    result = await service.run_turn(make_turn())
    assert result.status == "options_ready", result.model_dump()
    assert result.options
    assert not result.questions
    state = await service.get_session("alice", "trip-1")
    assert str(state.request.departure_date) == "2026-11-24"
    assert str(state.request.return_date) == "2026-12-01"
    assert state.request.adults == 1
    assert state.request.budget_minor == 8_000_000


async def test_missing_party_does_not_silently_assume_one_traveler(service):
    result = await service.run_turn(make_turn(text="Из Москвы в Ереван с 24 ноября по 1 декабря 2026"))
    assert result.status == "needs_input"
    assert not result.options
    state = await service.get_session("alice", "trip-1")
    assert state.request.adults is None
    assert result.questions or result.message


async def test_historical_total_and_unknown_costs_are_not_invented(service):
    result = await service.run_turn(make_turn())
    assert result.status == "options_ready"
    candidate = next(o for o in result.options if o["total_minor"] == 6_792_000)
    assert sorted(c["price_minor"] for c in candidate["components"]) == [3_270_200, 3_521_800]
    assert all(c["data_origin"] == "recorded_fixture" for c in candidate["components"])
    assert all(c["source_recorded_at"].startswith("2026-10-26") for c in candidate["components"])
    assert all(c["source_recorded_at"] != c["fetched_at"] for c in candidate["components"])
    serialized = str(result.model_dump()).lower()
    assert "трансфер" in serialized
    assert "архив" in serialized or "историческ" in serialized


async def test_request_repeat_returns_original_result_despite_old_state_version(service):
    original = make_turn(request_id="same-request")
    first = await service.run_turn(original)
    assert first.state_version > 0
    again = await service.run_turn(original)
    assert again.model_dump() == first.model_dump()
    assert (await service.get_session("alice", "trip-1")).state_version == first.state_version


async def test_same_request_id_with_other_input_is_rejected(service):
    first = await service.run_turn(make_turn(request_id="same-request"))
    conflict = await service.run_turn(make_turn(first.state_version, "Бюджет 50 тысяч", request_id="same-request"))
    assert conflict.status == "error"
    assert conflict.error["code"] == "request_id_conflict"
    state = await service.get_session("alice", "trip-1")
    assert state.state_version == first.state_version
    assert state.request.budget_minor == 8_000_000


async def test_stale_state_cannot_overwrite_newer_trip(service):
    first = await service.run_turn(make_turn())
    stale = await service.run_turn(make_turn(0, "Бюджет 50 тысяч"))
    assert stale.status == "error"
    assert stale.error["code"] == "state_conflict"
    state = await service.get_session("alice", "trip-1")
    assert state.state_version == first.state_version
    assert state.request.budget_minor == 8_000_000


async def test_selection_is_bound_to_options_version(service):
    result = await service.run_turn(make_turn())
    option = result.options[0]
    stale = await service.run_turn(make_turn(result.state_version, kind="select_option", payload={
        "option_id": option["option_id"], "options_version": option["options_version"] - 1,
    }))
    assert stale.status == "error"
    assert not stale.current_plan
    assert not (await service.get_session("alice", "trip-1")).current_plan


async def test_demo_purchase_is_explicit_and_persists(service, tmp_path):
    pending = await pending_plan(service)
    done = await service.run_turn(make_turn(pending.state_version, kind="confirm_action", payload=confirmation_payload(pending)))
    assert done.status == "completed_demo", done.model_dump()
    assert done.demo_order["mode"] == "demo"
    assert done.demo_order["status"] == "simulated_confirmed"
    assert "не бронировал" in done.message.lower()
    reopened = service_at(tmp_path / "agent.sqlite")
    state = await reopened.get_session("alice", "trip-1")
    assert state.demo_order["local_id"] == done.demo_order["local_id"]
    assert state.pending_action is None


async def test_confirming_one_action_twice_creates_one_order(service):
    pending = await pending_plan(service)
    payload = confirmation_payload(pending)
    done = await service.run_turn(make_turn(pending.state_version, kind="confirm_action", payload=payload))
    again = await service.run_turn(make_turn(done.state_version, kind="confirm_action", payload=payload))
    assert done.status == again.status == "completed_demo"
    assert done.demo_order["local_id"] == again.demo_order["local_id"]


@pytest.mark.parametrize("field,value", [
    ("payload_hash", "tampered"), ("plan_version", 999), ("action_id", "not-this-action"),
    ("plan_id", "another-plan"),
])
async def test_confirmation_must_match_exact_pending_action(service, field, value):
    pending = await pending_plan(service)
    payload = confirmation_payload(pending) | {field: value}
    result = await service.run_turn(make_turn(pending.state_version, kind="confirm_action", payload=payload))
    assert result.status == "error", result.model_dump()
    assert result.demo_order is None
    assert (await service.get_session("alice", "trip-1")).demo_order is None


async def test_changed_request_invalidates_previous_confirmation(service):
    pending = await pending_plan(service)
    payload = confirmation_payload(pending)
    changed = await service.run_turn(make_turn(pending.state_version, "Бюджет теперь 50 тысяч рублей на всю поездку"))
    state = await service.get_session("alice", "trip-1")
    assert state.request.budget_minor == 5_000_000
    assert state.pending_action is None
    stale = await service.run_turn(make_turn(changed.state_version, kind="confirm_action", payload=payload))
    assert stale.status == "error", stale.model_dump()
    assert stale.demo_order is None


async def test_expired_confirmation_cannot_create_order(service):
    pending = await pending_plan(service)
    expired = await service.run_turn(make_turn(
        pending.state_version, kind="confirm_action", payload=confirmation_payload(pending),
        now="2026-10-28T12:00:00+03:00",
    ))
    assert expired.status in ("error", "needs_input"), expired.model_dump()
    assert expired.pending_action is None
    assert expired.demo_order is None


async def test_restart_restores_pending_confirmation(tmp_path):
    service = service_at(tmp_path / "agent.sqlite")
    pending = await pending_plan(service)
    restarted = service_at(tmp_path / "agent.sqlite")
    state = await restarted.get_session("alice", "trip-1")
    assert state.pending_action == pending.pending_action
    result = await restarted.run_turn(make_turn(state.state_version, kind="confirm_action", payload=confirmation_payload(pending)))
    assert result.status == "completed_demo"


async def test_same_session_id_for_two_users_does_not_mix_state(service):
    alice = await service.run_turn(make_turn())
    bob = await service.run_turn(make_turn(text="Хочу отдохнуть, не знаю где", user="bob"))
    alice_state = await service.get_session("alice", "trip-1")
    bob_state = await service.get_session("bob", "trip-1")
    assert alice_state.state_version == alice.state_version
    assert alice_state.request.destination
    assert bob_state.request.destination is None
    assert not bob_state.options
    assert bob.session_id == alice.session_id


async def test_archived_data_never_fabricates_unrecorded_dates(service):
    text = FULL_TRIP.replace("24 ноября", "25 ноября")
    result = await service.run_turn(make_turn(text=text))
    assert result.error and result.error["code"] == "fixture_missing", result.model_dump()
    assert not result.options
    assert not result.current_plan


async def test_unknown_pet_policy_blocks_checkout(service):
    result = await service.run_turn(make_turn(text=FULL_TRIP + ", обязательно с собакой"))
    state = await service.get_session("alice", "trip-1")
    assert state.request.pets is True
    if result.options:
        option = result.options[0]
        result = await service.run_turn(make_turn(result.state_version, kind="select_option", payload={
            "option_id": option["option_id"], "options_version": option["options_version"],
        }))
    result = await service.run_turn(make_turn(result.state_version, "Оформить"))
    assert result.status not in ("needs_confirmation", "completed_demo")
    assert result.pending_action is None
    assert result.demo_order is None


async def test_free_question_keeps_selected_trip(service):
    chosen = await ready_plan(service)
    before = await service.get_session("alice", "trip-1")
    await service.run_turn(make_turn(chosen.state_version, "Что входит в стоимость?"))
    after = await service.get_session("alice", "trip-1")
    assert after.current_plan == before.current_plan
    assert after.request == before.request


async def test_concurrent_changes_use_state_version_without_lost_update(service):
    import asyncio
    first, second = await asyncio.gather(
        service.run_turn(make_turn()),
        service.run_turn(make_turn(text="Хочу отдохнуть, не знаю где")),
    )
    assert sorted([first.status, second.status]) == ["error", "options_ready"]
    conflict = next(result for result in (first, second) if result.status == "error")
    assert conflict.error["code"] == "state_conflict"
    assert (await service.get_session("alice", "trip-1")).state_version == 1


async def test_price_change_requires_new_confirmation(tmp_path):
    from travel_agent.adapters.replay import ReplayAdapter

    class ChangedPriceAdapter(ReplayAdapter):
        refresh_count = 0

        async def refresh(self, components, request, now):
            result = await super().refresh(components, request, now)
            self.refresh_count += 1
            if self.refresh_count >= 2 and result["components"]:
                result["components"][0]["price_minor"] += 100
                result["components"][0]["data_origin"] = "synthetic"
                result["status"] = "changed"
                result["warnings"] = ["Synthetic fixture: changed fare for regression test"]
            return result

    service = service_at(tmp_path / "agent.sqlite", adapter=ChangedPriceAdapter())
    pending = await pending_plan(service)
    old_payload = confirmation_payload(pending)
    changed = await service.run_turn(make_turn(pending.state_version, kind="confirm_action", payload=old_payload))
    assert changed.status == "needs_confirmation", changed.model_dump()
    assert changed.demo_order is None
    assert changed.pending_action["plan_version"] > pending.pending_action["plan_version"]
    assert changed.pending_action["action_id"] != pending.pending_action["action_id"]
    assert changed.pending_action["payload_hash"] != pending.pending_action["payload_hash"]
    rejected = await service.run_turn(make_turn(changed.state_version, kind="confirm_action", payload=old_payload))
    assert rejected.status == "error"
    assert rejected.demo_order is None


async def test_train_request_is_not_silently_replaced_with_flight(service):
    result = await service.run_turn(make_turn(text=FULL_TRIP + ", ехать обязательно поездом"))
    assert result.status not in ("options_ready", "plan_ready", "needs_confirmation")
    assert not result.options
    assert not any(c["product_type"] == "avia" for c in result.transport_options)
    assert (await service.get_session("alice", "trip-1")).request.products == ["rail", "hotel"]


async def test_unknown_child_age_prevents_search_as_adult_only_party(service):
    result = await service.run_turn(make_turn(text=FULL_TRIP + ", со мной ребёнок"))
    assert result.status == "needs_input"
    assert not result.options


async def test_refundable_ticket_does_not_select_nonrefundable_airfare(service):
    result = await service.run_turn(make_turn(text=FULL_TRIP + ", нужен возвратный билет"))
    suitable = [o for o in result.options if o["eligibility"] == "satisfied"]
    assert suitable
    for option in suitable:
        flight = next(c for c in option["components"] if c["product_type"] == "avia")
        assert flight["conditions"]["refundable"] is True


async def test_cheaper_request_keeps_hard_baggage_constraint(service):
    first = await service.run_turn(make_turn(text=FULL_TRIP + ", багаж 23 кг"))
    result = await service.run_turn(make_turn(first.state_version, "Сделай дешевле"))
    suitable = [o for o in result.options if o["eligibility"] == "satisfied"]
    assert suitable
    for option in suitable:
        flight = next(c for c in option["components"] if c["product_type"] == "avia")
        assert flight["conditions"]["baggage_kg"] >= 23


async def test_refreshed_explanation_uses_new_price(service):
    from copy import deepcopy
    from travel_agent.domain.planning import refresh_plan_explanation, rub
    result = await service.run_turn(make_turn())
    plan = deepcopy(result.options[0])
    plan["components"][0]["price_minor"] += 10000
    refresh_plan_explanation(plan)
    assert rub(plan["components"][0]["price_minor"]) in " ".join(plan["reasons"])
