"""Independent traveller-facing tariff regressions found during the 28 October audit."""
import pytest

from travel_agent.adapters.normalize import normalize_hotel, normalize_rail, match_refresh
from travel_agent.domain.planning import component_options_from_search
from test_component_adapter import REQUEST, NOW, hotel
from test_rail_adapter import train


def rail_component(request, return_class="2К"):
    outbound, inbound = train("outbound"), train("inbound")
    inbound["variants"][0]["service_class"] = return_class
    return normalize_rail(outbound, outbound["variants"][0], inbound, inbound["variants"][0], request, NOW)


def test_same_rail_class_in_each_direction_satisfies_requested_class():
    request = {**REQUEST, "products": ["rail"], "constraints": {"service_class": "2К"}}
    item = rail_component(request)
    cards = component_options_from_search({"trains": [item]}, request, 1)["transport_options"]
    assert len(cards) == 1
    assert item["conditions"]["service_class"] == "2К"
    assert item["price_minor"] == 400000


def test_different_return_class_does_not_satisfy_requested_class():
    request = {**REQUEST, "products": ["rail"], "constraints": {"service_class": "2К"}}
    item = rail_component(request, "3Э")
    assert not component_options_from_search({"trains": [item]}, request, 1)["transport_options"]


@pytest.mark.parametrize("direction", ["outbound", "inbound"])
@pytest.mark.parametrize("field", ["departure_at", "arrival_at"])
def test_rail_segment_times_must_match_the_advertised_schedule(direction, field):
    outbound, inbound = train("outbound"), train("inbound")
    selected = outbound if direction == "outbound" else inbound
    selected["legs"][0]["segments"][0][field] = "2026-11-11T12:00:00+03:00"
    with pytest.raises(ValueError, match="inconsistent_rail_times"):
        normalize_rail(outbound, outbound["variants"][0], inbound, inbound["variants"][0], REQUEST, NOW)


def hotel_component(deadline, now=NOW):
    detail = hotel("first")
    room = detail["rooms"][0]
    rate = room["rates"][0]
    rate.update(free_cancellation=True, free_cancellation_until=deadline)
    request = {**REQUEST, "products": ["hotel"], "constraints": {"free_cancellation": True}}
    return normalize_hotel(detail, room, rate, request, now), request


@pytest.mark.parametrize("deadline", [
    "2026-10-27T12:00:00+03:00",  # already expired
    "2026-10-28T12:00:00+03:00",  # exact boundary
    "2026-10-28T08:59:59Z",      # UTC representation of the past
])
def test_expired_free_cancellation_is_not_recommended(deadline):
    item, request = hotel_component(deadline)
    assert item["conditions"]["free_cancellation"] is False
    assert not component_options_from_search({"hotels": [item]}, request, 1)["hotel_options"]


@pytest.mark.parametrize("deadline", ["not-a-date", "2026-11-01", "2026-11-01T12:00:00"])
def test_unverifiable_cancellation_deadline_does_not_claim_free_cancellation(deadline):
    item, request = hotel_component(deadline)
    assert item["conditions"]["free_cancellation"] is None
    assert not component_options_from_search({"hotels": [item]}, request, 1)["hotel_options"]


def test_future_cancellation_window_remains_eligible_across_timezones():
    item, request = hotel_component("2026-10-28T09:00:01Z")
    assert item["conditions"]["free_cancellation"] is True
    assert len(component_options_from_search({"hotels": [item]}, request, 1)["hotel_options"]) == 1


def test_deadline_expiration_changes_refresh_without_losing_tariff_identity():
    selected, _ = hotel_component("2026-10-28T09:01:00Z")
    refreshed, _ = hotel_component("2026-10-28T09:01:00Z", now="2026-10-28T09:02:00Z")
    result = match_refresh([selected], [refreshed])
    assert result["status"] == "changed"
    assert result["components"][0]["conditions"]["free_cancellation"] is False


