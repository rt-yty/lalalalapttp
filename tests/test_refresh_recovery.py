from copy import deepcopy

from travel_agent.adapters.replay import ReplayAdapter
from test_core import make_turn, service_at


class UnconfirmedAdapter(ReplayAdapter):
    fail = True

    async def refresh(self, components, request, now):
        if self.fail:
            return {"status": "unknown", "components": [], "warnings": [],
                    "error": {"code": "stale_offer", "retryable": True}}
        return {"status": "unchanged", "components": deepcopy(components), "warnings": [], "error": None}


async def select_plan(service):
    found = await service.run_turn(make_turn())
    option = found.options[0]
    return await service.run_turn(make_turn(found.state_version, kind="select_option", payload={
        "option_id": option["option_id"], "options_version": option["options_version"]}))


async def test_stale_tariff_blocks_checkout_and_refreshes_choices_without_changing_wishes(tmp_path):
    adapter = UnconfirmedAdapter()
    service = service_at(tmp_path / "recover.sqlite", adapter=adapter)
    try:
        selected = await select_plan(service)
        before = (await service.get_session("alice", "trip-1")).request
        failed = await service.run_turn(make_turn(selected.state_version, "Оформить"))
        assert failed.status == "partial" and not failed.pending_action
        assert failed.current_plan["verification_status"] == "unverified"
        assert failed.suggestions[0].id == "refresh-offers"
        assert "Обновим варианты?" in failed.message
        refreshed = await service.run_turn(make_turn(failed.state_version, failed.suggestions[0].text))
        assert refreshed.status == "options_ready"
        assert refreshed.current_plan is None and not refreshed.pending_action
        assert (await service.get_session("alice", "trip-1")).request == before
    finally:
        service.close()


async def test_successful_recheck_clears_unverified_status_before_confirmation(tmp_path):
    adapter = UnconfirmedAdapter()
    service = service_at(tmp_path / "recheck.sqlite", adapter=adapter)
    try:
        selected = await select_plan(service)
        failed = await service.run_turn(make_turn(selected.state_version, "Оформить"))
        adapter.fail = False
        checked = await service.run_turn(make_turn(failed.state_version, "Оформить"))
        assert checked.status == "needs_confirmation" and checked.pending_action
        assert "verification_status" not in checked.current_plan
        assert checked.demo_order is None
    finally:
        service.close()
