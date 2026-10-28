"""Credential setup uses private temporary files and mocked HTTP only."""
from __future__ import annotations

import json
import stat
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from dotenv.parser import parse_stream
from io import StringIO

from travel_agent import setup_yandex
from travel_agent.setup_yandex import (
    CHECK_QUERY,
    LOCAL_APP,
    apply_to_running_app,
    check_connection,
    load_credentials,
    normalize_credentials,
    print_report,
    save_credentials,
)

KEY = "mock-secret-key-not-real"
FOLDER = "mock-folder-not-real"
TOKEN = "mock-csrf-token"


def private_mode(path):
    return stat.S_IMODE(Path(path).stat().st_mode)


def local_app_transport(calls=None):
    def handle(request):
        if calls is not None:
            calls.append(request)
        assert str(request.url).startswith(LOCAL_APP + "/")
        if request.url.path == "/api/bootstrap":
            assert request.method == "GET"
            assert not request.content
            return httpx.Response(200, json={"app": "ostrovok-travel-agent", "csrf_token": TOKEN})
        assert request.url.path == "/api/search/config"
        assert request.method == "POST"
        assert request.headers["X-Travel-Token"] == TOKEN
        assert json.loads(request.content) == {"api_key": KEY, "folder_id": FOLDER}
        return httpx.Response(200, json={"configured": True})
    return httpx.MockTransport(handle)


class FakeSearch:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, []

    async def search(self, query, now):
        self.calls.append((query, now))
        if self.error is not None:
            raise self.error
        return self.result


@pytest.mark.parametrize("pasted", [KEY, "  " + KEY + "  ", "Api-Key " + KEY, "Authorization: Api-Key " + KEY, "'Authorization: Api-Key " + KEY + "'"])
def test_normalize_credentials_preserves_secret_value_and_normalizes_supported_copy_formats(pasted):
    assert normalize_credentials(pasted, "  " + FOLDER + "  ") == (KEY, FOLDER)


@pytest.mark.parametrize("key,folder", [
    ("", FOLDER), ("Bearer " + KEY, FOLDER), (KEY + "\nINJECTED=value", FOLDER),
    ("${OTHER_SECRET}", FOLDER), (KEY + "'", FOLDER), (KEY, "folder/other"),
    (KEY, "folder\nINJECTED"), (KEY, "f" * 51), (KEY, KEY),
])
def test_invalid_credentials_do_not_echo_secret_values(key, folder):
    with pytest.raises(ValueError) as exc:
        normalize_credentials(key, folder)
    assert KEY not in str(exc.value)
    assert FOLDER not in str(exc.value)


def test_save_preserves_unrelated_multiline_values_and_removes_all_old_yandex_bindings(tmp_path):
    path = tmp_path / ".env"
    unrelated = '# Existing configuration\nOTHER_TOKEN="keep-this"\nMULTILINE="first line\nYANDEX_FOLDER_ID=inside-value\nlast line"\nEMPTY=\n'
    path.write_text(unrelated + "YANDEX_SEARCH_API_KEY='old-key-1'\nexport YANDEX_FOLDER_ID='old-folder-1'\nYANDEX_SEARCH_API_KEY=old-key-2\nYANDEX_FOLDER_ID=old-folder-2\nTAIL='untouched'", encoding="utf-8")
    path.chmod(0o644)

    assert save_credentials(tmp_path, "Api-Key " + KEY, FOLDER) == path
    written = path.read_text(encoding="utf-8")
    assert written.startswith(unrelated)
    assert "TAIL='untouched'\n" in written
    assert "old-key" not in written and "old-folder" not in written
    bindings = list(parse_stream(StringIO(written)))
    for name in ("YANDEX_SEARCH_API_KEY", "YANDEX_FOLDER_ID"):
        assert sum(binding.key == name for binding in bindings) == 1
    assert next(binding.value for binding in bindings if binding.key == "MULTILINE") == "first line\nYANDEX_FOLDER_ID=inside-value\nlast line"
    assert load_credentials(tmp_path) == (KEY, FOLDER)
    assert private_mode(path) == 0o600
    assert list(tmp_path.glob(".yandex-setup-*")) == []


def test_new_file_is_private_and_repeated_save_is_stable(tmp_path):
    path = save_credentials(tmp_path, KEY, FOLDER)
    first = path.read_bytes()
    save_credentials(tmp_path, KEY, FOLDER)
    assert path.read_bytes() == first
    assert private_mode(path) == 0o600
    assert load_credentials(tmp_path) == (KEY, FOLDER)


