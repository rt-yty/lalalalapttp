"""Diagnostics respect requested products and transport alternatives."""
from copy import deepcopy
import json

import pytest

from travel_agent.domain.search_diagnostics import build_search_diagnostics, format_search_diagnostics, legacy_search_diagnostics

NOW = "2026-10-28T12:00:00+03:00"
REQUEST = {"origin": "Москва", "destination": "Санкт-Петербург", "departure_date": "2026-11-10",
           "return_date": "2026-11-17", "adults": 1, "children_ages": []}


def fare(kind):
    return {"product_type": kind, "price_minor": 100000, "currency": "RUB", "title": "Test",
            "provider_refs": {"hotel_id": "h1"} if kind == "hotel" else {}}


def row(kind, count, **extra):
    return {"product": kind, "count": count, "query": deepcopy(REQUEST), "complete": True,
            "total": count, "total_exact": True, "pages": [1], "stop_reason": "end_of_results", **extra}


def result(*, flights=0, trains=0, hotels=0):
    return {"flights": [fare("avia") for _ in range(flights)], "trains": [fare("rail") for _ in range(trains)],
            "hotels": [fare("hotel") for _ in range(hotels)], "component_errors": {}, "error": None,
            "coverage": [row("avia", flights), row("rail", trains), row("hotel", hotels)]}


def diagnose(data, products, **kwargs):
    return build_search_diagnostics(data, {**REQUEST, "products": products}, NOW, **kwargs)


def message(diag):
    return format_search_diagnostics(diag)["message"].lower()


def test_hotel_only_missing_never_mentions_flights_or_origin():
    diag = diagnose(result(), ["hotel"])
    assert diag["code"] == "missing_hotels"
    answer = format_search_diagnostics(diag)
    assert "проживания на эти даты не нашлось" in answer["message"]
    assert "перел" not in answer["message"].lower() and "москва" not in answer["message"].lower()
    assert answer["question"] == "Рассмотрим другой город?"


def test_hotel_only_candidates_do_not_require_transport_or_price_pair():
    diag = diagnose(result(hotels=1), ["hotel"])
    assert diag["code"] == "candidates_received"
    assert "avia" not in diag["products"]


def test_avia_only_candidates_do_not_require_hotels():
    assert diagnose(result(flights=1), ["avia"])["code"] == "candidates_received"


@pytest.mark.parametrize("products", [["rail"], ["rail", "hotel"]])
def test_rail_missing_names_trains_not_flights(products):
    diag = diagnose(result(hotels=1), products)
    assert diag["code"] == "missing_trains"
    text = message(diag)
    assert "билетов на поезд" in text and "перел" not in text
    assert "повтор" not in text and "ещё раз" not in text


def test_empty_rail_and_hotel_names_both_requested_parts():
    diag = diagnose(result(), ["rail", "hotel"])
    assert diag["code"] == "missing_both"
    assert "поезд" in message(diag) and "проживания" in message(diag)
    assert "перел" not in message(diag)


def test_train_alternative_prevents_missing_flight_error():
    diag = diagnose(result(trains=1, hotels=1), ["avia", "rail", "hotel"])
    assert diag["code"] == "candidates_received"
    assert diag["counts"]["trains"] == 1
    assert "перелётов на эти даты не нашлось" not in message(diag)


def test_flight_alternative_prevents_missing_train_error():
    diag = diagnose(result(flights=1), ["avia", "rail"])
    assert diag["code"] == "candidates_received"


def test_both_transport_alternatives_empty_have_transport_diagnostic():
    diag = diagnose(result(hotels=1), ["avia", "rail", "hotel"])
    assert diag["code"] == "missing_transport"
    assert "авиабилетов и билетов на поезд" in message(diag)


def test_missing_train_array_is_unknown_not_proven_empty():
    data = result()
    del data["trains"]
    data["coverage"] = []
    diag = diagnose(data, ["rail"])
    assert diag["code"] == "candidates_received" and diag["counts"]["trains"] is None
    assert "билетов на поезд" not in message(diag)


