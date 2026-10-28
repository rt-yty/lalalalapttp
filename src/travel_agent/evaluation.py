"""Small offline acceptance suite shared by the installed CLI.

No pytest dependency, model calls, credentials or external booking actions.
This suite measures the deterministic replay path, not live-model quality.
"""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from uuid import uuid4

from .domain.models import TurnInput
from .interpretation import RuleBasedGateway
from .service import AgentService

NOW = "2026-10-27T12:00:00+03:00"
TRIP = "Из Москвы в Ереван с 24 ноября по 1 декабря 2026, один взрослый, общий бюджет 80 тысяч рублей на перелёт и отель"


def _turn(version=0, text=TRIP, *, kind="message", payload=None, user="eval-user", request_id=None):
    return TurnInput(request_id=request_id or str(uuid4()), user_id=user, session_id="eval-trip",
                     expected_state_version=version, input=kind,
                     payload=payload if payload is not None else {"text": text},
                     now=NOW, timezone="Europe/Moscow")


def _require(condition, explanation):
    if not condition:
        raise AssertionError(explanation)


async def _options(service):
    result = await service.run_turn(_turn())
    _require(result.status == "options_ready", "Полный запрос не привёл к вариантам: " + result.status)
    _require(bool(result.options), "Варианты отсутствуют")
    return result


async def _pending(service):
    options = await _options(service)
    option = options.options[0]
    selected = await service.run_turn(_turn(options.state_version, kind="select_option", payload={
        "option_id": option["option_id"], "options_version": option["options_version"]}))
    _require(selected.status == "plan_ready", "Не удалось выбрать действующий вариант")
    pending = await service.run_turn(_turn(selected.state_version, "Оформить"))
    _require(pending.status == "needs_confirmation", "Нет явного шага подтверждения")
    return pending


def _confirm_payload(result):
    return {key: result.pending_action[key] for key in ("action_id", "plan_id", "plan_version", "payload_hash")}


async def _known_total(service, path):
    result = await _options(service)
    option = next((item for item in result.options if item["total_minor"] == 6_792_000), None)
    _require(option is not None, "Не воспроизведён контрольный итог 67 920 ₽")
    _require(sorted(item["price_minor"] for item in option["components"]) == [3_270_200, 3_521_800], "Цены компонентов умножены повторно или взят неверный тариф")
    _require(all(item["data_origin"] == "recorded_fixture" for item in option["components"]), "Архив выдан за живые данные")
    return {"total_minor": option["total_minor"]}


async def _missing_guests(service, path):
    result = await service.run_turn(_turn(text="Из Москвы в Ереван с 24 ноября по 1 декабря 2026"))
    _require(result.status == "needs_input" and not result.options, "Число гостей придумано вместо уточнения")
    return {"status": result.status}


async def _request_replay(service, path):
    turn = _turn(request_id="same-request")
    first = await service.run_turn(turn)
    second = await service.run_turn(turn)
    _require(first.model_dump() == second.model_dump(), "Повтор request_id изменил результат")
    conflict = await service.run_turn(_turn(first.state_version, "Бюджет 50 тысяч", request_id="same-request"))
    _require(conflict.error and conflict.error["code"] == "request_id_conflict", "Изменённый ввод принят под прежним request_id")
    return {"status": first.status, "duplicate": "same_result"}


async def _single_demo(service, path):
    pending = await _pending(service)
    payload = _confirm_payload(pending)
    done = await service.run_turn(_turn(pending.state_version, kind="confirm_action", payload=payload))
    _require(done.status == "completed_demo", "Демонстрационный сценарий не завершён")
    again = await service.run_turn(_turn(done.state_version, kind="confirm_action", payload=payload))
    _require(again.demo_order and again.demo_order["local_id"] == done.demo_order["local_id"], "Повтор подтверждения создал другой заказ")
    _require(done.demo_order["status"] == "simulated_confirmed", "Результат не помечен имитацией")
    return {"status": done.status, "duplicate": "same_order"}


