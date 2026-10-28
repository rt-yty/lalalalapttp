from copy import deepcopy

from travel_agent.adapters.tutu_mcp import TutuAdapter, hotel_request_for_flights
from travel_agent.domain.planning import check_components, options_from_search

REQUEST = {"origin": "Тестовый город A", "destination": "Тестовый город B", "departure_date": "2027-05-07", "return_date": "2027-05-10", "adults": 1, "children_ages": [], "constraints": {}}
FLIGHT = {"departure_at": "2027-05-07T20:00:00+03:00", "arrival_at": "2027-05-08T10:00:00-06:00", "return_departure_at": "2027-05-10T12:00:00-06:00", "return_arrival_at": "2027-05-11T18:00:00+03:00", "carriers": ["Test Airline"], "variants": [{"price": {"amount": 1000, "currency": "RUB"}, "service_class": "ECONOMIC", "conditions": {"fare_family": "Test"}}]}


class SyntheticAdapter(TutuAdapter):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def capabilities(self):
        return {}

    async def _call(self, name, args, budget):
        self.calls.append((name, deepcopy(args)))
        if name == "search_avia":
            return {"offers": [deepcopy(FLIGHT)], "meta": {"has_more": False}}
        if name == "search_hotels":
            return {"hotels": [{"hotel_id": "test-hotel"}], "meta": {"has_more": False}}
        assert name == "get_offer_details"
        return {"hotel": {"hotel_id": "test-hotel", "name": "Test hotel"}, "rooms": [{"room_name": "Test room", "rates": [{"price": {"amount": 2000, "currency": "RUB"}}]}]}


async def test_overnight_flight_fetches_and_prices_only_actual_hotel_nights():
    adapter = SyntheticAdapter()
    result = await adapter.search(REQUEST, "2026-10-27T20:00:00+03:00")
    assert result["error"] is None
    for name, args in adapter.calls:
        if name in {"search_hotels", "get_offer_details"}:
            assert args["check_in"] == "2027-05-08"
            assert args["check_out"] == "2027-05-10"
    options = options_from_search(result, REQUEST, 1)
    assert len(options) == 1
    assert options[0]["total_minor"] == 300000
    hotel = next(c for c in options[0]["components"] if c["product_type"] == "hotel")
    assert hotel["query"]["departure_date"] == "2027-05-08"
    assert REQUEST["departure_date"] == "2027-05-07"
    wrong = deepcopy(options[0]["components"])
    wrong[1]["query"]["departure_date"] = "2027-05-07"
    check = next(c for c in check_components(wrong, REQUEST) if c["constraint_id"] == "dates_compatible")
    assert check["status"] == "violated"
    assert "требуется да" not in check["explanation"]


def test_invalid_departure_does_not_change_hotel_dates():
    flight = {**FLIGHT, "departure_at": "2027-05-06T20:00:00+03:00"}
    assert hotel_request_for_flights(REQUEST, [flight]) == REQUEST


def test_naive_or_missing_times_are_not_treated_as_confirmed():
    assert hotel_request_for_flights(REQUEST, [{}]) == REQUEST
    assert hotel_request_for_flights(REQUEST, [{**FLIGHT, "arrival_at": "2027-05-08T10:00:00"}]) == REQUEST


def test_local_arrival_date_is_not_converted_to_moscow_time():
    flight = {**FLIGHT, "arrival_at": "2027-05-07T23:00:00-06:00"}
    assert hotel_request_for_flights(REQUEST, [flight])["departure_date"] == "2027-05-07"
