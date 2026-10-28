from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import importlib.metadata
import json
import os
import platform
from pathlib import Path
import shutil
import subprocess
import tempfile
from uuid import uuid4
from zoneinfo import ZoneInfo

from .config import load_settings, make_gateway
from .domain.models import TurnInput
from .domain.planning import rub
from .service import AgentService

DEMO_TEXT = "Из Москвы в Ереван с 24 ноября по 1 декабря 2026, один взрослый, общий бюджет 80 тысяч рублей на перелёт и отель. Нужен багаж 23 кг и завтрак."


def print_result(result, json_output=False):
    if json_output:
        print(result.model_dump_json(indent=2))
        return
    print("\nАгент: " + result.message)
    if result.status in ("options_ready", "partial"):
        labels = {"satisfied": "соответствует запросу", "unknown": "нужна проверка", "violated": "не соответствует"}
        for i, option in enumerate(result.options, 1):
            print(f"\n  {i}. {option['title']} — {rub(option['total_minor'])}")
            print("     " + labels[option["eligibility"]])
            for reason in option["reasons"][:2]:
                print("     " + reason)
            for item in option["components"]:
                c = item.get("conditions", {})
                if item["product_type"] == "avia":
                    print(f"     Багаж: {c.get('baggage_kg', 'неизвестно')} кг; тариф: {c.get('fare_family', 'неизвестно')}")
                else:
                    breakfast = {True: "включён", False: "не включён", None: "неизвестно"}.get(c.get("breakfast"), "неизвестно")
                    print(f"     Завтрак: {breakfast}; номер: {c.get('room_name', 'неизвестно')}")
            for warning in option["warnings"]:
                if not warning.startswith("Трансфер"):
                    print("     • " + warning)
    if result.plan_diff:
        print("Изменения:")
        for item in result.plan_diff:
            print(f"  {item['field']}: {item['before']} → {item['after']}")
    for warning in result.warnings:
        print("  Примечание: " + warning)
    if result.error:
        print("  Код: " + result.error["code"])
    print()


def doctor(args):
    from .skill_loader import SkillRegistry
    from .adapters.replay import ReplayAdapter
    checks = {"python": platform.python_version(), "package": importlib.metadata.version("ostrovok-travel-agent"),
              "langgraph": importlib.metadata.version("langgraph"),
              "skills": [s["name"] for s in SkillRegistry().catalog()],
              "replay": ReplayAdapter().fixture["request"],
              "codex_installed": bool(shutil.which("codex")),
              "hf_token_present": bool(os.getenv("HF_TOKEN")),
              "default_model_backend": args.model_backend}
    if checks["codex_installed"]:
        proc = subprocess.run(["codex", "login", "status"], capture_output=True, text=True, timeout=15)
        checks["codex_authenticated"] = proc.returncode == 0
        checks["codex_auth_type"] = "ChatGPT" if "ChatGPT" in proc.stderr + proc.stdout else "другая/не определена"
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    if args.probe_codex:
        async def probe():
            gateway = make_gateway("codex")
            result = await gateway.interpret(DEMO_TEXT, {}, "2026-10-27T12:00:00+03:00", "Europe/Moscow", skills=[])
            print("Проверка Codex:")
            print(json.dumps(result, ensure_ascii=False, indent=2))
        asyncio.run(probe())


def make_service(args, db=None):
    return AgentService(db_path=db or args.db, data_mode=args.data_mode, checkout_mode=args.checkout_mode, gateway=make_gateway(args.model_backend))


async def chat(args):
    service = make_service(args)
    session_id = args.session or uuid4().hex[:12]
    try:
        session = await service.get_session(args.user, session_id)
        version = session.state_version
    except KeyError:
        version = 0
    print("О!дин запрос · прототип агента путешествий")
    print(f"Модель: {args.model_backend} · данные: {args.data_mode} · оформление: {args.checkout_mode}")
    if args.data_mode == "replay":
        print("Исторические данные Туту от 26.10.2026. Доступно: Москва → Ереван, 24.11–01.12.2026, один взрослый.")
    if args.model_backend == "rules":
        print("Включён ограниченный тестовый разбор текста без LLM.")
    print(f"Сессия: {session_id}. Продолжить позже: travel-agent chat --session {session_id}")
    print("Команды: /пример, /состояние, /назад, /выход. Можно писать обычным текстом.")
    print("Пример: " + DEMO_TEXT)

    async def progress(event):
        if args.events:
            print(f"  [{event['type']}] {event['payload']}")

    try:
        while True:
            try:
                text = input("Вы: ").strip()
            except EOFError:
                break
            if text in ("/выход", "/exit", "/quit"):
                break
            if not text:
                continue
            if text == "/состояние":
                try:
                    print((await service.get_session(args.user, session_id)).model_dump_json(indent=2))
                except KeyError:
                    print("Поездка ещё не начата.")
                continue
            if text == "/пример":
                text = DEMO_TEXT
                print(text)
            kind, payload = ("undo", {}) if text == "/назад" else ("message", {"text": text})
            turn = TurnInput(request_id=uuid4().hex, user_id=args.user, session_id=session_id, expected_state_version=version, input=kind, payload=payload, now=datetime.now(ZoneInfo("Europe/Moscow")))
            print("Агент обрабатывает запрос…", flush=True)
            result = await service.run_turn(turn, on_event=progress)
            version = result.state_version
            print_result(result, args.json)
    finally:
        service.close()


