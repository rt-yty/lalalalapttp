"""State-machine audit through the public service, without a model or network.

The provider is deliberately controllable: its malformed responses exercise the
boundary where a transport adapter cannot be trusted to preserve an old quote.
"""
import asyncio
from copy import deepcopy
from datetime import date, timedelta
import random
from uuid import uuid4

import pytest

from test_component_flow import ComponentAdapter, ScriptedGateway, REQUEST, NOW
from travel_agent.domain.models import TurnInput
from travel_agent.domain.planning import plan_hash, stable_components
from travel_agent.service import AgentService


class AuditAdapter(ComponentAdapter):
    def __init__(self):
        super().__init__(overnight=True)
        self.errors = set()
        self.malformed = None
        self.refresh_mode = "unchanged"

    async def search(self, request, now):
        # Rail currently has no child-price contract. Synthetic replies retain
        # that limitation instead of inventing a child fare.
        missing = self.missing.copy()
        if request.get("children_ages") or request["adults"] > 4:
            self.missing.add("rail")
        self.missing.update(self.errors)
        result = await super().search(request, now)
        self.missing = missing
        for kind in request["products"]:
            if kind in self.errors:
                result["component_errors"][kind] = {"code": "timeout", "retryable": True,
                    "message": "Источник временно недоступен."}
            if kind == "rail" and (request.get("children_ages") or request["adults"] > 4):
                result["component_errors"][kind] = {"code": "unsupported_party", "retryable": False}
        for key in ("flights", "hotels"):
            for card in result[key]:
                if key == "flights":
                    card["price_minor"] *= request["adults"] + len(request.get("children_ages", []))
                if self.malformed == "currency":
                    card["currency"] = "USD"
                elif self.malformed == "none_price":
                    card["price_minor"] = None
                elif self.malformed == "wrong_party":
                    card["query"]["adults"] += 1
                elif self.malformed == "wrong_destination":
                    card["query"]["destination"] = "Не тот город"
                elif self.malformed == "broken_return" and key == "flights":
                    card["return_arrival_at"] = card["return_departure_at"].replace("12:00", "10:00")
        if result["component_errors"] and not any(result[k] for k in ("flights", "trains", "hotels")):
            result["error"] = next(iter(result["component_errors"].values()))
        return result

    async def refresh(self, components, request, now):
        result = await super().refresh(components, request, now)
        if self.refresh_mode == "timeout":
            return {"status": "unknown", "components": [], "warnings": [],
                    "error": {"code": "timeout", "retryable": True}}
        if self.refresh_mode == "empty":
            result.update(status="changed", components=[])
        elif self.refresh_mode == "currency":
            result["status"] = "changed"
            result["components"][0]["currency"] = "USD"
        elif self.refresh_mode == "none_price":
            result["status"] = "changed"
            result["components"][0]["price_minor"] = None
        elif self.refresh_mode == "wrong_query":
            result["status"] = "changed"
            result["components"][0]["query"]["destination"] = "Не тот город"
        elif self.refresh_mode == "price_increase":
            result["status"] = "changed"
            result["components"][0]["price_minor"] += 10000
        elif self.refresh_mode == "fresh_timestamp":
            for card in result["components"]:
                card["fetched_at"] = "2026-10-28T12:01:00+03:00"
        return result


class Harness:
    def __init__(self, path, request=None):
        self.path = path
        self.adapter = AuditAdapter()
        self.gateway = ScriptedGateway(deepcopy(request or REQUEST), {
            "checkout": {"intent": "checkout", "patch": {}},
            "repeat_search": {"intent": "replan", "patch": {}},
        })
        self.service = self.open()
        self.version = 0
        self.steps = []
        self.last_turn = None

    def open(self):
        return AgentService(db_path=self.path, data_mode="live", checkout_mode="demo",
                            gateway=self.gateway, adapter=self.adapter)

    async def step(self, text="start", *, action="message", payload=None, decision=None):
        if decision:
            self.gateway.decisions[text] = deepcopy(decision)
        self.last_turn = TurnInput(request_id=str(uuid4()), user_id="audit", session_id="trip",
            expected_state_version=self.version, input=action, payload=payload or {"text": text}, now=NOW)
        result = await self.service.run_turn(self.last_turn)
        self.version = result.state_version
        self.steps.append({"action": action, "text": text, "status": result.status,
                           "version": result.state_version, "error": (result.error or {}).get("code"),
                           "decision": deepcopy(decision), "payload": deepcopy(payload)})
        return result

    async def patch(self, patch, **extra):
        return await self.step("patch-" + str(len(self.steps)), decision={"intent": "replan", "patch": patch, **extra})

    async def choose(self, card):
        return await self.step(action="select_component", payload={"component_id": card["component_id"],
                              "options_version": card["options_version"]})

    async def complete(self):
        found = await self.step()
        chosen = await self.choose(next(c for c in found.transport_options if c["arrival_at"][:10] == REQUEST["departure_date"]))
        return await self.choose(chosen.hotel_options[0])

    async def snapshot(self):
        return await self.service.get_session("audit", "trip")

    def close(self):
        self.service.close()


