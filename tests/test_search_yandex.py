"""Yandex API contract and failure behavior, without any external requests."""
from __future__ import annotations

import asyncio
import base64
import json
from html import escape

import httpx
import pytest

from travel_agent.adapters.search_yandex import ENDPOINT, MAX_RESPONSE_BYTES, YandexSearchAdapter

NOW = "2026-10-27T12:00:00+03:00"


def api_response(xml: str, status=200):
    return httpx.Response(status, json={"rawData": base64.b64encode(xml.encode()).decode()})


def xml_documents(*documents: str):
    return '<yandexsearch version="1.0"><response><results><grouping>' + ''.join('<group>' + doc + '</group>' for doc in documents) + '</grouping></results></response></yandexsearch>'


def doc(url: str, title="Концерт", body="Дата и место выступления"):
    return f'<doc><url>{escape(url)}</url><title>{title}</title><passages><passage>{body}</passage></passages></doc>'


def adapter(handler, **kwargs):
    return YandexSearchAdapter(api_key="test-api-key", folder_id="test-folder", transport=httpx.MockTransport(handler), **kwargs)


async def test_repeated_query_reuses_success_without_mutating_evidence_or_fetch_time(monkeypatch):
    calls = []
    clock = [100.0]
    monkeypatch.setattr("travel_agent.adapters.search_yandex.monotonic", lambda: clock[0])
    def handle(request):
        calls.append(request)
        return api_response(xml_documents(doc("https://example.org/tour")))
    provider = adapter(handle)
    first = await provider.search("Концерты Aurora", NOW)
    first["results"][0]["title"] = "Untrusted caller mutation"
    cached = await provider.search("концерты Aurora", "2026-10-27T12:01:00+03:00")
    assert len(calls) == 1 and cached["results"][0]["title"] == "Концерт"
    assert cached["fetched_at"] == NOW
    clock[0] += 301
    refreshed = await provider.search("Концерты Aurora", "2026-10-27T12:06:00+03:00")
    assert len(calls) == 2 and refreshed["fetched_at"] == "2026-10-27T12:06:00+03:00"