async def demo(args):
    # A separate temporary DB keeps the reproducible demo out of personal trips.
    with tempfile.TemporaryDirectory(prefix="travel-agent-demo-") as tmp:
        service = make_service(args, str(Path(tmp) / "agent.sqlite"))
        version = 0
        now = "2026-10-27T12:00:00+03:00"
        async def run(kind, payload):
            nonlocal version
            turn = TurnInput(request_id=uuid4().hex, user_id="demo", session_id="demo", expected_state_version=version, input=kind, payload=payload, now=now)
            result = await service.run_turn(turn)
            version = result.state_version
            print_result(result, args.json)
            return result
        try:
            print("ДЕМО: " + DEMO_TEXT)
            result = await run("message", {"text": DEMO_TEXT})
            candidates = [o for o in result.options if o["eligibility"] == "satisfied"]
            option = next((o for o in candidates if o["total_minor"] == 6792000), candidates[0] if candidates else None)
            if not option:
                raise RuntimeError("Демо не получило подходящих вариантов")
            await run("select_option", {"option_id": option["option_id"], "options_version": option["options_version"]})
            result = await run("message", {"text": "Оформить демонстрационный заказ"})
            if not result.pending_action:
                raise RuntimeError("Демо не дошло до подтверждения")
            result = await run("confirm_action", {k: result.pending_action[k] for k in ("action_id", "plan_id", "plan_version", "payload_hash")})
            if result.status != "completed_demo":
                raise RuntimeError("Демонстрационный заказ не создан")
            print("Сквозной сценарий успешно пройден. Реальной покупки нет.")
        finally:
            service.close()


def main():
    settings = load_settings()
    parser = argparse.ArgumentParser(description="О!дин запрос — агент путешествий")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "chat", "demo", "eval", "web"):
        p = sub.add_parser(name)
        p.add_argument("--model-backend", choices=["codex", "rules", "hf"], default=settings["model_backend"])
        p.add_argument("--data-mode", choices=["replay", "live"], default="live" if name == "web" and not os.getenv("AGENT_DATA_MODE") else settings["data_mode"])
        p.add_argument("--checkout-mode", choices=["demo", "disabled"], default=settings["checkout_mode"])
        p.add_argument("--db", default=os.getenv("DATABASE_URL") or ("data/web-agent.sqlite" if name == "web" else settings["db_path"]))
        p.add_argument("--json", action="store_true")
        if name == "web":
            p.add_argument("--port", type=int, default=8765)
            p.add_argument("--open", action="store_true")
        if name == "doctor":
            p.add_argument("--probe-codex", action="store_true")
        if name == "chat":
            p.add_argument("--session")
            p.add_argument("--user", default="local")
            p.add_argument("--events", action="store_true")
        if name == "eval":
            p.add_argument("--suite", choices=["core"], default="core")
    args = parser.parse_args()
    try:
        if args.command == "doctor":
            doctor(args)
        elif args.command == "chat":
            asyncio.run(chat(args))
        elif args.command == "demo":
            asyncio.run(demo(args))
        elif args.command == "web":
            from .web import serve
            serve(db_path=args.db, model_backend=args.model_backend, data_mode=args.data_mode, checkout_mode=args.checkout_mode, port=args.port, open_browser=args.open)
        else:
            from .evaluation import run_core
            result = asyncio.run(run_core())
            print(json.dumps(result, ensure_ascii=False, indent=2))
            raise SystemExit(1 if result["failed"] else 0)
    except KeyboardInterrupt:
        print("\nОстановлено. Последний завершённый ход сохранён.")
    except Exception as exc:
        print(f"Ошибка: {exc}")
        raise SystemExit(1)
