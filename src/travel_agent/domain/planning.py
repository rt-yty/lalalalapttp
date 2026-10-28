from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from itertools import product

from travel_agent.storage.sqlite import fingerprint

UNPRICED = ["Трансферы и дорога до места отправления", "Питание сверх тарифа", "Страховка и личные расходы"]


def event_checkout_key(request):
    """Keep a same-day checkout acknowledgement tied to this exact itinerary."""
    return fingerprint([request.get(key) for key in
                        ("destination", "event_date", "event_start_at", "departure_date", "return_date")])


def rub(minor):
    whole, kopecks = divmod(minor, 100)
    return f"{whole:,}".replace(",", " ") + (f",{kopecks:02d}" if kopecks else "") + " ₽"


def local_moment(value):
    """Display a verified local timestamp without leaking an ISO wire format."""
    try:
        moment = datetime.fromisoformat(str(value))
        if moment.tzinfo is None:
            return "время уточняется"
        offset = moment.strftime("%z")
        zone = "UTC" + offset[:3] + (":" + offset[3:] if offset[3:] != "00" else "")
        return moment.strftime("%d.%m.%Y, %H:%M") + " (" + zone + ")"
    except (TypeError, ValueError):
        return "время уточняется"


def stable_components(components):
    keys = ("product_type", "title", "price_minor", "currency", "query", "conditions", "provider_refs", "departure_at", "arrival_at", "return_departure_at", "return_arrival_at", "data_origin", "availability")
    return [{key: c.get(key) for key in keys} for c in components]


def plan_hash(plan):
    return fingerprint({"plan_id": plan["plan_id"], "version": plan["version"], "request": plan["request"], "components": stable_components(plan["components"]), "total_minor": plan["total_minor"]})


def component_matches_request(component, request):
    """Reject a misplaced or malformed quote before displaying or pricing it."""
    from ..adapters.normalize import search_key
    kind, query = component.get("product_type"), component.get("query") or {}
    if kind not in request.get("products", ["avia", "hotel"]):
        return False
    if component.get("currency") != "RUB" or type(component.get("price_minor")) is not int or component["price_minor"] < 0:
        return False
    target = search_key(request)
    keys = ("destination", "return_date", "adults", "children_ages")
    if kind != "hotel":
        keys += ("origin", "departure_date")
    if any(query.get(k) != target.get(k) for k in keys):
        return False
    if kind == "hotel":
        check_in = query.get("departure_date")
        if not isinstance(check_in, str) or not target["departure_date"]:
            return False
        if any(p in request.get("products", ["avia", "hotel"]) for p in ("avia", "rail", "bus")):
            return target["departure_date"] <= check_in < target["return_date"]
        return check_in == target["departure_date"]
    try:
        times = [datetime.fromisoformat(component[k]) for k in
                 ("departure_at", "arrival_at", "return_departure_at", "return_arrival_at")]
        return (all(t.tzinfo is not None for t in times)
                and all(a < b for a, b in zip(times, times[1:]))
                and times[0].date().isoformat() == target["departure_date"]
                and times[2].date().isoformat() == target["return_date"])
    except (KeyError, TypeError, ValueError):
        return False


