"""Ask for one trip parameter and keep restored dialogue consistent with it."""
from __future__ import annotations

from copy import deepcopy
import re

BUDGET_AMOUNT_QUESTION = (r"(?:какой(?: у вас)?(?: общий)? бюджет(?: (?:на поездку|на проживание))?|"
                          r"какой(?: у вас)? лимит по бюджету|какую сумму (?:использовать|планируете)|"
                          r"сколько(?: вы)? (?:готовы|планируете) потратить(?: на поездку)?)")

QUESTIONS = {
    "destination": "Куда хотите поехать?",
    "origin": "Из какого города поедете?",
    "departure_date": "Когда хотите вылететь?",
    "return_date": "Когда хотите вернуться?",
    "adults": "Сколько взрослых поедет?",
    "budget_scope": "Указанный бюджет — на всех или на одного человека?",
}
_TOPICS = {
    "origin": r"откуда|из какого города|город(?:а)? отправления|город вылета",
    "destination": r"куда (?:хотите )?поех|какое направление|в каком городе|какой город",
    "dates": r"дат[ауы]|когда|числ[ао]|заезд|выезд|вылет[а-я]* и возвращ|вылет[а-я]* и возврат",
    "adults": r"сколько.{0,25}(?:взросл|человек)|число взрослых|состав (?:группы|участников)",
    "ages": r"возраст|сколько лет",
    "budget_scope": r"на всех|на человека|на одного|область бюджета",
    "budget": r"бюджет|сумм[ау]|сколько готовы",
}
_BLOCKER = re.compile(r"не поддерж|не реализ|железн|поезд(?:а|ов|ом)|прототип поддерживает|неоднознач", re.I)


def required_trip_fields(request: dict) -> tuple[str, ...]:
    products = request.get("products") or ["avia", "hotel"]
    transport = any(product in products for product in ("avia", "rail", "bus"))
    return ("destination", *(("origin",) if transport else ()), "departure_date", "return_date", "adults")


def trip_question(field: str, request: dict) -> str:
    products = request.get("products") or ["avia", "hotel"]
    if products == ["hotel"]:
        return {"destination": "В каком городе искать отель?", "departure_date": "Когда планируете заезд?",
                "return_date": "Когда планируете выезд?"}.get(field, QUESTIONS[field])
    if field == "departure_date" and any(p in products for p in ("rail", "bus")):
        return "Когда хотите отправиться?"
    return QUESTIONS[field]


def next_trip_question(request: dict) -> str | None:
    for field in required_trip_fields(request):
        if request.get(field) is None:
            return trip_question(field, request)
    if request.get("budget_minor") and not request.get("budget_scope"):
        return QUESTIONS["budget_scope"]
    return None


def answered_slot_question(question: str, request: dict) -> bool:
    """Recognize only plain slot prompts whose answer is already in the request.

    Full matches intentionally leave alternatives, explicit dates, proposed
    changes, unsupported capabilities and other semantic clarifications alone.
    """
    text = re.sub(r"\s+", " ", question.casefold().replace("ё", "е")).strip().rstrip(" ?.!:;")
    desire = r"(?:(?:вы )?(?:хотите|планируете) )?"
    fields = {
        "origin": r"(?:откуда (?:хотите поехать|поедете|вылетаете)|из какого города (?:поедете|вылетаете|планируете вылет)|какой город отправления)",
        "destination": r"(?:куда (?:хотите поехать|поедете)|в каком городе (?:искать отель|планируете проживание))",
        "departure_date": rf"(?:(?:когда|какого числа|в какой день) {desire}(?:вылететь|вылетаете|отправиться|отправляетесь|заезд|заехать|заселиться|приехать)(?: в отель| в гостиницу)?|на какую дату {desire}(?:вылет|отправление|заезд)(?: в отель)?)",
        "return_date": rf"(?:(?:когда|какого числа|в какой день) {desire}(?:вернуться|возвращаетесь|выезд|выехать|выселиться)(?: из отеля| из гостиницы)?|на какую дату {desire}(?:возвращение|выезд)(?: из отеля)?)",
        "adults": r"сколько взрослых (?:поедет|поедут|едет|путешествует)",
        "budget_scope": r"(?:указанный )?бюджет(?: указан)? ?(?:[—–-] )?на всех или на (?:одного человека|человека)",
        "budget_minor": BUDGET_AMOUNT_QUESTION,
    }
    for field, pattern in fields.items():
        if request.get(field) is not None and re.fullmatch(pattern, text):
            return True
    duration = r"(?:на )?сколько (?:ночей|дней)(?: (?:ищем отель|искать отель|планируете поездку|планируете проживание))?"
    return bool(request.get("departure_date") and request.get("return_date") and re.fullmatch(duration, text))