@pytest.mark.parametrize("original,invalid_key", [
    ("UNRELATED='unterminated\n", KEY),
    ("UNRELATED=preserve\n", "${OTHER_SECRET}"),
])
def test_bad_existing_format_or_bad_input_cannot_partially_overwrite_env(tmp_path, original, invalid_key):
    path = tmp_path / ".env"
    path.write_text(original, encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ValueError):
        save_credentials(tmp_path, invalid_key, FOLDER)
    assert path.read_bytes() == before
    assert list(tmp_path.glob(".yandex-setup-*")) == []


def test_atomic_replace_failure_preserves_old_env_and_removes_temporary_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("UNCHANGED=value\n", encoding="utf-8")
    before = path.read_bytes()

    def fail_replace(source, destination):
        assert private_mode(source) == 0o600
        raise OSError("mock disk failure")

    monkeypatch.setattr(setup_yandex.os, "replace", fail_replace)
    with pytest.raises(OSError):
        save_credentials(tmp_path, KEY, FOLDER)
    assert path.read_bytes() == before
    assert list(tmp_path.glob(".yandex-setup-*")) == []


@pytest.mark.parametrize("kind", ["symlink", "dangling_symlink", "directory"])
def test_load_and_save_reject_nonregular_env_paths_without_touching_target(tmp_path, kind):
    path, target = tmp_path / ".env", tmp_path / "target"
    if kind == "directory":
        path.mkdir()
    else:
        if kind == "symlink":
            target.write_text("ORIGINAL=do-not-touch\n", encoding="utf-8")
        path.symlink_to(target)
    for action in (lambda: load_credentials(tmp_path), lambda: save_credentials(tmp_path, KEY, FOLDER)):
        with pytest.raises(ValueError):
            action()
    if kind == "symlink":
        assert target.read_text() == "ORIGINAL=do-not-touch\n"
    elif kind == "dangling_symlink":
        assert not target.exists()
    assert list(tmp_path.glob(".yandex-setup-*")) == []


@pytest.mark.parametrize("content", [None, "OTHER=value\n", "YANDEX_SEARCH_API_KEY='unterminated\n", "YANDEX_SEARCH_API_KEY='${OTHER_KEY}'\nYANDEX_FOLDER_ID='folder'\n"])
def test_load_rejects_missing_incomplete_malformed_or_interpolated_credentials(tmp_path, content):
    if content is not None:
        (tmp_path / ".env").write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        load_credentials(tmp_path)


async def test_running_app_is_identified_before_credentials_are_posted_with_csrf():
    calls = []
    result = await apply_to_running_app(KEY, FOLDER, transport=local_app_transport(calls))
    assert result == {"status": "applied"}
    assert [(call.method, call.url.path) for call in calls] == [("GET", "/api/bootstrap"), ("POST", "/api/search/config")]
    assert KEY not in str(calls[0].url) and KEY not in str(calls[0].headers)


@pytest.mark.parametrize("advertised", [True, False])
async def test_mandatory_account_boundary_is_not_bypassed(advertised, capsys):
    calls=[]

    def handle(request):
        calls.append(request)
        if request.url.path=='/api/bootstrap':
            return httpx.Response(200,json={'app':'ostrovok-travel-agent','csrf_token':TOKEN,
                'user':None,'auth_required':advertised})
        return httpx.Response(401,json={'detail':'Войдите в аккаунт, чтобы продолжить.'})

    result=await apply_to_running_app(KEY,FOLDER,transport=httpx.MockTransport(handle))
    assert result=={'status':'auth_required'}
    assert len(calls)==(1 if advertised else 2)
    print_report({'local_app':result,'ok':True})
    output=capsys.readouterr().out
    assert 'перезапустите приложение' in output and KEY not in output


@pytest.mark.parametrize("bootstrap", [
    {}, {"app": "unrelated-service", "csrf_token": TOKEN},
    {"app": "ostrovok-travel-agent"},
    {"app": "ostrovok-travel-agent", "csrf_token": ""},
    {"app": "ostrovok-travel-agent", "csrf_token": 123},
    {"app": "ostrovok-travel-agent", "csrf_token": "x" * 201},
    ["ostrovok-travel-agent", TOKEN],
])
async def test_unrecognized_local_service_never_receives_credentials(bootstrap):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=bootstrap)

    result = await apply_to_running_app(KEY, FOLDER, transport=httpx.MockTransport(handle))
    assert result == {"status": "unrecognized"}
    assert len(calls) == 1 and calls[0].method == "GET" and calls[0].content == b""
    assert KEY not in str(calls[0].headers) and FOLDER not in str(calls[0].url)


