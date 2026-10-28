"""Bounded, read-only Streamable HTTP client for the inspected Tutu MCP API.

The adapter never exposes provider checkout references, never invokes booking or
passenger-registration tools and never substitutes replay data after a failure.
"""
from __future__ import annotations

import json
from itertools import product
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from jsonschema import Draft202012Validator, FormatChecker

from .normalize import normalize_flight, normalize_hotel, normalize_rail, normalize_bus, match_refresh, search_key, money_minor, fingerprint

ALLOWLIST = frozenset({"search_avia", "search_rail", "search_bus", "get_bus_instructions", "search_hotels", "get_offer_details", "get_avia_instructions", "get_rail_instructions", "get_hotels_instructions"})
REQUIRED_FIELDS = {
    "search_bus": {"origin", "destination", "departure_date", "adults", "children", "page", "page_size", "view", "direct_only"},
    "search_avia": {"origin", "destination", "departure_date", "return_date", "adults", "children", "infants", "page", "page_size"},
    "search_hotels": {"city_name", "check_in", "check_out", "adults", "children_ages", "page", "page_size"},
    "search_rail": {"origin", "destination", "departure_date", "passengers", "page", "page_size", "view", "direct_only"},
    "get_offer_details": {"product_type", "hotel_id", "check_in", "check_out", "adults", "children_ages", "view"},
}


class AdapterError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        self.code, self.message, self.retryable = code, message, retryable
        super().__init__(message)

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "retryable": self.retryable, "provider": "tutu"}


def hotel_request_for_flights(request: dict, flights: list[dict]) -> dict:
    """Use an actual local arrival date for the bounded hotel search.

    Different arrival dates are not mixed into the same package: the domain
    matcher accepts only flights matching the fetched hotel's stay dates.
    """
    dates = []
    for flight in flights:
        try:
            departure, arrival, returning = (datetime.fromisoformat(flight[k]) for k in
                ("departure_at", "arrival_at", "return_departure_at"))
            if (departure.tzinfo and arrival.tzinfo and returning.tzinfo
                    and departure < arrival < returning
                    and departure.date().isoformat() == str(request["departure_date"])
                    and returning.date().isoformat() == str(request["return_date"])
                    and arrival.date() < returning.date()):
                dates.append(arrival.date().isoformat())
        except (KeyError, TypeError, ValueError):
            continue
    return {**request, "departure_date": min(dates)} if dates else dict(request)