def optional_preferences_question(question: str, request: dict) -> bool:
    """A complete search request needs no generic preference questionnaire.

    Concrete ambiguities about location, dates or mandatory conditions are
    deliberately outside these full matches.
    """
    text = re.sub(r"\s+", " ", question.casefold().replace("ё", "е")).strip().rstrip(" ?.!:;")
    if request.get("budget_minor") is None and re.fullmatch(
            r"(?:уточните область бюджета|(?:указанный )?бюджет (?:— )?на всех или на (?:одного человека|человека)|"
            r"бюджет указан на всех или на (?:одного человека|человека))", text):
        return True
    if next_trip_question(request) is not None:
        return False
    if request.get("budget_minor") is None and re.fullmatch(BUDGET_AMOUNT_QUESTION, text):
        return True
    if re.fullmatch(r"(?:есть ли (?:у вас )?питомец|(?:вы )?(?:путешествуете|едете) ли (?:вы )?с питомцем|нужны ли дополнительные услуги)", text):
        return True
    if not request.get("children_ages") and re.fullmatch(r"нужно ли учитывать детей(?: при заселении| в поездке)?", text):
        return True
    # A complete request already authorizes searching. Match only generic
    # questionnaire/permission prompts, never a concrete change or checkout.
    extra = r"(?:что(?:-то|-нибудь|-либо)? еще|еще что(?:-то|-нибудь|-либо)?)"
    action = r"(?:включить|добавить|учесть|уточнить)"
    search_scope = r"(?: в поиск| при поиске| для поиска)?"
    generic_questions = (
        rf"(?:нужно|надо) ли (?:{action} {extra}|{extra} {action}){search_scope}",
        rf"(?:хотите(?: ли вы)?|хотели бы(?: вы)?) {action} {extra}{search_scope}",
        r"(?:есть ли|остались ли)(?: у вас)?(?: еще)? (?:какие-то |какие-нибудь |какие-либо |дополнительные |особые )?"
        r"(?:пожелания|предпочтения)(?: к (?:поиску|поездке|проживанию|отелю))?",
        r"(?:начать|начинаем|начнем|запустить|запускаем|запустим) (?:подбор|поиск)(?: (?:вариантов|отелей|проживания|билетов))?",
    )
    if any(re.fullmatch(pattern, text) for pattern in generic_questions):
        return True
    return bool(re.fullmatch(
        r"(?:есть ли (?:у вас )?(?:какие-то |какие-либо |особые )?предпочтения|"
        r"какие (?:у вас )?предпочтения) (?:по|к) (?:отелю|отелям|проживанию|перелету)", text))


def normalize_clarification(question: str, request: dict) -> str:
    """Reduce recognizable compound slot questions without dropping blockers.

    The interpreter still owns semantic ambiguities and unsupported requests;
    its instructions require a single question there as well.
    """
    topics = {key for key, pattern in _TOPICS.items() if re.search(pattern, question, re.I)}
    if "budget_scope" in topics:
        topics.discard("budget")
    if len(topics) < 2 or _BLOCKER.search(question):
        return question
    if "ages" in topics:
        return "Сколько лет каждому ребёнку на дату поездки?"
    required = required_trip_fields(request)
    for field in ("origin", "destination", "departure_date", "return_date", "adults", "budget_scope"):
        if field != "budget_scope" and field not in required:
            continue
        topic = "dates" if field in {"departure_date", "return_date"} else field
        if topic in topics and request.get(field) is None:
            return trip_question(field, request)
    if "budget" in topics and request.get("budget_minor") is None:
        return "Какой бюджет планируете на поездку?"
    return question


def normalize_session_question(state: dict) -> dict:
    """Use the same repaired question for display and the next interpretation.

    Only the current unfinished clarification is adapted. Immutable old turn
    receipts and earlier history are left alone.
    """
    result = deepcopy(state)
    old = result.get("last_question")
    if not old:
        return result
    if ((result.get("concert_intake") or {}).get("mode") == "trip"
            and old.startswith("Я сначала уточню пожелания к поездке:")):
        return result
    question = normalize_clarification(old, result.get("request", {}))
    if question != old:
        result["last_question"] = question
        dialogue = result.get("dialogue") or []
        if dialogue and dialogue[-1].get("role") == "assistant" and dialogue[-1].get("text") == old:
            dialogue[-1]["text"] = question
    return result
