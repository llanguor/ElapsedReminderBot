import heapq
import itertools
import threading
import time
from datetime import datetime, timedelta
from telebot.apihelper import ApiTelegramException
from settings import settings
from events_handler import get_event
from widgets_handler import get_all_widgets, get_widget, delete_widget
from elapsed_formatter import format_widget, now
from telegram_errors import is_gone
from logger import get_logger

logger = get_logger()

# Every widget has its own due time. The updater thread sleeps until the earliest one (or until a new widget
# is scheduled), so nothing runs between updates and the event is read only at the moment its widget is refreshed.
condition = threading.Condition()
queue = []
counter = itertools.count()


# Seconds until the next update: updates happen at first_update + N * interval (the interval is in seconds, kept in
# the widget) by the clock of the bot's timezone, so a widget is refreshed at the same time of day (or of the week)
# every time. If first_update is still in the future, the first update waits for it.
def next_update_delay(widget):
    fallback = settings["widget_fallback_interval"]
    if widget is None:
        return fallback
    try:
        first = datetime.fromisoformat(widget["first_update"])
        interval = widget["interval"]
        if interval < 1:
            raise ValueError("interval")
    except (KeyError, TypeError, ValueError):
        logger.error(f"The widget {widget.get('message_id')} has no valid interval or first_update, retrying in {fallback} seconds")
        return fallback
    current = now()
    if first > current:
        return (first - current).total_seconds()
    steps = int((current - first).total_seconds() // interval) + 1
    return (first + timedelta(seconds=steps * interval) - current).total_seconds()


def schedule_widget(scope, chat_id, message_id, delay):
    with condition:
        heapq.heappush(queue, (time.monotonic() + delay, next(counter), scope, chat_id, message_id))
        condition.notify()


def pop_due():
    with condition:
        while True:
            if not queue:
                condition.wait()
                continue
            remaining = queue[0][0] - time.monotonic()
            if remaining <= 0:
                return heapq.heappop(queue)
            condition.wait(remaining)


def unpin_widget(bot, widget):
    try:
        bot.unpin_chat_message(widget["chat_id"], widget["message_id"])
    except ApiTelegramException:
        pass


# Returns False when the widget is gone and must not be scheduled again.
def update_widget(bot, scope, widget):
    event = get_event(scope, widget["event_id"])
    if event is None:
        delete_widget(scope, widget["chat_id"], widget["message_id"])
        unpin_widget(bot, widget)
        return False
    try:
        bot.edit_message_text(format_widget(event, widget["mask"]), widget["chat_id"], widget["message_id"])
    except ApiTelegramException as e:
        if "message is not modified" in str(e):
            return True
        if not is_gone(e):
            raise
        delete_widget(scope, widget["chat_id"], widget["message_id"])
        return False
    return True


def refresh_widget(bot, entry):
    _, _, scope, chat_id, message_id = entry
    widget = None
    alive = True
    try:
        widget = get_widget(scope, chat_id, message_id)
        alive = widget is not None and update_widget(bot, scope, widget)
    except Exception as e:
        logger.error(f"Error updating the widget {message_id} for the scope {scope}: {e}")
    if alive:
        schedule_widget(scope, chat_id, message_id, next_update_delay(widget))


def run_widget_updater(bot):
    for scope, widget in get_all_widgets():
        schedule_widget(scope, widget["chat_id"], widget["message_id"], 0)
    while True:
        try:
            refresh_widget(bot, pop_due())
        except Exception as e:
            logger.error(f"Exception in widget updater: {e}")
        time.sleep(settings["widget_update_delay"])
