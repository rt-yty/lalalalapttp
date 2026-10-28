"""Collect explicit concert-trip preferences before researching destinations."""
from __future__ import annotations

from difflib import SequenceMatcher
import re

from pydantic import BaseModel, ConfigDict, Field
from typing import Literal


class ConcertIntakePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["trip", "information", "cancel"] | None = None
    event_name: str | None = Field(default=None, max_length=180)
    open_to_suggestions: bool | None = None
    period: str | None = Field(default=None, max_length=240)
    locations: str | None = Field(default=None, max_length=240)
    budget_flexible: bool | None = None
    children_pending: bool | None = None


class ConcertIntakeState(BaseModel):
    mode: Literal["inactive", "trip", "information"] = "inactive"
    event_name: str | None = None
    open_to_suggestions: bool = False
    period: str | None = None
    locations: str | None = None
    budget_flexible: bool = False
    children_pending: bool = False


EVENT = re.compile(r"концерт|фестивал|выступлени|\bconcert|\bfestival", re.I)
MONTH = r"январ[ьяе]|феврал[ьяе]|март[ае]?|апрел[ьяе]|ма[йяе]|июн[ьяе]|июл[ьяе]|август[ае]?|сентябр[ьяе]|октябр[ьяе]|ноябр[ьяе]|декабр[ьяе]"
GENERIC_NAMES = {"концерт", "концерты", "мероприятие", "событие", "исполнитель", "артист", "неизвестный", "unknown", "concert", "artist", "разные концерты", "любой", "любые", "на концерт", "поездка на концерт", "мне все равно", "мне всё равно", "не знаю", "спасибо", "да", "нет", "пожалуйста", "пока не знаю", "ближайшие", "ближайший"}


def cancels_intake(text):
    return bool(re.fullmatch(r"(?:не\s+надо[, ]*)?(?:отмена|отменяю|стоп|передумал[а]?)[.! ]*|не\s+надо[.! ]*", text.strip(), re.I))


def meaningful_event_name(value):
    if not isinstance(value, str):
        return False
    clean = re.sub(r"[\W_]+", " ", value.casefold()).strip()
    return (len(clean) >= 2 and clean not in GENERIC_NAMES
            and not re.match(r"(?:я|мы|мне|нам|нас|еду|поеду|один|одна|двое|трое|не знаю|пока|спасибо|все равно|всё равно|без ограничений)\b", clean)
            and not re.fullmatch(r"(?:неизвестн\w*|не указан\w*|разн\w*|ближайш\w*)\s*(?:артист|исполнитель|концерт)?", clean)
            and clean not in {"various artists", "различные артисты", "исполнитель не указан"})


def _present_name(name, text):
    candidate = re.sub(r"\W", "", name.casefold())
    words = re.findall(r"[\w-]+", text.casefold())
    return candidate in re.sub(r"\W", "", text.casefold()) or any(
        len(word) >= 4 and SequenceMatcher(None, candidate, word).ratio() >= .7 for word in words)


