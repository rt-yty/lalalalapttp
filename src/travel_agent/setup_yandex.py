"""Interactive local credential setup. Secrets never enter argv, reports or logs."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import getpass
from io import StringIO
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import warnings
from zoneinfo import ZoneInfo

from dotenv.parser import parse_stream
import httpx

from .adapters.search_yandex import YandexSearchAdapter, _normalize_api_key

ENV_NAMES = ("YANDEX_SEARCH_API_KEY", "YANDEX_FOLDER_ID")
LOCAL_APP = "http://127.0.0.1:8765"
CHECK_QUERY = "Yandex Cloud Search API"
SAFE_REASONS = {"unauthorized", "permission_denied", "key_scope", "folder_or_role", "billing", "key_expired", "key_unknown"}
SAFE_CODES = {"search_auth_error", "search_unconfigured", "search_invalid_query", "search_rate_limited", "search_timeout", "search_unavailable", "search_invalid_response", "search_response_too_large"}


def normalize_credentials(api_key: str, folder_id: str) -> tuple[str, str]:
    key, folder = _normalize_api_key(api_key), folder_id.strip()
    if not YandexSearchAdapter(key, folder).configured or not re.fullmatch(r"[A-Za-z0-9_-]{1,50}", folder):
        raise ValueError("Проверьте формат: нужен полный секретный API-ключ и ID каталога Yandex Cloud.")
    # Literal dotenv values must not interpolate variables or inject new lines.
    if re.search(r"[\s\x00-\x1f\x7f\"'$\\]", key):
        raise ValueError("В ключе есть посторонние символы. Скопируйте секретный ключ целиком из окна его создания.")
    if key == folder:
        raise ValueError("Ключ и ID каталога должны быть разными значениями.")
    return key, folder


def _atomic_private_write(path: Path, text: str) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("Файл настроек должен быть обычным файлом, а не ссылкой или папкой.")
    fd, temporary = tempfile.mkstemp(prefix=".yandex-setup-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def save_credentials(root: Path, api_key: str, folder_id: str) -> Path:
    key, folder = normalize_credentials(api_key, folder_id)
    path = root / ".env"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("Файл .env должен быть обычным файлом; настройки не изменены.")
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    bindings = list(parse_stream(StringIO(original)))
    if any(binding.error for binding in bindings):
        raise ValueError("В существующем .env есть ошибка формата. Настройки не изменены.")
    kept = "".join(binding.original.string for binding in bindings if binding.key not in ENV_NAMES)
    if kept and not kept.endswith("\n"):
        kept += "\n"
    # Inputs are validated to exclude quoting/interpolation characters above.
    content = kept + f"YANDEX_SEARCH_API_KEY='{key}'\nYANDEX_FOLDER_ID='{folder}'\n"
    _atomic_private_write(path, content)
    return path


def load_credentials(root: Path) -> tuple[str, str]:
    path = root / ".env"
    if path.is_symlink() or not path.is_file():
        raise ValueError("Сначала запустите «Настроить Яндекс.command» и введите настройки.")
    bindings = list(parse_stream(StringIO(path.read_text(encoding="utf-8"))))
    if any(binding.error for binding in bindings):
        raise ValueError("В файле .env есть ошибка формата.")
    values = {binding.key: binding.value for binding in bindings if binding.key in ENV_NAMES}
    return normalize_credentials(values.get(ENV_NAMES[0]) or "", values.get(ENV_NAMES[1]) or "")


async def apply_to_running_app(api_key: str, folder_id: str, *, transport=None) -> dict:
    """Only the known loopback app receives secrets; redirects are disabled."""
    try:
        async with httpx.AsyncClient(timeout=5, trust_env=False, follow_redirects=False, transport=transport) as client:
            response = await client.get(LOCAL_APP + "/api/bootstrap")
            if response.status_code != 200:
                return {"status": "unrecognized"}
            bootstrap = response.json()
            if not isinstance(bootstrap, dict) or bootstrap.get("app") != "ostrovok-travel-agent":
                return {"status": "unrecognized"}
            if bootstrap.get("auth_required") and not bootstrap.get("user"):
                # The operator already saved .env locally. Do not bypass the
                # account boundary or send secrets to an anonymous API call.
                return {"status": "auth_required"}
            token = bootstrap.get("csrf_token")
            if not isinstance(token, str) or not token or len(token) > 200:
                return {"status": "unrecognized"}
            response = await client.post(LOCAL_APP + "/api/search/config",
                headers={"X-Travel-Token": token}, json={"api_key": api_key, "folder_id": folder_id})
            if response.status_code == 401:
                return {"status": "auth_required"}
            accepted = response.status_code == 200 and response.json().get("configured") is True
            return {"status": "applied" if accepted else "rejected"}
    except httpx.ConnectError:
        return {"status": "not_running"}
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return {"status": "unavailable"}


async def check_connection(root: Path, api_key: str, folder_id: str, *, search_adapter=None, local_transport=None) -> dict:
    key, folder = normalize_credentials(api_key, folder_id)
    applied = await apply_to_running_app(key, folder, transport=local_transport)
    adapter = search_adapter if search_adapter is not None else YandexSearchAdapter(key, folder)
    now = datetime.now(ZoneInfo("Europe/Moscow")).isoformat()
    try:
        result = await adapter.search(CHECK_QUERY, now)
        error = result.get("error")
        diagnostics = error.get("diagnostics") or {} if isinstance(error, dict) else {}
        code = error.get("code") if isinstance(error, dict) else None
        reason = diagnostics.get("reason")
        http_status = diagnostics.get("http_status")
        report = {"checked_at": now, "provider": "yandex", "ok": error is None,
                  "local_app": applied, "result_count": len(result.get("results") or []),
                  "error_code": code if code in SAFE_CODES else ("search_unavailable" if error else None),
                  "reason": reason if reason in SAFE_REASONS else None,
                  "http_status": http_status if type(http_status) is int and 100 <= http_status <= 599 else None}
    except Exception:
        # An exception can contain a URL/header. Do not echo it or its traceback.
        report = {"checked_at": now, "provider": "yandex", "ok": False, "local_app": applied,
                  "result_count": 0, "error_code": "search_unavailable", "reason": None, "http_status": None}
    directory = root / "data"
    directory.mkdir(mode=0o700, exist_ok=True)
    _atomic_private_write(directory / "yandex-connection-check.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def print_report(report: dict) -> None:
    if report["local_app"]["status"] == "applied":
        print("Настройки применены к открытому прототипу без перезапуска.")
    elif report["local_app"]["status"] == "not_running":
        print("Прототип сейчас не запущен. При следующем запуске он прочитает сохранённые настройки.")
    elif report["local_app"]["status"] == "auth_required":
        print("Приложение требует входа в аккаунт. Настройки сохранены в .env; перезапустите приложение, чтобы применить их.")
    else:
        print("Настройки сохранены в .env, но передать их открытому приложению не удалось. Перезапустите приложение, чтобы применить их.")
    if report["ok"]:
        print("Яндекс выполнил проверочный поиск. Доступ работает.")
    else:
        print("Настройки сохранены, но проверка поиска не прошла.")
        if report["http_status"]:
            print("HTTP:", report["http_status"])
        explanations = {
            "key_expired": "Яндекс сообщает, что срок API-ключа истёк.",
            "key_unknown": "Яндекс не распознал ключ. Проверьте его полное значение и область действия yc.search-api.execute.",
            "key_scope": "Яндекс указывает на ограничение области действия ключа: нужна yc.search-api.execute.",
            "folder_or_role": "Яндекс указывает на каталог или роль сервисного аккаунта search-api.webSearch.user.",
            "billing": "Яндекс указывает на ограничение платёжного аккаунта.",
            "unauthorized": "Авторизация отклонена. Точная причина не установлена: проверьте значение, срок и область действия ключа.",
            "permission_denied": "Яндекс запретил доступ. Проверьте каталог и права сервисного аккаунта.",
        }
        print(explanations.get(report["reason"], "Код проверки: " + str(report["error_code"])))
    print("Отчёт без ключа: data/yandex-connection-check.json")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Сохранить и проверить настройки поиска Яндекса")
    parser.add_argument("--check", action="store_true", help="повторить проверку уже сохранённых настроек")
    args = parser.parse_args(argv)
    root = Path.cwd()
    try:
        if args.check:
            key, folder = load_credentials(root)
        else:
            if not sys.stdin.isatty() or not sys.stderr.isatty():
                print("Откройте «Настроить Яндекс.command» в Терминале: ключ вводится скрыто.")
                return 2
            print("Настройка поиска Яндекса")
            print("Нужны секретный API-ключ и ID каталога. ID API-ключа здесь не нужен.")
            print("Ключ сохранится в локальном .env с доступом только для вашего пользователя.")
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                key = getpass.getpass("Секретный API-ключ (ввод скрыт): ")
            try:
                _, saved_folder = load_credentials(root)
            except (ValueError, OSError):
                saved_folder = ""
            folder_prompt = "ID каталога Yandex Cloud (Enter — оставить сохранённый): " if saved_folder else "ID каталога Yandex Cloud: "
            folder = input(folder_prompt).strip() or saved_folder
            key, folder = normalize_credentials(key, folder)
            save_credentials(root, key, folder)
            print("Настройки сохранены в .env.")
        print("Применяю настройки и выполняю один проверочный запрос к Яндексу…", flush=True)
        report = asyncio.run(check_connection(root, key, folder))
        print_report(report)
        return 0 if report["ok"] else 1
    except getpass.GetPassWarning:
        print("Этот терминал не поддерживает скрытый ввод. Откройте скрипт в приложении Терминал.")
        return 2
    except (KeyboardInterrupt, EOFError):
        print("\nНастройка остановлена.")
        return 130
    except ValueError as error:
        print(str(error))
        return 2
    except (OSError, RuntimeError):
        print("Не удалось завершить настройку. Проверьте доступ к папке проекта и повторите запуск.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
