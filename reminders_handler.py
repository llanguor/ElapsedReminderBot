import uuid
from datetime import datetime
from dateutil.relativedelta import relativedelta
from json_io import load_data, update_data
from settings import settings

REMINDERS_FILE = settings["reminders_file"]


def next_occurrence(start, repeat, every, after):
    count = 0
    occurrence = start
    while occurrence <= after:
        count += 1
        occurrence = start + relativedelta(**{f"{repeat}s": every * count})
    return occurrence


def add_reminder(scope, text, start, repeat, every, chat_id, thread_id):
    moment = start.isoformat(timespec="minutes")
    reminder = {"id": uuid.uuid4().hex[:8], "text": text, "start": moment, "next": moment, "repeat": repeat, "every": every, "chat_id": chat_id, "thread_id": thread_id}
    update_data(REMINDERS_FILE, lambda data: data.setdefault(str(scope), []).append(reminder))


def get_reminders(scope):
    return load_data(REMINDERS_FILE).get(str(scope), [])


def get_reminder(scope, reminder_id):
    return next((reminder for reminder in get_reminders(scope) if reminder["id"] == reminder_id), None)


def get_all_reminders():
    return [(scope, reminder) for scope, reminders in load_data(REMINDERS_FILE).items() for reminder in reminders]


def delete_reminder(scope, reminder_id):
    def updater(data):
        reminders = data.get(str(scope), [])
        for reminder in reminders:
            if reminder["id"] == reminder_id:
                reminders.remove(reminder)
                return reminder
        return None

    return update_data(REMINDERS_FILE, updater)


def complete_reminder(scope, reminder_id, after):
    def updater(data):
        reminders = data.get(str(scope), [])
        for reminder in reminders:
            if reminder["id"] != reminder_id:
                continue
            if reminder["repeat"] == "once":
                reminders.remove(reminder)
            else:
                following = next_occurrence(datetime.fromisoformat(reminder["start"]), reminder["repeat"], reminder["every"], after)
                reminder["next"] = following.isoformat(timespec="minutes")
            return

    update_data(REMINDERS_FILE, updater)