@pytest.fixture
def audit(tmp_path):
    h = Harness(tmp_path / "audit.sqlite")
    yield h
    h.close()


def assert_invariants(state):
    request = state.request.model_dump(mode="json")
    selected = state.selected_components
    assert sum(c["product_type"] == "hotel" for c in selected) <= 1
    assert sum(c["product_type"] in ("avia", "rail") for c in selected) <= 1
    assert all(c["product_type"] in request["products"] for c in selected)
    for card in selected + state.transport_options + state.hotel_options:
        assert AgentService._valid_component(card, request), ("incompatible card", card, request)
        assert card["currency"] == "RUB" and type(card["price_minor"]) is int and card["price_minor"] >= 0
    for card in state.transport_options + state.hotel_options:
        assert card["options_version"] == state.options_version
    if state.current_plan:
        plan = state.current_plan
        assert plan["components"] == selected, "selected components diverged from current plan"
        assert plan["request"] == request, "current plan refers to obsolete trip request"
        assert plan["total_minor"] == sum(c["price_minor"] for c in selected)
        assert plan["currency"] == "RUB"
        missing = []
        if any(p in request["products"] for p in ("avia", "rail")) and not any(c["product_type"] in ("avia", "rail") for c in selected):
            missing.append("transport")
        if "hotel" in request["products"] and not any(c["product_type"] == "hotel" for c in selected):
            missing.append("hotel")
        assert plan["missing_products"] == missing
        assert bool(plan["complete"]) == (not missing)
        assert {c["product_type"] for c in plan["components"]} <= set(request["products"])
    else:
        assert selected == [], "selected components exist without a current plan"
    if state.pending_action:
        assert state.current_plan and state.current_plan["complete"]
        assert state.current_plan["eligibility"] == "satisfied"
        assert state.current_plan.get("verification_status") != "unverified"
        assert state.pending_action["payload_hash"] == plan_hash(state.current_plan)
        assert state.pending_action["total_minor"] == state.current_plan["total_minor"]
    for option in state.options:
        assert all(AgentService._valid_component(c, request) for c in option["components"])
        assert option["options_version"] == state.options_version, "old package survived a new component offer version"
        assert option["total_minor"] == sum(c["price_minor"] for c in option["components"])


async def test_same_request_provider_failure_invalidates_confirmation_and_old_plan(audit):
    await audit.complete()
    pending = await audit.step("checkout")
    assert pending.pending_action
    audit.adapter.errors = {"avia", "hotel"}
    failed = await audit.step("repeat_search")
    assert failed.status == "partial"
    assert failed.pending_action is None
    assert_invariants(await audit.snapshot())


async def test_clarification_after_product_change_preserves_consistent_partial_plan(audit):
    await audit.complete()
    result = await audit.patch({"products": ["hotel"]}, question="Какой район предпочитаете?")
    assert result.status == "needs_input"
    assert_invariants(await audit.snapshot())


async def test_arrival_hotel_search_invalidates_old_combined_option_ids(audit):
    found = await audit.step()
    old_option = found.options[0]
    night = next(c for c in found.transport_options if c["arrival_at"][:10] > REQUEST["departure_date"])
    selected = await audit.choose(night)
    state = await audit.snapshot()
    assert selected.hotel_options[0]["query"]["departure_date"] == "2026-11-11"
    assert_invariants(state)
    stale = await audit.step(action="select_option", payload={"option_id": old_option["option_id"],
                                                            "options_version": state.options_version})
    assert stable_components(stale.selected_components) == stable_components(selected.selected_components)