@pytest.mark.parametrize("header,expected", [("2", 2), ("3600", 3600), ("99999", None), ("private-diagnostic", None)])
async def test_rate_limit_metadata_is_bounded_and_rejection_is_not_cached(header, expected):
    calls = []
    def handle(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": header}, json={"message": "private-account-id"})
        return api_response(xml_documents(doc("https://example.org/tour")))
    provider = adapter(handle)
    first = await provider.search("Aurora концерты", NOW)
    assert first["error"]["diagnostics"] == {"http_status": 429, "retry_after_seconds": expected, "limit_type": "unspecified"}
    assert "private" not in json.dumps(first)
    assert (await provider.search("Aurora концерты", NOW))["error"] is None
    assert len(calls) == 2


async def test_missing_credentials_never_sends_request_and_health_hides_values(monkeypatch):
    monkeypatch.delenv("YANDEX_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("YANDEX_FOLDER_ID", raising=False)
    calls = []
    provider = YandexSearchAdapter(transport=httpx.MockTransport(lambda r: calls.append(r)))
    assert provider.configured is False
    result = await provider.search("афиша концертов", NOW)
    assert result["error"]["code"] == "search_unconfigured"
    assert result["results"] == [] and calls == []
    configured = YandexSearchAdapter(api_key="secret-key", folder_id="secret-folder")
    assert configured.configured is True
    health = configured.health()
    assert health["network_checked"] is False
    assert "secret" not in json.dumps(health)


async def test_request_uses_documented_v2_contract_and_parses_xml():
    calls = []
    xml = xml_documents('<doc><url>https://example.org/events?city=1&amp;year=2026</url><title>Концерт <hlword>Моргенштерна</hlword></title><headline>Официальная афиша</headline><passages><passage>Ближайшие <hlword>выступления</hlword>.</passage><passage>Официальная афиша</passage></passages></doc>')

    def handle(request):
        calls.append(request)
        assert str(request.url) == ENDPOINT
        assert request.method == "POST"
        assert request.headers["Authorization"] == "Api-Key test-api-key"
        body = json.loads(request.content)
        assert body["query"] == {"searchType": "SEARCH_TYPE_RU", "queryText": "концерт Моргенштерна", "familyMode": "FAMILY_MODE_MODERATE", "page": "0"}
        assert body["folderId"] == "test-folder"
        assert body["responseFormat"] == "FORMAT_XML"
        assert body["groupSpec"] == {"groupMode": "GROUP_MODE_FLAT", "groupsOnPage": "5", "docsInGroup": "1"}
        return api_response(xml)

    result = await adapter(handle).search("концерт Моргенштерна", NOW)
    assert result["error"] is None and result["provider"] == "yandex"
    assert result["fetched_at"] == NOW and len(calls) == 1
    assert result["results"] == [{"id": result["results"][0]["id"], "title": "Концерт Моргенштерна", "url": "https://example.org/events?city=1&year=2026", "snippet": "Официальная афиша Ближайшие выступления."}]
    assert result["results"][0]["id"].startswith("yandex-")


async def test_results_are_deduplicated_limited_and_html_is_plain_text():
    xml = xml_documents(doc("https://example.org/0#first", "&lt;b&gt;Афиша&lt;/b&gt;", "&lt;script&gt;alert(1)&lt;/script&gt; Даты"), doc("https://example.org/0#duplicate"), *(doc(f"https://example.org/{i}") for i in range(1, 9)))
    result = await adapter(lambda request: api_response(xml), max_results=100).search("концерт", NOW)
    assert result["error"] is None and len(result["results"]) == 5
    assert result["results"][0]["title"] == "Афиша"
    assert result["results"][0]["snippet"] == "Даты"
    assert all("#" not in item["url"] for item in result["results"])


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "file:///etc/passwd", "https://user:secret@example.org/", "http://localhost/",
    "https://api.localhost/", "http://127.0.0.1/", "http://10.2.3.4/", "https://192.168.1.1/",
    "http://169.254.169.254/", "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://2130706433/",
    "http://127.1/", "http://0x7f.0.0.1/", "https://service.internal/", "https://host.local/",
    "https://example.org:99999/", "https://example.org\\@127.0.0.1/", "http://%31%32%37.0.0.1/",
])
async def test_unsafe_links_are_omitted_without_fetching(url):
    calls = []
    def handle(request):
        calls.append(request)
        return api_response(xml_documents(doc(url), doc("https://example.org/safe")))
    result = await adapter(handle).search("концерт", NOW)
    assert result["error"] is None
    assert [item["url"] for item in result["results"]] == ["https://example.org/safe"]
    assert len(calls) == 1


@pytest.mark.parametrize("query", ["", "   ", "a" * 401, "a" * 1001, "bad\x00query", None])
async def test_invalid_queries_do_not_reach_network(query):
    calls = []
    result = await adapter(lambda request: calls.append(request)).search(query, NOW)
    assert result["error"]["code"] == "search_invalid_query"
    assert calls == []


@pytest.mark.parametrize("status,code,retryable", [(401, "search_auth_error", False), (403, "search_auth_error", False), (429, "search_rate_limited", True), (504, "search_timeout", True), (503, "search_unavailable", True), (400, "search_invalid_query", False), (302, "search_unavailable", False)])
async def test_http_errors_are_safe_and_redirects_are_not_followed(status, code, retryable):
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://other-provider.example/"}, json={"message": "secret-key secret-folder raw upstream details"})
    result = await adapter(handle).search("концерт", NOW)
    assert result["error"]["code"] == code and result["error"]["retryable"] is retryable
    assert "secret" not in json.dumps(result, ensure_ascii=False)
    assert len(calls) == 1


@pytest.mark.parametrize("code,expected", [("42", "search_auth_error"), ("32", "search_rate_limited"), ("55", "search_rate_limited"), ("1", "search_invalid_query"), ("20", "search_unavailable")])
async def test_http_200_xml_errors_are_not_reported_as_success(code, expected):
    xml = f'<yandexsearch><response><error code="{code}">secret upstream content</error></response></yandexsearch>'
    result = await adapter(lambda request: api_response(xml)).search("концерт", NOW)
    assert result["error"]["code"] == expected and result["results"] == []
    assert "secret" not in json.dumps(result)


