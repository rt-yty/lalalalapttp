from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from ..concert_intake import ConcertIntakeState


class TripRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origin: str | None = None
    destination: str | None = None
    departure_date: date | None = None
    return_date: date | None = None
    adults: int | None = Field(default=None, ge=1, le=9, strict=True)
    children_ages: list[int] = Field(default_factory=list)
    pets: bool = False
    budget_minor: int | None = Field(default=None, ge=1, strict=True)
    budget_scope: Literal["trip", "per_person"] | None = None
    nightly_budget_minor: int | None = Field(default=None, ge=1, strict=True)
    constraints: dict[str, Any] = Field(default_factory=dict)
    products: list[Literal["avia", "rail", "bus", "hotel"]] = Field(default_factory=lambda: ["avia", "hotel"], min_length=1, max_length=4)
    event_date: date | None = None
    event_start_at: datetime | None = None
    event_checkout_ack: str | None = None

    @field_validator("products")
    @classmethod
    def unique_products(cls, value):
        return list(dict.fromkeys(value))

    @field_validator("event_start_at")
    @classmethod
    def event_timezone(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("Время мероприятия должно содержать часовой пояс")
        return value

    @field_validator("children_ages", mode="before")
    @classmethod
    def ages(cls, v):
        if not isinstance(v, list) or len(v) > 8 or any(type(age) is not int or age < 0 or age > 17 for age in v):
            raise ValueError("Возраст каждого ребёнка должен быть от 0 до 17 лет")
        return v

    @model_validator(mode="after")
    def dates(self):
        if self.departure_date and self.return_date and self.return_date <= self.departure_date:
            raise ValueError("Дата возвращения должна быть позже даты выезда")
        return self


class TurnInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    request_id: str = Field(min_length=1, max_length=150)
    user_id: str = Field(min_length=1, max_length=150)
    session_id: str = Field(min_length=1, max_length=150)
    expected_state_version: int = Field(ge=0)
    input: Literal["message", "select_event", "select_option", "select_component", "remove_component", "confirm_action", "reject_action", "undo", "update_preference"]
    payload: dict[str, Any]
    now: datetime
    timezone: str = "Europe/Moscow"

    @field_validator("now")
    @classmethod
    def aware(cls, v):
        if v.tzinfo is None:
            raise ValueError("now должен содержать часовой пояс")
        return v

    @field_validator("timezone")
    @classmethod
    def zone(cls, v):
        ZoneInfo(v)
        return v


class Suggestion(BaseModel):
    """An editable next-message draft; never an executable domain action."""
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=72)
    text: str = Field(min_length=1, max_length=500)


class TurnResult(BaseModel):
    request_id: str
    session_id: str
    state_version: int
    status: Literal["needs_input", "research_ready", "options_ready", "plan_ready", "needs_confirmation", "completed_demo", "handoff_ready", "in_progress", "partial", "error"]
    message: str
    response_kind: Literal["trip", "chat"] = "trip"
    questions: list[str] = Field(default_factory=list)
    suggestions: list[Suggestion] = Field(default_factory=list, max_length=3)
    suggestions_version: int = 6
    web_search: dict[str, Any] | None = None
    event_choices: list[dict[str, Any]] = Field(default_factory=list)
    event_choices_version: int = 0
    selected_event: dict[str, Any] | None = None
    options: list[dict[str, Any]] = Field(default_factory=list)
    transport_options: list[dict[str, Any]] = Field(default_factory=list)
    hotel_options: list[dict[str, Any]] = Field(default_factory=list)
    selected_components: list[dict[str, Any]] = Field(default_factory=list)
    component_errors: dict[str, Any] = Field(default_factory=dict)
    current_plan: dict[str, Any] | None = None
    plan_diff: list[dict[str, Any]] = Field(default_factory=list)
    pending_action: dict[str, Any] | None = None
    demo_order: dict[str, Any] | None = None
    handoff: dict[str, Any] | None = None
    evidence: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    search_coverage: list[dict[str, Any]] = Field(default_factory=list)
    search_diagnostics: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


class SessionSnapshot(BaseModel):
    user_id: str
    session_id: str
    state_version: int = 0
    data_mode: Literal["live", "replay"]
    checkout_mode: Literal["demo", "disabled"]
    request: TripRequest = Field(default_factory=TripRequest)
    profile_context: dict[str, Any] = Field(default_factory=dict)
    concert_intake: ConcertIntakeState = Field(default_factory=ConcertIntakeState)
    options: list[dict[str, Any]] = Field(default_factory=list)
    transport_options: list[dict[str, Any]] = Field(default_factory=list)
    hotel_options: list[dict[str, Any]] = Field(default_factory=list)
    selected_components: list[dict[str, Any]] = Field(default_factory=list)
    component_errors: dict[str, Any] = Field(default_factory=dict)
    options_version: int = 0
    current_plan: dict[str, Any] | None = None
    plan_history: list[dict[str, Any]] = Field(default_factory=list)
    plan_revision: int = 0
    pending_action: dict[str, Any] | None = None
    demo_order: dict[str, Any] | None = None
    active_skills: list[dict[str, str]] = Field(default_factory=list)
    pins: dict[str, Any] = Field(default_factory=dict)
    search_coverage: list[dict[str, Any]] = Field(default_factory=list)
    search_diagnostics: dict[str, Any] | None = None
    last_question: str | None = None
    children_pending: bool = False
    dialogue: list[dict[str, str]] = Field(default_factory=list, max_length=24)
    last_web_search: dict[str, Any] | None = None
    event_choices: list[dict[str, Any]] = Field(default_factory=list)
    event_choices_version: int = 0
    selected_event: dict[str, Any] | None = None
    continued_messages: list[dict[str, Any]] = Field(default_factory=list, max_length=24)
    warnings: list[str] = Field(default_factory=list)
