from datetime import datetime
from zoneinfo import ZoneInfo
from dateutil.relativedelta import relativedelta
from emoji import emoji_list
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


# The emoji the event title starts with ("" if it does not start with one).
def leading_emoji(title):
    found = emoji_list(title)
    return found[0]["emoji"] if found and found[0]["match_start"] == 0 else ""


# The emoji tag exists only in widgets (widget=True); in reminders it stays plain text.
def parse_template(text, widget=False):
    tags = settings["tags"]
    suffix = settings["word_suffix"]
    terminator = settings["tag_terminator"]
    tokens = []
    index = 0
    while index < len(text):
        char = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if char == "\\" and following == "\\":
            tokens.append(("text", "\\"))
            index += 2
        elif widget and char == "\\" and following == settings["emoji_tag"]:
            swallow = text.startswith(" ", index + 2)
            tokens.append(("emoji", swallow))
            index += 3 if swallow else 2
        elif char == "\\" and following in tags:
            unit = tags[following]
            width = 1
            while text.startswith(following, index + 1 + width):
                width += 1
            end = index + 1 + width
            if text.startswith(terminator, end):
                tokens.append(("value", unit, width))
                index = end + len(terminator)
            elif text.startswith(suffix, end):
                tokens.append(("word", unit))
                index = end + len(suffix)
            else:
                tokens.append(("value", unit, width))
                index = end
        else:
            tokens.append(("text", char))
            index += 1
    return tokens


def template_units(tokens):
    return {token[1] for token in tokens if token[0] in ("value", "word")}


# The emoji tag takes the space after it along when there is no emoji, so the text never starts with a space.
def render_template(tokens, values, emoji=""):
    parts = []
    for token in tokens:
        if token[0] == "text":
            parts.append(token[1])
        elif token[0] == "emoji":
            parts.append(emoji + " " if emoji and token[1] else emoji)
        elif token[0] == "value":
            parts.append(str(values[token[1]]).zfill(token[2]))
        else:
            parts.append(plural(values[token[1]], settings["plural_forms"][token[1]]))
    return "".join(parts)


def is_valid_mask(mask):
    return bool(template_units(parse_template(mask, widget=True)))


# Pass the event when rendering a widget: only then the emoji tag works.
def render_between(text, start, end, event=None):
    tokens = parse_template(text, widget=event is not None)
    emoji = leading_emoji(event["title"]) if event is not None else ""
    return render_template(tokens, elapsed_values(start, end, template_units(tokens)), emoji)


def format_widget(event, mask):
    start = datetime.fromisoformat(event["date"])
    end = now()
    if not has_time(event["date"]):
        end = end.replace(hour=0, minute=0, second=0, microsecond=0)
    return render_between(mask, start, end, event)