async def test_xml_no_results_code_is_successful_empty_search():
    xml = '<yandexsearch><response><error code="15">Ничего не найдено</error></response></yandexsearch>'
    result = await adapter(lambda request: api_response(xml)).search("концерт", NOW)
    assert result["error"] is None and result["results"] == []


@pytest.mark.parametrize("payload", [{}, {"rawData": 12}, {"rawData": "%%%"}, {"response": {"rawData": "x"}}, [1, 2]])
async def test_invalid_json_contract_fails_safely(payload):
    result = await adapter(lambda request: httpx.Response(200, json=payload)).search("концерт", NOW)
    assert result["error"]["code"] == "search_invalid_response"


@pytest.mark.parametrize("xml", [
    '<!DOCTYPE yandexsearch [<!ENTITY secret SYSTEM "file:///etc/passwd">]><yandexsearch><response>&secret;</response></yandexsearch>',
    '<!DOCTYPE yandexsearch [<!ENTITY a "boom">]><yandexsearch><response>&a;</response></yandexsearch>',
    '<html><body>Wrong format</body></html>', '<yandexsearch><response>', '<yandexsearch/>',
])
async def test_unsafe_or_invalid_xml_is_rejected(xml):
    result = await adapter(lambda request: api_response(xml)).search("концерт", NOW)
    assert result["error"]["code"] == "search_invalid_response"


