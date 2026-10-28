"""Bounded public-page evidence, without scripts, subrequests or ambient secrets.

Callers must choose URLs from their search results, not arbitrary chat input.
DNS is resolved once per hop; the HTTP socket uses that validated literal IP.
The original Host and httpcore's sni_hostname extension retain TLS hostname
verification. A fresh client per hop prevents cookie forwarding and cross-host
connection reuse when two hostnames happen to share an IP.
"""
from __future__ import annotations

import asyncio
import codecs
import inspect
import ipaddress
import json
import re
import socket
import zlib
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

MAX_PAGE_BYTES = 1024 * 1024
MAX_PAGE_CHARS = 18000
MAX_REDIRECTS = 3
MAX_PAGE_SECONDS = 12.0
_REDIRECTS = {301, 302, 303, 307, 308}
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home", ".localdomain")
_TRANSITION_NETWORKS = tuple(ipaddress.ip_network(value) for value in
    ("64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32", "::ffff:0:0/96"))
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class _PageError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _public_ip(value: str) -> str:
    try:
        if not isinstance(value, str) or "%" in value:
            raise ValueError
        address = ipaddress.ip_address(value)
        if not address.is_global or address.is_multicast or address.is_reserved:
            raise ValueError
        if address.version == 6 and any(address in network for network in _TRANSITION_NETWORKS):
            raise ValueError
        return str(address)
    except ValueError:
        raise _PageError("page_blocked_address") from None


def _public_url(value: str) -> tuple[str, str, int]:
    if (not isinstance(value, str) or not value or len(value) > 4096
            or re.search(r"[\s\x00-\x1f\x7f\\]", value)):
        raise _PageError("page_invalid_url")
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or parsed.username is not None or parsed.password is not None:
            raise ValueError
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if port not in {80, 443} or parsed.port == 0:
            raise ValueError
        host = (parsed.hostname or "").rstrip(".").lower()
        if not host or "%" in host or host == "localhost" or host.endswith(_LOCAL_SUFFIXES):
            raise _PageError("page_blocked_address")
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            if "." not in host or re.fullmatch(r"(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*", host):
                raise _PageError("page_blocked_address")
            host = host.encode("idna").decode("ascii")
            if len(host) > 253 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in host.split(".")):
                raise ValueError
        else:
            host = _public_ip(str(literal))
        authority = f"[{host}]" if ":" in host else host
        default_port = 443 if parsed.scheme == "https" else 80
        if port != default_port:
            authority += f":{port}"
        normalized = urlunsplit((parsed.scheme, authority, parsed.path or "/", parsed.query, ""))
        # Validate URL syntax now; only this canonical spelling reaches httpx.
        httpx.URL(normalized)
        return normalized, host, port
    except (ValueError, UnicodeError, httpx.InvalidURL):
        raise _PageError("page_invalid_url") from None


async def _resolve(host: str) -> list[str]:
    records = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    return list(dict.fromkeys(record[4][0] for record in records))


def _plain(value: str, limit: int) -> str:
    value = _CONTROL.sub(" ", value)
    return "\n".join(line for raw in value.splitlines() if (line := re.sub(r"\s+", " ", raw).strip()))[:limit]


def _short(value, limit=300):
    if isinstance(value, str):
        return _plain(value, limit)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)[:80]
    return ""


def _location(value):
    if isinstance(value, str):
        return _short(value, 500)
    if isinstance(value, list):
        return [item for raw in value[:3] if (item := _location(raw))]
    if not isinstance(value, dict):
        return None
    result = {"name": _short(value.get("name"))}
    address = value.get("address")
    if isinstance(address, str):
        result["address"] = _short(address, 500)
    elif isinstance(address, dict):
        result["address"] = {key: text for key in ("streetAddress", "addressLocality", "addressRegion", "postalCode", "addressCountry")
                             if (text := _short(address.get(key)))}
    return {key: item for key, item in result.items() if item}


def _offers(value):
    if isinstance(value, list):
        return [item for raw in value[:3] if (item := _offers(raw))]
    if not isinstance(value, dict):
        return None
    return {key: text for key in ("price", "lowPrice", "highPrice", "priceCurrency", "availability", "validFrom")
            if (text := _short(value.get(key), 200))}