def check_components(components, request):
    checks = []
    flight = next((c for c in components if c["product_type"] in ("avia", "rail", "bus")), {})
    hotel = next((c for c in components if c["product_type"] == "hotel"), {})
    products = request.get("products", ["avia", "hotel"])
    needs_transport = any(p in products for p in ("avia", "rail", "bus"))
    needs_hotel = "hotel" in products

    def add(key, actual, wanted, compare=lambda a, b: a == b, hard=True, evidence=None):
        status = "unknown" if actual is None else "satisfied" if compare(actual, wanted) else "violated"
        labels = {"wheelchair_accessible": "Доступный вход для человека на коляске", "quiet_room": "Тихий номер", "away_from_elevator": "Номер вдали от лифта", "baggage_kg": "Багаж, кг", "breakfast": "Завтрак включён", "direct": "Прямой маршрут", "pets": "Размещение с питомцем", "avia.pets": "Перевозка питомца", "budget_known_components": "Бюджет выбранных компонентов", "hotel_name": "Выбранный отель", "flight_refundable": "Возвратность билета", "hotel_refundable": "Возвратность отеля", "avia.refundable": "Возвратность билета", "hotel.refundable": "Возвратность отеля", "free_cancellation": "Бесплатная отмена отеля", "dates_compatible": "Совпадение дат", "avia.availability": "Наличие билета", "hotel.availability": "Наличие номера", "avia.details_verified": "Подробности авиатарфа", "hotel.details_verified": "Подробности тарифа отеля"}
        labels.update({"hotel.capacity": "Вместимость номера, гостей", "rail_refundable": "Возвратность билета на поезд",
                       "seat_category": "Категория места", "service_class": "Класс обслуживания"})
        for transport_kind in ("rail", "bus"):
            labels.update({transport_kind + ".pets": "Перевозка питомца",
                           transport_kind + ".availability": "Наличие билета",
                           transport_kind + ".details_verified": "Подробности тарифа транспорта",
                           transport_kind + ".refundable": "Возвратность билета"})
        def display(value):
            if value is None:
                return "нет подтверждённых данных"
            if type(value) is bool:
                return "да" if value else "нет"
            if value == "reported_available":
                return "есть в выдаче"
            if key == "budget_known_components":
                return rub(value)
            return str(value)
        explanation = (f"Стоимость выбранных компонентов: {display(actual)}; ваш лимит — не более {display(wanted)}"
                       if key == "budget_known_components" else
                       f"{labels.get(key, key)}: {display(actual)}; требуется {display(wanted)}")
        checks.append({"constraint_id": key, "status": status, "hard": hard, "actual": actual, "required": wanted, "explanation": explanation, "evidence_refs": evidence or []})

    required = []
    if needs_transport:
        required.append((flight.get("product_type", "avia"), flight))
    if needs_hotel:
        required.append(("hotel", hotel))
    for name, item in required:
        add(name + ".availability", item.get("availability"), "reported_available", evidence=item.get("evidence_refs"))
        add(name + ".details_verified", item.get("conditions", {}).get("details_verified"), True, evidence=item.get("evidence_refs"))
    capacity = hotel.get("conditions", {}).get("max_occupancy")
    if needs_hotel and capacity is not None:
        guests = (request.get("adults") or 0) + len(request.get("children_ages", []))
        add("hotel.capacity", capacity, guests, lambda actual, required: type(actual) is int and actual >= required,
            evidence=hotel.get("evidence_refs"))
    for key, value in request.get("constraints", {}).items():
        if value is None or key.startswith("prefer_") or (key == "pets" and not request.get("pets")):
            continue
        if key in {"wheelchair_accessible", "quiet_room", "away_from_elevator"} and value is False:
            continue
        if key == "refundable":
            for name, item in required:
                add(name + ".refundable", item.get("conditions", {}).get("refundable"), value, evidence=item.get("evidence_refs"))
            continue
        if key == "flight_refundable" and flight.get("product_type") in ("rail", "bus"):
            continue
        if key == "rail_refundable" and flight.get("product_type") in ("avia", "bus"):
            continue
        is_transport = key in ("baggage_kg", "direct", "flight_refundable", "rail_refundable", "seat_category", "service_class")
        if (is_transport and not needs_transport) or (not is_transport and not needs_hotel):
            continue
        source = flight if is_transport else hotel
        condition_key = "refundable" if key in ("flight_refundable", "rail_refundable", "hotel_refundable") else key
        actual = source.get("conditions", {}).get(condition_key)
        compare = lambda a, b: a == b
        if key == "baggage_kg":
            compare = lambda a, b: isinstance(a, (float, int)) and a >= b
        if key == "hotel_name":
            actual = hotel.get("title")
            compare = lambda a, b: str(b).casefold() in str(a).casefold()
        add(key, actual, value, compare, evidence=source.get("evidence_refs"))
    if needs_hotel and request.get("pets") and "pets" not in request.get("constraints", {}):
        add("pets", hotel.get("conditions", {}).get("pets"), True, evidence=hotel.get("evidence_refs"))
    if needs_transport and request.get("pets"):
        add(flight.get("product_type", "avia") + ".pets", flight.get("conditions", {}).get("pets"), True, evidence=flight.get("evidence_refs"))
    total = sum(c["price_minor"] for c in components)
    if request.get("budget_minor"):
        budget = request["budget_minor"]
        if request.get("budget_scope") == "per_person":
            budget *= request["adults"] + len(request.get("children_ages", []))
        add("budget_known_components", total, budget, lambda a, b: a <= b)
    departure, arrival, returning = (flight.get(k) for k in ("departure_at", "arrival_at", "return_departure_at"))
    compatible = None
    hotel_query = hotel.get("query") or {}
    if departure and arrival and returning:
        try:
            departure_dt, arrival_dt, return_dt = map(datetime.fromisoformat, (departure, arrival, returning))
            compatible = bool(departure_dt.tzinfo and arrival_dt.tzinfo and return_dt.tzinfo
                and departure_dt < arrival_dt < return_dt
                and departure_dt.date().isoformat() == str(request["departure_date"])
                and return_dt.date().isoformat() == str(request["return_date"])
                and (not needs_hotel or (hotel_query.get("departure_date") == arrival_dt.date().isoformat()
                    and hotel_query.get("return_date") == return_dt.date().isoformat()
                    and arrival_dt.date() < return_dt.date())))
        except (ValueError, TypeError):
            compatible = None
    if not needs_transport:
        compatible = bool(hotel and hotel_query.get("departure_date") == str(request.get("departure_date"))
                          and hotel_query.get("return_date") == str(request.get("return_date")))
    add("dates_compatible", compatible, True, evidence=flight.get("evidence_refs") or hotel.get("evidence_refs"))
    if compatible is not True:
        checks[-1]["explanation"] = ("Даты транспорта и проживания не удалось согласовать." if compatible is False
                                      else "Не удалось подтвердить даты транспорта и проживания.")
    if needs_transport and (request.get("event_date") or request.get("event_start_at")):
        before_event = None
        try:
            arrival_dt = datetime.fromisoformat(arrival)
            return_dt = datetime.fromisoformat(returning)
            if request.get("event_start_at"):
                event = datetime.fromisoformat(str(request["event_start_at"]))
                before_event = bool(event.tzinfo and arrival_dt.tzinfo and return_dt.tzinfo and arrival_dt < event < return_dt)
            else:
                event_day = str(request["event_date"])
                before_event = arrival_dt.date().isoformat() <= event_day <= return_dt.date().isoformat()
        except (ValueError, TypeError):
            pass
        add("event_timing", before_event, True, evidence=flight.get("evidence_refs"))
        checks[-1]["explanation"] = ("Расписание транспорта позволяет находиться в городе в дату мероприятия." if before_event else "Расписание транспорта не подтверждает прибытие к мероприятию и возвращение после него.")
    elif needs_hotel and (request.get("event_date") or request.get("event_start_at")):
        event_day = str(request.get("event_date") or request["event_start_at"])[:10]
        check_in, check_out = hotel_query.get("departure_date"), hotel_query.get("return_date")
        within_stay = None
        if check_in and check_out:
            within_stay = check_in <= event_day < check_out
            if check_in <= event_day == check_out:
                within_stay = True if request.get("event_checkout_ack") == event_checkout_key(request) else None
        add("event_timing", within_stay, True, evidence=hotel.get("evidence_refs"))
        checks[-1]["explanation"] = ("Даты проживания согласованы с мероприятием." if within_stay else
                                     "Нужно согласовать даты проживания с мероприятием.")
    if needs_transport and needs_hotel:
        checks.append({"constraint_id": "transfer", "status": "unknown", "hard": False, "explanation": "Дорога от аэропорта или вокзала оплачивается отдельно; время переезда не рассчитано.", "evidence_refs": []})
        check_in = hotel.get("conditions", {}).get("check_in_time")
        try:
            if check_in and arrival and datetime.fromisoformat(arrival).strftime("%H:%M") < check_in[:5]:
                checks.append({"constraint_id": "check_in_time", "status": "unknown", "hard": False,
                               "explanation": f"Заселение с {check_in}; транспорт прибывает раньше. Раннее заселение нужно согласовать с отелем.", "evidence_refs": hotel.get("evidence_refs", [])})
        except (ValueError, TypeError):
            pass
    return checks


