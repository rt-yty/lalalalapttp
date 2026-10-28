"""Travel-search explanations distinguish empty, unverified, and failed searches."""
from copy import deepcopy
import json

import pytest

from travel_agent.domain.search_diagnostics import (
    build_search_diagnostics, format_search_diagnostics, legacy_search_diagnostics,
)

NOW = "2026-10-27T20:10:00+03:00"
REQUEST = {"origin": "Москва", "destination": "Хайфа", "departure_date": "2026-10-28", "return_date": "2026-10-31", "adults": 1, "children_ages": []}


def rate(hotel_id=None, *, title="Отель", price=10000):
    return {"title": title, "price_minor": price, "currency": "RUB", "provider_refs": {"hotel_id": hotel_id} if hotel_id else {}}


def coverage(product, count, *, complete=True, **extra):
    return {"provider": "tutu", "product": product, "query": deepcopy(REQUEST), "count": count,
            "pages": [1], "complete": complete, "total": count, "total_exact": True,
            "stop_reason": "end_of_results", **extra}


def screenshot_result():
    return {"flights": [], "hotels": [rate("a"), rate("a"), rate("b"), rate("c")], "error": None,
            "coverage": [coverage("avia", 0), coverage("hotel", 20, complete=False, detailed_hotels=3,
                                                        pages=[1, 2], stop_reason="detail_limit")]}


def test_empty_flight_keeps_precise_evidence_but_shows_only_useful_next_step():
    result = screenshot_result()
    original = deepcopy(result)
    diag = build_search_diagnostics(result, REQUEST, NOW)
    assert diag["code"] == "missing_flights"
    assert diag["counts"] == {"flights": 0, "hotel_rates": 4, "hotels": 3}
    assert diag["products"]["hotel"]["raw_count"] == 20
    assert diag["products"]["hotel"]["detailed_hotels"] == 3
    answer = format_search_diagnostics(diag)
    assert "Москва → Хайфа, 28–31 октября 2026" in answer["message"]
    assert "перелётов на эти даты не нашлось" in answer["message"]
    for detail in ("туту", "тариф", "отел", "причину", "ограничена", "повтор"):
        assert detail not in answer["message"].lower()
    assert "предложений отелей: 20" not in answer["message"]
    assert answer["question"] == "Рассмотрим другой город?"
    assert len(answer["message"]) < 160
    assert result == original


def test_followup_uses_saved_facts_without_repeating_a_question():
    diag = build_search_diagnostics(screenshot_result(), REQUEST, NOW)
    followup = format_search_diagnostics(diag, followup=True)
    assert "перелётов на эти даты не нашлось" in followup["message"]
    assert "тариф" not in followup["message"] and "Туту" not in followup["message"]
    assert len(followup["message"]) < 120
    assert followup["question"] is None


def test_raw_offers_without_normalized_fares_are_not_called_empty_air_search():
    result = screenshot_result()
    result["coverage"][0] = coverage("avia", 6)
    diag = build_search_diagnostics(result, REQUEST, NOW)
    assert diag["code"] == "no_usable_fares"
    message = format_search_diagnostics(diag)["message"]
    assert "не удалось подобрать перелёт по этим условиям" in message
    assert "перелётов на эти даты не нашлось" not in message
    assert "(6)" not in message and "Туту" not in message
    assert diag["products"]["avia"]["raw_count"] == 6


def test_missing_coverage_does_not_prove_empty_provider_search():
    result = screenshot_result()
    result["coverage"] = []
    diag = build_search_diagnostics(result, REQUEST, NOW)
    assert diag["products"] == {} and diag["counts"]["flights"] == 0
    message = format_search_diagnostics(diag)["message"]
    assert "не удалось подобрать перелёт по этим условиям" in message
    assert "перелётов на эти даты не нашлось" not in message


def test_missing_normalized_array_is_unknown_not_zero():
    result = screenshot_result()
    result.pop("flights")
    result["coverage"] = []
    diag = build_search_diagnostics(result, REQUEST, NOW)
    assert diag["counts"]["flights"] is None
    assert diag["code"] == "candidates_received"
    assert "Туту не вернул" not in format_search_diagnostics(diag)["message"]