@pytest.mark.parametrize("mode", ["currency", "none_price", "empty", "wrong_query"])
async def test_bad_refresh_never_prepares_or_keeps_confirmation(audit, mode):
    await audit.complete()
    pending = await audit.step("checkout")
    assert pending.pending_action
    audit.adapter.refresh_mode = mode
    result = await audit.step("checkout")
    assert result.status == "partial", result.model_dump()
    assert result.pending_action is None
    assert_invariants(await audit.snapshot())


async def test_successful_refresh_keeps_selected_snapshot_in_sync(audit):
    await audit.complete()
    audit.adapter.refresh_mode = "fresh_timestamp"
    result = await audit.step("checkout")
    assert result.pending_action
    assert_invariants(await audit.snapshot())


@pytest.mark.parametrize("mode", ["currency", "none_price", "wrong_party", "wrong_destination", "broken_return"])
async def test_provider_invalid_offers_are_not_advertised_as_current(audit, mode):
    audit.adapter.malformed = mode
    found = await audit.step()
    assert found.status != "error", found.model_dump()
    if mode == "broken_return":
        assert found.transport_options == []
    else:
        assert found.transport_options == [] and found.hotel_options == []
    assert_invariants(await audit.snapshot())


async def test_price_change_requires_new_confirmation_and_old_payload_is_rejected(audit):
    await audit.complete()
    original = await audit.step("checkout")
    audit.adapter.refresh_mode = "price_increase"
    changed = await audit.step(action="confirm_action", payload=original.pending_action)
    assert changed.status == "needs_confirmation" and changed.demo_order is None
    assert changed.pending_action["action_id"] != original.pending_action["action_id"]
    assert changed.current_plan["total_minor"] == original.current_plan["total_minor"] + 10000
    audit.adapter.refresh_mode = "unchanged"
    stale = await audit.step(action="confirm_action", payload=original.pending_action)
    assert stale.error["code"] == "stale_confirmation" and stale.demo_order is None
    assert_invariants(await audit.snapshot())


async def test_party_and_per_person_budget_use_quote_for_every_traveler(audit):
    found = await audit.patch({**REQUEST, "adults": 2, "children_ages": [7],
                               "budget_minor": 1_300_000, "budget_scope": "per_person"})
    day = next(c for c in found.transport_options if c["arrival_at"][:10] == REQUEST["departure_date"])
    assert day["price_minor"] == 3_000_000
    selected = await audit.choose(day)
    result = await audit.choose(selected.hotel_options[0])
    assert result.current_plan["total_minor"] == 3_800_000 and result.current_plan["complete"]
    budget = next(c for c in result.current_plan["checks"] if c["constraint_id"] == "budget_known_components")
    assert budget["required"] == 3_900_000 and budget["status"] == "satisfied"
    changed = await audit.patch({"adults": 1, "children_ages": [], "budget_scope": "trip", "budget_minor": 3_000_000})
    assert not changed.selected_components
    assert all(c["query"]["adults"] == 1 and c["query"]["children_ages"] == [] for c in changed.transport_options + changed.hotel_options)
    assert_invariants(await audit.snapshot())


async def test_event_deadline_filters_overnight_after_city_and_dates_change(audit):
    await audit.complete()
    changed = await audit.patch({"destination": "Казань", "departure_date": "2026-11-12",
                                 "return_date": "2026-11-19", "event_date": "2026-11-13",
                                 "event_start_at": "2026-11-13T09:00:00+03:00"})
    assert not changed.selected_components
    assert changed.transport_options
    assert all(c["arrival_at"] < "2026-11-13T09:00:00+03:00" for c in changed.transport_options)
    assert_invariants(await audit.snapshot())


async def test_partial_hotel_error_and_rail_children_are_separate(audit):
    audit.adapter.errors = {"hotel"}
    result = await audit.patch({**REQUEST, "products": ["avia", "rail", "hotel"], "children_ages": [9], "budget_minor": 5_000_000})
    assert result.status == "partial" and result.transport_options
    assert {c["product_type"] for c in result.transport_options} == {"avia"}
    assert result.component_errors["rail"]["code"] == "unsupported_party"
    assert result.component_errors["hotel"]["code"] == "timeout"
    chosen = await audit.choose(result.transport_options[0])
    assert chosen.selected_components and not chosen.hotel_options
    assert chosen.component_errors["rail"]["code"] == "unsupported_party"
    assert_invariants(await audit.snapshot())


