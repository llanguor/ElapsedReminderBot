import re
from datetime import datetime
from settings import settings
from elapsed_formatter import elapsed_values, has_time, parse_event_date, plural

REFERENCE_PATTERN = re.compile(r"\s*\[([^\[\]\n]*)\]\s*$")


def split_reference(text):
    match = REFERENCE_PATTERN.search(text)
    if match is None:
        return text, None
    try:
        reference = datetime.fromisoformat(parse_event_date(match.group(1)))
    except ValueError:
        return text, None
    return text[:match.start()], reference


def render_reminder(text, current):
    text, reference = split_reference(text)
    if reference is None:
        return text
    tags = settings["reminder_tags"]
    result = []
    index = 0
    while index < len(text):
        char = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if char == "\\" and following == "\\":
            result.append("\\")
            index += 2
        elif char == "\\" and following in tags:
            unit = tags[following]
            result.append(str(elapsed_values(reference, current, {unit})[unit]))
            index += 2
        else:
            result.append(char)
            index += 1
    return "".join(result)


def parse_reminder_time(text):
    iso = parse_event_date(text)
    moment = datetime.fromisoformat(iso)
    if not has_time(iso):
        hour, minute = map(int, settings["reminder_default_time"].split(":"))
        moment = moment.replace(hour=hour, minute=minute)
    return moment


def describe_repeat(repeat, every):
    if repeat == "once":
        return settings["repeat_once"]
    if every == 1:
        return settings["repeat_every_one"][repeat]
    return settings["repeat_every_many"].format(n=every, unit=plural(every, settings["plural_forms"][repeat]))


def reminder_preview(reminder):
    text, _ = split_reference(reminder["text"])
    return " ".join(text.split())[:settings["preview_length"]]


def reminder_date(reminder):
    return datetime.fromisoformat(reminder["next"]).strftime(settings["datetime_format"])