@pytest.mark.parametrize("error_code,retryable,phrase", [("timeout", True, "не успел завершить поиск"), ("schema_changed", False, "не смог проверить найденные предложения"), ("fixture_missing", False, "в архиве")])
def test_provider_error_keeps_partial_counts_and_never_becomes_empty_search(error_code, retryable, phrase):
    result = screenshot_result()
    result["error"] = {"code": error_code, "retryable": retryable, "message": "secret opaque provider error", "request_id": "secret-id"}
    diag = build_search_diagnostics(result, REQUEST, NOW)
    assert diag["code"] == "provider_error"
    assert diag["error"] == {"code": error_code, "retryable": retryable}
    assert diag["counts"]["hotel_rates"] == 4
    answer = format_search_diagnostics(diag)
    assert phrase in answer["message"].lower()
    assert "Туту не вернул авиапредложений" not in answer["message"]
    assert bool(answer["question"]) is retryable
    assert "secret" not in json.dumps(diag)


def test_incomplete_air_coverage_only_describes_checked_pages():
    result = screenshot_result()
    result["coverage"][0] = coverage("avia", 0, complete=False, stop_reason="page_limit", pages=[1, 2])
    message = format_search_diagnostics(build_search_diagnostics(result, REQUEST, NOW))["message"]
    assert "перелётов на эти даты не нашлось" in message
    assert "страниц" not in message and "ограничен" not in message
    diag = build_search_diagnostics(result, REQUEST, NOW)
    assert diag["products"]["avia"]["complete"] is False
    assert diag["products"]["avia"]["pages_read"] == 2


def test_constraint_reasons_choose_nearest_checked_candidate_and_preserve_budget():
    result = {"flights": [rate()], "hotels": [rate("a"), rate("b")], "coverage": [], "error": None}
    rejections = [[{"constraint_id": "budget_known_components", "status": "violated", "explanation": "Бюджет перелёта и отеля: 67 920 ₽; требуется 50 000 ₽", "evidence_refs": ["secret-provider-ref"]}],
                  [{"status": "unknown", "explanation": "Багаж не подтверждён"}, {"status": "unknown", "explanation": "Завтрак не подтверждён"}]]
    before = deepcopy(rejections)
    diag = build_search_diagnostics(result, REQUEST, NOW, rejections)
    assert diag["code"] == "conditions_unmet"
    assert diag["reasons"] == [rejections[0][0]["explanation"]]
    answer = format_search_diagnostics(diag, followup=True)
    assert "67 920" in answer["message"] and "50 000" in answer["message"]
    assert answer["question"] is None and "secret" not in json.dumps(diag)
    assert rejections == before


def test_some_rejections_do_not_mean_every_candidate_failed():
    result = {"flights": [rate()], "hotels": [rate("a"), rate("b")], "coverage": []}
    diag = build_search_diagnostics(result, REQUEST, NOW, [[{"status":"violated","explanation":"Один тариф не подошёл"}]])
    assert diag["code"] == "candidates_received" and diag["reasons"] == []


def test_pin_mismatch_explains_missing_pinned_hotel():
    result = {"flights": [rate()], "hotels": [rate("a", title="Другой отель")], "coverage": []}
    diag = build_search_diagnostics(result, REQUEST, NOW, pins={"hotel":"Выбранный отель"})
    assert diag["code"] == "conditions_unmet"
    assert "Закреплённый отель" in diag["reasons"][0]


@pytest.mark.parametrize("price", [True, -1, 1.2, None])
def test_invalid_price_pairs_do_not_invent_budget_reason(price):
    result = {"flights": [rate(price=price)], "hotels": [rate("a")], "coverage": []}
    diag = build_search_diagnostics(result, REQUEST, NOW)
    assert diag["code"] == "no_priced_pairs"
    assert "не удалось подтвердить стоимость поездки" in format_search_diagnostics(diag)["message"]
    assert format_search_diagnostics(diag)["question"] is None
    assert "бюджет" not in format_search_diagnostics(diag)["message"].lower()


def test_missing_hotel_ids_make_unique_hotel_count_unknown():
    result = screenshot_result()
    result["hotels"].append(rate())
    diag = build_search_diagnostics(result, REQUEST, NOW)
    assert diag["counts"]["hotel_rates"] == 5 and diag["counts"]["hotels"] is None


