"""Small, bounded conversation context shared by the service and web history."""
from __future__ import annotations


def bound_dialogue(messages, *, limit=24, char_budget=16000):
    selected = []
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") not in ("user", "assistant"):
            continue
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        text = text.strip()[:min(4000, char_budget)]
        if not text:
            break
        selected.append({"role": message["role"], "text": text})
        char_budget -= len(text)
        if len(selected) >= limit or char_budget <= 0:
            break
    return list(reversed(selected))


def event_selection_text(event):
    """Use one label for optimistic selections, saved history and model context."""
    from datetime import datetime
    from .concert_intake import meaningful_event_name
    if not event:
        return "Выбираю событие"
    title = event.get("title")
    if not meaningful_event_name(title) and meaningful_event_name(event.get("artist")):
        title = event["artist"]
    date_label = event.get("date", "")
    try:
        date_label = datetime.fromisoformat(date_label).strftime("%d.%m.%Y")
    except (ValueError, TypeError):
        date_label = ""
    return "Выбираю концерт: " + ", ".join(str(value) for value in (event.get("city"), date_label, title) if value)


def turn_text(turn, session):
    if turn.input == "message":
        return turn.payload.get("text", "")
    if turn.input == "select_option":
        option = next((o for o in session.options if o["option_id"] == turn.payload.get("option_id")), None)
        return "Выбираю: " + option["title"] if option else "Выбираю вариант поездки"
    if turn.input == "select_component":
        cards = session.transport_options + session.hotel_options
        component = next((c for c in cards if c["component_id"] == turn.payload.get("component_id")), None)
        return "Выбираю: " + component["title"] if component else "Выбираю вариант поездки"
    if turn.input == "select_event":
        event = session.selected_event
        return event_selection_text(event)
    if turn.input == "remove_component":
        names = {"avia": "перелёт", "rail": "поезд", "bus": "автобус", "hotel": "отель"}
        return "Убираю выбранный " + names.get(turn.payload.get("product_type"), "вариант")
    return {"confirm_action": "Подтверждаю демонстрационный заказ", "reject_action": "Отменяю подтверждение",
            "undo": "Вернуть предыдущий вариант", "update_preference": "Изменить предпочтения"}.get(turn.input, turn.input)