async def _stale_confirmation(service, path):
    pending = await _pending(service)
    old = _confirm_payload(pending)
    updated = await service.run_turn(_turn(pending.state_version, "Общий бюджет теперь 50 тысяч рублей"))
    stale = await service.run_turn(_turn(updated.state_version, kind="confirm_action", payload=old))
    _require(stale.status == "error" and stale.demo_order is None, "Старое подтверждение создало заказ после изменения условий")
    return {"status": stale.status}


async def _restart(service, path):
    pending = await _pending(service)
    second = AgentService(db_path=path, data_mode="replay", checkout_mode="demo", gateway=RuleBasedGateway())
    try:
        state = await second.get_session("eval-user", "eval-trip")
        _require(state.pending_action == pending.pending_action, "Ожидающее подтверждение потеряно при перезапуске")
        result = await second.run_turn(_turn(state.state_version, kind="confirm_action", payload=_confirm_payload(pending)))
        _require(result.status == "completed_demo", "Подтверждение не продолжилось после перезапуска")
        return {"status": result.status}
    finally:
        second.close()


async def _isolation(service, path):
    await _options(service)
    await service.run_turn(_turn(text="Не знаю, куда поехать", user="another-user"))
    alice = await service.get_session("eval-user", "eval-trip")
    bob = await service.get_session("another-user", "eval-trip")
    _require(alice.request.destination and bob.request.destination is None and not bob.options, "Истории пользователей смешались")
    return {"isolated_users": 2}


async def _fixture_missing(service, path):
    result = await service.run_turn(_turn(text=TRIP.replace("24 ноября", "25 ноября")))
    _require(result.error and result.error["code"] == "fixture_missing", "Неизвестный архивный запрос не вернул fixture_missing")
    _require(not result.options, "Для неизвестного архивного запроса придуманы предложения")
    return {"error_code": result.error["code"]}


async def _pets_unknown(service, path):
    result = await service.run_turn(_turn(text=TRIP + ", обязательно с собакой"))
    _require(all(option["eligibility"] != "satisfied" for option in result.options), "Неизвестное условие питомца признано выполненным")
    checkout = await service.run_turn(_turn(result.state_version, "Оформить"))
    _require(checkout.pending_action is None and checkout.demo_order is None, "Можно оформить непроверенные условия")
    return {"status": checkout.status}


async def run_core() -> dict:
    """Return measured, JSON-serializable results for offline acceptance checks."""
    scenarios = [
        ("recorded_total_67920", _known_total),
        ("clarify_missing_guests", _missing_guests),
        ("request_idempotency", _request_replay),
        ("single_demo_order", _single_demo),
        ("stale_confirmation_rejected", _stale_confirmation),
        ("restart_pending_action", _restart),
        ("user_isolation", _isolation),
        ("unknown_fixture_rejected", _fixture_missing),
        ("unknown_pet_policy_blocks_checkout", _pets_unknown),
    ]
    started = perf_counter()
    results = []
    with TemporaryDirectory(prefix="travel-agent-eval-") as directory:
        for index, (name, scenario) in enumerate(scenarios):
            path = Path(directory) / str(index) / "agent.sqlite"
            service = AgentService(db_path=path, data_mode="replay", checkout_mode="demo", gateway=RuleBasedGateway())
            before = perf_counter()
            try:
                details = await scenario(service, path)
                results.append({"name": name, "passed": True, "details": details,
                                "duration_seconds": round(perf_counter() - before, 4)})
            except Exception as exc:
                results.append({"name": name, "passed": False,
                                "error": f"{type(exc).__name__}: {exc}",
                                "duration_seconds": round(perf_counter() - before, 4)})
            finally:
                service.close()
    passed = sum(result["passed"] for result in results)
    return {"suite": "core", "data_mode": "replay", "gateway": "rules", "live_model_tested": False,
            "passed": passed, "failed": len(results) - passed, "results": results,
            "duration_seconds": round(perf_counter() - started, 4)}
