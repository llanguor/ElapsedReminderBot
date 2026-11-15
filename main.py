import functools
import threading
import time
import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException
from settings import settings
from events_handler import add_event, get_events, get_event, delete_event
from widgets_handler import add_widget, pop_event_widgets
from widget_updater import run_widget_updater, unpin_widget
from reminders_handler import add_reminder, get_reminder, get_reminders, delete_reminder
from reminder_sender import run_reminder_sender
from reminder_formatter import describe_repeat, parse_reminder_time, reminder_date, reminder_preview, split_reference
from elapsed_formatter import now, parse_event_date, is_valid_mask, describe_elapsed, describe_in_unit, format_widget
from logger import get_logger

logger = get_logger()
messages = settings["messages"]
buttons = settings["buttons"]


def thread_of(message):
    return getattr(message, "message_thread_id", None) if getattr(message, "is_topic_message", None) else None


def message_context(message):
    return message.chat.id, message.from_user.id, thread_of(message)


def call_context(call):
    return call.message.chat.id, call.from_user.id, thread_of(call.message)


def update_context(update):
    return call_context(update) if isinstance(update, types.CallbackQuery) else message_context(update)


def register_handlers(bot):
    pending = {}
    drafts = {}

    def send(ctx, text, keyboard=None):
        chat_id, _, thread_id = ctx
        return bot.send_message(chat_id, text, reply_markup=keyboard, message_thread_id=thread_id, disable_notification=True)

    def edit(message, text, keyboard=None):
        try:
            bot.edit_message_text(text, message.chat.id, message.message_id, reply_markup=keyboard)
        except ApiTelegramException as e:
            if "message is not modified" not in str(e):
                raise

    def safe_handler(func):
        @functools.wraps(func)
        def wrapper(update):
            try:
                logger.info(f"User with id {update.from_user.id} called {func.__name__}")
                func(update)
            except Exception as e:
                logger.error(f"Error in the {func.__name__} function for the user with ID {update.from_user.id}: {e}")
                send(update_context(update), messages["error"])
        return wrapper

    def begin(call):
        bot.answer_callback_query(call.id)
        ctx = call_context(call)
        pending.pop(ctx, None)
        return ctx

    def expect(ctx, handler, *args):
        pending[ctx] = (handler, args)

    def ask(ctx, text, handler, *args):
        expect(ctx, handler, *args)
        send(ctx, text)

    def choice_keyboard(options, prefix, suffix=""):
        keyboard = types.InlineKeyboardMarkup(row_width=1)
        for key, label in options.items():
            keyboard.add(types.InlineKeyboardButton(text=label, callback_data=f"{prefix}{key}{suffix}"))
        return keyboard

    def icon_button(icon, callback_data):
        return types.InlineKeyboardButton(text=buttons[icon], callback_data=callback_data)

    # A route says where the back button leads: "" - nowhere (no button), "s" - to the start menu,
    # "l" + route - to the list that was opened with that route. "d" marks the delete-selection screen.
    def pack(route):
        return route or "-"

    def unpack(token):
        return "" if token == "-" else token

    def list_route(route):
        if route.startswith("l"):
            return route[1:]
        return "" if route == "d" else route

    def back_row(route, kind):
        if route.startswith("s"):
            return [icon_button("back_icon", "nav_start")]
        if route.startswith("l"):
            return [icon_button("back_icon", f"nav_{kind}_{pack(route[1:])}")]
        return []

    def items_keyboard(items, prefix, bottom_row=None):
        keyboard = types.InlineKeyboardMarkup(row_width=1)
        for label, item_id in items:
            keyboard.add(types.InlineKeyboardButton(text=label, callback_data=f"{prefix}{item_id}"))
        if bottom_row:
            keyboard.row(*bottom_row)
        return keyboard

    def empty_screen(text, label, callback_data, back):
        keyboard = types.InlineKeyboardMarkup(row_width=1)
        keyboard.add(types.InlineKeyboardButton(text=buttons[label], callback_data=callback_data))
        if back:
            keyboard.row(*back)
        return text, keyboard

    def with_note(note, screen):
        text, keyboard = screen
        return f"{note.rstrip()}\n{text}", keyboard

    def confirm_screen(text, yes_data, no_data):
        keyboard = types.InlineKeyboardMarkup(row_width=2)
        keyboard.row(types.InlineKeyboardButton(text=buttons["confirm_no"], callback_data=no_data),
                     types.InlineKeyboardButton(text=buttons["confirm_yes"], callback_data=yes_data))
        return text, keyboard

    def start_screen():
        keyboard = types.InlineKeyboardMarkup(row_width=1)
        keyboard.add(
            types.InlineKeyboardButton(text=buttons["events"], callback_data="menu_events"),
            types.InlineKeyboardButton(text=buttons["reminders"], callback_data="menu_reminds"))
        return messages["start"], keyboard

    def events_empty_screen(route=""):
        return empty_screen(messages["events_empty"], "create_event", f"menu_addevent_{pack(route)}", back_row(route, "events"))

    def events_screen(user_id, route=""):
        events = get_events(user_id)
        if not events:
            return events_empty_screen(route)
        bottom_row = back_row(route, "events") + [icon_button("create_icon", f"menu_addevent_{pack(route)}")]
        return messages["events_list"], items_keyboard([(event["title"], event["id"]) for event in events], f"event_{pack('l' + route)}_", bottom_row)

    def events_delete_screen(user_id):
        events = get_events(user_id)
        if not events:
            return events_empty_screen()
        bottom_row = [icon_button("back_icon", "nav_events_-")]
        return messages["delete_prompt"], items_keyboard([(event["title"], event["id"]) for event in events], "delask_d_", bottom_row)

    def event_keyboard(event, route):
        keyboard = types.InlineKeyboardMarkup(row_width=1)
        for key, label in buttons["units"].items():
            keyboard.add(types.InlineKeyboardButton(text=label, callback_data=f"unit_{key}_{pack(route)}_{event['id']}"))
        keyboard.row(*back_row(route, "events"), icon_button("delete_icon", f"delask_{pack(route)}_{event['id']}"))
        return keyboard

    def event_screen(event, route=""):
        return messages["event_info"].format(title=event["title"], elapsed=describe_elapsed(event["date"])), event_keyboard(event, route)

    def reminder_items(user_id):
        return [(f"{reminder_preview(reminder)} — {reminder_date(reminder)}", reminder["id"]) for reminder in get_reminders(user_id)]

    def reminders_empty_screen(route=""):
        return empty_screen(messages["reminds_empty"], "create_remind", f"menu_addremind_{pack(route)}", back_row(route, "reminds"))

    def reminders_screen(user_id, route=""):
        items = reminder_items(user_id)
        if not items:
            return reminders_empty_screen(route)
        bottom_row = back_row(route, "reminds") + [icon_button("create_icon", f"menu_addremind_{pack(route)}")]
        return messages["reminds_list"], items_keyboard(items, f"remind_{pack('l' + route)}_", bottom_row)

    def reminders_delete_screen(user_id):
        items = reminder_items(user_id)
        if not items:
            return reminders_empty_screen()
        return messages["remind_delete_prompt"], items_keyboard(items, "rdelask_d_", [icon_button("back_icon", "nav_reminds_-")])

    def reminder_screen(reminder, route=""):
        text = messages["remind_info"].format(text=reminder["text"], date=reminder_date(reminder), repeat=describe_repeat(reminder["repeat"], reminder["every"]))
        keyboard = types.InlineKeyboardMarkup(row_width=1)
        keyboard.row(*back_row(route, "reminds"), icon_button("delete_icon", f"rdelask_{pack(route)}_{reminder['id']}"))
        return text, keyboard

    def create_widget(ctx, event, widget_format, mask):
        chat_id, user_id, _ = ctx
        sent = send(ctx, format_widget(event, widget_format, mask))
        add_widget(user_id, event["id"], chat_id, sent.message_id, widget_format, mask)
        try:
            bot.pin_chat_message(chat_id, sent.message_id, disable_notification=True)
        except ApiTelegramException as e:
            logger.error(f"Failed to pin the widget in chat {chat_id}: {e}")
            send(ctx, messages["pin_failed"])

    def save_reminder(ctx, draft, repeat, every):
        chat_id, user_id, thread_id = ctx
        add_reminder(user_id, draft["text"], draft["start"], repeat, every, chat_id, thread_id)
        drafts.pop(ctx, None)
        date = draft["start"].strftime(settings["datetime_format"])
        note = messages["remind_added"].format(date=date, repeat=describe_repeat(repeat, every))
        return with_note(note, reminders_screen(user_id, draft.get("route", "")))

    def receive_title(message, route=""):
        ctx = message_context(message)
        title = (message.text or "").strip()
        if title and len(title) <= settings["max_title_length"]:
            ask(ctx, messages["ask_date"], receive_date, title, route)
        else:
            ask(ctx, messages["invalid_title"].format(max_length=settings["max_title_length"]), receive_title, route)

    def receive_date(message, title, route=""):
        ctx = message_context(message)
        try:
            event_date = parse_event_date(message.text or "")
        except ValueError:
            ask(ctx, messages["invalid_date"], receive_date, title, route)
            return
        add_event(ctx[1], title, event_date)
        send(ctx, *with_note(messages["event_added"].format(title=title), events_screen(ctx[1], route)))

    def receive_widget_mask(message, event_id):
        ctx = message_context(message)
        event = get_event(ctx[1], event_id)
        if event is None:
            send(ctx, messages["event_not_found"])
            return
        mask = (message.text or "").strip()
        if len(mask) > settings["max_mask_length"] or not is_valid_mask(mask):
            ask(ctx, messages["invalid_mask"].format(max_length=settings["max_mask_length"]), receive_widget_mask, event_id)
            return
        create_widget(ctx, event, "custom", mask)

    def receive_remind_text(message, route=""):
        ctx = message_context(message)
        text = (message.text or "").strip()
        if not split_reference(text)[0].strip() or len(text) > settings["max_reminder_length"]:
            ask(ctx, messages["invalid_remind_text"].format(max_length=settings["max_reminder_length"]), receive_remind_text, route)
            return
        ask(ctx, messages["remind_ask_date"].format(time=settings["reminder_default_time"]), receive_remind_date, text, route)

    def receive_remind_date(message, text, route=""):
        ctx = message_context(message)
        try:
            start = parse_reminder_time(message.text or "")
        except ValueError:
            ask(ctx, messages["remind_invalid_date"], receive_remind_date, text, route)
            return
        if start <= now():
            ask(ctx, messages["remind_past_date"], receive_remind_date, text, route)
            return
        drafts[ctx] = {"text": text, "start": start, "route": route}
        send(ctx, messages["remind_repeat_prompt"], choice_keyboard(buttons["repeat"], "rrepeat_"))

    def receive_remind_every(message, repeat):
        ctx = message_context(message)
        draft = drafts.get(ctx)
        if draft is None:
            send(ctx, messages["remind_expired"])
            return
        number = (message.text or "").strip()
        if not (number.isascii() and number.isdigit() and 1 <= int(number) <= settings["max_repeat_every"]):
            ask(ctx, messages["remind_invalid_number"].format(max_value=settings["max_repeat_every"]), receive_remind_every, repeat)
            return
        send(ctx, *save_reminder(ctx, draft, repeat, int(number)))

    @bot.message_handler(func=lambda message: message.from_user is not None and message_context(message) in pending)
    @safe_handler
    def handle_pending(message):
        entry = pending.pop(message_context(message), None)
        if entry is None:
            return
        if message.text and message.text.startswith("/"):
            bot.process_new_messages([message])
            return
        handler, args = entry
        handler(message, *args)

    @bot.message_handler(commands=['start'])
    @safe_handler
    def start(message):
        send(message_context(message), *start_screen())

    @bot.message_handler(commands=['addevent'])
    @safe_handler
    def addevent(message):
        ask(message_context(message), messages["ask_title"], receive_title)

    @bot.message_handler(commands=['events'])
    @safe_handler
    def events(message):
        ctx = message_context(message)
        send(ctx, *events_screen(ctx[1]))

    @bot.message_handler(commands=['delevent'])
    @safe_handler
    def delevent(message):
        ctx = message_context(message)
        send(ctx, *events_delete_screen(ctx[1]))

    @bot.message_handler(commands=['widget'])
    @safe_handler
    def widget(message):
        ctx = message_context(message)
        events = get_events(ctx[1])
        if events:
            send(ctx, messages["widget_prompt"], items_keyboard([(event["title"], event["id"]) for event in events], "widget_"))
        else:
            send(ctx, *events_empty_screen())

    @bot.message_handler(commands=['setremind'])
    @safe_handler
    def setremind(message):
        ask(message_context(message), messages["remind_ask_text"], receive_remind_text)

    @bot.message_handler(commands=['reminds'])
    @safe_handler
    def reminds(message):
        ctx = message_context(message)
        send(ctx, *reminders_screen(ctx[1]))

    @bot.message_handler(commands=['delremind'])
    @safe_handler
    def delremind(message):
        ctx = message_context(message)
        send(ctx, *reminders_delete_screen(ctx[1]))

    @bot.callback_query_handler(func=lambda call: call.data.startswith("menu_"))
    @safe_handler
    def handle_menu(call):
        ctx = begin(call)
        parts = call.data.split("_", 2)
        action = parts[1]
        route = unpack(parts[2]) if len(parts) > 2 else ""
        if action == "addevent":
            ask(ctx, messages["ask_title"], receive_title, route)
        elif action == "events":
            edit(call.message, *events_screen(ctx[1], "s"))
        elif action == "reminds":
            edit(call.message, *reminders_screen(ctx[1], "s"))
        elif action == "addremind":
            ask(ctx, messages["remind_ask_text"], receive_remind_text, route)

    @bot.callback_query_handler(func=lambda call: call.data.startswith("nav_"))
    @safe_handler
    def handle_nav(call):
        ctx = begin(call)
        parts = call.data.split("_", 2)
        route = unpack(parts[2]) if len(parts) > 2 else ""
        screens = {
            "start": start_screen,
            "events": lambda: events_screen(ctx[1], route),
            "reminds": lambda: reminders_screen(ctx[1], route),
            "delevents": lambda: events_delete_screen(ctx[1]),
            "delreminds": lambda: reminders_delete_screen(ctx[1])}
        edit(call.message, *screens[parts[1]]())

    @bot.callback_query_handler(func=lambda call: call.data.startswith("event_"))
    @safe_handler
    def handle_event(call):
        ctx = begin(call)
        _, token, event_id = call.data.split("_", 2)
        route = unpack(token)
        event = get_event(ctx[1], event_id)
        if event is None:
            edit(call.message, *with_note(messages["event_not_found"], events_screen(ctx[1], list_route(route))))
            return
        edit(call.message, *event_screen(event, route))

    @bot.callback_query_handler(func=lambda call: call.data.startswith("unit_"))
    @safe_handler
    def handle_unit(call):
        ctx = begin(call)
        _, unit, token, event_id = call.data.split("_", 3)
        route = unpack(token)
        event = get_event(ctx[1], event_id)
        if event is None:
            edit(call.message, *with_note(messages["event_not_found"], events_screen(ctx[1], list_route(route))))
            return
        text = messages["unit_result"].format(title=event["title"], value=describe_in_unit(event["date"], unit))
        edit(call.message, text, event_keyboard(event, route))

    @bot.callback_query_handler(func=lambda call: call.data.startswith("widget_"))
    @safe_handler
    def handle_widget_event(call):
        ctx = begin(call)
        event = get_event(ctx[1], call.data[len("widget_"):])
        if event is None:
            send(ctx, messages["event_not_found"])
            return
        keyboard = types.InlineKeyboardMarkup(row_width=1)
        for key in settings["widget_formats"]:
            keyboard.add(types.InlineKeyboardButton(text=format_widget(event, key), callback_data=f"wfmt_{key}_{event['id']}"))
        keyboard.add(types.InlineKeyboardButton(text=buttons["custom_format"], callback_data=f"wfmt_custom_{event['id']}"))
        edit(call.message, messages["widget_format_prompt"], keyboard)

    @bot.callback_query_handler(func=lambda call: call.data.startswith("wfmt_"))
    @safe_handler
    def handle_widget_format(call):
        ctx = begin(call)
        _, widget_format, event_id = call.data.split("_", 2)
        event = get_event(ctx[1], event_id)
        if event is None:
            send(ctx, messages["event_not_found"])
            return
        if widget_format == "custom":
            edit(call.message, messages["widget_mask_prompt"])
            expect(ctx, receive_widget_mask, event_id)
            return
        create_widget(ctx, event, widget_format, None)
        edit(call.message, messages["widget_created"])

    @bot.callback_query_handler(func=lambda call: call.data.startswith("rrepeat_"))
    @safe_handler
    def handle_remind_repeat(call):
        ctx = begin(call)
        draft = drafts.get(ctx)
        if draft is None:
            send(ctx, messages["remind_expired"])
            return
        repeat = call.data[len("rrepeat_"):]
        if repeat == "once":
            edit(call.message, *save_reminder(ctx, draft, repeat, 1))
        else:
            edit(call.message, messages["remind_every_prompt"][repeat])
            expect(ctx, receive_remind_every, repeat)

    @bot.callback_query_handler(func=lambda call: call.data.startswith("remind_"))
    @safe_handler
    def handle_remind(call):
        ctx = begin(call)
        _, token, reminder_id = call.data.split("_", 2)
        route = unpack(token)
        reminder = get_reminder(ctx[1], reminder_id)
        if reminder is None:
            edit(call.message, *with_note(messages["remind_not_found"], reminders_screen(ctx[1], list_route(route))))
            return
        edit(call.message, *reminder_screen(reminder, route))

    @bot.callback_query_handler(func=lambda call: call.data.startswith("rdelask_"))
    @safe_handler
    def handle_remind_delete_ask(call):
        ctx = begin(call)
        _, token, reminder_id = call.data.split("_", 2)
        reminder = get_reminder(ctx[1], reminder_id)
        if reminder is None:
            edit(call.message, *with_note(messages["remind_not_found"], reminders_screen(ctx[1], list_route(unpack(token)))))
            return
        cancel = "nav_delreminds" if token == "d" else f"remind_{token}_{reminder_id}"
        text = messages["remind_delete_confirm"].format(text=reminder_preview(reminder), date=reminder_date(reminder))
        edit(call.message, *confirm_screen(text, f"rdel_{token}_{reminder_id}", cancel))

    @bot.callback_query_handler(func=lambda call: call.data.startswith("rdel_"))
    @safe_handler
    def handle_remind_delete(call):
        ctx = begin(call)
        _, token, reminder_id = call.data.split("_", 2)
        reminder = delete_reminder(ctx[1], reminder_id)
        note = messages["remind_deleted"] if reminder else messages["remind_not_found"]
        edit(call.message, *with_note(note, reminders_screen(ctx[1], list_route(unpack(token)))))

    @bot.callback_query_handler(func=lambda call: call.data.startswith("delask_"))
    @safe_handler
    def handle_delete_ask(call):
        ctx = begin(call)
        _, token, event_id = call.data.split("_", 2)
        event = get_event(ctx[1], event_id)
        if event is None:
            edit(call.message, *with_note(messages["event_not_found"], events_screen(ctx[1], list_route(unpack(token)))))
            return
        cancel = "nav_delevents" if token == "d" else f"event_{token}_{event_id}"
        edit(call.message, *confirm_screen(messages["delete_confirm"].format(title=event["title"]), f"delete_{token}_{event_id}", cancel))

    @bot.callback_query_handler(func=lambda call: call.data.startswith("delete_"))
    @safe_handler
    def handle_delete(call):
        ctx = begin(call)
        _, token, event_id = call.data.split("_", 2)
        route = list_route(unpack(token))
        event = delete_event(ctx[1], event_id)
        if event is None:
            edit(call.message, *with_note(messages["event_not_found"], events_screen(ctx[1], route)))
            return
        for pinned in pop_event_widgets(ctx[1], event["id"]):
            unpin_widget(bot, pinned)
        edit(call.message, *with_note(messages["event_deleted"].format(title=event["title"]), events_screen(ctx[1], route)))


def run_polling(bot):
    while True:
        try:
            bot.polling(non_stop=True, interval=settings["polling_interval"])
        except Exception as e:
            logger.error(f"Exception in polling: {e}")
            time.sleep(settings["polling_retry_delay"])


if __name__ == "__main__":
    if not settings.get("tg_token"):
        logger.error("tg_token is not set. Add it to appsettings.local.json")
        raise SystemExit(1)
    bot = telebot.TeleBot(settings["tg_token"], num_threads=settings["worker_threads"])
    register_handlers(bot)
    threading.Thread(target=run_widget_updater, args=(bot,), daemon=True).start()
    threading.Thread(target=run_reminder_sender, args=(bot,), daemon=True).start()
    logger.info("Bot started")
    run_polling(bot)
