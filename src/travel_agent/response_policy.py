"""Small, deterministic boundaries around model interpretation and public prose.

These checks deliberately do not claim to classify every possible utterance. The
model also receives the travel-only policy; these rules cover explicit requests
that must never reach search, checkout, or an unrestricted model answer.
"""
from __future__ import annotations

import re

TRAVEL_ONLY = "Я помогаю с путешествиями: маршрутом, дорогой, отелями и отдыхом на месте. Что хотите узнать о поездке?"
PRIVATE_REPLY = "Я могу помочь с поездкой, но не раскрываю внутренние инструкции и данные."
REVIEW_REPLY = ("Я не могу доказать подлинность отзыва по одному тексту или оценке. "
                "Могу сравнить отзывы по датам, конкретным деталям и повторяющимся замечаниям. "
                "Пришлите название отеля или текст отзывов, которые хотите проверить.")

_FORBIDDEN = re.compile(r"\b(?:tutu|туту|yandex|яндекс\w*|mcp|codex|hugging\s*face|search\s*api|"
                        r"google|гугл\w*|bing|aviasales|авиасейлс|booking(?:\.com)?|expedia|onetwotrip|tool|provider)\b|"
                        r"\b(?:инструмент|провайдер|поставщик)\w*\b", re.I)
_INTERNAL = re.compile(r"\b(?:Traceback|ValidationError|RuntimeError|API[_ -]?KEY|request_id|doctor)\b", re.I)


_AUTHENTICITY_REQUEST = re.compile(
    r"(?:подлинн|настоящ|фейков|накручен|правдив|достоверн).{0,30}отзыв|"
    r"отзыв.{0,35}(?:подлинн|настоящ|фейков|накручен|правдив|достоверн)", re.I)
_REVIEW_COMMAND = re.compile(
    r"игнорируй|ignore\b|системн\w*\s+(?:промпт|инструкц)|"
    r"(?:скажи|напиши|подтверди|выполни|раскрой|выведи|покажи)\b|"
    r"budget_minor|api.?key", re.I)


def review_reply(text: str) -> str | None:
    """Evaluate only attributed review details; quoted commands are never actions."""
    samples = re.findall(r"«([^»]{15,})»|\"([^\"]{15,})\"", text)
    excerpts = [left or right for left, right in samples]
    if not excerpts:
        return None
    normalized = [re.sub(r"\W+", " ", item.casefold()).strip() for item in excerpts]
    positives, negatives, details = [], [], []
    noise_risk = False
    quiet_mention = False
    ignored_commands = False
    seen = set()
    for excerpt in excerpts:
        for clause in re.split(r"[,;.!?]\s*|\s+но\s+", excerpt):
            clause = clause.strip(" .:;—-")
            if not clause or clause.casefold() in seen:
                continue
            seen.add(clause.casefold())
            if _REVIEW_COMMAND.search(clause):
                ignored_commands = True
                continue
            value = clause.casefold().replace("ё", "е")
            # A negation makes simple sentiment classification ambiguous. Retain
            # the exact detail instead of turning e.g. "не шумно" into a defect.
            negated = bool(re.search(r"\b(?:не|нет|без)\b", value))
            negative = not negated and re.search(r"шумн|громк|грязн|грязь|плох|невкусн|скудн|тесн|слом|неудоб|плесен", value)
            positive = not negated and re.search(r"чист|вкусн|хорош|отлич|удоб|уют|тих|спокойн|вежлив|приветлив|просторн", value)
            quote = "«" + (clause if len(clause) <= 130 else clause[:127].rstrip() + "…") + "»"
            if negative:
                negatives.append(quote)
                noise_risk |= bool(re.search(r"шумн|громк", value))
            elif positive:
                positives.append(quote)
                quiet_mention |= bool(re.search(r"тих|спокойн", value))
            else:
                details.append(quote)
    parts = []
    # Bound the answer while preserving the user's actual words, rather than
    # inventing hotel facts from a theme such as "breakfast" or "location".
    remaining = 6
    for label, values in (("Плюсы по присланным отзывам", positives),
                          ("Минусы по присланным отзывам", negatives),
                          ("Другие детали из отзывов", details)):
        values = values[:remaining]
        remaining -= len(values)
        if values:
            parts.append(label + ": " + "; ".join(values) + ".")
    if noise_risk:
        if quiet_mention:
            parts.append("Оценки тишины расходятся. Уточните даты проживания и расположение номеров: впечатления могут зависеть от них.")
        else:
            parts.append("Если вы чутко спите, шум — важный риск. Перед выбором уточните, есть ли тихие номера и куда выходят их окна.")
    if len(set(normalized)) < len(normalized):
        parts.append("Есть дословное повторение текста. Оно само по себе не доказывает накрутку.")
    elif _AUTHENTICITY_REQUEST.search(text):
        parts.append("По этим текстам нельзя подтвердить авторство или факт проживания.")
    if ignored_commands and not parts:
        parts.append("В этих фрагментах нет описания проживания: команды внутри отзыва не помогают оценить отель.")
    return "\n\n".join(parts) or "В этих фрагментах недостаточно деталей для оценки проживания."