def component_score(component, request):
    """Preferences rank already eligible offers; never relax a hard condition."""
    c = request.get("constraints", {})
    cond = component.get("conditions", {})
    score = []
    if c.get("prefer_daytime") and component["product_type"] in ("avia", "rail", "bus"):
        times = [component.get(k) for k in ("departure_at", "arrival_at", "return_departure_at", "return_arrival_at")]
        try:
            score.append(sum(not 7 <= datetime.fromisoformat(t).hour < 23 for t in times))
        except (ValueError, TypeError):
            score.append(5)
    if c.get("prefer_shorter") and component["product_type"] in ("avia", "rail", "bus"):
        try:
            score.append(sum((datetime.fromisoformat(component[b]) - datetime.fromisoformat(component[a])).total_seconds()
                             for a, b in (("departure_at", "arrival_at"), ("return_departure_at", "return_arrival_at"))))
        except (ValueError, TypeError, KeyError):
            score.append(float("inf"))
    if c.get("prefer_hotel_rating") is not None and component["product_type"] == "hotel":
        rating = cond.get("rating")
        try:
            score.append(-float(rating) if rating is not None else 1)
        except (TypeError, ValueError):
            score.append(1)
    score.append(component["price_minor"])
    return tuple(score)


def component_options_from_search(result, request, options_version):
    """Independent verified cards. Whole-trip compatibility is checked on selection."""
    groups = {"transport_options": [], "hotel_options": []}
    products = request.get("products", ["avia", "hotel"])
    for key in ("flights", "trains", "buses", "hotels"):
        seen = set()
        for item in result.get(key, []):
            kind = item.get("product_type")
            if kind not in products or not component_matches_request(item, request):
                continue
            query = {**request, "products": [kind]}
            if kind == "hotel":
                query["departure_date"] = item.get("query", {}).get("departure_date", request.get("departure_date"))
            checks = check_components([item], query)
            if any(c.get("hard") and c["status"] != "satisfied" for c in checks):
                continue
            reference = item.get("provider_refs", {}).get("reference_key") or fingerprint(stable_components([item]))
            if reference in seen:
                continue
            seen.add(reference)
            card = deepcopy(item)
            card.update(component_id=fingerprint([options_version, kind, reference])[:20], options_version=options_version,
                        eligibility="satisfied", checks=checks, reasons=component_reasons(item),
                        warnings=[c["explanation"] for c in checks if not c.get("hard") and c["status"] != "satisfied"])
            groups["hotel_options" if kind == "hotel" else "transport_options"].append(card)
    for name, cards in groups.items():
        # Do not drown train alternatives in the many fares of one flight.
        cards.sort(key=lambda c: component_score(c, request))
        if name == "hotel_options":
            distinct, extra, seen = [], [], set()
            for card in cards:
                identity = card.get("provider_refs", {}).get("hotel_id") or card["title"]
                if identity in seen:
                    extra.append(card)
                else:
                    seen.add(identity)
                    distinct.append(card)
            groups[name] = (distinct + extra)[:8]
        else:
            groups[name] = [c for kind in ("avia", "rail", "bus") for c in [x for x in cards if x["product_type"] == kind][:6]]
    return groups


