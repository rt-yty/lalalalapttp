"""Small factual explanations of a travel search, without provider records."""
from __future__ import annotations

from datetime import date, datetime
import re

from travel_agent.adapters.normalize import search_key

_ERROR_MESSAGES = {
    "timeout": "Я не успел завершить поиск за отведённое время.",
    "rate_limited": "Я пока не могу продолжить поиск. Смогу повторить его чуть позже.",
    "unavailable": "Я пока не смог завершить поиск.",
    "schema_changed": "Я пока не смог проверить найденные предложения.",
    "unsupported": "Я пока не смог проверить этот маршрут.",
    "call_limit": "Я пока не смог завершить поиск.",
    "invalid_arguments": "Я не смог проверить поездку с указанными параметрами.",
    "fixture_missing": "Я не нашёл эту поездку в архиве. Текущие предложения я ещё не проверял.",
    "stale_offer": "Я больше не могу подтвердить выбранный вариант.",
    "unsupported_party": "Я пока не могу подобрать поездку для такого состава путешественников.",
    "unverified_fares": "Я не смог подтвердить тарифы и наличие мест для всех путешественников.",
}

_STOP_REASONS = {"end_of_results", "page_limit", "detail_limit", "detail_error", "call_limit", "error", "fare_limit", "unverified_fares", "selected_hotels"}
_MONTHS = ("", "января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря")


def _text(value, limit=300):
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", value)).strip()[:limit]


def _number(value):
    return value if type(value) is int and 0 <= value <= 1_000_000_000 else None


def _day(value):
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(value).isoformat() if isinstance(value, str) and len(value) == 10 else ""
    except ValueError:
        return ""


def _query(request):
    request = request if isinstance(request, dict) else {}
    ages = request.get("children_ages")
    return search_key({
        "origin": _text(request.get("origin"), 120), "destination": _text(request.get("destination"), 120),
        "departure_date": _day(request.get("departure_date")), "return_date": _day(request.get("return_date")),
        "adults": request.get("adults") if type(request.get("adults")) is int and 1 <= request["adults"] <= 9 else None,
        "children_ages": [age for age in ages[:8] if type(age) is int and 0 <= age <= 17] if isinstance(ages, list) else [],
    })


def _products(coverage):
    products = {}
    for row in coverage[:20] if isinstance(coverage, list) else []:
        if not isinstance(row, dict) or row.get("product") not in {"avia", "rail", "bus", "hotel"}:
            continue
        pages = row.get("pages")
        item = {
            "raw_count": _number(row.get("count")), "total": _number(row.get("total")),
            "total_exact": row.get("total_exact") if type(row.get("total_exact")) is bool else None,
            "complete": row.get("complete") if type(row.get("complete")) is bool else None,
            "pages_read": len({page for page in pages[:20] if type(page) is int and page >= 1}) if isinstance(pages, list) else None,
            "stop_reason": row.get("stop_reason") if isinstance(row.get("stop_reason"), str) and row["stop_reason"] in _STOP_REASONS else None,
            "detailed_hotels": _number(row.get("detailed_hotels")) if row["product"] == "hotel" else None,
        }
        kind = row["product"]
        if kind in ("rail", "bus") and row.get("direction") in ("outbound", "inbound"):
            previous = products.get(kind, {})
            directions = {**previous.get("directions", {}), row["direction"]: item}
            rows = list(directions.values())
            item = {**item, "directions": directions,
                    "raw_count": sum(r["raw_count"] for r in rows) if all(r["raw_count"] is not None for r in rows) else None,
                    "complete": len(directions) == 2 and all(r["complete"] is True for r in rows),
                    "total": None, "total_exact": None}
        products[kind] = item
    return products


def _requested(request):
    values = request.get("products") if isinstance(request, dict) else None
    if not isinstance(values, list):
        return ["avia", "hotel"]
    return list(dict.fromkeys(v for v in values if isinstance(v, str) and v in {"avia", "rail", "bus", "hotel"})) or ["avia", "hotel"]


