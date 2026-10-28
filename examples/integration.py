"""Offline agent/backend integration example.

Run: uv run python examples/integration.py [--demo-order]
Uses rules + historical replay in a temporary database; no model or network calls.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from travel_agent.domain.models import TurnInput
from travel_agent.interpretation import RuleBasedGateway
from travel_agent.service import AgentService


NOW = datetime.fromisoformat("2026-10-28T12:00:00+03:00")
QUERY = (
    "Из Москвы в Ереван с 24 ноября по 1 декабря 2026, один взрослый, "
    "общий бюджет 80 тысяч рублей на перелёт и отель. Багаж 23 кг и завтрак."
)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


async def integration_example(*, demo_order=False):
    # A real backend derives this value from its verified identity, never from
    # an untrusted browser payload. This example has no HTTP/authentication layer.
    user_id, session_id = "verified-example-user", str(uuid4())
    checkout_mode = "demo" if demo_order else "disabled"
    with TemporaryDirectory(prefix="travel-agent-integration-") as directory:
        db_path = Path(directory) / "agent.sqlite"

        def open_service():
            return AgentService(db_path=db_path, data_mode="replay", checkout_mode=checkout_mode,
                                gateway=RuleBasedGateway())

        def request(version, kind, payload):
            return TurnInput(request_id=str(uuid4()), user_id=user_id, session_id=session_id,
                             expected_state_version=version, input=kind, payload=payload,
                             now=NOW, timezone="Europe/Moscow")

        def show(stage, result):
            print(json.dumps({"stage": stage, "status": result.status, "state_version": result.state_version,
                              "transport_options": len(result.transport_options), "hotel_options": len(result.hotel_options),
                              "selected_products": [card["product_type"] for card in result.selected_components],
                              "missing_products": (result.current_plan or {}).get("missing_products"),
                              "message": result.message}, ensure_ascii=False))

        service = open_service()
        try:
            events = []
            first_request = request(0, "message", {"text": QUERY})
            found = await service.run_turn(first_request, on_event=events.append)
            require(found.status == "options_ready", found.message)
            show("search", found)

            # A transport retry sends the exact original envelope, including now.
            repeated = await service.run_turn(first_request)
            require(repeated.model_dump() == found.model_dump(), "Identical retry changed the result")

            flight = found.transport_options[0]
            selected = await service.run_turn(request(found.state_version, "select_component", {
                "component_id": flight["component_id"], "options_version": flight["options_version"],
            }))
            require([card["product_type"] for card in selected.selected_components] == ["avia"], selected.message)
            show("select_transport", selected)

            # Recreate the service on the same store; this is trip persistence,
            # not a persistent user preference profile.
            service.close()
            service = open_service()
            restored = await service.get_session(user_id, session_id)
            require(restored.selected_components == selected.selected_components, "Chosen transport was lost")
            hotel = restored.hotel_options[0]
            complete = await service.run_turn(request(restored.state_version, "select_component", {
                "component_id": hotel["component_id"], "options_version": hotel["options_version"],
            }))
            require(complete.current_plan and not complete.current_plan["missing_products"], complete.message)
            show("restore_and_select_hotel", complete)

            answered = await service.run_turn(request(complete.state_version, "message", {"text": "Какой багаж включён?"}))
            require(answered.selected_components == complete.selected_components, "A question changed the selected trip")
            show("ask_about_selected_transport", answered)

            # Unselecting a component keeps the requested product and other choices.
            removed = await service.run_turn(request(answered.state_version, "remove_component", {"product_type": "hotel"}))
            require([card["product_type"] for card in removed.selected_components] == ["avia"], removed.message)
            require(removed.current_plan["missing_products"] == ["hotel"], "Removing the hotel changed the requested trip")
            show("unselect_hotel", removed)
            hotel = removed.hotel_options[0]
            complete = await service.run_turn(request(removed.state_version, "select_component", {
                "component_id": hotel["component_id"], "options_version": hotel["options_version"],
            }))
            require(complete.current_plan and not complete.current_plan["missing_products"], complete.message)
            require(complete.pending_action is None and complete.demo_order is None, "Selection unexpectedly checked out")
            show("select_hotel_again", complete)

            if demo_order:
                pending = await service.run_turn(request(complete.state_version, "message", {"text": "Оформить"}))
                require(pending.status == "needs_confirmation", pending.message)
                # In a UI this exact confirmation is shown to the user first.
                # --demo-order explicitly opts into this scripted local simulation.
                payload = {key: pending.pending_action[key] for key in ("action_id", "plan_id", "plan_version", "payload_hash")}
                done = await service.run_turn(request(pending.state_version, "confirm_action", payload))
                require(done.status == "completed_demo", done.message)
                show("optional_local_demo_order", done)

            print(json.dumps({"verified": True, "data_mode": "replay", "model_backend": "rules",
                              "live_model_tested": False, "live_provider_tested": False,
                              "first_turn_events": [event["type"] for event in events]}, ensure_ascii=False))
        finally:
            service.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo-order", action="store_true", help="Also simulate a confirmed local demo order")
    asyncio.run(integration_example(demo_order=parser.parse_args().demo_order))
