"""Small, conservative normalizers for the inspected Tutu MCP schemas."""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from datetime import datetime
from typing import Any
import math


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:24]


def money_minor(price: dict) -> int:
    if price.get("currency") != "RUB":
        raise ValueError("unsupported_currency")
    try:
        amount = Decimal(str(price["amount"])) * 100
        if not amount.is_finite() or amount < 0 or amount != amount.to_integral_value():
            raise ValueError("invalid_money")
        return int(amount)
    except (KeyError, InvalidOperation) as exc:
        raise ValueError("invalid_money") from exc


def search_key(request: dict) -> dict:
    aliases = {"москва": "Москва", "moscow": "Москва", "mow": "Москва", "мск": "Москва",
               "ереван": "Ереван", "yerevan": "Ереван", "evn": "Ереван"}
    def city(key: str) -> str:
        value = str(request.get(key) or "").strip()
        return aliases.get(value.casefold(), value)
    return {"origin": city("origin"), "destination": city("destination"),
            "departure_date": str(request.get("departure_date") or ""),
            "return_date": str(request.get("return_date") or ""),
            "adults": request.get("adults"), "children_ages": list(request.get("children_ages") or [])}


def _base(kind: str, title: str, price: dict, key: str, request: dict, now: str,
          origin: str, recorded_at: str, source: str) -> dict:
    return {"snapshot_id": fingerprint([key, price, now, origin]), "provider": "tutu",
            "product_type": kind, "title": title, "price_minor": money_minor(price),
            "currency": "RUB", "data_origin": origin, "source_recorded_at": recorded_at,
            "fetched_at": now, "query": search_key(request), "conditions": {},
            "provider_refs": {"reference_key": key}, "availability": "reported_available",
            "evidence_refs": [source], "expires_at": None,
            "freshness": "historical" if origin == "recorded_fixture" else "fetched",
            "unknown_charges": ["Дополнительные сборы, не раскрытые поставщиком"]}


def normalize_flight(offer: dict, variant: dict, request: dict, now: str, *,
                     data_origin: str = "live", recorded_at: str | None = None,
                     source: str = "tutu:search_avia") -> dict:
    conditions = variant.get("conditions") or {}
    legs = offer.get("legs") or []
    segments = [{k: segment.get(k) for k in ("from", "to", "departure_at", "arrival_at", "voyage_no", "carrier")}
                for leg in legs for segment in leg.get("segments", [])]
    identity = [search_key(request), segments, conditions.get("fare_family"), variant.get("service_class")]
    # Different fare conditions must not collapse to one supposedly identical fare.
    identity.append({k: conditions.get(k) for k in ("baggage", "cabin_baggage", "refundable", "changeable")})
    key = "flight-" + fingerprint(identity)
    item = _base("avia", " / ".join(offer.get("carriers") or ["Перелёт"]) + " · " +
                 str(conditions.get("fare_family") or "Тариф не указан"), variant["price"], key,
                 request, now, data_origin, recorded_at or now, source)
    item["provider_refs"].update({"flight_numbers": [s["voyage_no"] for s in segments if s.get("voyage_no")],
                                  "segments": segments, "fare_family": conditions.get("fare_family")})
    item["conditions"] = {"details_verified": bool(variant.get("price") and variant.get("conditions")),
        "baggage_kg": (conditions.get("baggage") or {}).get("kg"),
        "baggage_pieces": (conditions.get("baggage") or {}).get("pieces"),
        "cabin_baggage_kg": (conditions.get("cabin_baggage") or {}).get("kg"),
        "refundable": conditions.get("refundable"), "changeable": conditions.get("changeable"),
        "fare_family": conditions.get("fare_family"), "pets": None,
        "service_class": variant.get("service_class"), "price_basis": "party_round_trip_total",
        "direct": bool(legs) and all(len(leg.get("segments", [])) == 1 for leg in legs)}
    for field in ("departure_at", "arrival_at", "return_departure_at", "return_arrival_at"):
        item[field] = offer.get(field)
    return item