def update_intake(previous, text, decision, request):
    state = previous.model_copy(deep=True)
    supplied = ConcertIntakePatch.model_validate(decision.get("concert_intake") or {}).model_dump(exclude_none=True)
    lower = text.casefold().replace("ё", "е")
    mentions_event = bool(EVENT.search(text))
    trip_signal = mentions_event and bool(re.search(r"хочу\s+(?:(?:поехать|съездить|попасть|пойти)\s+)?на\s+|поездк|путешеств|поехать|съездить|спланир|отел[ьяь]|дорог", lower))
    information_only = bool(re.search(r"только (?:афиша|афишу|расписание)|поездку.{0,15}не планир|не планир.{0,15}поездк", lower))
    verify_first = bool((mentions_event or previous.mode != "inactive") and re.search(
        r"сначала\s+(?:проверь|узнай|найди|посмотри)|(?:проверь|узнай).{0,30}(?:есть ли|будет ли|состоится ли|существует ли)", lower))
    information_only = information_only or verify_first
    # A refusal of a city, companion or another concert condition is still a
    # trip correction. Only an unqualified refusal can cancel it by keyword.
    if supplied.get("mode") == "cancel" or re.fullmatch(
            r"\s*(?:я\s+)?(?:без|(?:больше\s+)?не хочу|не нужен|отменяю)\s+"
            r"(?:больше\s+)?(?:на\s+)?концерт(?:а|ы)?[.!? ]*", lower):
        return ConcertIntakeState()
    if information_only:
        state.mode = "information"
    elif trip_signal:
        state.mode = "trip"
    elif supplied.get("mode") in {"trip", "information"}:
        state.mode = supplied["mode"]
    elif state.mode == "inactive" and mentions_event:
        state.mode = "information"
    if state.mode == "inactive":
        return state

    name = supplied.get("event_name")
    if meaningful_event_name(name) and _present_name(name, text):
        state.event_name = name.strip()
    elif mentions_event:
        match = re.search(r"(?:концерт(?:а|у|ы|ов|е)?|выступление|фестиваль)\s+([^,.!?;\n]+)", text, re.I)
        if match:
            candidate = re.split(r"\s+(?:в|на|из|с|для|ближайш\w*)\s+|\s+(?:помоги|подбери|покажи|найди|хочу)\b", match[1], maxsplit=1, flags=re.I)[0].strip(' «»"')
            if meaningful_event_name(candidate) and not re.search(r"^(?:в|на|из|с|без)\s|дат|город|билет|поездк|куда|какие|когда|\b(?:мне|нам)\b|" + MONTH, candidate, re.I):
                state.event_name = candidate
    if not state.event_name and previous.mode != "inactive":
        first = re.split(r"[.;\n]", text, maxsplit=1)[0].strip(' «»"')
        if meaningful_event_name(first) and len(first.split()) <= 4 and not re.search(r"\d|из |бюджет|любо|подбер|предлож|огранич|дат|город|стран|взросл|ребен|дет|" + MONTH, first, re.I):
            state.event_name = first

    explicit_open = bool(re.search(r"(?:подбери|предложи|выбери|посоветуй).{0,35}(?:концерт|исполнител|артист|мероприят)|(?:артист|исполнител|концерт).{0,20}(?:любо|не\s*важ|без предпочт)|на (?:твой|ваш) вкус|выбери сам", lower))
    if explicit_open:
        state.open_to_suggestions = True
    if supplied.get("open_to_suggestions") is False and state.event_name:
        state.open_to_suggestions = False

    any_dates = bool(re.search(r"(?:дат\w*|период|время).{0,20}(?:люб\w*|неваж\w*|гибк\w*|без огранич)|(?:люб\w*|гибк\w*).{0,15}(?:дат\w*|период)|когда угодно", lower))
    any_places = bool(re.search(r"(?:город\w*|стран\w*|направлен\w*).{0,20}(?:люб\w*|неваж\w*|без огранич)|(?:люб\w*).{0,15}(?:город\w*|стран\w*|направлен\w*)|куда угодно|по всему миру", lower))
    if any_dates:
        state.period = "любые будущие даты"
    elif supplied.get("period") and (re.search(MONTH + r"|\d|выходн|недел|осен|зим|весн|летом|ближайш", lower) or supplied["period"].casefold() in lower):
        state.period = supplied["period"]
    else:
        period = re.search(r"(?:в\s+)?(?:" + MONTH + r")(?:\s+20\d{2})?", text, re.I)
        if period:
            state.period = period[0]
    if any_places:
        state.locations = "любые города и страны"
    elif supplied.get("locations") and supplied["locations"].casefold() in lower:
        state.locations = supplied["locations"]
    elif supplied.get("locations") and re.search(r"город|стран|в России|в Европе|только|рассматриваю|лучше\s+в", text, re.I):
        state.locations = supplied["locations"]
    # A shared explicit qualifier can cover both slots: «город и дата любые».
    if re.search(r"город\w*\s+и\s+дат\w*\s+люб|дат\w*\s+и\s+город\w*\s+люб", lower):
        state.period, state.locations = "любые будущие даты", "любые города и страны"
    if re.search(r"(?:бюджет|цен\w*|сумм\w*).{0,25}(?:без огранич|не огранич|любо|неваж)|без ограничени[йя] (?:по бюджету|бюджета|по сумме)|бюджет неважен", lower):
        state.budget_flexible = True
    elif supplied.get("budget_flexible") is False or request.get("budget_minor"):
        state.budget_flexible = False
    children_mentioned = bool(re.search(r"реб[её]н|дет(?:и|ьми|ей)|доч|сын", text, re.I))
    if re.search(r"без\s+детей|детей\s+нет", lower) and not re.search(r"не\s+(?:хочу|поеду|поедем|еду|едем)[^.!?]{0,45}без\s+детей", lower):
        state.children_pending = False
    elif supplied.get("children_pending") is not None:
        state.children_pending = supplied["children_pending"]
    elif children_mentioned and not request.get("children_ages"):
        state.children_pending = True
    elif request.get("children_ages"):
        state.children_pending = False
    return state


def missing_preferences(state, request):
    missing = []
    if not state.event_name and not state.open_to_suggestions:
        missing.append(("event", "на чей концерт хотите поехать — или мне подобрать исполнителя"))
    if state.mode != "trip":
        return missing
    if not state.period and not (request.get("departure_date") and request.get("return_date")) and not request.get("event_date"):
        missing.append(("period", "какой период вам подходит — можно указать любые даты"))
    if not state.locations and not request.get("destination"):
        missing.append(("locations", "какие города или страны рассматривать — можно без ограничений"))
    if not request.get("origin") and request.get("products") != ["hotel"]:
        missing.append(("origin", "из какого города поедете"))
    if request.get("adults") is None:
        missing.append(("party", "сколько взрослых и детей поедет"))
    elif state.children_pending:
        missing.append(("children", "сколько лет каждому ребёнку"))
    if not request.get("budget_minor") and not state.budget_flexible:
        missing.append(("budget", "какой бюджет на всю поездку — или он без ограничений"))
    elif (request.get("budget_minor") and not request.get("budget_scope")
          and (request.get("adults") != 1 or request.get("children_ages") or state.children_pending)):
        missing.append(("budget_scope", "названный бюджет рассчитан на всех или на одного человека"))
    return missing


