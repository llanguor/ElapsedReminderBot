import uuid
from json_io import load_data, update_data
from settings import settings

EVENTS_FILE = settings["events_file"]


def add_event(user_id, title, event_date):
    event = {"id": uuid.uuid4().hex[:8], "title": title, "date": event_date}
    update_data(EVENTS_FILE, lambda data: data.setdefault(str(user_id), []).append(event))


def get_events(user_id):
    return load_data(EVENTS_FILE).get(str(user_id), [])


def get_event(user_id, event_id):
    return next((event for event in get_events(user_id) if event["id"] == event_id), None)


def delete_event(user_id, event_id):
    def updater(data):
        events = data.get(str(user_id), [])
        for event in events:
            if event["id"] == event_id:
                events.remove(event)
                return event
        return None

    return update_data(EVENTS_FILE, updater)