def _current_free_cancellation(rate: dict, now: str) -> bool | None:
    """A provider flag cannot override an already expired cancellation window."""
    available = rate.get("free_cancellation")
    if type(available) is not bool:
        return None
    deadline = rate.get("free_cancellation_until")
    if not available or not deadline:
        return available
    try:
        until, current = datetime.fromisoformat(deadline), datetime.fromisoformat(now)
        if until.tzinfo is None or current.tzinfo is None:
            return None
        return current < until
    except (TypeError, ValueError):
        return None


def normalize_hotel(detail: dict, room: dict, rate: dict, request: dict, now: str, *,
                    data_origin: str = "live", recorded_at: str | None = None,
                    source: str = "tutu:get_offer_details") -> dict:
    hotel = detail["hotel"]
    hotel_id = str(hotel.get("hotel_id") or hotel.get("hotel_geo_id") or "")
    amenities = [a.get("name", "").casefold() for group in hotel.get("amenity_groups", [])
                 for a in group.get("amenities", []) if isinstance(a, dict)]
    pets = False if any("с питомцами нельзя" in a for a in amenities) else None
    # Pet acceptance may need restrictions/supplement checks, so do not infer True.
    cancellation = []
    for step in rate.get("cancellation_policy") or []:
        penalty = (step.get("penalty") or {}).get("full") or {}
        amount = penalty.get("value")
        cancellation.append({"start": step.get("start") or None, "end": step.get("end") or None,
                             "penalty_minor": money_minor({"amount": amount, "currency": penalty.get("currency")}) if amount is not None else None})
    rate_identity = {k: rate.get(k) for k in ("breakfast_included", "refundable", "free_cancellation", "free_cancellation_until", "pay_at_hotel", "pay_online")}
    meal = (rate.get("meal") or {}).get("name")
    if meal is not None:
        rate_identity["meal"] = meal
    key = "hotel-" + fingerprint([hotel_id, room.get("room_name"), room.get("bed_type"), rate_identity, search_key(request)])
    item = _base("hotel", hotel.get("name") or "Отель", rate["price"], key, request, now,
                 data_origin, recorded_at or now, source)
    item["provider_refs"].update({"hotel_id": hotel_id, "room_name": room.get("room_name")})
    location = hotel.get("location") or {}
    lat, lng = location.get("lat"), location.get("lng")
    if (type(lat) in (int, float) and type(lng) in (int, float)
            and math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180):
        item["location"] = {"latitude": lat, "longitude": lng, "address": hotel.get("address") or ""}
    elif isinstance(hotel.get("address"), str) and hotel["address"].strip():
        item["location"] = {"address": hotel["address"].strip()}
    item["conditions"] = {"details_verified": True, "breakfast": rate.get("breakfast_included"),
        "refundable": rate.get("refundable"), "free_cancellation": _current_free_cancellation(rate, now),
        "free_cancellation_until": rate.get("free_cancellation_until"), "cancellation_policy": cancellation,
        "pets": pets, "room_name": room.get("room_name"), "bed_type": room.get("bed_type"),
        "bed_summary": room.get("bed_summary"), "max_occupancy": room.get("max_occupancy"),
        "check_in_time": hotel.get("check_in_time"), "check_out_time": hotel.get("check_out_time"),
        "rating": hotel.get("rating"), "stars": hotel.get("stars"), "meal": (rate.get("meal") or {}).get("name"),
        "price_basis": "stay_total", "pay_at_hotel": rate.get("pay_at_hotel"),
        "pay_online": rate.get("pay_online"), "room_size_sqm": room.get("room_size_sqm")}
    return item