def test_unusable_rail_fares_are_not_claimed_to_be_empty_inventory():
    data = result()
    data["coverage"] = [row("rail", 8)]
    diag = diagnose(data, ["rail"])
    assert diag["code"] == "no_usable_fares"
    assert "не удалось подобрать поездку на поезде" in message(diag)
    assert "на эти даты не нашлось" not in message(diag)


def test_one_empty_direction_means_no_round_trip_without_hiding_other_direction():
    data = result()
    data["coverage"] = [row("rail", 8, direction="outbound"), row("rail", 0, direction="inbound")]
    diag = diagnose(data, ["rail"])
    assert diag["code"] == "missing_trains"
    assert diag["products"]["rail"]["directions"]["outbound"]["raw_count"] == 8
    assert diag["products"]["rail"]["directions"]["inbound"]["raw_count"] == 0
    assert "туда и обратно" in message(diag)


def test_unsupported_party_does_not_claim_no_trains_or_offer_retry():
    data = result(hotels=1)
    data["component_errors"]["rail"] = {"code": "unsupported_party", "retryable": True, "message": "opaque secret"}
    diag = diagnose(data, ["rail", "hotel"])
    answer = format_search_diagnostics(diag)
    assert diag["code"] == "partial_provider_error"
    assert "состава путешественников" in answer["message"]
    assert "я пока не могу подобрать" in answer["message"] and answer["question"] is None
    assert "не нашлось" not in answer["message"] and "secret" not in json.dumps(diag)
    assert diag["counts"]["hotel_rates"] == 1


def test_hotel_timeout_is_local_and_keeps_transport_counts():
    data = result(trains=1)
    data["component_errors"]["hotel"] = {"code": "timeout", "retryable": True}
    diag = diagnose(data, ["rail", "hotel"])
    answer = format_search_diagnostics(diag)
    assert diag["counts"]["trains"] == 1 and diag["code"] == "partial_provider_error"
    assert "не успел завершить поиск отелей" in answer["message"]
    assert "за отведённое время" in answer["message"] and "не нашлось" not in answer["message"]
    assert answer["question"] == "Попробовать ещё раз?"
    assert format_search_diagnostics(diag, followup=True)["question"] is None


def test_error_from_unrequested_product_does_not_poison_hotel_only():
    data = result(hotels=1)
    data["component_errors"]["rail"] = {"code": "unsupported_party"}
    diag = diagnose(data, ["hotel"])
    assert diag["code"] == "candidates_received" and diag["error"] is None
    assert diag["component_errors"] == {}


def test_unverified_rail_fares_keep_their_distinct_diagnostic():
    data = result()
    data["component_errors"]["rail"] = {"code": "unverified_fares", "retryable": True}
    diag = diagnose(data, ["rail"])
    assert diag["code"] == "provider_error"
    assert "не смог подтвердить тарифы и места" in message(diag)
    assert "не нашлось" not in message(diag)


def test_product_specific_conditions_use_single_product_candidate_count():
    data = result(hotels=1)
    rejection = [[{"status": "violated", "explanation": "Бюджет: требуется дешевле"}]]
    diag = diagnose(data, ["hotel"], rejections=rejection)
    assert diag["code"] == "conditions_unmet" and diag["reasons"] == ["Бюджет: требуется дешевле"]


def test_legacy_rail_matches_actual_directional_query_shape():
    data = []
    for direction, origin, destination, day, count in (
        ("outbound", "Москва", "Санкт-Петербург", "2026-11-10", 4),
        ("inbound", "Санкт-Петербург", "Москва", "2026-11-17", 0)):
        data.append(row("rail", count, direction=direction, query={"origin": origin, "destination": destination,
            "departure_date": day, "passengers": 1, "view": "full", "direct_only": True}))
    diag = legacy_search_diagnostics(data, {**REQUEST, "products": ["rail"]})
    assert diag["code"] == "missing_trains" and diag["counts"]["trains"] is None
    assert "туда и обратно" in message(diag)
    assert legacy_search_diagnostics(data, {**REQUEST, "products": ["rail"], "adults": 2}) is None


def test_default_avia_hotel_count_contract_is_unchanged():
    diag = build_search_diagnostics(result(hotels=1), REQUEST, NOW)
    assert diag["counts"] == {"flights": 0, "hotel_rates": 1, "hotels": 1}
    assert diag["code"] == "missing_flights"