class TutuAdapter:
    def __init__(self, url: str = "https://mcp.tutu.ru/mcp", timeout_seconds: float = 20,
                 max_pages: int = 2, max_calls: int = 20, *, transport=None):
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("TUTU_MCP_URL must be an HTTPS URL without embedded credentials")
        self.url, self.timeout_seconds = url, timeout_seconds
        self.max_pages, self.max_calls = min(max_pages, 3), min(max_calls, 20)
        self._transport, self._schemas, self._session_id = transport, {}, None
        self._request_id = 0

    async def _rpc(self, method: str, params: dict) -> dict | str:
        self._request_id += 1
        request_id = self._request_id
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": "2025-03-26"}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self._transport, follow_redirects=False) as client:
                response = await client.post(self.url, headers=headers, json={"jsonrpc": "2.0", "id": request_id,
                    "method": method, "params": params})
            if response.status_code == 429:
                raise AdapterError("rate_limited", "Туту ограничил частоту запросов.", retryable=True)
            response.raise_for_status()
            if response.headers.get("Mcp-Session-Id"):
                self._session_id = response.headers["Mcp-Session-Id"]
            try:
                payload = response.json()
            except ValueError:
                messages = []
                for event in response.text.replace("\r\n", "\n").split("\n\n"):
                    data = "\n".join(line[5:].strip() for line in event.splitlines() if line.startswith("data:"))
                    if data:
                        try:
                            messages.append(json.loads(data))
                        except ValueError:
                            continue
                payload = next((m for m in messages if isinstance(m, dict) and m.get("id") == request_id), None)
                if payload is None:
                    raise AdapterError("schema_changed", "Туту вернул ответ в неподдерживаемом формате.")
            if not isinstance(payload, dict):
                raise AdapterError("schema_changed", "Неверный формат JSON-RPC ответа Туту.")
            if payload.get("error"):
                # Raw server text can contain opaque provider IDs/links; do not expose it.
                raise AdapterError("unavailable", "Туту не выполнил запрос чтения.", retryable=True)
            result = payload.get("result")
            if not isinstance(result, dict):
                raise AdapterError("schema_changed", "В ответе Туту отсутствует объект result.")
            if result.get("isError"):
                raise AdapterError("unavailable", "Поставщик сообщил ошибку поиска или деталей.", retryable=True)
            if "structuredContent" in result:
                return result["structuredContent"]
            if "tools" in result:
                return result
            blocks = result.get("content", [])
            text_blocks = [block["text"] for block in blocks if block.get("type") == "text"]
            if len(text_blocks) == 1:
                try:
                    return json.loads(text_blocks[0])
                except ValueError:
                    return text_blocks[0]
            return result
        except httpx.TimeoutException as exc:
            raise AdapterError("timeout", "Туту не ответил за отведённое время.", retryable=True) from exc
        except httpx.HTTPStatusError as exc:
            raise AdapterError("unavailable", f"Туту недоступен (HTTP {exc.response.status_code}).", retryable=True) from exc
        except httpx.RequestError as exc:
            raise AdapterError("unavailable", "Не удалось подключиться к Туту.", retryable=True) from exc

    async def capabilities(self) -> dict:
        if not self._schemas:
            result = await self._rpc("tools/list", {})
            if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
                raise AdapterError("schema_changed", "Не удалось прочитать каталог инструментов Туту.")
            schemas = {x.get("name"): x.get("inputSchema") for x in result["tools"] if x.get("name") in ALLOWLIST}
            self._schemas = schemas
        return {"provider": "tutu", "mode": "live", "products": [p for p, name in (("avia", "search_avia"), ("rail", "search_rail"), ("bus", "search_bus"), ("hotel", "search_hotels")) if name in self._schemas],
                "tools": sorted(self._schemas), "checkout": "demo_only", "max_pages": self.max_pages}

    async def _call(self, name: str, arguments: dict, budget: list[int]):
        if name not in ALLOWLIST:
            raise AdapterError("unsupported", "Инструмент не разрешён политикой чтения.")
        if name not in self._schemas:
            raise AdapterError("unsupported", "Разрешённая возможность отсутствует у поставщика.")
        schema = self._schemas[name]
        if not isinstance(schema, dict) or not REQUIRED_FIELDS.get(name, set()) <= set(schema.get("properties", {})):
            raise AdapterError("schema_changed", f"Изменился контракт разрешённого инструмента {name}.")
        try:
            Draft202012Validator.check_schema(schema)
        except Exception as exc:
            raise AdapterError("schema_changed", "Схема инструмента поставщика некорректна.") from exc
        if budget[0] >= self.max_calls:
            raise AdapterError("call_limit", "Достигнут лимит обращений к поставщику.", retryable=True)
        errors = list(Draft202012Validator(self._schemas[name], format_checker=FormatChecker()).iter_errors(arguments))
        if errors:
            raise AdapterError("invalid_arguments", f"Аргументы {name} не соответствуют проверенной схеме.")
        budget[0] += 1
        return await self._rpc("tools/call", {"name": name, "arguments": arguments})

    async def search(self, request: dict, now: str) -> dict:
        result = {"flights": [], "trains": [], "buses": [], "hotels": [], "component_errors": {}, "coverage": [], "warnings": [], "error": None}
        budget = [0]
        products = request.get("products", ["avia", "hotel"])
        try:
            if not isinstance(products, list) or not products or any(p not in ("avia", "rail", "bus", "hotel") for p in products):
                raise AdapterError("invalid_arguments", "Укажите поддерживаемые компоненты поездки.")
            await self.capabilities()
            query = search_key(request)
            if not all(query[k] for k in ("destination", "departure_date", "return_date", "adults")):
                raise AdapterError("invalid_arguments", "Для поиска нужны маршрут, даты и состав путешественников.")
            if type(query["adults"]) is not int or not 1 <= query["adults"] <= 9:
                raise AdapterError("invalid_arguments", "Неверное количество взрослых.")
            ages = query["children_ages"]
            if any(type(age) is not int or age < 0 or age > 17 for age in ages):
                raise AdapterError("invalid_arguments", "Возраст ребёнка должен быть целым числом от 0 до 17.")
        except AdapterError as exc:
            result["error"] = exc.as_dict()
            return result
        # Each product is isolated; one dependency failure must not erase others.
        for kind in (p for p in ("avia", "rail", "bus", "hotel") if p in products):
            try:
                if kind != "hotel" and not query["origin"]:
                    raise AdapterError("invalid_arguments", "Для поиска транспорта нужен город отправления.")
                instructions = "get_" + ("hotels" if kind == "hotel" else kind) + "_instructions"
                if instructions in self._schemas:
                    await self._call(instructions, {}, budget)
                if kind == "avia":
                    args = {"origin": query["origin"], "destination": query["destination"],
                        "departure_date": query["departure_date"], "return_date": query["return_date"],
                        "adults": query["adults"] + sum(age >= 12 for age in ages),
                        "children": sum(2 <= age <= 11 for age in ages), "infants": sum(age < 2 for age in ages),
                        "page_size": 10, "view": "full"}
                    if request.get("_flight_numbers"):
                        args["flight_numbers"] = request["_flight_numbers"]
                    if (request.get("constraints") or {}).get("direct") is True:
                        args["direct_only"] = True
                    items = await self._pages("avia", "search_avia", args, "offers", query, budget, result)
                    for index, offer in enumerate(items):
                        for variant_index, variant in enumerate(offer.get("variants") or []):
                            result["flights"].append(normalize_flight(offer, variant, request, now,
                                source=f"tutu:search_avia/offers/{index}/variants/{variant_index}"))
                elif kind == "rail":
                    result["trains"] = await self._rail(request, now, budget, result)
                elif kind == "bus":
                    result["buses"] = await self._bus(request, now, budget, result)
                else:
                    hotel_request = hotel_request_for_flights(request, result["flights"] + result["trains"] + result["buses"])
                    args = {"city_name": query["destination"], "check_in": hotel_request["departure_date"],
                        "check_out": query["return_date"], "adults": query["adults"], "children_ages": ages, "page_size": 10}
                    if request.get("_hotel_ids"):
                        if not isinstance(request["_hotel_ids"], list) or any(not isinstance(h, str) or not h for h in request["_hotel_ids"]):
                            raise AdapterError("invalid_arguments", "Некорректный идентификатор выбранного отеля.")
                        hotel_ids = list(dict.fromkeys(request["_hotel_ids"]))
                        result["coverage"].append({"provider": "tutu", "product": "hotel", "query": search_key(hotel_request),
                            "pages": [], "count": len(hotel_ids), "complete": True, "stop_reason": "selected_hotels"})
                    else:
                        items = await self._pages("hotel", "search_hotels", args, "hotels", search_key(hotel_request), budget, result)
                        hotel_ids = list(dict.fromkeys(str(h.get("hotel_id") or h.get("hotel_geo_id") or "") for h in items))
                        hotel_ids = [h for h in hotel_ids if h]
                    coverage = result["coverage"][-1]
                    coverage["detailed_hotels"] = 0
                    for hotel_id in hotel_ids[:3]:
                        try:
                            result["hotels"].extend(await self._hotel_details(hotel_id, hotel_request, now, budget))
                            coverage["detailed_hotels"] += 1
                        except (AdapterError, ValueError, TypeError, KeyError) as exc:
                            result["component_errors"]["hotel"] = self._error_dict(exc)
                            coverage.update(complete=False, stop_reason="detail_error")
                    if len(hotel_ids) > 3:
                        result["warnings"].append("Подробные тарифы проверены только у первых трёх отелей выдачи.")
                        coverage.update(complete=False, stop_reason="detail_limit")
            except (AdapterError, ValueError, TypeError, KeyError) as exc:
                result["component_errors"][kind] = self._error_dict(exc)
        for name in ("flights", "trains", "buses", "hotels"):
            result[name] = list({x["provider_refs"]["reference_key"]: x for x in result[name]}.values())
        if result["component_errors"] and not any(result[k] for k in ("flights", "trains", "buses", "hotels")):
            result["error"] = next(iter(result["component_errors"].values()))
        return result

    @staticmethod
    def _error_dict(exc):
        return exc.as_dict() if isinstance(exc, AdapterError) else {
            "code": "schema_changed", "retryable": False, "provider": "tutu",
            "message": "Не удалось безопасно нормализовать данные поставщика."}

    async def _pages(self, kind, name, args, output_key, query, budget, result, direction=None):
        items = []
        coverage = {"provider": "tutu", "product": kind, "query": query, "pages": [],
                    "count": 0, "total": None, "total_exact": False, "complete": False, "stop_reason": "error"}
        if direction:
            coverage["direction"] = direction
        result["coverage"].append(coverage)
        for page in range(1, self.max_pages + 1):
            try:
                raw = await self._call(name, {**args, "page": page}, budget)
                if not isinstance(raw, dict) or not isinstance(raw.get(output_key), list):
                    raise AdapterError("schema_changed", f"Изменился формат ответа {name}.")
                meta = raw.get("meta") or {}
                if kind == "rail" and (meta.get("pricing") or {}).get("basis") != "per_seat":
                    raise AdapterError("schema_changed", "Не подтверждена стоимость железнодорожного тарифа за одно место.")
                if kind == "bus" and (meta.get("pricing") or {}).get("basis") != "party_total":
                    raise AdapterError("schema_changed", "Не подтверждена стоимость автобусного тарифа для всех пассажиров.")
                items.extend(raw[output_key])
                coverage["pages"].append(page)
                more = bool(meta.get("has_more"))
                coverage.update(count=len(items), total=meta.get("total_matched", meta.get("total")),
                    total_exact=meta.get("total_matched_exact", False), complete=not more,
                    stop_reason="page_limit" if more else "end_of_results")
                if not more:
                    break
            except AdapterError as exc:
                if not items:
                    raise
                result["component_errors"][kind] = exc.as_dict()
                coverage.update(complete=False, stop_reason="error")
                break
        if not coverage["complete"]:
            result["warnings"].append(f"{kind}: проверена только часть выдачи поставщика.")
        return items

    async def _hotel_details(self, hotel_id, request, now, budget):
        query = search_key(request)
        detail = await self._call("get_offer_details", {"product_type": "hotels", "hotel_id": hotel_id,
            "check_in": query["departure_date"], "check_out": query["return_date"],
            "adults": query["adults"], "children_ages": query["children_ages"], "view": "rules", "review_limit": 0}, budget)
        if not isinstance(detail, dict) or not isinstance(detail.get("hotel"), dict) or not isinstance(detail.get("rooms"), list):
            raise AdapterError("schema_changed", "Детали отеля не содержат конкретные номера и тарифы.")
        actual_id = str(detail["hotel"].get("hotel_id") or detail["hotel"].get("hotel_geo_id") or "")
        if actual_id != str(hotel_id):
            raise AdapterError("schema_changed", "Поставщик вернул подробности другого отеля.")
        return [normalize_hotel(detail, room, rate, request, now,
                source=f"tutu:get_offer_details/{hotel_id}/rooms/{ri}/rates/{ti}")
                for ri, room in enumerate(detail["rooms"]) for ti, rate in enumerate(room.get("rates") or [])]

    async def _rail(self, request, now, budget, result):
        query = search_key(request)
        if query["children_ages"] or query["adults"] > 6:
            raise AdapterError("unsupported_party", "Поиск поездов пока поддерживает только 1–6 взрослых: детские тарифы поставщик не рассчитывает.")
        sides = []
        unverified = 0
        for direction in ("outbound", "inbound"):
            outward = direction == "outbound"
            args = {"origin": query["origin" if outward else "destination"],
                "destination": query["destination" if outward else "origin"],
                "departure_date": query["departure_date" if outward else "return_date"],
                "passengers": query["adults"], "page_size": 10, "view": "full", "direct_only": True}
            target = (request.get("_rail_refs") or {}).get(direction) or {}
            if target.get("train_numbers"):
                args["train_numbers"] = target["train_numbers"]
            category = (request.get("constraints") or {}).get("seat_category")
            if category:
                args["seat_categories"] = [category]
            offers = await self._pages("rail", "search_rail", args, "offers", args, budget, result, direction)
            fares = {}
            for offer in offers:
                for fare in offer.get("variants") or []:
                    if type(fare.get("seats_left")) is not int:
                        unverified += 1
                        continue
                    if fare["seats_left"] < query["adults"]:
                        continue
                    # Same class/conditions may occur in several cars sharing inventory.
                    identity = fingerprint([offer.get("departure_at"), offer.get("arrival_at"), offer.get("legs"),
                        fare.get("service_class"), fare.get("seat_category"), fare.get("conditions")])
                    if identity not in fares or money_minor(fare["price"]) < money_minor(fares[identity][1]["price"]):
                        fares[identity] = (offer, fare)
            ranked = sorted(fares.values(), key=lambda pair: money_minor(pair[1]["price"]))
            if not target and len(ranked) > 40:
                result["coverage"][-1].update(complete=False, stop_reason="fare_limit")
                result["warnings"].append("Поезда: сравниваются первые 40 подходящих тарифов в каждую сторону.")
                ranked = ranked[:40]
            sides.append(ranked)
        components = []
        for (outward, ofare), (inward, ifare) in product(*sides):
            try:
                component = normalize_rail(outward, ofare, inward, ifare, request, now)
            except (ValueError, TypeError, KeyError):
                unverified += 1
                continue
            refs = request.get("_rail_refs") or {}
            if any(refs.get(d, {}).get("reference_key") and refs[d]["reference_key"] != component["provider_refs"][d]["reference_key"] for d in ("outbound", "inbound")):
                continue
            components.append(component)
        if unverified:
            result["warnings"].append("Часть железнодорожных тарифов не показана: состав, время или наличие мест не удалось подтвердить.")
            for coverage in result["coverage"]:
                if coverage["product"] == "rail":
                    coverage.update(complete=False, stop_reason="unverified_fares")
            if not components:
                result["component_errors"]["rail"] = AdapterError("unverified_fares", "Не удалось подтвердить подходящие железнодорожные тарифы для всего состава.", retryable=True).as_dict()
        result["warnings"].append("Поезда: проверены прямые поездки туда и обратно; конкретные и соседние места не проверены.")
        components.sort(key=lambda c: c["price_minor"])
        if not request.get("_rail_refs") and len(components) > 100:
            result["warnings"].append("Поезда: сохранены первые 100 сочетаний тарифов по суммарной стоимости.")
            for coverage in result["coverage"]:
                if coverage["product"] == "rail":
                    coverage.update(complete=False, combinations_limited=True)
            components = components[:100]
        return components

    async def _bus(self, request, now, budget, result):
        query = search_key(request)
        if query["children_ages"] or query["adults"] > 8:
            raise AdapterError("unsupported_party", "Я пока подбираю автобусы для 1–8 взрослых. Детский тариф зависит от возрастных правил перевозчика, которые я не смог подтвердить.")
        sides = []
        for direction in ("outbound", "inbound"):
            outward = direction == "outbound"
            args = {"origin": query["origin" if outward else "destination"],
                "destination": query["destination" if outward else "origin"],
                "departure_date": query["departure_date" if outward else "return_date"],
                "adults": query["adults"], "children": 0, "page_size": 10, "view": "full", "direct_only": True}
            offers = await self._pages("bus", "search_bus", args, "offers", args, budget, result, direction)
            fares = []
            for offer in offers:
                for fare in offer.get("variants") or [{"price": offer.get("price")}]:
                    try:
                        money_minor(fare["price"])
                    except (ValueError, TypeError, KeyError, AttributeError):
                        continue
                    fares.append((offer, fare))
            fares.sort(key=lambda pair: money_minor(pair[1]["price"]))
            if len(fares) > 40:
                result["coverage"][-1].update(complete=False, stop_reason="fare_limit")
            sides.append(fares[:40])
        components, unverified = [], 0
        refs = request.get("_bus_refs") or {}
        for (outward, ofare), (inward, ifare) in product(*sides):
            try:
                component = normalize_bus(outward, ofare, inward, ifare, request, now)
            except (ValueError, TypeError, KeyError):
                unverified += 1
                continue
            if any(refs.get(d, {}).get("reference_key") and refs[d]["reference_key"] != component["provider_refs"][d]["reference_key"] for d in ("outbound", "inbound")):
                continue
            components.append(component)
        if unverified:
            for row in result["coverage"]:
                if row["product"] == "bus":
                    row.update(complete=False, stop_reason="unverified_fares")
            result["warnings"].append("Я не включил автобусные рейсы с неподтверждённым расписанием или остановками.")
        components.sort(key=lambda c: c["price_minor"])
        if len(components) > 100:
            for row in result["coverage"]:
                if row["product"] == "bus":
                    row.update(complete=False, combinations_limited=True)
            components = components[:100]
        return components

    async def refresh(self, components: list[dict], request: dict, now: str) -> dict:
        # Re-search transport; hotel details are fetched by the selected hotel's ID,
        # not by its current position in the destination's search results.
        flight_numbers = sorted({n for x in components if x.get("product_type") == "avia"
                                 for n in x.get("provider_refs", {}).get("flight_numbers", [])})
        transports = list(dict.fromkeys(c["product_type"] for c in components if c["product_type"] in ("avia", "rail", "bus")))
        rail = next((c for c in components if c["product_type"] == "rail"), {})
        result = await self.search({**request, "products": transports, "_flight_numbers": flight_numbers,
                                   "_rail_refs": rail.get("provider_refs", {}),
                                   "_bus_refs": next((c.get("provider_refs", {}) for c in components if c["product_type"] == "bus"), {})}, now) if transports else {"flights": [], "trains": [], "warnings": [], "error": None}
        candidates = result["flights"] + result["trains"] + result.get("buses", [])
        try:
            await self.capabilities()
            budget = [0]
            for hotel in (c for c in components if c["product_type"] == "hotel"):
                hotel_id = hotel.get("provider_refs", {}).get("hotel_id")
                if not hotel_id:
                    raise AdapterError("stale_offer", "Не сохранился идентификатор выбранного отеля.", retryable=True)
                candidates.extend(await self._hotel_details(hotel_id, {**request, **hotel["query"]}, now, budget))
        except (AdapterError, ValueError, TypeError, KeyError) as exc:
            return {"status": "unknown", "components": [], "warnings": result["warnings"], "error": self._error_dict(exc)}
        matched = match_refresh(components, candidates, result["warnings"])
        if matched["status"] == "unknown" and result["error"]:
            matched["error"] = result["error"]
        return matched