def component_reasons(component):
    c = component.get("conditions", {})
    reasons = ["Проживание за весь срок" if component["product_type"] == "hotel" else "Дорога туда и обратно"]
    if c.get("baggage_kg") is not None:
        reasons.append(f"Багаж {c['baggage_kg']} кг")
    if c.get("breakfast") is True:
        reasons.append("Завтрак включён")
    if c.get("free_cancellation") is True:
        reasons.append("Есть бесплатная отмена по условиям тарифа")
    if c.get("seat_category"):
        reasons.append(str(c["seat_category"]))
    if c.get("rating") is not None:
        reasons.append(f"Рейтинг {c['rating']}")
    return reasons


def option_from_components(components, request, options_version=0):
    checks = check_components(components, request)
    missing = []
    products = request.get("products", ["avia", "hotel"])
    if any(p in products for p in ("avia", "rail", "bus")) and not any(c["product_type"] in ("avia", "rail", "bus") for c in components):
        missing.append("transport")
    if "hotel" in products and not any(c["product_type"] == "hotel" for c in components):
        missing.append("hotel")
    violations = [c for c in checks if c.get("hard") and c["status"] != "satisfied"]
    return {"option_id": fingerprint([options_version, stable_components(components)])[:16], "options_version": options_version,
            "title": " + ".join(c["title"] for c in components), "components": deepcopy(components),
            "total_minor": sum(c["price_minor"] for c in components), "currency": "RUB", "checks": checks,
            "eligibility": "unknown" if missing else "violated" if violations else "satisfied", "complete": not missing,
            "missing_products": missing, "unpriced": UNPRICED, "reasons": [r for c in components for r in component_reasons(c)],
            "warnings": [c["explanation"] for c in checks if not c.get("hard") and c["status"] != "satisfied"]}