@pytest.mark.parametrize("redirect_at", ["bootstrap", "configuration"])
async def test_redirects_are_never_followed_or_forwarded_with_credentials(redirect_at):
    calls = []

    def handle(request):
        calls.append(request)
        assert request.url.host == "127.0.0.1"
        if request.url.path == "/api/bootstrap" and redirect_at == "configuration":
            return httpx.Response(200, json={"app": "ostrovok-travel-agent", "csrf_token": TOKEN})
        return httpx.Response(307, headers={"Location": "https://unrelated.example/collect"})

    result = await apply_to_running_app(KEY, FOLDER, transport=httpx.MockTransport(handle))
    assert result == {"status": "unrecognized" if redirect_at == "bootstrap" else "rejected"}
    assert len(calls) == (1 if redirect_at == "bootstrap" else 2)
    assert all(call.url.host == "127.0.0.1" for call in calls)


async def test_local_connection_error_is_redacted():
    def handle(request):
        raise httpx.ConnectError("Unable to connect with " + KEY, request=request)

    assert await apply_to_running_app(KEY, FOLDER, transport=httpx.MockTransport(handle)) == {"status": "not_running"}


async def test_success_check_persists_only_allowlisted_private_report(tmp_path, capsys):
    adapter = FakeSearch({"error": None, "results": [{"title": KEY, "url": "https://example.org/" + FOLDER}, {"snippet": KEY}], "debug": KEY})
    calls = []
    report = await check_connection(tmp_path, KEY, FOLDER, search_adapter=adapter, local_transport=local_app_transport(calls))
    assert report["ok"] is True and report["result_count"] == 2
    assert report["local_app"] == {"status": "applied"}
    assert report["error_code"] is None and report["reason"] is None and report["http_status"] is None
    assert len(adapter.calls) == 1 and adapter.calls[0][0] == CHECK_QUERY
    assert datetime.fromisoformat(adapter.calls[0][1]).utcoffset() is not None
    path = tmp_path / "data" / "yandex-connection-check.json"
    assert json.loads(path.read_text()) == report
    assert private_mode(path) == 0o600 and private_mode(path.parent) == 0o700
    assert set(report) == {"checked_at", "provider", "ok", "local_app", "result_count", "error_code", "reason", "http_status"}
    assert not (tmp_path / ".env").exists()
    print_report(report)
    all_visible = path.read_text() + capsys.readouterr().out
    assert KEY not in all_visible and FOLDER not in all_visible and TOKEN not in all_visible


@pytest.mark.parametrize("code,reason,http_status,expected", [
    ("search_auth_error", "key_expired", 401, ("search_auth_error", "key_expired", 401)),
    (KEY, KEY, KEY, ("search_unavailable", None, None)),
    ("search_auth_error", "permission_denied", True, ("search_auth_error", "permission_denied", None)),
])
async def test_failure_report_keeps_only_known_codes_reasons_and_http_status(tmp_path, capsys, code, reason, http_status, expected):
    adapter = FakeSearch({"error": {"code": code, "message": "raw upstream " + KEY, "diagnostics": {"reason": reason, "http_status": http_status, "api_key": KEY, "folder": FOLDER}}, "results": [], "headers": {"Authorization": KEY}})
    report = await check_connection(tmp_path, KEY, FOLDER, search_adapter=adapter, local_transport=local_app_transport())
    assert report["ok"] is False
    assert (report["error_code"], report["reason"], report["http_status"]) == expected
    path = tmp_path / "data" / "yandex-connection-check.json"
    assert json.loads(path.read_text()) == report
    assert private_mode(path) == 0o600
    print_report(report)
    assert KEY not in path.read_text() + capsys.readouterr().out
    assert FOLDER not in path.read_text()


async def test_search_exception_with_secret_is_redacted_in_report_and_terminal(tmp_path, capsys):
    adapter = FakeSearch(error=RuntimeError("Authorization: Api-Key " + KEY + "; folder=" + FOLDER))
    report = await check_connection(tmp_path, KEY, FOLDER, search_adapter=adapter, local_transport=local_app_transport())
    assert report["ok"] is False and report["error_code"] == "search_unavailable"
    assert report["result_count"] == 0 and report["reason"] is None and report["http_status"] is None
    assert len(adapter.calls) == 1
    print_report(report)
    text = (tmp_path / "data" / "yandex-connection-check.json").read_text() + capsys.readouterr().out
    assert KEY not in text and FOLDER not in text and "Authorization" not in text


async def test_existing_report_symlink_is_not_followed_or_overwritten(tmp_path):
    directory = tmp_path / "data"
    directory.mkdir()
    target = tmp_path / "untouched.txt"
    target.write_text("untouched", encoding="utf-8")
    (directory / "yandex-connection-check.json").symlink_to(target)
    with pytest.raises(ValueError):
        await check_connection(tmp_path, KEY, FOLDER, search_adapter=FakeSearch({"error": None, "results": []}), local_transport=local_app_transport())
    assert target.read_text() == "untouched"
