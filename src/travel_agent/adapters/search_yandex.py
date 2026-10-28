"""Bounded Yandex Search API v2 client; search results are untrusted evidence.

REST contract: https://aistudio.yandex.ru/ru/docs/search-api/api-ref/WebSearch/search
Only the configured Yandex endpoint is contacted. Result pages are never fetched.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import ipaddress
import json
import os
import re
from copy import deepcopy
from time import monotonic
from html.parser import HTMLParser
from urllib.parse import urldefrag, urlsplit
from xml.etree import ElementTree as ET

import httpx

ENDPOINT = "https://searchapi.api.cloud.yandex.net/v2/web/search"
# The official v2 REST schema limits queryText to 400 characters.
MAX_QUERY_CHARS = 400
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_RESULTS = 5
MAX_AUTH_ERROR_BYTES = 16 * 1024


class _SearchError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False, *, diagnostics: dict | None = None):
        self.code, self.message, self.retryable = code, message, retryable
        self.diagnostics = diagnostics
        super().__init__(message)

    def as_dict(self) -> dict:
        result = {"code": self.code, "message": self.message, "retryable": self.retryable}
        if self.diagnostics is not None:
            result["diagnostics"] = self.diagnostics
        return result


def _invalid_response() -> _SearchError:
    return _SearchError("search_invalid_response", "Яндекс вернул неподдерживаемый ответ поиска.")


def _normalize_api_key(value: str) -> str:
    """Accept a copied Authorization header, never infer a different auth method."""
    value = value.strip()
    candidate = value[1:-1] if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'} else value
    match = re.fullmatch(r"(?:Authorization:[ \t]*)?Api-Key[ \t]+([^\r\n]*)", candidate, re.IGNORECASE)
    return match[1].strip() if match else candidate


def _auth_error(http_status: int, reason: str = "permission_denied") -> _SearchError:
    messages = {
        "unauthorized": "Яндекс не принял API-ключ. Проверьте его значение, срок действия и область yc.search-api.execute. Точная причина в ответе не указана.",
        "key_expired": "Яндекс сообщил, что срок действия API-ключа истёк. Создайте новый ключ с областью yc.search-api.execute и обновите подключение.",
        "key_unknown": "Яндекс не распознал API-ключ. Причиной может быть значение ключа или его область действия. Проверьте действующий ключ и наличие области yc.search-api.execute.",
        "permission_denied": "Яндекс запретил доступ к поиску. Проверьте каталог, роль сервисного аккаунта search-api.webSearch.user и область действия ключа yc.search-api.execute. Точная причина в ответе не указана.",
        "key_scope": "Ответ Яндекса указывает на ограничение области действия API-ключа. Для поиска нужна область yc.search-api.execute; создайте или выберите ключ с этим разрешением.",
        "folder_or_role": "Ответ Яндекса указывает на отсутствие доступа к каталогу или нужной роли. Проверьте идентификатор каталога и роль search-api.webSearch.user у сервисного аккаунта в этом каталоге.",
        "billing": "Ответ Яндекса указывает на ограничение биллинга. Проверьте состояние платежного аккаунта и подключение оплаты для используемого каталога.",
    }
    if reason not in messages:
        reason = "permission_denied"
    return _SearchError("search_auth_error", messages[reason], diagnostics={"http_status": http_status, "reason": reason})


def _auth_reason(http_status: int, payload: object) -> str:
    """Classify only known error fields; raw messages/IDs are never returned."""
    default = "unauthorized" if http_status == 401 else "permission_denied"
    if not isinstance(payload, dict):
        return default
    error = payload.get("error") if isinstance(payload.get("error"), dict) else payload
    message = error.get("message") if isinstance(error.get("message"), str) else ""
    code = error.get("code")
    unauthenticated = http_status == 401 or code in (16, "16", "UNAUTHENTICATED")
    # Only explicit causes refine the status. Documentation references and
    # arbitrary fields are not evidence. Known key errors must lead the message.
    text = message.casefold().strip()
    if unauthenticated and re.match(r"(?:(?:unauthenticated:|rpc error: code = unauthenticated desc =)\s*)?the apikey has expired\b", text):
        return "key_expired"
    if re.search(r"(?:insufficient|missing|invalid) (?:(?:required|api[- ]key) )?scopes?\b|(?:api[- ]key|key)[^.!?;\n]{0,60}(?:has no|does not have|lacks)[^.!?;\n]{0,30}scope\b|yc\.search-api\.execute (?:is )?(?:missing|not granted|not allowed)\b", text):
        return "key_scope"
    if unauthenticated:
        if re.match(r"(?:(?:unauthenticated:|rpc error: code = unauthenticated desc =)\s*)?(?:unknown api key|api key not found or invalid)\b", text):
            return "key_unknown"
        return "unauthorized"
    if re.search(r"billing (?:account|service).{0,100}(?:blocked|disabled|inactive|not active|not enabled|suspended)|insufficient funds|плат[её]жн.{0,30}аккаунт.{0,80}(?:заблок|неактив|отключ)", text):
        return "billing"
    if re.search(r"(?:missing|insufficient) (?:required )?role search-api\.websearch\.user\b|search-api\.websearch\.user (?:role )?(?:is )?(?:missing|not granted)\b|(?:permission|access) denied\s+(?:for|to|on|in)\s+(?:the\s+)?folder|(?:no|missing|insufficient)[^.!?;\n]{0,30}(?:role|permissions?)[^.!?;\n]{0,60}folder|folder[^.!?;\n]{0,80}(?:not found|does not exist|access denied)|нет доступа.{0,30}каталог", text):
        return "folder_or_role"
    return "permission_denied"


async def _read_auth_diagnostic(response: httpx.Response) -> _SearchError:
    """Read at most 16 KiB of JSON solely to produce an allowlisted diagnosis."""
    status = response.status_code
    default = _auth_error(status, "unauthorized" if status == 401 else "permission_denied")
    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type != "application/json" and not content_type.endswith("+json"):
        return default
    try:
        declared_size = int(response.headers.get("content-length", "0"))
        if declared_size > MAX_AUTH_ERROR_BYTES:
            return default
    except ValueError:
        return default
    body = bytearray()
    try:
        async with asyncio.timeout(1.0):
            async for chunk in response.aiter_bytes(chunk_size=4096):
                if len(body) + len(chunk) >= MAX_AUTH_ERROR_BYTES:
                    return default
                body.extend(chunk)
        payload = json.loads(body)
    except (ValueError, UnicodeError, httpx.RequestError, TimeoutError):
        return default
    return _auth_error(status, _auth_reason(status, payload))


class _TextOnly(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() in {"script", "style"}:
            self.hidden += 1
        elif not self.hidden:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag.lower() in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _clean_text(element: ET.Element | None, limit: int) -> str:
    if element is None:
        return ""
    parser = _TextOnly()
    parser.feed("".join(element.itertext()))
    parser.close()
    text = re.sub(r"[\x00-\x1f\x7f]", " ", "".join(parser.parts))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _local_tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1] if isinstance(element.tag, str) else ""


def _first(element: ET.Element, tag: str) -> ET.Element | None:
    return next((child for child in element.iter() if _local_tag(child) == tag), None)


def _public_http_url(value: str) -> str | None:
    """Validate link syntax and literal hosts without resolving or fetching them."""
    value = value.strip()
    if not value or len(value) > 4096 or re.search(r"[\s\x00-\x1f\x7f\\]", value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            return None
        _ = parsed.port  # Reject malformed or out-of-range ports.
        host = parsed.hostname.rstrip(".").lower()
        if "%" in host or host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home", ".localdomain")):
            return None
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            # Browsers may interpret legacy decimal/octal/hex IPv4 spellings.
            if "." not in host or re.fullmatch(r"(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*", host):
                return None
            ascii_host = host.encode("idna").decode("ascii")
            if len(ascii_host) > 253 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) for part in ascii_host.split(".")):
                return None
        else:
            if not address.is_global or address.is_multicast:
                return None
        return urldefrag(value)[0]
    except (ValueError, UnicodeError):
        return None


def _parse_xml(raw: bytes, limit: int) -> list[dict]:
    try:
        text = raw.decode("utf-8-sig")
        if "\x00" in text or re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.IGNORECASE):
            raise _invalid_response()
        root = ET.fromstring(text)
    except (UnicodeError, ET.ParseError):
        raise _invalid_response() from None
    if _local_tag(root) != "yandexsearch":
        raise _invalid_response()
    response = _first(root, "response")
    if response is None:
        raise _invalid_response()
    error = _first(response, "error")
    if error is not None:
        code = error.get("code", "")
        # Official XML error 15 means an empty, successful search result.
        if code == "15":
            return []
        if code in {"31", "33", "42", "44", "48"}:
            # XML 42 covers both authentication and authorization, so it does
            # not by itself prove an invalid key. Code 31 identifies a folder issue.
            raise _auth_error(200, "folder_or_role" if code == "31" else "permission_denied")
        if code in {"32", "55"}:
            raise _SearchError("search_rate_limited", "Достигнут лимит запросов Яндекс Поиска. Повторите позже.", True)
        if code in {"1", "2", "18", "19", "37", "10002"}:
            raise _SearchError("search_invalid_query", "Яндекс не принял поисковый запрос. Уточните его формулировку.")
        raise _SearchError("search_unavailable", "Яндекс не выполнил поиск.", code != "100")
    results, seen = [], set()
    for document in response.iter():
        if _local_tag(document) != "doc":
            continue
        url_element = _first(document, "url")
        url = _public_http_url("".join(url_element.itertext()) if url_element is not None else "")
        if not url or url in seen:
            continue
        seen.add(url)
        title = _clean_text(_first(document, "title"), 240) or urlsplit(url).hostname or "Источник"
        snippets = []
        headline = _clean_text(_first(document, "headline"), 1000)
        if headline:
            snippets.append(headline)
        for passage in document.iter():
            if _local_tag(passage) == "passage":
                value = _clean_text(passage, 1000)
                if value and value not in snippets:
                    snippets.append(value)
        results.append({"id": "yandex-" + hashlib.sha256(url.encode()).hexdigest()[:16],
                        "title": title, "url": url, "snippet": " ".join(snippets)[:1200]})
        if len(results) >= limit:
            break
    return results


class YandexSearchAdapter:
    provider = "yandex"

    def __init__(self, api_key: str | None = None, folder_id: str | None = None,
                 timeout_seconds: float = 15, max_results: int = 5, *, transport=None):
        self._api_key = _normalize_api_key(api_key if api_key is not None else os.getenv("YANDEX_SEARCH_API_KEY", ""))
        self._folder_id = (folder_id if folder_id is not None else os.getenv("YANDEX_FOLDER_ID", "")).strip()
        self.timeout_seconds = max(0.001, min(float(timeout_seconds), 15.0))
        self.max_results = max(1, min(int(max_results), MAX_RESULTS))
        self._transport = transport
        self._successful_queries = {}

    @property
    def configured(self) -> bool:
        return bool(self._api_key and self._folder_id and len(self._api_key) <= 4096
                    and len(self._folder_id) <= 50 and self._api_key.isascii() and self._folder_id.isascii()
                    and not re.search(r"[\s\x00-\x1f\x7f]", self._api_key + self._folder_id))

    def health(self) -> dict:
        """Configuration metadata only; this does not verify account access."""
        return {"provider": self.provider, "configured": self.configured,
                "credential_present": bool(self._api_key), "folder_present": bool(self._folder_id),
                "max_query_chars": MAX_QUERY_CHARS, "max_results": self.max_results,
                "timeout_seconds": self.timeout_seconds, "network_checked": False}

    async def search(self, query: str, now: str) -> dict:
        result = {"provider": self.provider, "query": query[:1000] if isinstance(query, str) else "",
                  "results": [], "fetched_at": now, "error": None}
        received_auth_status = None
        try:
            if not self.configured:
                raise _SearchError("search_unconfigured", "Поиск Яндекса не подключен: задайте YANDEX_SEARCH_API_KEY и YANDEX_FOLDER_ID.")
            if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", query):
                raise _SearchError("search_invalid_query", f"Поисковый запрос должен содержать от 1 до {MAX_QUERY_CHARS} символов.")
            query = query.strip()
            result["query"] = query
            cached = self._successful_queries.get(query.casefold())
            if cached and monotonic() - cached[0] < 300:
                return deepcopy(cached[1])
            request = {
                "query": {"searchType": "SEARCH_TYPE_RU", "queryText": query,
                          "familyMode": "FAMILY_MODE_MODERATE", "page": "0"},
                "groupSpec": {"groupMode": "GROUP_MODE_FLAT", "groupsOnPage": str(self.max_results), "docsInGroup": "1"},
                "maxPassages": "3", "folderId": self._folder_id, "responseFormat": "FORMAT_XML",
            }
            headers = {"Authorization": "Api-Key " + self._api_key, "Content-Type": "application/json", "Accept": "application/json"}
            async with asyncio.timeout(self.timeout_seconds):
                async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self._transport, follow_redirects=False) as client:
                    async with client.stream("POST", ENDPOINT, headers=headers, json=request) as response:
                        if response.status_code in {401, 403}:
                            received_auth_status = response.status_code
                            raise await _read_auth_diagnostic(response)
                        if response.status_code == 429:
                            delay = response.headers.get("retry-after", "")
                            seconds = int(delay) if delay.isdigit() and int(delay) <= 3600 else None
                            raise _SearchError("search_rate_limited", "Достигнут лимит запросов Яндекс Поиска. Повторите позже.", True,
                                               diagnostics={"http_status": 429, "retry_after_seconds": seconds, "limit_type": "unspecified"})
                        if response.status_code in {408, 504}:
                            raise _SearchError("search_timeout", "Яндекс не ответил за отведенное время.", True)
                        if response.status_code == 400:
                            raise _SearchError("search_invalid_query", "Яндекс не принял параметры поискового запроса.")
                        if response.status_code != 200:
                            raise _SearchError("search_unavailable", "Яндекс Поиск временно недоступен.", response.status_code >= 500)
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                                raise _SearchError("search_response_too_large", "Ответ Яндекса превысил допустимый размер.")
                            body.extend(chunk)
            try:
                payload = json.loads(body)
                if not isinstance(payload, dict) or not isinstance(payload.get("rawData"), str):
                    raise _invalid_response()
                raw = base64.b64decode(payload["rawData"], validate=True)
            except (ValueError, UnicodeError, binascii.Error):
                raise _invalid_response() from None
            result["results"] = _parse_xml(raw, self.max_results)
            if len(self._successful_queries) >= 32:
                self._successful_queries.pop(next(iter(self._successful_queries)))
            self._successful_queries[query.casefold()] = (monotonic(), deepcopy(result))
        except _SearchError as error:
            result["error"] = error.as_dict()
        except (httpx.TimeoutException, TimeoutError):
            if received_auth_status is not None:
                # The known HTTP rejection remains authoritative when its optional
                # diagnostic body outlives the overall request deadline.
                reason = "unauthorized" if received_auth_status == 401 else "permission_denied"
                result["error"] = _auth_error(received_auth_status, reason).as_dict()
            else:
                result["error"] = _SearchError("search_timeout", "Яндекс не ответил за отведенное время.", True).as_dict()
        except httpx.RequestError:
            result["error"] = _SearchError("search_unavailable", "Не удалось подключиться к Яндекс Поиску.", True).as_dict()
        return result