def _events(scripts: list[str]) -> str:
    events, seen = [], set()
    examined = 0
    for script in scripts:
        try:
            root = json.loads(script)
        except (ValueError, RecursionError):
            continue
        stack = [(root, 0)]
        while stack and examined < 2000 and len(events) < 10:
            item, depth = stack.pop()
            examined += 1
            if depth > 12:
                continue
            if isinstance(item, list):
                stack.extend((child, depth + 1) for child in item[:50])
            elif isinstance(item, dict):
                kinds = item.get("@type", [])
                if isinstance(kinds, str):
                    kinds = [kinds]
                if isinstance(kinds, list) and any(isinstance(kind, str) and (kind.rsplit("/", 1)[-1] == "Festival" or kind.rsplit("/", 1)[-1].endswith("Event")) for kind in kinds):
                    event = {key: text for key in ("name", "startDate", "endDate") if (text := _short(item.get(key)))}
                    for key, cleaner in (("location", _location), ("offers", _offers)):
                        if cleaned := cleaner(item.get(key)):
                            event[key] = cleaned
                    if event:
                        encoded = json.dumps(event, ensure_ascii=False, sort_keys=True)
                        if encoded not in seen:
                            events.append(encoded)
                            seen.add(encoded)
                stack.extend((child, depth + 1) for child in list(item.values())[:50] if isinstance(child, (dict, list)))
    return "\n".join(events)[:6000]


