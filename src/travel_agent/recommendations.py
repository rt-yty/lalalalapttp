"""Explainable travel discovery, independent of live offers and ticket availability.

The catalogue is deliberately editorial: it stores related interests, never dates,
prices or invented demand. Only canonical topics are shared between accounts.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import math
import re
from typing import Any

EXTRACTOR_VERSION = 2


@dataclass(frozen=True)
class Topic:
    id: str
    title: str
    target: str
    aliases: str
    description: str
    related: tuple[str, ...]
    connection: str
    land: bool = False

    @property
    def kind(self):
        return self.id.split(":", 1)[0]


def destination(key, title, target, aliases, description, related, connection, land=False):
    return Topic("destination:" + key, title, target, aliases, description,
                 tuple("destination:" + item for item in related.split()), connection, land)


def artist(key, title, aliases, related, connection):
    return Topic("artist:" + key, title, title, aliases,
                 "Найти будущие концерты и спланировать поездку вокруг выступления.",
                 tuple("artist:" + item for item in related.split()), connection)


CATALOG = {topic.id: topic for topic in (
    destination("japan", "Япония", "Японию", r"япони\w*|токио|киото|осака|japan|tokyo|kyoto", "Города, местная кухня и новые культурные впечатления.", "korea taiwan singapore", "города, кухня и культура Восточной Азии"),
    destination("korea", "Южная Корея", "Южную Корею", r"(?:южн\w*\s+)?коре[яюие]\w*|сеул\w*|пусан\w*|south korea|seoul", "Сеул, местная кухня и прогулки за пределами привычных маршрутов.", "japan taiwan singapore", "города, кухня и культура Восточной Азии"),
    destination("taiwan", "Тайвань", "Тайвань", r"тайван\w*|тайбэ\w*|taiwan|taipei", "Городские прогулки, ночные рынки и природа в одной поездке.", "japan korea vietnam", "городские впечатления и местная кухня"),
    destination("singapore", "Сингапур", "Сингапур", r"сингапур\w*|singapore", "Архитектура, сады и гастрономические открытия.", "korea japan thailand", "городские прогулки и новые вкусы"),
    destination("thailand", "Таиланд", "Таиланд", r"та[ий]ланд\w*|пхукет\w*|бангкок\w*|thailand|phuket", "Совместить отдых у моря с городами и местной кухней.", "vietnam indonesia srilanka", "море, природа и отдых в Азии"),
    destination("vietnam", "Вьетнам", "Вьетнам", r"вьетнам\w*|дананг\w*|нячанг\w*|ханой|vietnam", "Море, городские прогулки и путешествие ради кухни.", "thailand srilanka indonesia", "море, природа и отдых в Азии"),
    destination("indonesia", "Бали", "Бали", r"индонези\w*|бали|indonesia|bali", "Островная природа, океан и неспешный отдых.", "srilanka thailand vietnam", "островной отдых и природа"),
    destination("srilanka", "Шри-Ланка", "Шри-Ланку", r"шри[\s-]?ланк\w*|sri lanka", "Океан, зелёные холмы и маршрут с остановками.", "indonesia vietnam thailand", "океан и путешествия на природе"),
    destination("turkey", "Турция", "Турцию", r"турци\w*|стамбул\w*|антали\w*|истанбул\w*|turkey|istanbul", "Выбрать между морем, историческими городами и гастрономией.", "georgia armenia uzbekistan", "исторические города и местная кухня"),
    destination("georgia", "Грузия", "Грузию", r"грузи\w*|тбилиси|батуми|georgia|tbilisi", "Городские прогулки, горы и путешествие ради вкусов.", "armenia uzbekistan turkey", "горы, кухня и городские прогулки", True),
    destination("armenia", "Армения", "Армению", r"армени\w*|ереван\w*|armenia|yerevan", "Ереван, местная кухня и выезды за город.", "georgia uzbekistan turkey", "кухня, культура и поездки за город", True),
    destination("uzbekistan", "Узбекистан", "Узбекистан", r"узбекистан\w*|самарканд\w*|бухар\w*|ташкент\w*|uzbekistan", "Историческая архитектура и маршрут через несколько городов.", "armenia georgia kazan", "историческая архитектура и местная кухня", True),
    destination("france", "Париж", "Париж", r"франци\w*|париж\w*|france|paris", "Музеи, архитектура и прогулки по городским кварталам.", "italy spain prague", "музеи, архитектура и городские прогулки"),
    destination("italy", "Италия", "Италию", r"итали\w*|рим(?:а|е|у|ом)?|флоренци\w*|венец\w*|италия|italy|rome", "Искусство, старые города и поездка ради еды.", "france spain prague", "искусство, архитектура и гастрономия"),
    destination("spain", "Испания", "Испанию", r"испани\w*|барселон\w*|мадрид\w*|spain|barcelona", "Архитектура, музеи и прогулки у моря.", "italy france portugal", "города, искусство и отдых у моря"),
    destination("portugal", "Португалия", "Португалию", r"португали\w*|лиссабон\w*|порту|portugal|lisbon", "Городские кварталы, океан и неспешные прогулки.", "spain italy france", "городские прогулки и побережье"),
    destination("prague", "Прага", "Прагу", r"праг[ауеиой]*|чехи\w*|prague|czechia", "Исторический центр, архитектура и музеи.", "france italy spain", "исторические города и архитектура"),
    destination("kazan", "Казань", "Казань", r"казан\w*|kazan", "Кремль, городские прогулки и татарская кухня.", "nizhny petersburg kaliningrad", "короткие городские поездки и местная кухня", True),
    destination("nizhny", "Нижний Новгород", "Нижний Новгород", r"нижн\w*\s+новгород\w*|nizhny novgorod", "Набережные, старые кварталы и выходные в новом городе.", "kazan petersburg kaliningrad", "короткие поездки и прогулки по городу", True),
    destination("petersburg", "Санкт-Петербург", "Санкт-Петербург", r"санкт[\s-]?петербург\w*|петербург\w*|питер\w*|saint petersburg", "Музеи, архитектура и прогулки вдоль каналов.", "kaliningrad kazan nizhny", "музеи, архитектура и городские прогулки", True),
    destination("kaliningrad", "Калининград", "Калининград", r"калининград\w*|kaliningrad", "Городская архитектура и поездки к Балтийскому морю.", "petersburg kazan nizhny", "необычная архитектура и городские прогулки", True),
    destination("sochi", "Сочи", "Сочи", r"сочи|адлер\w*|sochi", "Соединить прогулки у моря и выезд в горы.", "dagestan altai georgia", "природа, горы и активный отдых", True),
    destination("altai", "Алтай", "Алтай", r"алта[йяею]\w*|altai", "Горные пейзажи и маршрут с остановками на природе.", "dagestan sochi georgia", "горы и отдых на природе", True),
    destination("dagestan", "Дагестан", "Дагестан", r"дагестан\w*|махачкал\w*|дербент\w*|dagestan", "Горные маршруты, старые города и местная кухня.", "altai georgia sochi", "горы, маршруты и местная кухня", True),
    artist("imagine-dragons", "Imagine Dragons", r"imagine\s+dragons|им[эе]джин\s+др[эе]гон\w*", "onerepublic coldplay muse", "интерес к большому концертному шоу"),
    artist("onerepublic", "OneRepublic", r"one\s*republic|ван\s*репаблик", "imagine-dragons coldplay muse", "интерес к мелодичным концертным выступлениям"),
    artist("coldplay", "Coldplay", r"coldplay|колдплей|колдплэй", "onerepublic imagine-dragons muse", "интерес к большому концертному шоу"),
    artist("muse", "Muse", r"muse|мьюз", "imagine-dragons linkin-park rammstein", "интерес к рок-концертам и сценическому шоу"),
    artist("rammstein", "Rammstein", r"rammstein|рамм?штайн\w*|рамм?ште[йи]н\w*", "metallica muse linkin-park", "интерес к тяжёлой музыке и сценическому шоу"),
    artist("metallica", "Metallica", r"metallica|металлик\w*", "rammstein linkin-park muse", "интерес к рок-концертам"),
    artist("linkin-park", "Linkin Park", r"linkin\s+park|линкин\s+парк", "muse metallica imagine-dragons", "интерес к рок-концертам"),
    artist("the-weeknd", "The Weeknd", r"the\s+weeknd|weeknd|зе\s+уикенд", "dua-lipa bruno-mars coldplay", "интерес к поп-концертам и живому шоу"),
    artist("dua-lipa", "Dua Lipa", r"dua\s+lipa|ду[аэ]\s+лип\w*", "the-weeknd bruno-mars coldplay", "интерес к поп-концертам и живому шоу"),
    artist("bruno-mars", "Bruno Mars", r"bruno\s+mars|бруно\s+марс", "the-weeknd dua-lipa onerepublic", "интерес к поп-концертам и живому шоу"),
    Topic("theme:weekend", "Выходные в новом городе", "выходные в новом городе", r"выходн\w*|уик[еэ]нд|weekend", "Короткая смена обстановки: прогулки, местная кухня и новые места.", ("destination:nizhny", "destination:kazan", "destination:petersburg"), "интерес к коротким поездкам", True),
    Topic("theme:rail", "В путешествие без самолёта", "путешествие на поезде", r"поезд(?:е|ом|а|у)?|железнодорож\w*|без\s+самолет\w*", "Подобрать направление и удобный маршрут по железной дороге.", ("destination:kazan", "destination:nizhny", "destination:petersburg"), "интерес к путешествиям на поезде", True),
    Topic("theme:sea", "Несколько дней у моря", "отдых у моря", r"мор[еяю]|пляж\w*|океан\w*", "Найти побережье под ваши даты, бюджет и ритм отдыха.", ("destination:sochi", "destination:turkey", "destination:vietnam"), "интерес к отдыху у моря"),
    Topic("theme:mountains", "Ближе к горам", "поездку в горы", r"гор(?:ы|ах|ам|ами)|треккинг\w*|хайкинг\w*", "Пейзажи, прогулки и маршрут с временем на отдых.", ("destination:altai", "destination:dagestan", "destination:georgia"), "интерес к горам и природе"),
    Topic("theme:food", "Путешествие ради вкусов", "гастрономическое путешествие", r"гастроном\w*|местн\w*\s+кухн\w*|вкусн\w*\s+(?:ед\w*|поесть)", "Выбрать город, который интересно узнавать через местную кухню.", ("destination:kazan", "destination:georgia", "destination:uzbekistan"), "интерес к гастрономическим поездкам"),
    Topic("theme:concerts", "Поездка ради концерта", "поездку на концерт", r"концерт\w*|фестивал\w*|выступлени\w*", "Сначала найти подходящее выступление, затем спланировать дорогу и отель.", ("theme:concerts",), "интерес к поездкам на концерты"),
)}
_PATTERNS = {key: re.compile(r"\b(?:" + topic.aliases + r")\b", re.I) for key, topic in CATALOG.items()}
_STARTERS = ("theme:weekend", "theme:concerts", "theme:sea", "theme:rail", "theme:mountains", "theme:food")
_REFINEMENT = re.compile(r"бюджет|тысяч|тыс\b|руб|₽|взросл|ребен|дет(?:ей|ьми|и)\b|на\s+недел|на\s+\d+\s+(?:дн|ноч)|отел|гостиниц|билет|перелет|поезд(?:ом|е)\b|завтрак|багаж|без пересад|даты|числ[ао]|из\s+|ноябр|декабр|январ|феврал|март|апрел|ма[йяе]\b|июн|июл|август|сентябр|октябр", re.I)


def _clean(text):
    return str(text or "").casefold().replace("ё", "е")


_NEGATIVE_PREFIX = re.compile(
    r"\b(?:отменя\w*|исключи\w*|исключая|кроме|вместо|"
    r"не\s+(?:хочу|интересу\w*|нуж\w*|поед\w*|ед\w*|люблю|планиру\w*|"
    r"рассматрива\w*|предлага\w*|надо|стоит)|не\s+в)\b")
_NEGATIVE_SUFFIX = re.compile(
    r"^\s*(?:[—–:-]\s*)?(?:(?:мне|нам|больше|теперь)\s+)*"
    r"(?:не\s+(?:хочу|предлага\w*|интерес\w*|нуж\w*|рассматрива\w*|подход\w*|нрав\w*)|"
    r"отпадает|отменя\w*|исключаем|исключи\w*)\b")
_INTENT = re.compile(r"\b(?:хочу|хотим|поеду|поедем|выбираю|выберем|давай|рассмотрим|предложи|планирую)\b")
_ORIGIN_PREFIX = re.compile(
    r"\b(?:(?:из|от)\s+(?:города\s+)?|(?:живу|живем|проживаю|находимся|нахожусь)\s+в\s+(?:городе\s+)?|"
    r"(?:город\s+)?(?:вылета|отправления|проживания)\s*[:—–-]?\s*|(?:вылет|отправление|старт)\s*[:—–-]\s*)$")
_ORIGIN_SUFFIX = re.compile(r"^\s*(?:[—–:-]\s*)?(?:это\s+)?(?:мой\s+|наш\s+)?(?:город\s+)?(?:вылета|отправления|проживания)\b")


def _mention_states(text, kind=None):
    """Map explicit mentions to their final positive/negative meaning in a turn.

    Negation is bounded by clauses and by a later positive travel intention. This
    keeps «вместо Японии хочу в Корею» from negating both countries.
    """
    found = {}
    for key, pattern in _PATTERNS.items():
        if kind and CATALOG[key].kind != kind:
            continue
        for match in pattern.finditer(text):
            prefix = re.split(r"[,;.!?]|\b(?:но|а|зато|лучше|однако)\b", text[:match.start()])[-1][-160:]
            suffix = re.split(r"[,;.!?]|\b(?:но|а|зато|лучше|однако)\b", text[match.end():])[0][:100]
            if key == "destination:korea" and re.search(r"(?:северн\w*|north)\s+$", prefix):
                continue
            if CATALOG[key].kind == "destination" and (_ORIGIN_PREFIX.search(prefix) or _ORIGIN_SUFFIX.search(suffix)):
                continue
            # The last non-negated intention starts a new semantic clause even
            # without punctuation: «вместо Японии хочу в Корею».
            for intent in _INTENT.finditer(prefix):
                if not re.search(r"\bне\s*(?:очень\s*)?$", prefix[:intent.start()]):
                    prefix = prefix[intent.start():]
                    break
            if CATALOG[key].kind == "destination":
                # Rejecting a service does not reject its destination:
                # «не нужен перелёт в Японию, нужен только отель».
                prefix = re.sub(r"\bне\s+(?:хочу|нуж\w*|надо)\s+(?:(?:ехать|лететь|никаких|больше)\s+)?"
                                r"(?:пересад\w*|перелет\w*|самолет\w*|авиабилет\w*|билет\w*|отел\w*|"
                                r"гостиниц\w*|проживани\w*|багаж\w*|завтрак\w*|поезд(?:ом|е|а|ов)?)"
                                r"\s*(?:(?:на|по)\s+пути\s+)?(?:в|на)?\s*$", "", prefix)
            # If a refusal came after an earlier positive intention, it still
            # negates this topic. Do not equate «не был» with a refusal.
            rejected = bool(_NEGATIVE_PREFIX.search(prefix) or re.search(r"\b(?:не|без(?:\s+(?:любых|всяких|этих|таких))?)\s*$", prefix) or _NEGATIVE_SUFFIX.search(suffix))
            found[key] = rejected
    return found


def _positive_mentions(text, kind=None):
    return [key for key, rejected in _mention_states(text, kind).items() if not rejected]


def _context(request):
    """Validate both new structured requests and older persisted contexts."""
    if not isinstance(request, dict):
        return {}
    context = {}
    origin = request.get("origin")
    if isinstance(origin, str) and origin.strip() and len(origin) <= 150:
        context["origin"] = origin.strip()
    if isinstance(request.get("budget_scope"), str) and request["budget_scope"] in {"trip", "per_person"}:
        context["budget_scope"] = request["budget_scope"]
    for key in ("budget_minor", "nightly_budget_minor"):
        value = request.get(key)
        if type(value) is int and value > 0:
            context[key] = value
    adults = request.get("adults")
    if type(adults) is int and 1 <= adults <= 9:
        context["adults"] = adults
    ages = request.get("children_ages")
    if isinstance(ages, list) and ages and len(ages) <= 8 and all(type(age) is int and 0 <= age <= 17 for age in ages):
        context["children_ages"] = ages
    products = request.get("products")
    if isinstance(products, list) and products and all(isinstance(p, str) and p in {"avia", "rail", "bus", "hotel"} for p in products):
        context["products"] = list(dict.fromkeys(products))
    days = request.get("duration_days")
    if type(days) is int and 1 <= days <= 60:
        context["duration_days"] = days
    nights = request.get("nights")
    if type(nights) is int and 1 <= nights <= 60:
        context["nights"] = nights
    try:
        nights = (date.fromisoformat(str(request["return_date"])) - date.fromisoformat(str(request["departure_date"]))).days
        if 1 <= nights <= 60:
            context["nights"] = nights
    except (KeyError, TypeError, ValueError):
        pass
    if request.get("pets") is True:
        context["pets"] = True
    constraints = request.get("constraints") or {}
    if isinstance(constraints, dict):
        safe = {key: value for key, value in constraints.items() if key in {"direct", "breakfast", "free_cancellation"} and type(value) is bool}
        if type(constraints.get("baggage_kg")) is int and constraints["baggage_kg"] > 0:
            safe["baggage_kg"] = constraints["baggage_kg"]
        if safe:
            context["constraints"] = safe
    return context


_NUMBERS = {"один": 1, "одну": 1, "одна": 1, "две": 2, "два": 2, "три": 3, "четыре": 4,
            "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
            "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14,
            "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18,
            "девятнадцать": 19, "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50, "шестьдесят": 60}
_DURATION = re.compile(r"\b(?:на|длительностью|длительность\s*[:—–-]?)\s+"
                       r"(?:(\d{1,2}|" + "|".join(_NUMBERS) + r")\s+)?"
                       r"(дней|дня|день|ночей|ночи|ночь|неделю|недели|недель|недельку)\b")


def _explicit_duration(text):
    if re.search(r"(?:длительность|сколько\s+(?:дней|ночей)).{0,30}(?:не\s+знаю|не\s+решил|пока\s+не)|"
                 r"(?:не\s+знаю|не\s+решил).{0,30}(?:длительность|сколько\s+(?:дней|ночей))", text):
        return True, None, None
    found = list(_DURATION.finditer(text))
    if not found:
        return False, None, None
    match = found[-1]
    token = match[1]
    count = int(token) if token and token.isdigit() else _NUMBERS.get(token, 1)
    value = count * (7 if match[2].startswith("недел") else 1)
    unit = "nights" if match[2].startswith("ноч") else "duration_days"
    return True, value if 1 <= value <= 60 else None, unit


def extract_signals(state: dict, user_text: str) -> list[dict]:
    """Derive signed canonical interests, never assistant-proposed alternatives.

    A negative signal contains only ``rejected=True``. A later positive signal
    re-enables the same topic. Unknown names stay broad themes without inheriting
    a previous artist or the conditions of an unrelated destination.
    """
    text = _clean(user_text).strip()
    if not text or re.fullmatch(r"(?:спасибо|привет|здравствуй\w*|ок|хорошо|понятно|отмена|стоп|передумал[а]?)[.!\s]*", text):
        return []
    if re.search(r"напиши\s+(?:код|реферат|стих)|переведи|реши\s+задач", text):
        return []
    state = state if isinstance(state, dict) else {}
    request = state.get("request") if isinstance(state.get("request"), dict) else {}
    intake = state.get("concert_intake") if isinstance(state.get("concert_intake"), dict) else {}
    mentioned = _mention_states(text)
    origin_reply = False
    for key in _positive_mentions(_clean(request.get("origin")), "destination"):
        if (_PATTERNS[key].fullmatch(text.strip(" .!?")) and request.get("destination")
                and key not in _positive_mentions(_clean(request["destination"]), "destination")):
            mentioned.pop(key, None)
            origin_reply = True
    destinations = [key for key, rejected in mentioned.items() if not rejected and CATALOG[key].kind == "destination"]
    artists = [key for key, rejected in mentioned.items() if not rejected and CATALOG[key].kind == "artist"]
    topics = [key for key, rejected in mentioned.items() if not rejected]
    rejected = {key for key, negative in mentioned.items() if negative}
    active_destinations = _positive_mentions(_clean(request.get("destination")), "destination")
    active_artists = _positive_mentions(_clean(intake.get("event_name")), "artist") if intake.get("mode") in {"trip", "information"} else []
    cancelled = bool(re.search(r"\b(?:отмен\w*|передумал\w*)\b", text))
    event_mentioned = "theme:concerts" in mentioned
    if "theme:concerts" in rejected:
        rejected.update(active_artists)
    # If an unknown/new artist is explicitly named, a stale previous intake is
    # not evidence that this is another request for that previous artist.
    if not destinations and not artists and not cancelled and not any(CATALOG[key].kind in {"destination", "artist"} for key in rejected) and (_REFINEMENT.search(text) or origin_reply):
        if not event_mentioned:
            topics.extend(key for key in active_destinations if key not in mentioned)
            topics.extend(key for key in active_artists if key not in mentioned and key not in rejected)
    if any(CATALOG[key].kind == "artist" for key in topics):
        topics = [key for key in topics if key != "theme:concerts"]
    # An unrelated factual question may leave the prior trip snapshot intact.
    # Only carry that trip's conditions when its destination/artist matches.
    unrelated_destination = bool(destinations and active_destinations and not set(destinations) & set(active_destinations))
    unrelated_artist = bool(artists and active_artists and not set(artists) & set(active_artists))
    new_unknown_event = bool(event_mentioned and not artists and "theme:concerts" not in rejected)
    context = _context({"origin": request.get("origin")} if unrelated_destination or unrelated_artist or new_unknown_event else request)
    # Explicit current amounts override older structured snapshots. For legacy
    # jobs this also restores a grounded cap without inventing party or dates.
    from .interpretation import explicit_budget, nightly_budget
    nightly = nightly_budget(user_text)
    budget = nightly or explicit_budget(user_text)
    if budget:
        context.pop("budget_minor", None)
        context.pop("nightly_budget_minor", None)
        if budget[0] > 0:
            context["nightly_budget_minor" if nightly else "budget_minor"] = budget[0]
        if budget[1]:
            context["budget_scope"] = budget[1]
    has_duration, duration, duration_unit = _explicit_duration(text)
    if has_duration:
        # None marks a reset; do not carry the other unit from an earlier turn.
        context["nights"] = duration if duration_unit == "nights" else None
        context["duration_days"] = duration if duration_unit == "duration_days" else None
    positive = [{"topic_id": key, "context": dict(context)} for key in dict.fromkeys(topics) if key not in rejected]
    negative = [{"topic_id": key, "context": {"rejected": True}} for key in sorted(rejected)]
    return negative + positive[:max(0, 12 - len(negative))]


def _age(value, now):
    try:
        at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        age = (now - at).total_seconds() / 86400
        return max(0.0, age) if age >= -5 / 1440 else 91.0
    except (ValueError, TypeError):
        return 91.0


def _rub(value):
    # Budget precision is retained, including cents, without claiming a fare.
    rubles, cents = divmod(value, 100)
    return f"{rubles:,}".replace(",", " ") + (f",{cents:02d}" if cents else "") + " ₽"


def _prompt(topic, context, profile):
    if topic.kind == "artist":
        prompt = f"Хочу спланировать поездку на концерт {topic.title}. Сначала проверь, есть ли подтверждённые будущие выступления, и предложи подходящие города и даты."
    elif topic.id == "theme:concerts":
        prompt = "Хочу спланировать поездку на концерт. Помоги выбрать исполнителя и сначала проверь будущие выступления, города и даты."
    else:
        prompt = f"Помоги спланировать {('поездку в ' + topic.target) if topic.kind == 'destination' else topic.target}."
    origin = profile.get("origin") or context.get("origin")
    if isinstance(origin, str) and origin.strip():
        origin = origin.strip()[:150]
        genitive = {"москва": "Москвы", "казань": "Казани", "санкт-петербург": "Санкт-Петербурга", "петербург": "Санкт-Петербурга", "нижний новгород": "Нижнего Новгорода", "ереван": "Еревана", "стамбул": "Стамбула", "калининград": "Калининграда", "сочи": "Сочи", "тбилиси": "Тбилиси"}.get(_clean(origin))
        prompt += f" Отправляюсь из {genitive}." if genitive else f" Город отправления: {origin}."
    products = context.get("products") or []
    names = {"avia": "перелёт", "rail": "поезд", "bus": "автобус", "hotel": "отель"}
    if len(products) == 1:
        prompt += " Подбери только " + names[products[0]] + "."
    elif products:
        transport = [names[p] for p in products if p != "hotel"]
        prompt += (" Сравни " if len(transport) > 1 else " Подбери ") + " и ".join(transport) + "."
        prompt += " Также нужен отель." if "hotel" in products else " Отель не нужен."
    if context.get("adults"):
        prompt += f" {context['adults']} взрослый." if context["adults"] == 1 else f" {context['adults']} взрослых."
    if context.get("children_ages"):
        prompt += " Дети " + " и ".join(str(age) for age in context["children_ages"]) + " лет."
    if context.get("duration_days"):
        prompt += f" Длительность: {context['duration_days']} дней; новые даты и число ночей уточни у меня."
    elif context.get("nights"):
        prompt += f" Длительность: {context['nights']} ночей; новые даты уточни у меня."
    if context.get("nightly_budget_minor"):
        prompt += f" Лимит на проживание: {_rub(context['nightly_budget_minor'])} за ночь."
        if context.get("budget_scope") == "per_person":
            prompt += " Это лимит на человека."
        elif context.get("budget_scope") != "trip":
            prompt += " Уточни, на всех или на человека указан лимит."
    elif context.get("budget_minor"):
        prompt += f" Бюджет: до {_rub(context['budget_minor'])}"
        scope = context.get("budget_scope")
        prompt += " на человека." if scope == "per_person" else " на всех." if scope == "trip" else "; уточни, это на всех или на человека."
        prompt += " Проверь, можно ли уложиться в этот бюджет; не считай его подтверждённой ценой."
    if context.get("pets"):
        prompt += " Еду с питомцем; уточни условия перевозки и размещения."
    constraints = context.get("constraints") or {}
    for key, phrase in (("direct", "Нужна дорога без пересадок."), ("breakfast", "Нужен завтрак в отеле."), ("free_cancellation", "Нужна бесплатная отмена проживания.")):
        if constraints.get(key) is True:
            prompt += " " + phrase
    if type(constraints.get("baggage_kg")) is int and constraints["baggage_kg"] > 0:
        prompt += f" Нужен багаж {constraints['baggage_kg']} кг."
    return prompt


def _card(topic, source, context, profile, reason):
    nightly = context.get("nightly_budget_minor")
    budget = context.get("budget_minor") if not nightly else None
    budget_label = None
    if nightly:
        scope = context.get("budget_scope")
        budget_label = "Ориентир: до " + _rub(nightly) + " за ночь" + (" на человека" if scope == "per_person" else " на всех" if scope == "trip" else " · состав уточним")
    elif budget:
        scope = context.get("budget_scope")
        budget_label = "Ваш бюджет: до " + _rub(budget) + (" на человека" if scope == "per_person" else " на всех" if scope == "trip" else " · состав уточним")
    return {"id": topic.id, "title": ("На концерт " if topic.kind == "artist" else "") + topic.title,
            "description": topic.description, "reason": reason, "prompt": _prompt(topic, context, profile),
            "kind": topic.kind, "source": source, "budget_minor": budget, "budget_label": budget_label}


def build_recommendations(signals: list[dict], popular: list[dict], profile: dict,
                          now: datetime, dismissed: set[str] | None = None) -> dict[str, Any]:
    """Rank diverse related interests, keeping each source trip's own context."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    dismissed = set(dismissed or ())
    profile = _context(profile)
    latest = {}
    for signal in signals:
        if not isinstance(signal, dict):
            continue
        key = signal.get("topic_id")
        if not isinstance(key, str) or key not in CATALOG:
            continue
        age = _age(signal.get("occurred_at"), now)
        if age > 90:
            continue
        previous = latest.get(key)
        if previous is None or age < previous["age"]:
            raw = signal.get("context")
            latest[key] = {"age": age, "weight": math.pow(.5, age / 21),
                           "context": _context(raw), "rejected": isinstance(raw, dict) and raw.get("rejected") is True}
    rejected = {key for key, entry in latest.items() if entry["rejected"]}
    interests = {key: entry for key, entry in latest.items() if not entry["rejected"]}
    blocked = dismissed | rejected
    concert_refusal = latest.get("theme:concerts") if "theme:concerts" in rejected else None
    block_concerts = bool(concert_refusal and not any(CATALOG[key].kind == "artist" and entry["age"] < concert_refusal["age"] for key, entry in interests.items()))
    if block_concerts:
        blocked.update(key for key, topic in CATALOG.items() if topic.kind == "artist")
    profile_origins = set(_positive_mentions(_clean(profile.get("origin")), "destination"))
    candidates = {}
    for key, interest in interests.items():
        if key in blocked:
            continue
        source = CATALOG[key]
        context = interest["context"]
        source_origins = profile_origins or set(_positive_mentions(_clean(context.get("origin")), "destination"))
        ground_only = bool(set(context.get("products") or []) & {"rail", "bus"}) and "avia" not in (context.get("products") or [])
        for rank, target_id in enumerate(source.related):
            topic = CATALOG[target_id]
            if target_id in blocked or target_id in source_origins or (target_id in interests and target_id != "theme:concerts"):
                continue
            if ground_only and topic.kind == "destination" and not topic.land:
                continue
            score = interest["weight"] * (1 - rank * .12)
            if target_id not in candidates or score > candidates[target_id]["score"]:
                reason = f"Вы интересовались: {source.title}. Общая идея — {source.connection}."
                candidates[target_id] = {"score": score, "source_id": key, "context": context, "reason": reason}
    ranked = sorted(candidates, key=lambda key: (-candidates[key]["score"], key))
    chosen = []
    per_interest = {}
    # Let multiple recent interests appear before taking a third similar item.
    for limit in (2, 3):
        for key in ranked:
            entry = candidates[key]
            if key in chosen or per_interest.get(entry["source_id"], 0) >= limit:
                continue
            chosen.append(key)
            per_interest[entry["source_id"]] = per_interest.get(entry["source_id"], 0) + 1
            if len(chosen) == 4:
                break
        if len(chosen) == 4:
            break
    personalized = [_card(CATALOG[key], "personalized", candidates[key]["context"], profile, candidates[key]["reason"]) for key in chosen]
    excluded = blocked | set(chosen) | profile_origins
    valid_popular = []
    for row in popular:
        if not isinstance(row, dict) or not isinstance(row.get("topic_id"), str) or row["topic_id"] not in CATALOG:
            continue
        try:
            score, users = float(row.get("score") or 0), int(row.get("users") or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(score) and score > 0 and users >= 3:
            valid_popular.append((row["topic_id"], score))
    community = []
    for key, _ in sorted(valid_popular, key=lambda row: (-row[1], row[0])):
        if key not in excluded and key not in community:
            community.append(key)
    popular_cards = [_card(CATALOG[key], "popular", {}, profile, "Среди частых интересов путешественников за последние 90 дней.") for key in community[:6]]
    starter = []
    if not popular_cards:
        popular_cards = [_card(CATALOG[key], "starter", {}, profile, "Идея для первой поездки — выберите, что вам ближе.") for key in _STARTERS if key not in excluded][:6]
    elif not personalized and len(popular_cards) < 3:
        starter = [_card(CATALOG[key], "starter", {}, profile, "Ещё одна идея — выберите, что вам ближе.")
                   for key in _STARTERS if key not in excluded and key not in community][:6 - len(popular_cards)]
    return {"personalized": personalized, "popular": popular_cards, "starter": starter,
            "popular_source": "community" if community else "starter", "history_used": bool(latest)}