async def test_incompatible_new_selection_revokes_previous_confirmation(audit):
    await audit.patch({**REQUEST, "budget_minor": 2_000_000})
    found = await audit.snapshot()
    day = next(c for c in found.transport_options if c["arrival_at"][:10] == REQUEST["departure_date"])
    selected = await audit.choose(day)
    await audit.choose(next(c for c in selected.hotel_options if c["provider_refs"]["hotel_id"] == "h1"))
    pending = await audit.step("checkout")
    assert pending.pending_action
    expensive = next(c for c in pending.hotel_options if c["provider_refs"]["hotel_id"] == "h2")
    refused = await audit.choose(expensive)
    assert refused.status == "partial" and refused.pending_action is None
    assert refused.current_plan["total_minor"] == 1_800_000
    assert_invariants(await audit.snapshot())


async def test_failed_undo_revokes_previous_confirmation(audit):
    await audit.complete()
    pending = await audit.step("checkout")
    assert pending.pending_action
    audit.adapter.refresh_mode = "timeout"
    failed = await audit.step(action="undo")
    assert failed.status == "partial" and failed.pending_action is None
    assert_invariants(await audit.snapshot())


async def test_hotel_only_research_keeps_unsearched_rail_failure(audit):
    found = await audit.patch({**REQUEST, "products": ["avia", "rail", "hotel"], "children_ages": [7], "budget_minor": 5_000_000})
    assert found.component_errors["rail"]["code"] == "unsupported_party"
    updated = await audit.patch({"constraints": {"breakfast": True}}, keep_transport=True)
    assert audit.adapter.calls[-1]["products"] == ["hotel"]
    assert updated.component_errors["rail"]["code"] == "unsupported_party"


async def test_question_without_tariffs_is_answered_before_replan(audit):
    audit.adapter.missing = {"avia", "hotel"}
    await audit.step()
    updated = await audit.patch({"departure_date": "2026-11-11", "return_date": "2026-11-18"},
                                 question_text="Билет возвратный?")
    assert "возвратность не подтверждена" in updated.message.lower()
    assert (await audit.snapshot()).request.departure_date.isoformat() == "2026-11-11"


@pytest.mark.parametrize("question", [
    "Когда хотите вылететь?", "На какую дату планируете выезд из отеля?",
    "Когда хотите вернуться?", "На сколько ночей ищем отель?", "Сколько взрослых поедет?",
    "Какой город отправления?",
])
async def test_duration_answer_does_not_reask_a_known_plain_slot(audit, question):
    initial = await audit.patch({**REQUEST, "departure_date": "2026-11-24", "return_date": None})
    assert initial.status == "needs_input" and not audit.adapter.calls
    updated = await audit.step("Ищем отель на одну ночь.", decision={"intent": "plan",
        "patch": {"return_date": "2026-11-25"}, "question": question})
    assert updated.status == "options_ready", updated.model_dump()
    assert updated.questions == [] and len(audit.adapter.calls) == 1
    assert audit.adapter.calls[0]["departure_date"] == "2026-11-24"
    assert audit.adapter.calls[0]["return_date"] == "2026-11-25"
    assert audit.adapter.calls[0]["products"] == ["avia", "hotel"]
    assert_invariants(await audit.snapshot())


@pytest.mark.parametrize("question", [
    "Вы имеете в виду 24 или 25 ноября?",
    "Подтвердите поездку с 24 по 25 ноября?",
    "Перенесём вылет на 25 ноября?",
    "Когда хотите вылететь — утром или вечером?",
    "Конкретные места в поезде не поддерживаются. Продолжим поиск без выбора места?",
    "Сколько лет каждому ребёнку на дату поездки?",
])
async def test_known_dates_do_not_suppress_semantic_clarification(audit, question):
    initial = await audit.patch({**REQUEST, "departure_date": "2026-11-24", "return_date": None})
    assert initial.status == "needs_input"
    updated = await audit.patch({"return_date": "2026-11-25"}, question=question)
    assert updated.status == "needs_input" and updated.questions == [question]
    assert not audit.adapter.calls