def intake_message(state, missing):
    prompts = [text for _, text in missing[:3]]
    if state.mode == "information":
        return "Я уточню: афишу какого исполнителя или мероприятия посмотреть?"
    return "Я сначала уточню пожелания к поездке: " + "; ".join(prompts) + "?"


def research_query(state, request):
    subject = state.event_name or "концерты разных исполнителей"
    period = state.period or request.get("event_date") or " ".join(filter(None, (request.get("departure_date"), request.get("return_date"))))
    locations = state.locations or request.get("destination") or ""
    return f"{subject} расписание концертов {period or ''} {locations}".strip()[:400]


def filter_event_preferences(events, preferences, now):
    """Enforce recognizable date/geography bounds after model extraction."""
    from calendar import monthrange
    from datetime import date
    request = preferences.get("request") or {}
    period = str(preferences.get("period") or "").casefold()
    lower, upper = request.get("departure_date"), request.get("return_date")
    month_names = ("январ", "феврал", "март", "апрел", "ма", "июн", "июл", "август", "сентябр", "октябр", "ноябр", "декабр")
    # Preserve every named month, in text order. Iterating calendar order used
    # to silently discard all but one month (and broke cross-year intervals).
    month_matches = []
    for number, name in enumerate(month_names, 1):
        pattern = r"\b" + name + (r"[йяе]\b" if number == 5 else r"\w*\b") + r"(?:\s+(20\d{2}))?"
        month_matches.extend((match, number) for match in re.finditer(pattern, period))
    month_matches.sort(key=lambda item: item[0].start())
    years = re.findall(r"\b20\d{2}\b", period)
    shared_year = int(years[0]) if len(set(years)) == 1 else None
    month_windows = []
    resolved_years = [int(match[1]) if match[1] else None for match, _ in month_matches]
    anchors = [index for index, year in enumerate(resolved_years) if year is not None]
    if month_matches:
        if anchors:
            # «декабрь — январь 2027» starts in 2026; a year on the first
            # month similarly carries forward over a December/January boundary.
            for index in range(anchors[0] - 1, -1, -1):
                resolved_years[index] = resolved_years[index + 1] - (month_matches[index][1] > month_matches[index + 1][1])
        else:
            resolved_years[0] = shared_year or now.year + (month_matches[0][1] < now.month)
        for index, (_, number) in enumerate(month_matches):
            if resolved_years[index] is None:
                resolved_years[index] = resolved_years[index - 1] + (number < month_matches[index - 1][1])
            year = resolved_years[index]
            month_windows.append((date(year, number, 1).isoformat(), date(year, number, monthrange(year, number)[1]).isoformat()))
    if len(month_matches) == 2:
        connector = period[month_matches[0][0].end():month_matches[1][0].start()].strip()
        if re.fullmatch(r"по|до|[-–—]", connector) and month_windows[0][0] <= month_windows[1][1]:
            month_windows = [(month_windows[0][0], month_windows[1][1])]
    iso = re.findall(r"\b20\d{2}-\d{2}-\d{2}\b", period)
    if iso:
        lower, upper = max(str(lower or ""), min(iso)), min(str(upper or "9999-12-31"), max(iso))
    places = str(preferences.get("locations") or "").casefold()
    unrestricted = bool(re.search(r"люб|без огранич|неваж|куда угодно|всему миру", places))
    countries = {"росси": ("росси", "russia"), "герман": ("герман", "germany"), "франц": ("франц", "france"),
                 "литв": ("литв", "lithuania"), "япон": ("япон", "japan"), "коре": ("коре", "korea"),
                 "словак": ("словак", "slovakia"), "чех": ("чех", "czech"), "австри": ("австри", "austria"),
                 "егип": ("егип", "egypt"), "армен": ("армен", "armenia"), "израи": ("израи", "israel"),
                 "сша": ("сша", "united states"), "казах": ("казах", "kazakhstan")}
    country_roots = [root for root in countries if root in places]
    from .interpretation import CITY_ALIASES
    cities = {canonical.casefold() for alias, canonical in CITY_ALIASES.items()
              if re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", places)}
    selected = []
    for event in events:
        if month_windows and not any(start <= event["date"] <= end for start, end in month_windows):
            continue
        if lower and event["date"] < str(lower) or upper and event["date"] > str(upper):
            continue
        if places and not unrestricted:
            country = str(event.get("country") or "").casefold()
            country_match = any(any(root in country for root in countries[key]) for key in country_roots)
            city_match = event["city"].casefold() in cities
            if (country_roots or cities) and not (country_match or city_match):
                continue
        selected.append(event)
    return selected