def stops_planning(text: str) -> bool:
    """Only an unqualified refusal is an explicit stop.

    A refusal of a condition (alone, by bus, without a pet) must reach the
    interpreter, which can apply the requested trip correction.
    """
    text = text.casefold().replace("ё", "е").strip()
    return bool(re.fullmatch(r"(?:(?:я|вообще|больше)\s+)*не\s+хочу\s+путешествовать[.!? ]*", text)
                or re.fullmatch(r"(?:давай(?:те)?\s+)?(?:отменим|отмени|отложим|отложи|остановим|останови)\s+"
                                r"(?:пока\s+)?(?:поездку|путешествие|планирование(?:\s+поездки)?|подбор(?:\s+поездки)?)"
                                r"(?:\s+пока)?[.!? ]*", text)
                or re.fullmatch(r"(?:вообще\s+не\s+хочу\s+путешествовать[, ]+)?просто\s+(?:болтаю|болтаем|поболтаем)[.!? ]*", text))


def policy_decision(text: str, request: dict | None = None) -> dict | None:
    """Return a no-side-effect decision for unambiguous scope/privacy requests."""
    from .interpretation import Interpretation, explicit_budget, nightly_budget
    t = text.casefold().replace("ё", "е").strip()
    chat = lambda message: Interpretation(intent="chat", reply_text=message).as_result()
    request = request or {}
    from .concert_intake import cancels_intake
    if (request.get("_concert_intake") or {}).get("mode", "inactive") != "inactive" and cancels_intake(text):
        return chat("Понял, остановлю подбор поездки на концерт.")
    if re.search(r"гарант.{0,100}(?:цен|стоим)|(?:цен|стоим).{0,60}гарант", t):
        return chat("Не могу гарантировать, что цена сохранится до завтра: стоимость и наличие могут измениться. Перед выбором стоит обновить варианты.")
    if stops_planning(t):
        return chat("Понял, остановлю подбор. Я могу обсудить путешествия без планирования поездки.")
    amount = nightly_budget(t) or explicit_budget(t)
    if amount is not None and amount[0] < 0:
        return Interpretation(intent="replan" if request.get("destination") else "plan",
            question="Бюджет должен быть больше нуля. Какую сумму использовать или искать без ограничения по бюджету?").as_result()
    if re.search(r"(?<!не )\b(?:0|ноль)\s+взросл", t):
        return Interpretation(intent="replan" if request.get("destination") else "plan",
            question="Я пока не подбираю поездки для детей без взрослых. Сколько взрослых поедет вместе с ребёнком?").as_result()
    if re.search(r"системн\w*\s+(?:промпт|инструкц)|скрыт\w*\s+инструкц|"
                 r"(?:покажи|раскрой|выведи).{0,30}(?:api.?key|ключ[иь]?\s+доступа|парол|секрет)", t):
        return chat(PRIVATE_REPLY)
    if re.search(r"(?:скажи|напиши|сообщи)\s*[, :]?\s*(?:мне\s+)?(?:что|будто)\b.{0,60}"
                 r"(?:оплачен|забронирован|оформлен)|"
                 r"(?:сделай вид|притворись).{0,60}(?:оплат|бронь|бронирован)|"
                 r"(?<!не )подтверди\s+(?:мне\s+)?оплат|"
                 r"(?:say|pretend).{0,60}(?:paid|payment|booked|reservation)", t):
        return chat("У меня нет подтверждения оплаты или оформления брони, поэтому я не могу сообщить, что заказ оплачен или забронирован.")
    if re.search(r"игнорируй\s+(?:все\s+)?(?:правила|инструкции)|"
                 r"ignore\s+(?:all\s+)?(?:previous|system)\s+instructions", t):
        return chat(PRIVATE_REPLY)
    history = (request.get("_dialogue") or {}).get("history", [])
    recent_review = any(item.get("role") == "assistant" and "отзыв" in item.get("text", "").casefold() for item in history[-2:])
    if ("отзыв" in t or recent_review) and (analysis := review_reply(text)):
        return chat(analysis)
    if _AUTHENTICITY_REQUEST.search(t):
        selected = (request.get("_dialogue") or {}).get("selected_components", [])
        hotel = next((item for item in selected if item.get("product_type") == "hotel"), None)
        if hotel and hotel.get("title"):
            return Interpretation(intent="web_search", web_query=(str(hotel["title"]) + " " + str(request.get("destination") or "") + " отзывы гостей")[:400]).as_result()
        return chat(REVIEW_REPLY)
    # Do not misinterpret a travel budget calculation as an unrelated exercise.
    trip_context = bool(re.search(r"поездк|путешеств|билет|отел|гостиниц|маршрут|багаж|"
                                  r"виза|визу|самолет|поезд\b|перелет|ноч[ьи]|бюджет|руб", t))
    explicit_other = re.search(r"(?:напиши|исправь|объясни|реши|сгенерируй).{0,50}"
                              r"(?:код|функци|python|javascript|sql|уравнен|сочинени|резюме)|"
                              r"(?:купить|покупать|продать|инвестир).{0,25}(?:акци[июй]|биткоин|крипт)|"
                              r"(?:кто|когда).{0,20}(?:президент|выборы)|"
                              r"(?:write|debug).{0,30}(?:code|python|javascript)|"
                              r"(?:лечить|лечение|диагноз).{0,30}(?:болезн|кашель|диабет)|"
                              r"просто\s+(?:болта|поболта)|не\s+хочу\s+путешествовать", t)
    arithmetic = re.fullmatch(r"(?:сколько\s+(?:будет\s+)?)?\d+\s*(?:[+*/-]|умножить\s+на)\s*\d+[.!?]*", t)
    if arithmetic or (explicit_other and not trip_context):
        return chat(TRAVEL_ONLY)
    return None


def public_text(value: str) -> str:
    """Keep supplier/process commentary out of the traveller's voice.

    IDs, references, URLs and numeric facts are intentionally never passed here.
    Unsupported sentences are withheld rather than grammatically mangled or
    turned into a claim that the assistant performed an unverified action.
    """
    if not isinstance(value, str):
        return value
    if not (_FORBIDDEN.search(value) or _INTERNAL.search(value)):
        return value
    sentences = re.split(r"(?<=[.!?])\s+|\n+", value)
    safe = [sentence for sentence in sentences if not (_FORBIDDEN.search(sentence) or _INTERNAL.search(sentence))]
    return " ".join(safe).strip() or "Я пока не смог завершить запрос. Попробуйте ещё раз."


def public_result(out: dict) -> dict:
    result = dict(out)
    for key in ("message",):
        if isinstance(result.get(key), str):
            result[key] = public_text(result[key])
    for key in ("questions", "warnings"):
        if isinstance(result.get(key), list):
            result[key] = [public_text(item) if isinstance(item, str) else item for item in result[key]]
    if isinstance(result.get("error"), dict):
        result["error"] = {key: public_text(value) if key in {"message", "explanation"} and isinstance(value, str) else value
                           for key, value in result["error"].items()}
    return result