def options_from_search(result, request, options_version, pins=None, *, rejections=None):
    options = []
    pins = pins or {}
    products = request.get("products", ["avia", "hotel"])
    transports = (result.get("flights", []) if "avia" in products else []) + (result.get("trains", []) if "rail" in products else []) + (result.get("buses", []) if "bus" in products else [])
    hotels = result.get("hotels", []) if "hotel" in products else [None]
    for flight, hotel in product(transports if any(p in products for p in ("avia", "rail", "bus")) else [None], hotels):
        if pins.get("hotel") and hotel and hotel["title"] != pins["hotel"]:
            continue
        components = deepcopy([c for c in (flight, hotel) if c])
        if any(not component_matches_request(c, request) for c in components):
            continue
        checks = check_components(components, request)
        violations = [c for c in checks if c.get("hard") and c["status"] == "violated"]
        unknowns = [c for c in checks if c.get("hard") and c["status"] == "unknown"]
        eligibility = "violated" if violations else "unknown" if unknowns else "satisfied"
        if eligibility != "satisfied":
            if rejections is not None:
                rejections.append(violations + unknowns)
            continue
        total = sum(c["price_minor"] for c in components)
        options.append(option_from_components(components, request, options_version))
    options.sort(key=lambda o: ({"satisfied": 0, "unknown": 1, "violated": 2}[o["eligibility"]], o["total_minor"]))
    # Show diverse hotels first, then the next distinct fare; no popularity claims.
    picked, seen = [], set()
    for option in options:
        name = next((c["title"] for c in option["components"] if c["product_type"] == "hotel"), option["title"])
        if name not in seen:
            picked.append(option)
            seen.add(name)
        if len(picked) == 3:
            break
    for option in options:
        if len(picked) >= 5:
            break
        if option not in picked:
            picked.append(option)
    return sorted(picked, key=lambda o: ({"satisfied": 0, "unknown": 1, "violated": 2}[o["eligibility"]], o["total_minor"]))


