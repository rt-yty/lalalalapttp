"""Offline playback of a deliberately bounded, sanitized historical dataset."""
from __future__ import annotations

import copy
import json
from importlib.resources import files
from .normalize import fingerprint, match_refresh, search_key


class ReplayAdapter:
    def __init__(self, fixture: dict | None = None):
        self.fixture = fixture or json.loads(files("travel_agent").joinpath("fixtures/yerevan_2026-11-24.json").read_text(encoding="utf-8"))

    async def capabilities(self) -> dict:
        return {"provider": "tutu", "mode": "replay", "products": ["avia", "hotel"],
                "recorded_queries": [self.fixture["request"]], "checkout": "demo_only"}

    async def search(self, request: dict, now: str) -> dict:
        products = request.get("products", ["avia", "hotel"])
        query, expected = search_key(request), dict(self.fixture["request"])
        if products == ["hotel"]:
            query.pop("origin", None)
            expected.pop("origin", None)
        if query != expected:
            return {"flights": [], "trains": [], "buses": [], "hotels": [], "component_errors": {}, "coverage": [], "warnings": [], "error": {
                "code": "fixture_missing", "retryable": False,
                "message": "В архиве прототипа нет точного запроса. Доступно: Москва — Ереван, 24.11–01.12.2026, 1 взрослый, без детей."}}
        result = copy.deepcopy(self.fixture["result"])
        result["trains"] = []
        result["buses"] = []
        result["component_errors"] = {}
        if "avia" not in products:
            result["flights"] = []
        if "hotel" not in products:
            result["hotels"] = []
        result["coverage"] = [c for c in result.get("coverage", []) if c.get("product") in products]
        if "rail" in products:
            error = {"code": "fixture_missing", "retryable": False, "provider": "tutu",
                     "message": "В архиве нет железнодорожных тарифов. Цены поездов не подставлялись."}
            result["component_errors"]["rail"] = error
            if not result["flights"] and not result["hotels"]:
                result["error"] = error
        if "bus" in products:
            error = {"code": "fixture_missing", "retryable": False,
                     "message": "В архиве нет автобусных тарифов. Я пока не могу подтвердить цену автобуса."}
            result["component_errors"]["bus"] = error
            if not result["flights"] and not result["hotels"]:
                result["error"] = error
        for item in result["flights"] + result["hotels"]:
            item["fetched_at"] = now
            item["snapshot_id"] = fingerprint([item["provider_refs"]["reference_key"], item["price_minor"], now, "recorded_fixture"])
        return result

    async def refresh(self, components: list[dict], request: dict, now: str) -> dict:
        result = await self.search({**request, "products": list(dict.fromkeys(c["product_type"] for c in components))}, now)
        if result["error"]:
            return {"status": "unknown", "components": [], "warnings": [], "error": result["error"]}
        return match_refresh(components, result["flights"] + result["hotels"], [
            "Проверка воспроизводит архив; она не подтверждает нынешнюю цену или наличие."])