def normalize_rail(outbound: dict, outward_fare: dict, inbound: dict, return_fare: dict,
                   request: dict, now: str, *, source: str = "tutu:search_rail") -> dict:
    """Two independently priced direct rides, adult seat totals, never a through ticket.

    Tutu rail variants are per-seat, even with passengers > 1. The searched fare
    must explicitly have room for the whole party; inventory is not summed
    across variants because rows can share the same seats.
    """
    adults = request.get("adults")
    if type(adults) is not int or not 1 <= adults <= 6 or request.get("children_ages"):
        raise ValueError("unsupported_rail_party")
    parts = []
    for offer, fare, direction in ((outbound, outward_fare, "outbound"), (inbound, return_fare, "inbound")):
        seats = fare.get("seats_left")
        if type(seats) is not int or seats < adults:
            raise ValueError("unverified_rail_capacity")
        segments = [{k: segment.get(k) for k in ("from", "to", "departure_at", "arrival_at", "voyage_no", "carrier")}
                    for leg in offer.get("legs", []) for segment in leg.get("segments", [])]
        if len(segments) != 1 or not segments[0].get("voyage_no"):
            raise ValueError("unsupported_rail_connection")
        departure, arrival = (datetime.fromisoformat(offer[k]) for k in ("departure_at", "arrival_at"))
        if not departure.tzinfo or not arrival.tzinfo or departure >= arrival:
            raise ValueError("unverified_rail_times")
        expected = request["departure_date" if direction == "outbound" else "return_date"]
        if departure.date().isoformat() != str(expected):
            raise ValueError("unexpected_rail_date")
        if any(segments[0].get(field) != offer[field] for field in ("departure_at", "arrival_at")):
            raise ValueError("inconsistent_rail_times")
        conditions = fare.get("conditions") or {}
        if not fare.get("service_class") or not fare.get("seat_category"):
            raise ValueError("unverified_rail_class")
        identity = [segments, fare["service_class"], fare["seat_category"],
                    {k: conditions.get(k) for k in ("refundable", "changeable")}]
        parts.append({"reference_key": fingerprint(identity), "train_numbers": [segments[0]["voyage_no"]],
                      "segments": segments, "departure_at": offer["departure_at"], "arrival_at": offer["arrival_at"],
                      "price_minor": money_minor(fare["price"]) * adults,
                      "service_class": fare["service_class"], "seat_category": fare["seat_category"],
                      "refundable": conditions.get("refundable"), "changeable": conditions.get("changeable"),
                      "seats_left": seats})
    if datetime.fromisoformat(parts[0]["arrival_at"]) >= datetime.fromisoformat(parts[1]["departure_at"]):
        raise ValueError("incompatible_rail_return")
    total = sum(part["price_minor"] for part in parts)
    key = "rail-" + fingerprint([search_key(request), [p["reference_key"] for p in parts]])
    title = "Поезда " + " / ".join(p["train_numbers"][0] for p in parts)
    item = _base("rail", title, {"amount": str(Decimal(total) / 100), "currency": "RUB"},
                 key, request, now, "live", now, source)
    def both(field):
        values = [p[field] for p in parts]
        return False if False in values else True if all(v is True for v in values) else None
    item["conditions"] = {"details_verified": True, "direct": True,
        "refundable": both("refundable"), "changeable": both("changeable"), "pets": None,
        "seat_category": parts[0]["seat_category"] if parts[0]["seat_category"] == parts[1]["seat_category"] else None,
        "seat_categories": [p["seat_category"] for p in parts],
        "service_class": parts[0]["service_class"] if parts[0]["service_class"] == parts[1]["service_class"] else None,
        "service_classes": [p["service_class"] for p in parts],
        "price_basis": "party_round_trip_total", "passengers": adults,
        "seats_left": min(p["seats_left"] for p in parts), "exact_seats_verified": False}
    item.update(departure_at=parts[0]["departure_at"], arrival_at=parts[0]["arrival_at"],
                return_departure_at=parts[1]["departure_at"], return_arrival_at=parts[1]["arrival_at"])
    item["provider_refs"].update(outbound=parts[0], inbound=parts[1],
        train_numbers=[p["train_numbers"][0] for p in parts])
    item["warnings"] = ["Два отдельных железнодорожных билета. Конкретные и соседние места не проверены."]
    return item