def diff_plans(old, new):
    if not old:
        return []
    diff = []
    for key in ("origin", "destination", "departure_date", "return_date", "adults", "children_ages", "pets", "budget_minor", "constraints", "products", "event_date", "event_start_at"):
        a, b = old["request"].get(key), new["request"].get(key)
        if a != b:
            diff.append({"field": key, "before": a, "after": b})
    for kind in ("avia", "rail", "bus", "hotel"):
        a = next((c for c in old["components"] if c["product_type"] == kind), {})
        b = next((c for c in new["components"] if c["product_type"] == kind), {})
        for key in ("title", "price_minor", "conditions"):
            if a.get(key) != b.get(key):
                diff.append({"field": f"{kind}.{key}", "before": a.get(key), "after": b.get(key)})
    return diff


def refresh_plan_explanation(plan):
    """Keep derived text consistent with refreshed quotes and checks."""
    by_kind = {c["product_type"]: c for c in plan["components"]}
    flight, hotel = by_kind.get("avia") or by_kind.get("rail") or by_kind.get("bus"), by_kind.get("hotel")
    plan["reasons"] = []
    if flight:
        plan["reasons"].append(f"{ {'rail': 'Поезд', 'bus': 'Автобус', 'avia': 'Перелёт'}[flight['product_type']] } туда-обратно: {rub(flight['price_minor'])}")
    if hotel:
        plan["reasons"].append(f"Проживание за весь срок: {rub(hotel['price_minor'])}")
    plan["reasons"].append("Цена относится ко всему указанному составу гостей")
    plan["warnings"] = [c["explanation"] for c in plan["checks"] if c["status"] != "satisfied"]
    failed = any(c.get("hard") and c["status"] == "violated" for c in plan["checks"])
    unknown = any(c.get("hard") and c["status"] == "unknown" for c in plan["checks"])
    plan["eligibility"] = "violated" if failed else "unknown" if unknown else "satisfied"
    if flight and hotel:
        plan["title"] = hotel["title"] + " + " + flight["title"]
    return plan


def describe_components(plan):
    def known(value):
        return "неизвестно" if value is None else "да" if value is True else "нет" if value is False else str(value)
    lines = []
    for item in plan["components"]:
        c = item.get("conditions", {})
        if item["product_type"] in ("avia", "rail", "bus"):
            if item["product_type"] == "bus":
                lines.append(f"{item['title']}, {rub(item['price_minor'])} туда и обратно. Багаж и возврат не подтверждены.")
            elif item["product_type"] == "rail":
                lines.append(f"Поезд: {item['title']}, {rub(item['price_minor'])} туда и обратно. Категория: {c.get('seat_category') or 'не указана'}. Возвратный тариф: {known(c.get('refundable'))}.")
            else:
                lines.append(f"Перелёт: {item['title']}, {rub(item['price_minor'])}. Багаж: {known(c.get('baggage_kg'))} кг. Возвратный тариф: {known(c.get('refundable'))} (штрафы отдельно не проверены).")
            lines.append(f"Туда: {local_moment(item.get('departure_at'))} → {local_moment(item.get('arrival_at'))}. Обратно: {local_moment(item.get('return_departure_at'))} → {local_moment(item.get('return_arrival_at'))}.")
        else:
            lines.append(f"Отель: {item['title']}, {c.get('room_name') or 'номер не указан'}, {rub(item['price_minor'])} за весь срок. Завтрак: {known(c.get('breakfast'))}.")
            deadline = c.get("free_cancellation_until")
            if c.get("free_cancellation") is True and deadline:
                lines.append(f"Отель: бесплатная отмена до {local_moment(deadline)}; позднее действуют штрафы по тарифу.")
            else:
                lines.append(f"Бесплатная отмена отеля: {known(c.get('free_cancellation'))}.")
    return lines
