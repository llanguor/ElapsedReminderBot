from json_io import load_data, update_data
from settings import settings

WIDGETS_FILE = settings["widgets_file"]
DEFAULT_WIDGET = {"format": "datelong", "mask": None}


def add_widget(user_id, event_id, chat_id, message_id, widget_format, mask):
    widget = {"event_id": event_id, "chat_id": chat_id, "message_id": message_id, "format": widget_format, "mask": mask}
    update_data(WIDGETS_FILE, lambda data: data.setdefault(str(user_id), []).append(widget))


def get_all_widgets():
    return [(user_id, {**DEFAULT_WIDGET, **widget}) for user_id, widgets in load_data(WIDGETS_FILE).items() for widget in widgets]


def delete_widget(user_id, chat_id, message_id):
    def updater(data):
        widgets = data.get(str(user_id), [])
        data[str(user_id)] = [w for w in widgets if not (w["chat_id"] == chat_id and w["message_id"] == message_id)]

    update_data(WIDGETS_FILE, updater)


def pop_event_widgets(user_id, event_id):
    def updater(data):
        widgets = data.get(str(user_id), [])
        data[str(user_id)] = [w for w in widgets if w["event_id"] != event_id]
        return [w for w in widgets if w["event_id"] == event_id]

    return update_data(WIDGETS_FILE, updater)