async def test_response_size_is_bounded_while_streaming():
    class Oversized(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(3):
                yield b"x" * (MAX_RESPONSE_BYTES // 2)
    result = await adapter(lambda request: httpx.Response(200, stream=Oversized())).search("концерт", NOW)
    assert result["error"]["code"] == "search_response_too_large"


async def test_timeout_and_network_failures_do_not_escape_or_leak_messages():
    async def slow(request):
        await asyncio.sleep(1)
        return api_response(xml_documents())
    result = await adapter(slow, timeout_seconds=0.001).search("концерт", NOW)
    assert result["error"]["code"] == "search_timeout"
    def broken(request):
        raise httpx.ConnectError("secret connection metadata", request=request)
    result = await adapter(broken).search("концерт", NOW)
    assert result["error"]["code"] == "search_unavailable"
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize("pasted", ["test-api-key", "\"test-api-key\"", "'test-api-key'", "Api-Key test-api-key", "Authorization: Api-Key test-api-key", "'Authorization: Api-Key test-api-key'", '"authorization: api-key test-api-key"'])
async def test_copied_authorization_prefix_is_normalized_locally(pasted):
    calls = []
    def handle(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Api-Key test-api-key"
        return api_response(xml_documents())
    provider = YandexSearchAdapter(api_key=pasted, folder_id="test-folder", transport=httpx.MockTransport(handle))
    assert provider.configured
    assert (await provider.search("концерт", NOW))["error"] is None
    assert len(calls) == 1


@pytest.mark.parametrize("pasted", ["Bearer unknown-secret", "Authorization: Bearer unknown-secret", "Api-Key test\r\nX-Extra: injected", "не-ASCII-ключ"])
async def test_unsupported_or_invalid_auth_format_is_not_sent(pasted):
    calls = []
    provider = YandexSearchAdapter(api_key=pasted, folder_id="test-folder", transport=httpx.MockTransport(lambda request: calls.append(request)))
    assert provider.configured is False
    result = await provider.search("концерт", NOW)
    assert result["error"]["code"] == "search_unconfigured" and calls == []
    assert pasted not in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize("status,payload,reason,message_hint", [
    (401, {"code": 16, "message": "API key expired secret-key"}, "unauthorized", "не принял API-ключ"),
    (403, {"code": 7, "message": "Permission denied request secret-id"}, "permission_denied", "Точная причина"),
    (403, {"code": 7, "message": "API key has no required scope yc.search-api.execute secret-key"}, "key_scope", "yc.search-api.execute"),
    (403, {"error": {"message": "Missing required role search-api.webSearch.user for secret-account"}}, "folder_or_role", "search-api.webSearch.user"),
    (403, {"message": "Permission denied for folder secret-folder"}, "folder_or_role", "каталога"),
    (403, {"message": "Billing account is inactive secret-account"}, "billing", "платежного аккаунта"),
    (403, {"message": "Insufficient funds on account secret-account"}, "billing", "платежного аккаунта"),
    (403, {"code": "UNAUTHENTICATED", "message": "secret-key"}, "unauthorized", "не принял API-ключ"),
])
async def test_auth_diagnostics_are_specific_only_when_explicit_and_never_echo_upstream(status, payload, reason, message_hint):
    result = await adapter(lambda request: httpx.Response(status, json=payload)).search("концерт", NOW)
    error = result["error"]
    assert error["code"] == "search_auth_error" and error["retryable"] is False
    assert error["diagnostics"] == {"http_status": status, "reason": reason}
    assert message_hint in error["message"]
    assert "secret" not in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize("payload", [
    {"message": "Permission denied. Billing documentation and folder settings may help."},
    {"message": "Permission denied", "echoed_query": "API key has no required scope yc.search-api.execute"},
    {"message": "See scope yc.search-api.execute and role search-api.webSearch.user documentation."},
    {"code": "UNKNOWN_CODE", "details": [{"message": "Billing account is inactive"}]},
])
async def test_ambiguous_auth_response_does_not_guess_a_cause(payload):
    result = await adapter(lambda request: httpx.Response(403, json=payload)).search("концерт", NOW)
    assert result["error"]["diagnostics"] == {"http_status": 403, "reason": "permission_denied"}


async def test_auth_error_body_read_is_small_and_unknown_format_stays_generic():
    from travel_agent.adapters.search_yandex import MAX_AUTH_ERROR_BYTES
    consumed = []
    class OversizedAuth(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(100):
                consumed.append(4096)
                yield b"x" * 4096
    provider = adapter(lambda request: httpx.Response(403, headers={"Content-Type": "application/json"}, stream=OversizedAuth()))
    result = await provider.search("концерт", NOW)
    assert sum(consumed) <= MAX_AUTH_ERROR_BYTES
    assert result["error"]["diagnostics"] == {"http_status": 403, "reason": "permission_denied"}
    non_json = adapter(lambda request: httpx.Response(401, text="API key secret-key failed. Billing account is inactive."))
    result = await non_json.search("концерт", NOW)
    assert result["error"]["diagnostics"] == {"http_status": 401, "reason": "unauthorized"}
    assert "secret" not in json.dumps(result)


async def test_malformed_auth_json_preserves_known_status():
    result = await adapter(lambda request: httpx.Response(403, headers={"Content-Type": "application/json"}, content=b"{not-json secret-id")).search("концерт", NOW)
    assert result["error"]["code"] == "search_auth_error"
    assert result["error"]["diagnostics"] == {"http_status": 403, "reason": "permission_denied"}
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize("xml_code,reason", [("31", "folder_or_role"), ("42", "permission_denied")])
async def test_xml_auth_codes_keep_http_status_and_do_not_claim_bad_key_without_proof(xml_code, reason):
    xml = f'<yandexsearch><response><error code="{xml_code}">secret upstream diagnostic</error></response></yandexsearch>'
    result = await adapter(lambda request: api_response(xml)).search("концерт", NOW)
    assert result["error"]["diagnostics"] == {"http_status": 200, "reason": reason}
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize("message", [
    "Permission denied. See yc.search-api.execute documentation.",
    "Permission denied. See search-api.webSearch.user documentation.",
])
async def test_generic_denial_plus_documentation_is_not_evidence_of_missing_scope_or_role(message):
    result = await adapter(lambda request: httpx.Response(403, json={"code": 7, "message": message})).search("концерт", NOW)
    assert result["error"]["diagnostics"] == {"http_status": 403, "reason": "permission_denied"}


@pytest.mark.parametrize("status,reason", [(401, "unauthorized"), (403, "permission_denied")])
async def test_received_auth_status_survives_overall_timeout_while_reading_body(status, reason):
    class LateAuthBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(1)
            yield b'{"message":"secret diagnostic"}'
    result = await adapter(lambda request: httpx.Response(status, headers={"Content-Type": "application/json"}, stream=LateAuthBody()), timeout_seconds=0.01).search("концерт", NOW)
    assert result["error"]["code"] == "search_auth_error"
    assert result["error"]["diagnostics"] == {"http_status": status, "reason": reason}
    assert result["error"]["retryable"] is False
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize("message,reason,hint", [
    ("The apikey has expired: secret-key", "key_expired", "срок действия API-ключа истёк"),
    ("UNAUTHENTICATED: The apikey has expired secret-key", "key_expired", "срок действия API-ключа истёк"),
    ("Unknown api key 'secret-key'", "key_unknown", "значение ключа или его область действия"),
    ("UNAUTHENTICATED: Unknown api key 'secret-key'", "key_unknown", "значение ключа или его область действия"),
    ("API key has no required scope yc.search-api.execute secret-key", "key_scope", "yc.search-api.execute"),
    ("Missing scope yc.search-api.execute for secret-key", "key_scope", "yc.search-api.execute"),
])
@pytest.mark.parametrize("nested", [False, True])
async def test_401_known_key_errors_are_actionable_without_echoing_provider_values(message, reason, hint, nested):
    payload = {"code": 16, "message": message, "requestId": "secret-request-id"}
    if nested:
        payload = {"error": payload}
    result = await adapter(lambda request: httpx.Response(401, json=payload)).search("концерт", NOW)
    error = result["error"]
    assert error["code"] == "search_auth_error" and error["retryable"] is False
    assert error["diagnostics"] == {"http_status": 401, "reason": reason}
    assert hint in error["message"]
    assert "secret" not in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize("payload", [
    {"message": "Authentication failed. See Unknown api key documentation."},
    {"message": "Authentication failed. See The apikey has expired documentation."},
    {"message": "Authentication failed. See yc.search-api.execute documentation."},
    {"message": "Authentication failed", "details": [{"message": "The apikey has expired secret-key"}]},
    {"message": "Authentication failed", "echoed_query": "Unknown api key secret-key"},
])
async def test_401_unknown_diagnostic_stays_generic_and_mentions_scope_and_expiry(payload):
    result = await adapter(lambda request: httpx.Response(401, json=payload)).search("концерт", NOW)
    error = result["error"]
    assert error["diagnostics"] == {"http_status": 401, "reason": "unauthorized"}
    assert "срок действия" in error["message"] and "yc.search-api.execute" in error["message"]
    assert "secret" not in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize("message,reason", [
    ("rpc error: code = Unauthenticated desc = Unknown api key 'test****alid (A5470FC8)'", "key_unknown"),
    ("rpc error: code = Unauthenticated desc = API key not found or invalid", "key_unknown"),
    ("API key not found or invalid", "key_unknown"),
    ("Authentication failed. API key not found or invalid documentation may help.", "unauthorized"),
    ("rpc error: code = Unauthenticated desc = The apikey has expired 'test****alid (A5470FC8)'", "key_expired"),
    ("RPC ERROR: CODE = UNAUTHENTICATED DESC = Unknown api key 'test****alid (A5470FC8)'", "key_unknown"),
    ("Authentication failed. rpc error: code = Unauthenticated desc = Unknown api key 'test****alid (A5470FC8)'", "unauthorized"),
])
async def test_401_canonical_rpc_prefix_is_allowlisted_without_echoing_key_fragments(message, reason):
    payload = {"code": 16, "message": message, "details": []}
    result = await adapter(lambda request: httpx.Response(401, json=payload)).search("концерт", NOW)
    error = result["error"]
    assert error["diagnostics"] == {"http_status": 401, "reason": reason}
    assert error["code"] == "search_auth_error" and error["retryable"] is False
    serialized = json.dumps(result, ensure_ascii=False)
    assert "test****alid" not in serialized and "A5470FC8" not in serialized
