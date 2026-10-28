"""Rail prices are per-seat; direct round trips require two verified fare rows."""
from copy import deepcopy

import pytest

from travel_agent.adapters.normalize import normalize_rail
from test_component_adapter import NOW, REQUEST, SchemaAdapter


def train(direction, *, price=1000, seats=4, refundable=True):
    outward = direction == "outbound"
    depart = "2026-11-10T08:00:00+03:00" if outward else "2026-11-17T12:00:00+03:00"
    arrive = "2026-11-10T12:00:00+03:00" if outward else "2026-11-17T16:00:00+03:00"
    segment = {"from": "Москва" if outward else "Санкт-Петербург", "to": "Санкт-Петербург" if outward else "Москва",
               "departure_at": depart, "arrival_at": arrive, "voyage_no": "001А" if outward else "002А", "carrier": "ФПК"}
    return {"departure_at": depart, "arrival_at": arrive, "legs": [{"segments": [segment]}],
        "price": {"amount": 1, "currency": "RUB"}, "price_party": {"amount": 2, "currency": "RUB"},
        "variants": [{"price": {"amount": price, "currency": "RUB"}, "service_class": "2К",
            "seat_category": "COMPARTMENT", "seats_left": seats,
            "conditions": {"refundable": refundable, "changeable": False}}]}


class RailAdapter(SchemaAdapter):
    def __init__(self, seats=4, basis="per_seat"):
        super().__init__()
        self.seats, self.basis = seats, basis

    async def _rpc(self, method, params):
        if method == "tools/call" and params["name"] == "search_rail":
            args = params["arguments"]
            self.calls.append(("search_rail", deepcopy(args)))
            direction = "outbound" if args["origin"] == "Москва" else "inbound"
            offer = train(direction, price=1000 if direction == "outbound" else 1500, seats=self.seats)
            return {"offers": [offer], "meta": {"has_more": False, "pricing": {"basis": self.basis}}}
        return await super()._rpc(method, params)


async def test_round_trip_uses_two_searches_and_exact_fare_party_total():
    adapter = RailAdapter()
    result = await adapter.search({**REQUEST, "products": ["rail"]}, NOW)
    assert result["error"] is None and len(result["trains"]) == 1
    item = result["trains"][0]
    assert item["price_minor"] == (1000 + 1500) * 100 * 2
    assert item["conditions"]["price_basis"] == "party_round_trip_total"
    assert item["conditions"]["exact_seats_verified"] is False
    calls = [args for name, args in adapter.calls if name == "search_rail"]
    assert len(calls) == 2
    assert calls[0]["origin"] == calls[1]["destination"] == "Москва"
    assert calls[1]["departure_date"] == REQUEST["return_date"]
    assert all(c["passengers"] == 2 and c["view"] == "full" and c["direct_only"] for c in calls)
    assert not any(n in ("search_avia", "search_hotels") for n, _ in adapter.calls)


@pytest.mark.parametrize("seats", [1, None, True])
async def test_unverified_or_insufficient_capacity_never_becomes_party_offer(seats):
    result = await RailAdapter(seats=seats).search({**REQUEST, "products": ["rail"]}, NOW)
    assert result["trains"] == []
    if type(seats) is not int:
        assert result["component_errors"]["rail"]["code"] == "unverified_fares"


async def test_children_are_not_silently_priced_as_adults_and_hotels_survive():
    adapter = RailAdapter()
    result = await adapter.search({**REQUEST, "products": ["rail", "hotel"], "children_ages": [7]}, NOW)
    assert result["hotels"] and result["trains"] == [] and result["error"] is None
    assert result["component_errors"]["rail"]["code"] == "unsupported_party"
    assert not any(n == "search_rail" for n, _ in adapter.calls)


async def test_changed_price_basis_is_rejected():
    result = await RailAdapter(basis="unknown").search({**REQUEST, "products": ["rail"]}, NOW)
    assert result["trains"] == [] and result["component_errors"]["rail"]["code"] == "schema_changed"


async def test_refresh_targets_each_selected_train():
    adapter = RailAdapter()
    result = await adapter.search({**REQUEST, "products": ["rail"]}, NOW)
    adapter.calls.clear()
    refreshed = await adapter.refresh(result["trains"], REQUEST, NOW)
    assert refreshed["status"] == "unchanged"
    calls = [a for n, a in adapter.calls if n == "search_rail"]
    assert calls[0]["train_numbers"] == ["001А"]
    assert calls[1]["train_numbers"] == ["002А"]


def test_stable_rail_identity_excludes_price_but_not_refund_conditions():
    out, back = train("outbound"), train("inbound")
    original = normalize_rail(out, out["variants"][0], back, back["variants"][0], REQUEST, NOW)
    out["variants"][0]["price"]["amount"] = 1200
    changed = normalize_rail(out, out["variants"][0], back, back["variants"][0], REQUEST, NOW)
    assert original["provider_refs"]["reference_key"] == changed["provider_refs"]["reference_key"]
    assert original["price_minor"] != changed["price_minor"]
    out["variants"][0]["conditions"]["refundable"] = False
    different = normalize_rail(out, out["variants"][0], back, back["variants"][0], REQUEST, NOW)
    assert different["provider_refs"]["reference_key"] != changed["provider_refs"]["reference_key"]
    assert different["conditions"]["refundable"] is False