def normalize_bus(outbound: dict, outward_fare: dict, inbound: dict, return_fare: dict,
                  request: dict, now: str, *, source: str = "tutu:search_bus") -> dict:
    """Two direct bus tickets. Each searched fare already prices the whole party.

    Child-fare age policies differ by carrier, so this initial integration only
    accepts adults. Missing luggage, refund and exact-seat facts stay unknown.
    """
    adults = request.get("adults")
    if type(adults) is not int or not 1 <= adults <= 8 or request.get("children_ages"):
        raise ValueError("unsupported_bus_party")
    parts = []
    for offer, fare, direction in ((outbound, outward_fare, "outbound"), (inbound, return_fare, "inbound")):
        segments = [{k: segment.get(k) for k in ("from", "to", "departure_at", "arrival_at", "voyage_no", "carrier")}
                    for leg in offer.get("legs", []) for segment in leg.get("segments", [])]
        if len(segments) != 1 or not all(segments[0].get(k) for k in ("from", "to", "carrier")):
            raise ValueError("unverified_bus_stops")
        departure, arrival = (datetime.fromisoformat(offer[k]) for k in ("departure_at", "arrival_at"))
        if not departure.tzinfo or not arrival.tzinfo or departure >= arrival:
            raise ValueError("unverified_bus_times")
        expected = request["departure_date" if direction == "outbound" else "return_date"]
        if departure.date().isoformat() != str(expected):
            raise ValueError("unexpected_bus_date")
        if segments[0].get("departure_at") != offer["departure_at"] or segments[0].get("arrival_at") != offer["arrival_at"]:
            raise ValueError("inconsistent_bus_times")
        # Single-tariff identity survives price updates; multiple tariffs must
        # keep their specific returned ID rather than matching the cheapest.
        tariff = fare.get("variant_id") if len(offer.get("variants") or []) > 1 else "single"
        identity = [segments, tariff, fare.get("conditions") or {}]
        parts.append({"reference_key": fingerprint(identity), "segments": segments,
                      "departure_at": offer["departure_at"], "arrival_at": offer["arrival_at"],
                      "price_minor": money_minor(fare["price"]), "carrier": segments[0]["carrier"]})
    if datetime.fromisoformat(parts[0]["arrival_at"]) >= datetime.fromisoformat(parts[1]["departure_at"]):
        raise ValueError("incompatible_bus_return")
    total = sum(p["price_minor"] for p in parts)
    key = "bus-" + fingerprint([search_key(request), [p["reference_key"] for p in parts]])
    title = "Автобус · " + " / ".join(dict.fromkeys(p["carrier"] for p in parts))
    item = _base("bus", title, {"amount": str(Decimal(total) / 100), "currency": "RUB"},
                 key, request, now, "live", now, source)
    item["conditions"] = {"details_verified": True, "direct": True, "refundable": None,
        "changeable": None, "pets": None, "baggage_kg": None,
        "price_basis": "party_round_trip_total", "passengers": adults, "exact_seats_verified": False}
    item.update(departure_at=parts[0]["departure_at"], arrival_at=parts[0]["arrival_at"],
                return_departure_at=parts[1]["departure_at"], return_arrival_at=parts[1]["arrival_at"])
    item["provider_refs"].update(outbound=parts[0], inbound=parts[1])
    item["warnings"] = ["Два отдельных автобусных билета. Багаж, возврат и конкретные места не подтверждены."]
    return item


def comparable(offer: dict) -> dict:
    """Only fields the traveler confirms; acquisition timestamps are not a change."""
    return {k: offer.get(k) for k in ("product_type", "title", "price_minor", "currency", "query", "conditions",
           "data_origin", "availability", "departure_at", "arrival_at", "return_departure_at", "return_arrival_at")}


def match_refresh(components: list[dict], candidates: list[dict], warnings: list[str] | None = None) -> dict:
    by_key = {x["provider_refs"]["reference_key"]: x for x in candidates}
    refreshed = []
    for old in components:
        new = by_key.get(old.get("provider_refs", {}).get("reference_key"))
        if new is None:
            return {"status": "unknown", "components": [], "warnings": warnings or [], "error": {
                "code": "stale_offer", "retryable": True,
                "message": "Точный выбранный тариф не найден в проверенной области; выберите предложение заново."}}
        refreshed.append(new)
    changed = any(comparable(a) != comparable(b) for a, b in zip(components, refreshed))
    return {"status": "changed" if changed else "unchanged", "components": refreshed,
            "warnings": warnings or [], "error": None}