def test_boolean_counts_are_not_zero_or_one_and_no_opaque_fields_survive():
    result = {"flights": [], "hotels": [], "secret": "provider-data", "coverage": [
        coverage("avia", False, total=True, pages=[True, False], stop_reason={"secret":"value"}),
        coverage("hotel", True, detailed_hotels=True)], "error": {"code":{"secret":"value"},"retryable":1,"message":"secret"}}
    diag = build_search_diagnostics(result, REQUEST, NOW)
    assert diag["products"]["avia"]["raw_count"] is None and diag["products"]["avia"]["total"] is None
    assert diag["products"]["hotel"]["detailed_hotels"] is None
    assert diag["products"]["avia"]["pages_read"] == 0
    assert diag["error"] == {"code":"unavailable","retryable":False}
    assert "secret" not in json.dumps(diag)


def test_legacy_only_uses_matching_raw_coverage_not_normalized_counts():
    rows = screenshot_result()["coverage"]
    original = deepcopy(rows)
    diag = legacy_search_diagnostics(rows, REQUEST)
    assert diag["code"] == "missing_flights"
    assert diag["counts"] == {"flights":None,"hotel_rates":None,"hotels":None}
    assert diag["products"]["hotel"]["raw_count"] == 20
    message = format_search_diagnostics(diag, followup=True)["message"]
    assert "перелётов на эти даты не нашлось" in message
    assert "отелей" not in message and "тариф" not in message
    assert diag["products"]["hotel"]["raw_count"] == 20
    assert rows == original


@pytest.mark.parametrize("rows", [[], [coverage("avia", 0, query={**REQUEST,"destination":"Другой город"})], [{"product":"avia","count":0}]])
def test_unmatched_or_unbound_legacy_coverage_is_not_attached_to_current_trip(rows):
    assert legacy_search_diagnostics(rows, REQUEST) is None


def test_legacy_missing_product_is_unknown_and_booleans_do_not_prove_absence():
    diag = legacy_search_diagnostics([coverage("hotel", 20)], REQUEST)
    assert diag["code"] == "legacy_coverage" and "avia" not in diag["products"]
    assert "Туту не вернул авиапредложений" not in format_search_diagnostics(diag)["message"]
    diag = legacy_search_diagnostics([coverage("avia", False)], REQUEST)
    assert diag["code"] == "legacy_coverage"


def test_query_is_bounded_and_dates_are_formatted_from_saved_request():
    request = {**REQUEST,"origin":"мск","destination":"Г"*1000,"departure_date":"2026-11-24","return_date":"2026-12-01", "api_key":"secret", "children_ages":[True,5,{"secret":"value"}]}
    diag = build_search_diagnostics(screenshot_result(), request, "invalid timestamp")
    assert diag["query"]["origin"] == "Москва" and len(diag["query"]["destination"]) == 120
    assert diag["query"]["children_ages"] == [5] and diag["searched_at"] is None
    assert "24 ноября — 1 декабря 2026" in format_search_diagnostics(diag)["message"]
    assert "secret" not in json.dumps(diag)
    assert len(json.dumps(diag, ensure_ascii=False)) < 3000


def test_replay_explanation_never_claims_current_availability():
    diag = build_search_diagnostics(screenshot_result(), REQUEST, NOW)
    assert "архива, не текущая доступность" in format_search_diagnostics(diag, data_mode="replay")["message"]


@pytest.mark.parametrize("code", ["missing_flights", "missing_hotels", "missing_both", "no_usable_fares", "no_priced_pairs", "legacy_coverage", "conditions_unmet"])
def test_empty_or_unusable_results_never_offer_the_same_search_again(code):
    diag = build_search_diagnostics(screenshot_result(), REQUEST, NOW)
    diag["code"] = code
    for followup in (False, True):
        answer = format_search_diagnostics(diag, followup=followup)
        assert "повтор" not in answer["message"].lower()
        assert "ещё раз" not in answer["message"].lower()


@pytest.mark.parametrize("raw_hotel,expected", [(0, "подходящего проживания на эти даты не нашлось"), (6, "не удалось подобрать проживание по этим условиям")])
def test_missing_or_unusable_hotel_has_a_short_distinct_outcome(raw_hotel, expected):
    result = {"flights": [rate()], "hotels": [], "coverage": [coverage("avia", 1), coverage("hotel", raw_hotel)]}
    diag = build_search_diagnostics(result, REQUEST, NOW)
    answer = format_search_diagnostics(diag)
    assert expected in answer["message"]
    assert answer["question"] == "Рассмотрим другой город?"
    assert "Туту" not in answer["message"]
