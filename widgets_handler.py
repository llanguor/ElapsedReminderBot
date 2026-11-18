from json_io import load_data, update_data
from settings import settings
from elapsed_formatter import now

WIDGETS_FILE = settings["widgets_file"]


# The default moment of the first widget update: today, 00:00. Updates then follow every "interval" seconds.
def default_first_update():
    return now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="minutes")


def add_widget(scope, event_id, chat_id, message_id, mask, interval, first_update):
    widget = {"event_id": event_id, "chat_id": chat_id, "message_id": message_id, "mask": mask,
              "interval": interval, "first_update": first_update}
    update_data(WIDGETS_FILE, lambda data: data.setdefault(str(scope), []).append(widget))
    return widget


def get_all_widgets():
    return [(scope, widget) for scope, widgets in load_data(WIDGETS_FILE).items() for widget in widgets]


def get_widget(scope, chat_id, message_id):
    return next((w for w in load_data(WIDGETS_FILE).get(str(scope), []) if w["chat_id"] == chat_id and w["message_id"] == message_id), None)


def has_widget(chat_id, message_id):
    return any(widget["chat_id"] == chat_id and widget["message_id"] == message_id for _, widget in get_all_widgets())


def delete_widget(scope, chat_id, message_id):
    def updater(data):
        widgets = data.get(str(scope), [])
        data[str(scope)] = [w for w in widgets if not (w["chat_id"] == chat_id and w["message_id"] == message_id)]

    update_data(WIDGETS_FILE, updater)


def pop_event_widgets(scope, event_id):
    def updater(data):
        widgets = data.get(str(scope), [])
        data[str(scope)] = [w for w in widgets if w["event_id"] != event_id]
        return [w for w in widgets if w["event_id"] == event_id]

    return update_data(WIDGETS_FILE, updater)