@pytest.mark.parametrize("products,return_word", [(["hotel"], "выезда"), (["avia"], "возвращения")])
async def test_invalid_return_reply_keeps_the_departure_date_and_asks_for_return(tmp_path, products, return_word):
    from test_component_flow import ScriptedGateway, ComponentAdapter, turn
    from travel_agent.service import AgentService
    request = {**REQUEST, "products": products, "return_date": None}
    gateway = ScriptedGateway(request, {"31 ноября": {"intent": "plan", "patch": {}}})
    adapter = ComponentAdapter()
    service = AgentService(db_path=tmp_path / "calendar-reply.sqlite", gateway=gateway, adapter=adapter)
    try:
        initial = await service.run_turn(turn())
        result = await service.run_turn(turn(initial.state_version, text="31 ноября"))
        saved = await service.get_session("component-user", "trip")
        assert str(saved.request.departure_date) == REQUEST["departure_date"]
        assert saved.request.return_date is None
        assert "не существует" in result.message and return_word in result.message
        assert not adapter.calls
    finally:
        service.close()


@pytest.mark.parametrize("text,question,target", [
    ("31 ноября", "Когда хотите вернуться?", "return_date"),
    ("31 ноября", "Когда планируете выезд?", "return_date"),
    ("31 ноября", "Когда хотите вылететь?", "departure_date"),
    ("Вылет перенеси на 31 ноября", "Когда хотите вернуться?", "departure_date"),
    ("Вернусь 31 ноября", "Когда хотите вылететь?", "return_date"),
])
def test_invalid_single_date_respects_the_question_and_explicit_correction(text, question, target):
    from datetime import date
    from travel_agent.domain.input_validation import calendar_correction
    patch, message = calendar_correction(text, REQUEST, date(2026, 10, 28), last_question=question)
    assert patch == {target: None}
    assert "не существует" in message


OPTIONAL_SEARCH_QUESTIONS = [
    "Нужно ли включить что-то ещё в поиск?",
    "Надо ли что-нибудь ещё учесть?",
    "Нужно ли добавить ещё что-нибудь?",
    "Хотите добавить что-либо ещё?",
    "Есть ли ещё пожелания?",
    "Есть ли у вас дополнительные предпочтения к поиску?",
    "Остались ли у вас какие-то пожелания?",
    "Начать поиск?",
    "Начнём подбор вариантов?",
    "Запустить поиск отелей?",
]


@pytest.mark.parametrize("question", OPTIONAL_SEARCH_QUESTIONS)
async def test_complete_hotel_request_starts_search_without_optional_questionnaire(tmp_path, question):
    from test_component_flow import ScriptedGateway, ComponentAdapter, turn
    from travel_agent.service import AgentService
    text = "Только отель, с 15 по 20 ноября 2026, я один, бюджет 30000 рублей за всё"
    request = {**REQUEST, "origin": None, "destination": "Казань", "products": ["hotel"],
               "departure_date": "2026-11-15", "return_date": "2026-11-20", "adults": 1,
               "budget_minor": 3000000, "budget_scope": "trip"}
    gateway = ScriptedGateway(request, {text: {"intent": "plan", "patch": request, "question": question}})
    adapter = ComponentAdapter()
    service = AgentService(db_path=tmp_path / "optional-question.sqlite", gateway=gateway, adapter=adapter)
    try:
        result = await service.run_turn(turn(text=text))
        assert len(adapter.calls) == 1, result.model_dump()
        assert result.status == "options_ready" and result.hotel_options
        assert result.questions == [] and question not in result.message
        assert adapter.calls[0]["products"] == ["hotel"]
        assert adapter.calls[0]["budget_minor"] == 3000000
    finally:
        service.close()


