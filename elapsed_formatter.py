from datetime import datetime
import emoji
from zoneinfo import ZoneInfo
from dateutil.relativedelta import relativedelta
from settings import settings

timezone = ZoneInfo(settings["timezone"])
TIME_UNITS = ("hour", "minute")
MINUTES_IN_UNIT = (("week", 10080), ("day", 1440), ("hour", 60), ("minute", 1))


def now():
    return datetime.now(timezone).replace(tzinfo=None)


def parse_event_date(text):
    text = text.strip()
    try:
        return datetime.strptime(text, settings["datetime_format"]).isoformat(timespec="minutes")
    except ValueError:
        return datetime.strptime(text, settings["date_format"]).date().isoformat()


def is_future(event_date):
    return datetime.fromisoformat(event_date) > now()


def has_time(event_date):
    return "T" in event_date


def reference_now(event_date):
    current = now()
    return current if has_time(event_date) else current.replace(hour=0, minute=0, second=0, microsecond=0)


def event_span(event_date):
    return sorted((datetime.fromisoformat(event_date), reference_now(event_date)))


def plural(number, forms):
    remainder = number % 100
    if 11 <= remainder <= 19:
        return forms[2]
    last_digit = remainder % 10
    if last_digit == 1:
        return forms[0]
    if 2 <= last_digit <= 4:
        return forms[1]
    return forms[2]


def count_text(number, unit):
    return f"{number} {plural(number, settings['plural_forms'][unit])}"


def visible_texts(parts, show_time):
    return [count_text(value, unit) for unit, value in parts if value and (show_time or unit not in TIME_UNITS)]


def split_minutes(total_minutes, from_unit):
    parts = []
    started = False
    for unit, size in MINUTES_IN_UNIT:
        started = started or unit == from_unit
        if started:
            value, total_minutes = divmod(total_minutes, size)
            parts.append((unit, value))
    return parts


def join_texts(texts):
    if len(texts) == 1:
        return texts[0]
    return settings["unit_separator"].join(texts[:-1]) + settings["unit_last_separator"] + texts[-1]


def format_elapsed(event_date):
    earlier, later = event_span(event_date)
    delta = relativedelta(later, earlier)
    parts = [("year", delta.years), ("month", delta.months), ("day", delta.days), ("hour", delta.hours), ("minute", delta.minutes)]
    texts = visible_texts(parts, has_time(event_date))
    if texts:
        return join_texts(texts)
    return settings["less_than_minute"] if has_time(event_date) else settings["less_than_day"]


def format_in_unit(event_date, unit):
    if unit == "year":
        return format_elapsed(event_date)
    earlier, later = event_span(event_date)
    if unit == "month":
        delta = relativedelta(later, earlier)
        parts = [("month", delta.years * 12 + delta.months), ("day", delta.days), ("hour", delta.hours), ("minute", delta.minutes)]
    else:
        parts = split_minutes(int((later - earlier).total_seconds() // 60), unit)
    main_unit, main_value = parts[0]
    return join_texts([count_text(main_value, main_unit)] + visible_texts(parts[1:], has_time(event_date)))


def with_direction(event_date, text):
    prefix = settings["time_prefix"]["future" if is_future(event_date) else "past"]
    return f"{prefix} {text}"


def describe_elapsed(event_date):
    return with_direction(event_date, format_elapsed(event_date))


def describe_in_unit(event_date, unit):
    return with_direction(event_date, format_in_unit(event_date, unit))


def abbreviated_text(number, unit):
    return f"{number}{settings['abbreviations'][unit]}"


def elapsed_values(start, end, units):
    start, end = min(start, end), max(start, end)
    anchor = start
    values = {}
    if "year" in units:
        values["year"] = relativedelta(end, anchor).years
        anchor += relativedelta(years=values["year"])
    if "month" in units:
        delta = relativedelta(end, anchor)
        values["month"] = delta.years * 12 + delta.months
        anchor += relativedelta(months=values["month"])
    rest = end - anchor
    minutes = rest.days * 1440 + rest.seconds // 60
    for unit, size in MINUTES_IN_UNIT:
        if unit in units:
            values[unit], minutes = divmod(minutes, size)
    return values


def render_units(values, units, style):
    render = abbreviated_text if style == "short" else count_text
    texts = [render(values[unit], unit) for unit in units if values[unit] or unit in TIME_UNITS]
    if not texts:
        texts = [render(0, units[-1])]
    return settings["short_separator"].join(texts) if style == "short" else join_texts(texts)


def parse_mask(mask):
    tokens = []
    index = 0
    while index < len(mask):
        char = mask[index]
        if char == "\\" and index + 1 < len(mask):
            tokens.append(("text", mask[index + 1]))
            index += 2
        elif char in settings["mask_tokens"]:
            width = 1
            while index + width < len(mask) and mask[index + width] == char:
                width += 1
            tokens.append(("unit", settings["mask_tokens"][char], width))
            index += width
        else:
            tokens.append(("text", char))
            index += 1
    return tokens


def is_valid_mask(mask):
    return any(token[0] == "unit" for token in parse_mask(mask))


def format_widget(event, widget_format, mask=None):
    start = datetime.fromisoformat(event["date"])
    end = now()
    if not has_time(event["date"]):
        end = end.replace(hour=0, minute=0, second=0, microsecond=0)
    if widget_format == "custom":
        tokens = parse_mask(mask)
        values = elapsed_values(start, end, {token[1] for token in tokens if token[0] == "unit"})
        text = "".join(token[1] if token[0] == "text" else str(values[token[1]]).zfill(token[2]) for token in tokens)
    else:
        preset = settings["widget_formats"][widget_format]
        text = render_units(elapsed_values(start, end, preset["units"]), preset["units"], preset["style"])
    emojis = emoji.emoji_list(event["title"])
    return f"{emojis[0]['emoji']} {text}" if emojis else text