async def test_redundant_known_slot_falls_through_to_actual_missing_slot(audit):
    updated = await audit.patch({**REQUEST, "adults": None}, question="Когда хотите вылететь?")
    assert updated.status == "needs_input"
    assert updated.questions == ["Сколько взрослых поедет?"]
    assert not audit.adapter.calls


@pytest.mark.parametrize("question", ["Есть ли предпочтения по отелю?", "Какие у вас предпочтения по проживанию?"])
async def test_complete_request_searches_before_optional_preference_questionnaire(audit, question):
    updated = await audit.patch(REQUEST, question=question)
    assert updated.status == "options_ready"
    assert updated.questions == [] and audit.adapter.calls


async def test_specific_location_ambiguity_is_not_an_optional_questionnaire(audit):
    updated = await audit.patch(REQUEST, question="Рядом с какой площадкой искать отель?")
    assert updated.status == "needs_input"
    assert updated.questions == ["Рядом с какой площадкой искать отель?"]
    assert not audit.adapter.calls


async def test_redundant_slot_does_not_override_explicit_date_reset(audit):
    await audit.complete()
    before = len(audit.adapter.calls)
    updated = await audit.patch({}, reset_dates=True, question="Когда хотите вылететь?")
    assert updated.status == "needs_input" and updated.questions == ["Когда хотите вылететь?"]
    assert len(audit.adapter.calls) == before
    state = await audit.snapshot()
    assert state.request.departure_date is None and state.request.return_date is None
    assert state.current_plan is None and state.selected_components == []


async def test_chat_leaves_trip_and_pending_intact_and_reject_does_not_invent_action(audit):
    await audit.complete()
    await audit.step("checkout")
    before = await audit.snapshot()
    chat = await audit.step("chat", decision={"intent": "chat", "patch": {}, "reply_text": "Хорошо, сменим тему."})
    after = await audit.snapshot()
    assert chat.response_kind == "chat" and chat.message == "Хорошо, сменим тему."
    for field in ("request", "selected_components", "current_plan", "pending_action", "last_question"):
        assert getattr(after, field) == getattr(before, field)
    rejected = await audit.step(action="reject_action")
    assert rejected.pending_action is None and rejected.current_plan
    again = await audit.step(action="reject_action")
    assert "отмен" not in again.message.lower()
    assert_invariants(await audit.snapshot())


async def test_two_service_instances_share_versions_but_users_and_sessions_are_isolated(audit):
    found = await audit.step()
    other = audit.open()
    try:
        choose = {"component_id": found.transport_options[0]["component_id"],
                  "options_version": found.transport_options[0]["options_version"]}
        turns = [TurnInput(request_id=str(uuid4()), user_id="audit", session_id="trip",
                 expected_state_version=found.state_version, input="select_component", payload=choose, now=NOW) for _ in range(2)]
        results = await asyncio.gather(audit.service.run_turn(turns[0]), other.run_turn(turns[1]))
        assert sum(r.status == "plan_ready" for r in results) == 1
        assert sum((r.error or {}).get("code") == "state_conflict" for r in results) == 1
        audit.version = (await audit.snapshot()).state_version
        assert_invariants(await audit.snapshot())
        for user, session in (("other-user", "trip"), ("audit", "new-trip")):
            with pytest.raises(KeyError):
                await other.get_session(user, session)
            foreign = TurnInput(request_id=str(uuid4()), user_id=user, session_id=session,
                expected_state_version=0, input="select_component", payload=choose, now=NOW)
            refused = await other.run_turn(foreign)
            assert refused.selected_components == [] and refused.current_plan is None
        assert (await audit.snapshot()).state_version == audit.version
    finally:
        other.close()


CAMPAIGN_SEEDS = tuple(range(24))
CAMPAIGN_ACTIONS = 24
CAMPAIGN_REPORT = []


