import time
from datetime import datetime
from telebot.apihelper import ApiTelegramException
from settings import settings
from reminders_handler import get_all_reminders, delete_reminder, complete_reminder
from reminder_formatter import render_reminder
from elapsed_formatter import now
from telegram_errors import is_gone
from logger import get_logger

logger = get_logger()


def send_due_reminders(bot):
    current = now()
    for user_id, reminder in get_all_reminders():
        if datetime.fromisoformat(reminder["next"]) > current:
            continue
        try:
            bot.send_message(reminder["chat_id"], render_reminder(reminder["text"], current), message_thread_id=reminder["thread_id"])
            complete_reminder(user_id, reminder["id"], current)
        except Exception as e:
            logger.error(f"Error sending the reminder {reminder['id']} for the user with ID {user_id}: {e}")
            if isinstance(e, ApiTelegramException) and is_gone(e):
                delete_reminder(user_id, reminder["id"])


def run_reminder_sender(bot):
    while True:
        try:
            send_due_reminders(bot)
        except Exception as e:
            logger.error(f"Exception in reminder sender: {e}")
        time.sleep(settings["reminder_check_interval"])