def _raw_empty(kind, products):
    item = products.get(kind, {})
    if kind in ("rail", "bus") and item.get("directions"):
        return any(row.get("raw_count") == 0 for row in item["directions"].values())
    return item.get("raw_count") == 0


def _safe_error(error):
    if not error:
        return None
    error = error if isinstance(error, dict) else {}
    code = error.get("code")
    return {"code": code if isinstance(code, str) and code in _ERROR_MESSAGES else "unavailable", "retryable": error.get("retryable") is True}


def _rates(value):
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else None


def _hotel_count(hotels):
    if hotels is None:
        return None
    ids = []
    for hotel in hotels:
        refs = hotel.get("provider_refs")
        value = refs.get("hotel_id") if isinstance(refs, dict) else None
        if not isinstance(value, (str, int)) or isinstance(value, bool) or not str(value).strip():
            return None
        ids.append(str(value))
    return len(set(ids))


def _priced(rate):
    return rate.get("currency") == "RUB" and type(rate.get("price_minor")) is int and rate["price_minor"] >= 0


def _closest_reasons(rejections):
    rows = [row for row in rejections if isinstance(row, list) and row] if isinstance(rejections, list) else []
    if not rows:
        return []
    row = min(rows, key=lambda checks: (len(checks), sum(isinstance(check, dict) and check.get("status") == "unknown" for check in checks)))
    reasons = []
    for check in row:
        if not isinstance(check, dict):
            continue
        explanation = _text(check.get("explanation"), 350)
        if explanation and explanation not in reasons:
            reasons.append(explanation)
        if len(reasons) == 3:
            break
    return reasons


def build_search_diagnostics(result: dict, request: dict, now: str,
                             rejections: list | None = None, pins: dict | None = None) -> dict:
    """Copy normalized counts, coverage, and domain rejection reasons only."""
    result = result if isinstance(result, dict) else {}
    requested = _requested(request)
    flights, trains, hotels = _rates(result.get("flights")), _rates(result.get("trains")), _rates(result.get("hotels"))
    buses = _rates(result.get("buses"))
    counts = {"flights": len(flights) if flights is not None else None,
              "hotel_rates": len(hotels) if hotels is not None else None,
              "hotels": _hotel_count(hotels)}
    if "rail" in requested:
        counts["trains"] = len(trains) if trains is not None else None
    if "bus" in requested:
        counts["buses"] = len(buses) if buses is not None else None
    products = _products(result.get("coverage"))
    products = {kind: value for kind, value in products.items() if kind in requested}
    error, reasons = _safe_error(result.get("error")), _closest_reasons(rejections)
    raw_errors = result.get("component_errors")
    component_errors = {kind: _safe_error(raw_errors[kind]) for kind in requested
                        if isinstance(raw_errors, dict) and raw_errors.get(kind)}
    error_product = next(iter(component_errors), None)
    if error is None and error_product:
        error = component_errors[error_product]
    transport_kinds = [kind for kind in requested if kind in ("avia", "rail", "bus")]
    arrays = {"avia": flights, "rail": trains, "bus": buses, "hotel": hotels}
    missing_transport = bool(transport_kinds) and all(arrays[kind] == [] for kind in transport_kinds)
    missing_hotel = "hotel" in requested and hotels == []
    code = "candidates_received"
    if error:
        code = "partial_provider_error" if component_errors and any(arrays[kind] for kind in requested) else "provider_error"
    elif missing_transport or missing_hotel:
        if missing_transport and missing_hotel:
            code = "missing_both"
        elif missing_transport:
            code = "missing_flights" if transport_kinds == ["avia"] else "missing_trains" if transport_kinds == ["rail"] else "missing_transport"
        else:
            code = "missing_hotels"
        missing_kinds = (transport_kinds if missing_transport else []) + (["hotel"] if missing_hotel else [])
        if any((_number(products.get(kind, {}).get("raw_count")) or 0) > 0 and not _raw_empty(kind, products)
               for kind in missing_kinds):
            code = "no_usable_fares"
    elif all(arrays[kind] is not None for kind in requested):
        pin = pins.get("hotel") if isinstance(pins, dict) else None
        matching_hotels = [hotel for hotel in (hotels or []) if not pin or hotel.get("title") == pin]
        transport_count = sum(_priced(rate) for kind in transport_kinds for rate in arrays[kind]) if transport_kinds else 1
        hotel_count = sum(_priced(hotel) for hotel in matching_hotels) if "hotel" in requested else 1
        possible = transport_count * hotel_count
        if "hotel" in requested and pin and not matching_hotels:
            code, reasons = "conditions_unmet", ["Закреплённый отель не найден среди полученных тарифов; закрепление сохранено."]
        elif not possible:
            code = "no_priced_pairs"
        elif isinstance(rejections, list) and len(rejections) >= possible:
            code = "conditions_unmet"
    if code != "conditions_unmet":
        reasons = []
    try:
        stamp = datetime.fromisoformat(now).isoformat() if isinstance(now, str) else None
    except ValueError:
        stamp = None
    return {"code": code, "query": _query(request), "counts": counts, "products": products,
            "requested_products": requested, "component_errors": component_errors, "error_product": error_product,
            "reasons": reasons, "error": error, "searched_at": stamp}


