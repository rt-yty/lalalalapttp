"""Bounded Yandex research: refine queries and read their public source pages.

Page text exists only while composing this answer. It is not persisted in the
travel state or sent back to the browser. Reading never chooses a trip or books.
"""
from __future__ import annotations

import asyncio
import re
from copy import deepcopy
from itertools import zip_longest
from urllib.parse import urlsplit

from .research import _sources, plan_research


async def enrich_search(gateway, search_adapter, reader, search, dialogue, now, *, deadline, emit):
    # Deterministic/offline gateways retain their existing single-search path.
    if search.get("error") or not callable(getattr(gateway, "research_plan", None)):
        return deepcopy(search)
    result = deepcopy(search)
    initial, initial_urls = [], set()
    for source in _sources(search):
        if source["url"] not in initial_urls:
            initial.append(source)
            initial_urls.add(source["url"])
    result["results"] = initial
    from .event_research import event_query, primary_event_source, _self_identified_venue
    event_search = event_query(search.get("query", ""), dialogue)
    result["research"] = {"queries": [search.get("query", "")], "pages_read": 0, "pages_attempted": 0}
    loop = asyncio.get_running_loop()
    def budget(maximum):
        return max(0.0, min(maximum, deadline - loop.time() - 26))
    stamp = now.isoformat() if hasattr(now, "isoformat") else str(now)
    async def read(source):
        allowance = budget(12)
        if allowance <= 0:
            return
        result["research"]["pages_attempted"] += 1
        await emit("tool_started", {"operation": "web_read", "source_id": source["id"]})
        try:
            async with asyncio.timeout(allowance):
                page = await reader.read(source["url"])
            text = page.get("text") if isinstance(page, dict) else None
            if page.get("status") == "read" and isinstance(text, str) and text.strip():
                source["page_status"] = "read"
                source["page_text"] = text[:18000]
                # Retain the Yandex link as identity; the reader validates each
                # redirect, and its final URL is optional evidence metadata.
                if isinstance(page.get("final_url"), str):
                    source["final_url"] = page["final_url"]
                result["research"]["pages_read"] += 1
            else:
                source["page_status"] = "unavailable"
        except Exception:
            source["page_status"] = "unavailable"
        await emit("tool_finished", {"operation": "web_read", "source_id": source["id"], "status": source["page_status"]})


    plan = await plan_research(gateway, result, dialogue, now, timeout=budget(12))
    if not plan["queries"]:
        # A planner may reasonably choose to read before searching again. Give
        # it the actual page evidence, including unavailable/old listings,
        # before deciding whether a refined query is needed.
        by_id = {item["id"]: item for item in result["results"]}
        preferred = [by_id[sid] for sid in plan["source_ids"] if sid in by_id]
        first = (preferred or result["results"])[:2]
        await asyncio.gather(*(read(source) for source in first))
        plan = await plan_research(gateway, result, dialogue, now, timeout=budget(12))
    seen_queries = {search.get("query", "").strip().casefold()}
    queries = []
    for query in plan["queries"]:
        normalized = query.strip().casefold()
        if normalized and normalized not in seen_queries:
            queries.append(query)
            seen_queries.add(normalized)
    queries = queries[:2]
    if event_search and not queries and budget(15) > 0:
        # A single readable date is not a survey of a tour. Always give broad
        # event requests one wider lookup instead of accepting today's hit.
        year = now.year if hasattr(now, "year") else str(now)[:4]
        queries = [(str(search.get("query", ""))[:280] + f" все даты города тур {year}")[:400]]

    async def refine(query):
        allowance = budget(15)
        if allowance <= 0:
            return []
        result["research"]["queries"].append(query)
        await emit("tool_started", {"operation": "web_search_refine", "provider": "yandex", "query": query})
        error, sources = None, []
        try:
            async with asyncio.timeout(allowance):
                response = await search_adapter.search(query, stamp)
            if not response.get("error"):
                sources = _sources(response)
            else:
                error = "search_unavailable"
        except Exception:
            error = "search_unavailable"
        await emit("tool_finished", {"operation": "web_search_refine", "provider": "yandex", "count": len(sources), "error": error})
        return sources

    # Search accounts may have a small per-second quota. Page reads can remain
    # concurrent, while the two refinement queries are paced rather than burst.
    batches = []
    for index, query in enumerate(queries):
        if index and budget(15) > 1:
            await asyncio.sleep(1.1)
        batches.append(await refine(query))
    seen_ids = {item["id"] for item in result["results"]}
    seen_urls = {item["url"] for item in result["results"]}
    added = []
    # Interleave results from both refinements so neither query crowds out the
    # other; IDs/URLs always originate with the adapter, never with the model.
    for row in zip_longest(*batches):
        for source in row:
            if source is None or source["id"] in seen_ids or source["url"] in seen_urls or len(result["results"]) >= (16 if event_search else 10):
                continue
            result["results"].append(source)
            added.append(source)
            seen_ids.add(source["id"])
            seen_urls.add(source["url"])

    by_id = {item["id"]: item for item in result["results"]}
    preferred = [by_id[sid] for sid in plan["source_ids"] if sid in by_id]
    def schedule_score(source):
        text = (source.get("title", "") + " " + source.get("snippet", "")).casefold()
        score = 4 if re.search(r"афиша|расписани|все.{0,8}дат|full tour|world tour|календар", text) else 0
        score += 2 if re.search(r"20\d{2}", text) else 0
        artist = re.split(r"\s+(?:расписани|концерт|афиш|tour|concert)", str(search.get("query", "")), maxsplit=1, flags=re.I)[0].strip()
        if primary_event_source(source, artist=artist) or _self_identified_venue(source):
            score += 8
        score -= 6 if re.search(r"concert history|concertarchives|notifications|announcements|shop|винил|merch", text) else 0
        return score
    ordered = (preferred[:1] + sorted(result["results"], key=schedule_score, reverse=True)
               if event_search else preferred[:1] + added + preferred[1:] + result["results"])
    selected, selected_ids = [], set()
    remaining_reads = (6 if event_search else 4) - result["research"]["pages_attempted"]
    domains = {urlsplit(item["url"]).hostname for item in result["results"] if item.get("page_status") in {"read", "unavailable"}}
    for diverse in (True, False):
        for source in ordered:
            if remaining_reads <= 0 or len(selected) >= remaining_reads:
                break
            domain = urlsplit(source["url"]).hostname
            if event_search and diverse and domain in domains:
                continue
            if source["id"] not in selected_ids and source.get("page_status") not in {"read", "unavailable"}:
                selected.append(source)
                selected_ids.add(source["id"])
                domains.add(domain)


    await asyncio.gather(*(read(source) for source in selected))
    return result


def public_search(search):
    """Keep source provenance, but never store raw page contents in a trip."""
    result = deepcopy(search)
    result["results"] = [
        {key: value for key, value in source.items() if key in {"id", "title", "url", "snippet", "page_status"}}
        for source in search.get("results", [])
    ]
    return result


def search_context(search):
    """Small previous-search context; conversation retains the composed answer."""
    if not isinstance(search, dict):
        return None
    return {
        "query": str(search.get("query", ""))[:300],
        "results": [{"id": str(source.get("id", ""))[:80],
                     "title": str(source.get("title", ""))[:120],
                     "snippet": str(source.get("snippet", ""))[:300],
                     "page_status": source.get("page_status")}
                    for source in search.get("results", [])[:5] if isinstance(source, dict)],
    }
