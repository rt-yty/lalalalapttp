"""Small input corrections for objectively invalid calendar dates."""
from __future__ import annotations

import calendar
from datetime import date
import re

MONTHS = {'января': 1, 'февраля': 2, 'марта': 3, 'апреля': 4, 'мая': 5,
          'июня': 6, 'июля': 7, 'августа': 8, 'сентября': 9, 'октября': 10,
          'ноября': 11, 'декабря': 12}


def calendar_correction(text: str, request: dict, today: date, *, last_question: str | None = None) -> tuple[dict, str | None]:
    """Retain valid dates next to an impossible one, without guessing a replacement."""
    matches = list(re.finditer(r'\b(\d{1,2})\s+(' + '|'.join(MONTHS) + r')(?:\s+(20\d{2}))?', text.casefold()))
    if not matches:
        return {}, None
    # An explicit year on either end anchors the interval. December before
    # February 2027 belongs to 2026 even if February itself is an invalid day.
    years = [int(match[3]) if match[3] else None for match in matches]
    anchors = [index for index, year in enumerate(years) if year is not None]
    if anchors:
        for index in range(anchors[0] - 1, -1, -1):
            years[index] = years[index + 1] - (MONTHS[matches[index][2]] > MONTHS[matches[index + 1][2]])
    else:
        years[0] = today.year
    for index in range(1, len(matches)):
        if years[index] is None:
            years[index] = years[index - 1] + (MONTHS[matches[index][2]] < MONTHS[matches[index - 1][2]])
    dates, bad = [], []
    for match, current_year in zip(matches, years):
        day, month = int(match[1]), MONTHS[match[2]]
        try:
            parsed = date(current_year, month, day).isoformat()
        except ValueError:
            parsed = None
            bad.append((len(dates), day, month, current_year))
        dates.append(parsed)
    if not bad:
        return {}, None
    # A short reply belongs to the date being asked for. Explicit wording can
    # still change another date; never erase a known departure on a return reply.
    patch = {}
    return_marker = r'обратно|возвращ|верну|выезд|выех|высел'
    departure_marker = r'вылет|отправ|заезд|заех|засел|туда'
    explicit_return = bool(re.search(return_marker, text, re.I))
    explicit_departure = bool(re.search(departure_marker, text, re.I))
    asked_return = bool(re.search(return_marker, last_question or "", re.I))
    asked_departure = bool(re.search(departure_marker, last_question or "", re.I))
    return_only = len(dates) == 1 and (explicit_return or not explicit_departure and (
        asked_return or not asked_departure and request.get("departure_date") and not request.get("return_date")))
    for index, value in enumerate(dates[:2]):
        patch['return_date' if return_only or index == 1 else 'departure_date'] = value
    index, day, month, year = bad[0]
    target_return = return_only or index == 1
    hotel = request.get('products') == ['hotel']
    slot = ('выезда из отеля' if hotel else 'возвращения') if target_return else ('заезда' if hotel else 'отправления')
    maximum = calendar.monthrange(year, month)[1]
    question = (f'Даты {day:02d}.{month:02d}.{year} не существует: в этом месяце {maximum} дней. '
                f'Какую дату {slot} использовать?')
    return patch, question


def same_day_trip_requested(text: str) -> bool:
    """Recognize an explicitly requested same-day roundtrip before a model drops it."""
    t = text.casefold().replace('ё', 'е')
    if re.search(r'туда и обратно.{0,30}(?:в один день|одним днем)|(?:в один день|одним днем).{0,30}туда и обратно', t):
        return True
    if 'туда и обратно' not in t:
        return False
    interval = re.search(r'\b(\d{1,2})\s*(?:по|[-–—])\s*(\d{1,2})\s+(?:' + '|'.join(MONTHS) + r')\b', t)
    if interval:
        return interval[1] == interval[2]
    dates = re.findall(r'\b\d{1,2}\s+(?:' + '|'.join(MONTHS) + r')(?:\s+20\d{2})?|\b20\d{2}-\d{2}-\d{2}\b', t)
    return (len(dates) == 1 and bool(re.search(r'туда и обратно\s+(?:на\s+)?\d', t))
            and not re.search(r'через|на\s+(?:\w+\s+)?(?:недел\w*|дн\w*|ноч\w*)|возвращ|обратно.{0,10}(?:завтра|послезавтра)', t.replace('туда и обратно', '')))
