import time
from datetime import datetime, timedelta
from telebot.apihelper import ApiTelegramException
from settings import settings
from events_handler import get_event
from widgets_handler import get_all_widgets, delete_widget
from elapsed_formatter import timezone, format_widget
from telegram_errors import is_gone
from logger import get_logger

logger = get_logger()


def seconds_until_next_run():
    hour, minute = map(int, settings["widget_update_time"].split(":"))
    current = datetime.now(timezone)
    target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= current:
        target += timedelta(days=1)
    return target.timestamp() - current.timestamp()


def unpin_widget(bot, widget):
    try:
        bot.unpin_chat_message(widget["chat_id"], widget["message_id"])
    except ApiTelegramException:
        pass


def update_widget(bot, user_id, widget):
    event = get_event(user_id, widget["event_id"])
    if event is None:
        delete_widget(user_id, widget["chat_id"], widget["message_id"])
        unpin_widget(bot, widget)
        return
    try:
        bot.edit_message_text(format_widget(event, widget["format"], widget["mask"]), widget["chat_id"], widget["message_id"])
    except ApiTelegramException as e:
        if "message is not modified" in str(e):
            return
        if not is_gone(e):
            raise
        delete_widget(user_id, widget["chat_id"], widget["message_id"])


def update_all_widgets(bot):
    for user_id, widget in get_all_widgets():
        try:
            update_widget(bot, user_id, widget)
        except Exception as e:
            logger.error(f"Error updating the widget {widget['message_id']} for the user with ID {user_id}: {e}")
        time.sleep(settings["widget_update_delay"])


def run_widget_updater(bot):
    while True:
        try:
            update_all_widgets(bot)
        except Exception as e:
            logger.error(f"Exception in widget updater: {e}")
        time.sleep(seconds_until_next_run())