@pytest.mark.parametrize("question", [
    "Вы имеете в виду Казань в России или другой город?",
    "Указанный бюджет — на всех или на одного человека?",
    "Сколько лет каждому ребёнку на дату поездки?",
    "Нужно ли включить в поиск ещё один номер для ребёнка?",
    "Нужно ли учесть проезд с инвалидной коляской?",
    "Начать поиск с возвращением 22 ноября вместо 20 ноября?",
    "Начать бронирование?",
    "Бюджета недостаточно. Увеличить его до 40000 рублей?",
    "Есть ли подтверждение, что отель принимает питомцев?",
])
def test_concrete_or_commitment_questions_are_never_optional(question):
    from travel_agent.domain.clarifications import optional_preferences_question
    request = {**REQUEST, "products": ["hotel"], "budget_minor": 3000000, "budget_scope": "trip"}
    assert not optional_preferences_question(question, request)


@pytest.mark.parametrize("missing", ["destination", "departure_date", "return_date", "adults"])
@pytest.mark.parametrize("question", OPTIONAL_SEARCH_QUESTIONS)
def test_generic_question_is_not_skipped_if_required_trip_data_is_missing(missing, question):
    from travel_agent.domain.clarifications import optional_preferences_question
    request = {**REQUEST, "products": ["hotel"], missing: None}
    assert not optional_preferences_question(question, request)


def test_generic_question_does_not_override_unresolved_budget_scope():
    from travel_agent.domain.clarifications import optional_preferences_question
    request = {**REQUEST, "products": ["hotel"], "budget_minor": 3000000, "budget_scope": None}
    assert not optional_preferences_question("Нужно ли включить что-то ещё в поиск?", request)


@pytest.mark.parametrize("budget,scope,limit,status", [
    (5000, "trip", 5000, "violated"),
    (800000, "trip", 800000, "satisfied"),
    (400000, "per_person", 800000, "satisfied"),
])
def test_budget_check_names_the_cost_and_ceiling_without_changing_contract(budget, scope, limit, status):
    from travel_agent.domain.planning import check_components, rub
    detail = hotel("first")
    room = detail["rooms"][0]
    request = {**REQUEST, "products": ["hotel"], "budget_minor": budget, "budget_scope": scope}
    component = normalize_hotel(detail, room, room["rates"][0], request, NOW)
    check = next(c for c in check_components([component], request) if c["constraint_id"] == "budget_known_components")
    assert check["status"] == status and check["hard"] is True
    assert check["actual"] == 800000 and check["required"] == limit
    assert check["explanation"] == f"Стоимость выбранных компонентов: 8 000 ₽; ваш лимит — не более {rub(limit)}"


@pytest.mark.parametrize("products,expected,excluded", [
    (["hotel"], "размещением питомца", "перевозкой"),
    (["avia"], "подтверждённой перевозкой питомца", "размещением"),
    (["rail"], "подтверждённой перевозкой питомца", "размещением"),
    (["bus"], "подтверждённой перевозкой питомца", "размещением"),
    (["avia", "hotel"], "размещением или перевозкой питомца", None),
])
def test_pet_diagnostic_names_only_requested_product_services(products, expected, excluded):
    from travel_agent.domain.search_diagnostics import format_search_diagnostics
    diagnostic = {"code": "conditions_unmet", "requested_products": products,
                  "reasons": ["Размещение с питомцем: нет подтверждённых данных; требуется да"]}
    result = format_search_diagnostics(diagnostic)
    assert expected in result["message"]
    assert excluded is None or excluded not in result["message"]
    assert result["question"] == "Какое условие готовы изменить?"