class _VisibleHTML(HTMLParser):
    _HIDDEN = {"head", "script", "style", "noscript", "template", "svg", "canvas", "iframe", "nav", "header", "footer", "form", "button", "aside"}
    _VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
    _BLOCK = {"p", "div", "section", "article", "main", "h1", "h2", "h3", "h4", "li", "tr", "br", "table", "dl", "dt", "dd"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.parts, self.scripts = [], []
        self.text_size = self.script_size = 0
        self.capture = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        hidden = bool(self.stack and self.stack[-1][1]) or tag in self._HIDDEN
        style = (attributes.get("style") or "").lower()
        hidden |= "hidden" in attributes or (attributes.get("aria-hidden") or "").lower() == "true"
        hidden |= bool(re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", style))
        hidden |= (attributes.get("role") or "").lower() in {"navigation", "banner", "contentinfo"}
        marker = (attributes.get("class") or "") + " " + (attributes.get("id") or "")
        hidden |= bool(re.search(r"\b(?:cookie|consent|navbar|menu)(?:\b|[-_])", marker, re.I))
        if tag == "script" and (attributes.get("type") or "").lower().split(";", 1)[0].strip() == "application/ld+json" and self.script_size < 65536 and len(self.scripts) < 8:
            self.capture = []
        if tag in self._BLOCK and not hidden:
            self.parts.append("\n")
        if tag not in self._VOID:
            if len(self.stack) >= 128:
                raise _PageError("page_invalid_content")
            self.stack.append((tag, hidden))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self._VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == "script" and self.capture is not None:
            self.scripts.append("".join(self.capture))
            self.capture = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.capture is not None:
            part = data[:max(0, 65536 - self.script_size)]
            self.capture.append(part)
            self.script_size += len(part)
        elif not (self.stack and self.stack[-1][1]) and self.text_size < MAX_PAGE_CHARS * 2:
            part = data[:MAX_PAGE_CHARS * 2 - self.text_size]
            self.parts.append(part)
            self.text_size += len(part)

    def text(self, limit: int) -> str:
        event_text = _events(self.scripts)
        visible = _plain("".join(self.parts), MAX_PAGE_CHARS)
        if event_text:
            return ("Данные событий (JSON-LD со страницы):\n" + event_text + "\nТекст страницы:\n" + visible)[:limit]
        return visible[:limit]


def _decode(body: bytes, content_type: str) -> str:
    match = re.search(r"charset\s*=\s*[\"']?([a-zA-Z0-9_-]+)", content_type, re.I)
    encoding = match[1] if match else "utf-8"
    try:
        normalized = codecs.lookup(encoding).name
    except LookupError:
        normalized = "utf-8"
    if normalized not in {"utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "iso8859-1", "cp1251", "cp1252", "ascii"}:
        normalized = "utf-8"
    return body.decode(normalized, errors="replace")


async def _read_body(response: httpx.Response, limit: int) -> bytes:
    try:
        declared = int(response.headers.get("content-length", "0"))
    except ValueError:
        raise _PageError("page_invalid_content") from None
    if declared < 0:
        raise _PageError("page_invalid_content")
    if declared > limit:
        raise _PageError("page_too_large")
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in {"", "identity", "gzip", "deflate"}:
        raise _PageError("page_unsupported_encoding")
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS) if encoding in {"gzip", "deflate"} else None
    wire_size = 0
    output = bytearray()
    try:
        async for chunk in response.aiter_raw(chunk_size=16384):
            wire_size += len(chunk)
            if wire_size > limit:
                raise _PageError("page_too_large")
            remaining = limit - len(output)
            decoded = decoder.decompress(chunk, remaining + 1) if decoder else chunk
            if len(decoded) > remaining or (decoder and decoder.unconsumed_tail):
                raise _PageError("page_too_large")
            output.extend(decoded)
        if decoder and (not decoder.eof or decoder.unused_data):
            raise _PageError("page_invalid_content")
    except zlib.error:
        raise _PageError("page_invalid_content") from None
    return bytes(output)


class WebPageReader:
    def __init__(self, *, transport=None, resolver=None, timeout_seconds: float = MAX_PAGE_SECONDS,
                 max_bytes: int = MAX_PAGE_BYTES, max_chars: int = MAX_PAGE_CHARS, max_redirects: int = MAX_REDIRECTS):
        self._transport = transport  # A test transport; default creates a fresh verified pool per hop.
        self._resolver = resolver or _resolve
        self.timeout_seconds = max(0.001, min(float(timeout_seconds), MAX_PAGE_SECONDS))
        self.max_bytes = max(1, min(int(max_bytes), MAX_PAGE_BYTES))
        self.max_chars = max(1, min(int(max_chars), MAX_PAGE_CHARS))
        self.max_redirects = max(0, min(int(max_redirects), MAX_REDIRECTS))

    async def _address(self, host: str) -> str:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            addresses = self._resolver(host)
            addresses = await addresses if inspect.isawaitable(addresses) else addresses
            if not isinstance(addresses, (list, tuple)) or not addresses or len(addresses) > 32:
                raise _PageError("page_dns_failed")
            checked = list(dict.fromkeys(_public_ip(address) for address in addresses))
            return min(checked, key=lambda value: ipaddress.ip_address(value).version)
        return _public_ip(host)

    async def read(self, url: str) -> dict:
        final_url = ""
        try:
            async with asyncio.timeout(self.timeout_seconds):
                for hop in range(self.max_redirects + 1):
                    current, host, port = _public_url(url)
                    address = await self._address(host)
                    final_url = current
                    parsed = httpx.URL(current)
                    pinned = parsed.copy_with(host=address)
                    host_header = parsed.netloc.decode("ascii")
                    headers = {"Host": host_header, "Accept": "text/html,application/xhtml+xml,text/plain",
                               "Accept-Encoding": "identity", "User-Agent": "TravelPrototype/1.0 (public-page reader)"}
                    async with httpx.AsyncClient(transport=self._transport, timeout=self.timeout_seconds,
                                                 trust_env=False, follow_redirects=False, verify=True) as client:
                        async with client.stream("GET", pinned, headers=headers, extensions={"sni_hostname": host}) as response:
                            if response.status_code in _REDIRECTS:
                                if hop >= self.max_redirects:
                                    raise _PageError("page_redirect_limit")
                                location = response.headers.get("location")
                                if not location:
                                    raise _PageError("page_invalid_url")
                                url = urljoin(current, location)
                                continue
                            if response.status_code != 200:
                                raise _PageError("page_http_error")
                            content_type = response.headers.get("content-type", "")
                            mime = content_type.split(";", 1)[0].strip().lower()
                            if mime not in {"text/html", "application/xhtml+xml", "text/plain"}:
                                raise _PageError("page_unsupported_type")
                            body = await _read_body(response, self.max_bytes)
                    decoded = _decode(body, content_type)
                    if mime == "text/plain":
                        text = _plain(decoded, self.max_chars)
                    else:
                        parser = _VisibleHTML()
                        parser.feed(decoded)
                        parser.close()
                        text = parser.text(self.max_chars)
                    if not text.strip():
                        raise _PageError("page_empty")
                    return {"status": "read", "text": text, "final_url": final_url}
                raise _PageError("page_redirect_limit")
        except _PageError as error:
            code = error.code
        except (TimeoutError, httpx.TimeoutException):
            code = "page_timeout"
        except (socket.gaierror, UnicodeError):
            code = "page_dns_failed"
        except Exception:
            # Error messages may contain untrusted URLs, headers or server text.
            code = "page_fetch_failed"
        return {"status": "unavailable", "text": "", "final_url": final_url, "error": code}