def legacy_search_diagnostics(coverage, request) -> dict | None:
    """Recover only raw coverage facts tied to this exact normalized request."""
    query = _query(request)
    requested = _requested(request)
    def matches(row):
        if not isinstance(row, dict) or not isinstance(row.get("query"), dict) or row.get("product") not in requested:
            return False
        source = row["query"]
        if _query(source) == query:
            return True
        if row.get("product") in ("rail", "bus") and row.get("direction") in ("outbound", "inbound"):
            outward = row["direction"] == "outbound"
            normal = _query({**source, "adults": source.get("passengers")})
            return (normal["origin"] == query["origin" if outward else "destination"]
                    and normal["destination"] == query["destination" if outward else "origin"]
                    and normal["departure_date"] == query["departure_date" if outward else "return_date"]
                    and normal["adults"] == query["adults"] and not query["children_ages"])
        return False
    rows = [row for row in coverage if matches(row)] if isinstance(coverage, list) else []
    products = _products(rows)
    if not products:
        return None
    transport_kinds = [kind for kind in requested if kind in ("avia", "rail", "bus")]
    empty_transport = bool(transport_kinds) and all(_raw_empty(kind, products) for kind in transport_kinds)
    empty_hotel = "hotel" in requested and _raw_empty("hotel", products)
    transport_code = "missing_flights" if transport_kinds == ["avia"] else "missing_trains" if transport_kinds == ["rail"] else "missing_transport"
    code = "missing_both" if empty_transport and empty_hotel else transport_code if empty_transport else "missing_hotels" if empty_hotel else "legacy_coverage"
    counts = {"flights": None, "hotel_rates": None, "hotels": None}
    if "rail" in requested:
        counts["trains"] = None
    return {"code": code, "query": query, "counts": counts, "requested_products": requested,
            "products": products, "reasons": [], "error": None, "searched_at": None}


def _route(query):
    route = " → ".join(value for value in (query.get("origin"), query.get("destination")) if value)
    first, last = query.get("departure_date"), query.get("return_date")
    if first and last:
        start, end = date.fromisoformat(first), date.fromisoformat(last)
        if (start.year, start.month) == (end.year, end.month):
            dates = f"{start.day}–{end.day} {_MONTHS[end.month]} {end.year}"
        elif start.year == end.year:
            dates = f"{start.day} {_MONTHS[start.month]} — {end.day} {_MONTHS[end.month]} {end.year}"
        else:
            dates = f"{start.day} {_MONTHS[start.month]} {start.year} — {end.day} {_MONTHS[end.month]} {end.year}"
        route = route + (", " if route else "") + dates
    return route