@pytest.mark.parametrize("seed", CAMPAIGN_SEEDS)
async def test_seeded_public_action_sequences(tmp_path, seed):
    """24 fixed seeds × 25 committed/retried transitions = 600 state checks."""
    rng = random.Random(seed)
    h = Harness(tmp_path / f"campaign-{seed}.sqlite")
    remembered = []
    expected_products = list(REQUEST["products"])
    try:
        result = await h.step()
        assert_invariants(await h.snapshot())
        operations = ["products", "party", "dates", "destination", "budget", "select", "remove",
                      "undo", "repeat", "stale", "restart", "error", "empty", "checkout", "reject", "refresh", "keep"]
        for index in range(CAMPAIGN_ACTIONS):
            state = await h.snapshot()
            remembered.extend(deepcopy(state.transport_options + state.hotel_options))
            operation = operations[(seed + index) % len(operations)] if index < len(operations) else rng.choice(operations)
            if operation == "products":
                expected_products = rng.choice([["hotel"], ["avia"], ["rail"], ["avia", "rail"], ["avia", "hotel"], ["rail", "hotel"], ["avia", "rail", "hotel"]])
                result = await h.patch({"products": expected_products})
            elif operation == "party":
                result = await h.patch({"adults": rng.choice([1, 2, 3]), "children_ages": rng.choice([[], [5], [6, 12]])})
            elif operation == "dates":
                day = date(2026, 11, 10) + timedelta(days=rng.randrange(4))
                result = await h.patch({"departure_date": str(day), "return_date": str(day + timedelta(days=7))})
            elif operation == "destination":
                result = await h.patch({"destination": rng.choice(["Казань", "Сочи", "Санкт-Петербург"])})
            elif operation == "budget":
                result = await h.patch({"budget_minor": rng.choice([1_500_000, 3_000_000, 6_000_000]), "budget_scope": rng.choice(["trip", "per_person"])})
            elif operation == "select":
                cards = state.transport_options + state.hotel_options
                result = await h.choose(rng.choice(cards)) if cards else await h.step("repeat_search")
            elif operation == "remove":
                result = await h.step(action="remove_component", payload={"product_type": rng.choice(["avia", "rail", "hotel"])})
            elif operation == "undo":
                result = await h.step(action="undo")
                expected_products = (await h.snapshot()).request.products
            elif operation == "repeat":
                before = state.model_dump(mode="json")
                repeated = await h.service.run_turn(h.last_turn)
                assert repeated.model_dump(mode="json") == result.model_dump(mode="json")
                assert (await h.snapshot()).model_dump(mode="json") == before
                h.steps.append({"action": "repeat_request", "version": repeated.state_version, "status": repeated.status})
            elif operation == "stale":
                stale = next((c for c in remembered if c["options_version"] != state.options_version), None)
                if stale:
                    before = deepcopy(state.selected_components)
                    result = await h.choose(stale)
                    assert result.error["code"] == "stale_options"
                    assert (await h.snapshot()).selected_components == before
                else:
                    result = await h.step("repeat_search")
            elif operation == "restart":
                before = state.model_dump(mode="json")
                h.close()
                h.service = h.open()
                assert (await h.snapshot()).model_dump(mode="json") == before
                result = await h.step("repeat_search")
            elif operation in ("error", "empty"):
                if operation == "error":
                    h.adapter.errors = {rng.choice(state.request.products)}
                else:
                    h.adapter.missing = set(state.request.products)
                result = await h.step("repeat_search")
                h.adapter.errors = set()
                h.adapter.missing = set()
            elif operation == "checkout":
                result = await h.step("checkout")
            elif operation == "reject":
                result = await h.step(action="reject_action")
            elif operation == "refresh":
                h.adapter.refresh_mode = rng.choice(["timeout", "price_increase", "currency", "none_price", "empty"])
                result = await h.step("checkout")
                h.adapter.refresh_mode = "unchanged"
            elif operation == "keep":
                result = await h.patch({"constraints": {"breakfast": True}}, keep_transport=True)
            current = await h.snapshot()
            h.steps[-1]["operation"] = operation
            assert current.request.products == expected_products, (seed, index, operation, h.steps)
            assert not result.error or result.error["code"] not in ("dependency_error", "invalid_input"), (seed, operation, result.model_dump())
            if result.status == "partial":
                assert current.pending_action is None, (seed, operation, h.steps)
            try:
                assert_invariants(current)
            except AssertionError as exc:
                raise AssertionError(f"seed={seed}, step={index}, operation={operation}, steps={h.steps}: {exc}") from exc
        CAMPAIGN_REPORT.append({"seed": seed, "turns": len(h.steps), "steps": h.steps, "status": "passed"})
    finally:
        h.close()