def test_hotel_meal_tariffs_do_not_collapse_or_refresh_into_another_meal():
    from copy import deepcopy
    detail = hotel("same")
    room = detail["rooms"][0]
    breakfast = deepcopy(room["rates"][0])
    breakfast["meal"] = {"name": "Завтрак"}
    full_board = deepcopy(breakfast)
    full_board["meal"] = {"name": "Полный пансион"}
    full_board["price"]["amount"] = 20000
    request = {**REQUEST, "products": ["hotel"]}
    first = normalize_hotel(detail, room, breakfast, request, NOW)
    second = normalize_hotel(detail, room, full_board, request, NOW)
    assert first["provider_refs"]["reference_key"] != second["provider_refs"]["reference_key"]
    cards = component_options_from_search({"hotels": [first, second]}, request, 1)["hotel_options"]
    assert {card["conditions"]["meal"] for card in cards} == {"Завтрак", "Полный пансион"}
    assert match_refresh([first], [second])["status"] == "unknown"
    breakfast["price"]["amount"] = 9000
    repriced = normalize_hotel(detail, room, breakfast, request, NOW)
    assert match_refresh([first], [repriced])["status"] == "changed"


@pytest.mark.parametrize("capacity,adults,children,eligible", [
    (1, 2, [], False), (2, 2, [], True), (None, 2, [], True),
    (2, 2, [7], False), (3, 2, [7], True),
])
def test_explicit_room_capacity_must_cover_every_requested_guest(capacity, adults, children, eligible):
    detail = hotel("capacity")
    room = detail["rooms"][0]
    room["max_occupancy"] = capacity
    request = {**REQUEST, "products": ["hotel"], "adults": adults, "children_ages": children}
    component = normalize_hotel(detail, room, room["rates"][0], request, NOW)
    cards = component_options_from_search({"hotels": [component]}, request, 1)["hotel_options"]
    assert bool(cards) is eligible


@pytest.mark.parametrize("text,departure,returning", [
    ("Заезд 31 декабря, выезд 31 февраля 2027", "2026-12-31", None),
    ("Заезд 31 декабря 2026, выезд 31 февраля", "2026-12-31", None),
    ("Заезд 31 января, выезд 31 февраля 2027", "2027-01-31", None),
    ("Заезд 31 ноября, выезд 5 декабря 2026", None, "2026-12-05"),
])
def test_invalid_cross_year_date_does_not_move_valid_date_to_the_wrong_year(text, departure, returning):
    from datetime import date
    from travel_agent.domain.input_validation import calendar_correction
    patch, question = calendar_correction(text, {**REQUEST, "products": ["hotel"]}, date(2026, 10, 28))
    assert patch == {"departure_date": departure, "return_date": returning}
    assert "не существует" in question


def test_generic_children_offer_is_optional_when_no_children_were_requested():
    from travel_agent.domain.clarifications import optional_preferences_question
    request = {**REQUEST, "products": ["hotel"]}
    assert optional_preferences_question("Нужно ли учитывать детей при заселении?", request)


@pytest.mark.parametrize("question", ["Сколько лет детям при заселении?", "Нужно ли учитывать детей при заселении, если их возраст изменится?"])
def test_concrete_children_clarifications_are_preserved(question):
    from travel_agent.domain.clarifications import optional_preferences_question
    assert not optional_preferences_question(question, {**REQUEST, "products": ["hotel"]})


async def test_generic_children_offer_cannot_skip_pending_child_ages(tmp_path):
    from test_component_flow import ScriptedGateway, ComponentAdapter, turn
    from travel_agent.service import AgentService
    text = "Нужен отель в Казани с 15 по 20 ноября 2026, двое взрослых и ребёнок"
    request = {**REQUEST, "products": ["hotel"], "children_ages": []}
    gateway = ScriptedGateway(request, {text: {"intent": "plan", "patch": request,
        "question": "Нужно ли учитывать детей при заселении?"}})
    adapter = ComponentAdapter()
    service = AgentService(db_path=tmp_path / "pending-child.sqlite", gateway=gateway, adapter=adapter)
    try:
        result = await service.run_turn(turn(text=text))
        assert result.status == "needs_input" and not adapter.calls
        assert "Сколько лет каждому ребёнку" in result.message
    finally:
        service.close()