def format_search_diagnostics(diag, *, followup=False, data_mode="live") -> dict:
    """Give a short next step; retain detailed evidence only in diagnostics."""
    diag = diag if isinstance(diag, dict) else {}
    requested = _requested({"products": diag.get("requested_products")})
    counts = diag.get("counts") if isinstance(diag.get("counts"), dict) else {}
    products = diag.get("products") if isinstance(diag.get("products"), dict) else {}
    avia = products.get("avia") if isinstance(products.get("avia"), dict) else {}
    hotel = products.get("hotel") if isinstance(products.get("hotel"), dict) else {}
    flight_count, hotel_count = _number(counts.get("flights")), _number(counts.get("hotel_rates"))
    raw_flight, raw_hotel = _number(avia.get("raw_count")), _number(hotel.get("raw_count"))
    route_query = _query(diag.get("query"))
    if requested == ["hotel"]:
        route_query["origin"] = ""
    route = _route(route_query)
    code = diag.get("code")
    error = _safe_error(diag.get("error"))
    question = None
    if code in {"provider_error", "partial_provider_error"} or error:
        error = error or {"code": "unavailable", "retryable": False}
        message = _ERROR_MESSAGES[error["code"]]
        error_product = diag.get("error_product")
        labels = {"avia": "перелётов", "rail": "поездов", "bus": "автобусов", "hotel": "отелей"}
        if error_product in labels:
            label = labels[error_product]
            if error["code"] == "unsupported_party":
                message = f"Я пока не могу подобрать варианты {label} для такого состава путешественников."
            elif error["code"] == "unverified_fares":
                message = f"Я не смог подтвердить тарифы и места для всех пассажиров при поиске {label}."
            elif error["code"] == "timeout":
                message = f"Я не успел завершить поиск {label} за отведённое время."
            elif error["code"] == "rate_limited":
                message = f"Я смогу повторить поиск {label} чуть позже."
            elif error["code"] not in {"fixture_missing", "stale_offer"}:
                message = f"Я пока не смог завершить поиск {label}."
        if error["retryable"] and error["code"] not in {"unsupported_party", "invalid_arguments", "unsupported"} and not followup:
            question = "Попробовать ещё раз?"
    elif code == "conditions_unmet":
        reasons = [_text(reason, 350) for reason in diag.get("reasons", [])[:1] if isinstance(reason, str)]
        message = "Подходящих вариантов по вашим условиям не нашлось."
        date_problem = bool(reasons and ("Совпадение дат" in reasons[0] or "даты перелёта и проживания" in reasons[0].lower() or "Даты перелёта и проживания" in reasons[0]))
        if date_problem:
            message = "Не удалось согласовать даты транспорта и проживания." if "rail" in requested else "Не удалось согласовать даты перелёта и проживания."
        elif reasons and reasons[0]:
            if "питомц" in reasons[0].lower():
                pet_service = ("размещением" if requested == ["hotel"] else
                               "перевозкой" if "hotel" not in requested else "размещением или перевозкой")
                confirmed = "подтверждённой" if "hotel" not in requested else "подтверждённым"
                message = f"Я не нашёл вариант с {confirmed} {pet_service} питомца."
            else:
                message += " " + reasons[0]
        mandatory_labels = ("Доступный вход для человека на коляске", "Тихий номер", "Номер вдали от лифта")
        unconfirmed = [reason.split(":", 1)[0].lower() for reason in diag.get("reasons", [])[:3]
                       if isinstance(reason, str) and reason.startswith(mandatory_labels) and "нет подтверждённых данных" in reason]
        if unconfirmed:
            message = "Я пока не подтвердил обязательные условия: " + ", ".join(unconfirmed) + ". До выбора тарифа нужно подтверждение отеля."
        elif not followup:
            question = "Подобрать другие варианты поездки?" if date_problem else "Какое условие готовы изменить?"
    elif code == "no_priced_pairs":
        message = "Не удалось подтвердить стоимость поездки."
    elif code in {"missing_flights", "missing_trains", "missing_transport", "missing_hotels", "missing_both", "no_usable_fares"}:
        transport_kinds = [kind for kind in requested if kind in ("avia", "rail", "bus")]
        numbers = {"avia": flight_count, "rail": _number(counts.get("trains")), "bus": _number(counts.get("buses"))}
        no_transport = bool(transport_kinds) and all(numbers[kind] == 0 or (numbers[kind] is None and _raw_empty(kind, products)) for kind in transport_kinds)
        no_hotel = "hotel" in requested and (hotel_count == 0 or (hotel_count is None and raw_hotel == 0))
        raw_transport_empty = bool(transport_kinds) and all(_raw_empty(kind, products) for kind in transport_kinds)
        if transport_kinds == ["avia"]:
            transport_empty, transport_pick = "Перелётов", "перелёт"
        elif transport_kinds == ["rail"]:
            transport_empty, transport_pick = "Билетов на поезд туда и обратно", "поездку на поезде"
        elif transport_kinds == ["bus"]:
            transport_empty, transport_pick = "Автобусов туда и обратно", "поездку на автобусе"
        elif set(transport_kinds) == {"avia", "rail"}:
            transport_empty, transport_pick = "Авиабилетов и билетов на поезд", "транспорт"
        else:
            transport_empty, transport_pick = "Подходящего транспорта", "транспорт"
        if no_transport and no_hotel:
            message = (f"{transport_empty} и подходящего проживания на эти даты не нашлось."
                       if raw_transport_empty and raw_hotel == 0 else
                       f"Не удалось подобрать {transport_pick} и проживание по этим условиям.")
        elif no_transport:
            message = (f"{transport_empty} на эти даты не нашлось." if raw_transport_empty else
                       f"Не удалось подобрать {transport_pick} по этим условиям.")
        elif no_hotel:
            message = ("Подходящего проживания на эти даты не нашлось." if raw_hotel == 0 else
                       "Не удалось подобрать проживание по этим условиям.")
        else:
            message = "Не удалось подобрать поездку по этим условиям."
        if not followup:
            question = "Рассмотрим другой город?"
    else:
        message = "Подходящий вариант поездки пока не удалось подобрать."
    if route:
        message = route + ": " + message[0].lower() + message[1:]
    if data_mode == "replay" and (error or {}).get("code") != "fixture_missing":
        message += " Это результат проверки архива, не текущая доступность."
    if question:
        message += "\n" + question
    return {"message": message, "question": question}


def is_legacy_search_explanation(message: str) -> bool:
    """Recognize the historical diagnostic template, not a factual follow-up."""
    return any(marker in message for marker in ("Туту не вернул", "По отелям:",
               "Проверка ограничена:", "Повторить поиск с теми же условиями?"))


def normalize_search_session(state: dict) -> dict:
    """Adapt the last old diagnostic reply for the next interpretation only."""
    from copy import deepcopy
    result = deepcopy(state)
    diagnostic = result.get("search_diagnostics")
    dialogue = result.get("dialogue") or []
    if not diagnostic or result.get("options") or not dialogue or dialogue[-1].get("role") != "assistant":
        return result
    old = dialogue[-1].get("text", "")
    if not is_legacy_search_explanation(old):
        return result
    explained = format_search_diagnostics(diagnostic, followup=not bool(result.get("last_question")), data_mode=result.get("data_mode", "live"))
    dialogue[-1]["text"] = explained["message"]
    result["last_question"] = explained["question"]
    return result
