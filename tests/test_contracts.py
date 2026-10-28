"""Public-input validation: reject ambiguous time and impossible trip terms."""

import pytest
from pydantic import ValidationError

from travel_agent.domain.models import TripRequest, TurnInput


BASE_TURN = {
    "request_id": "turn-1", "user_id": "alice", "session_id": "trip-1",
    "expected_state_version": 0, "input": "message", "payload": {"text": "Привет"},
    "now": "2026-10-27T12:00:00+03:00", "timezone": "Europe/Moscow",
}


@pytest.mark.parametrize("change", [
    {"expected_state_version": -1},
    {"request_id": ""},
    {"user_id": ""},
    {"session_id": ""},
    {"input": "register_checkout_passengers"},
    {"now": "2026-10-27T12:00:00"},
    {"schema_version": 2},
    {"untrusted_extra": "override_policy"},
])
def test_invalid_turn_is_rejected(change):
    with pytest.raises(ValidationError):
        TurnInput.model_validate(BASE_TURN | change)


@pytest.mark.parametrize("trip_input", [
    {"adults": 0},
    {"budget_minor": -1},
    {"children_ages": [18]},
    {"children_ages": [-1]},
    {"children_ages": [True]},
    {"departure_date": "2026-11-24", "return_date": "2026-11-23"},
    {"departure_date": "2026-11-24", "return_date": "2026-11-24"},
])
def test_impossible_trip_is_rejected(trip_input):
    with pytest.raises(ValidationError):
        TripRequest.model_validate(trip_input)


def test_input_roundtrip_preserves_timezone_and_minor_units():
    turn = TurnInput.model_validate(BASE_TURN)
    assert turn.now.utcoffset().total_seconds() == 3 * 60 * 60
    assert TurnInput.model_validate_json(turn.model_dump_json()) == turn
    trip = TripRequest(budget_minor=6_792_001, adults=1)
    assert TripRequest.model_validate_json(trip.model_dump_json()).budget_minor == 6_792_001
