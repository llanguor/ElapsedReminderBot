import functools
import threading
import time
import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException
from settings import settings
from events_handler import add_event, get_events, get_event, delete_event
from widgets_handler import add_widget, default_first_update, has_widget, pop_event_widgets
from widget_updater import run_widget_updater, schedule_widget, next_update_delay, unpin_widget
from reminders_handler import add_reminder, get_reminder, get_reminders, delete_reminder
from reminder_sender import run_reminder_sender
from reminder_formatter import describe_repeat, parse_reminder_time, reminder_date, reminder_preview, split_reference
from elapsed_formatter import now, parse_event_date, is_valid_mask, describe_elapsed, describe_in_unit, format_widget
from logger import get_logger
from scope import scope_key

logger = get_logger()
messages = settings["messages"]
buttons = settings["buttons"]
PICK_TOKEN = "w"


def thread_of(message):
    return getattr(message, "message_thread_id", None) if getattr(message, "is_topic_message", None) else None


def message_context(message):
    return message.chat.id, message.from_user.id, thread_of(message)


def call_context(call):
    return call.message.chat.id, call.from_user.id, thread_of(call.message)


def update_context(update):
    return call_context(update) if isinstance(update, types.CallbackQuery) else message_context(update)


# Data (events, widgets, reminders) belongs to a chat or, in a group with topics, to a topic - not to a user.
def scope_of(ctx):
    return scope_key(ctx[0], ctx[2])


def register_handlers(bot):
    pending = {}
    drafts = {}
    widget_drafts = {}

    def send(ctx, text, keyboard=None, silent=True):
        chat_id, _, thread_id = ctx
        return bot.send_message(chat_id, text, reply_markup=keyboard, message_thread_id=thread_id, disable_notification=silent)

    # Messages that ask for data while an event or a reminder is being set up.
    def send_setup(ctx, text, keyboard=None):
        return send(ctx, text, keyboard, silent=settings["silent_setup_messages"])

    # Messages that already carry information: the start menu and the lists of events and reminders.
    def send_info(ctx, text, keyboard=None):
        return send(ctx, text, keyboard, silent=settings["silent_info_messages"])

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
        send_setup(ctx, text)

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

    def events_screen(scope, route=""):
        events = get_events(scope)
        if not events:
            return events_empty_screen(route)
        bottom_row = back_row(route, "events") + [icon_button("create_icon", f"menu_addevent_{pack(route)}")]
        return messages["events_list"], items_keyboard([(event["title"], event["id"]) for event in events], f"event_{pack('l' + route)}_", bottom_row)

    def events_delete_screen(scope):
        events = get_events(scope)
        if not events:
            return events_empty_screen()
        bottom_row = [icon_button("back_icon", "nav_events_-")]
        return messages["delete_prompt"], items_keyboard([(event["title"], event["id"]) for event in events], "delask_d_", bottom_row)

    def event_keyboard(event, route):
        keyboard = types.InlineKeyboardMarkup(row_width=1)
        for key, label in buttons["units"].items():
            keyboard.add(types.InlineKeyboardButton(text=label, callback_data=f"unit_{key}_{pack(route)}_{event['id']}"))
        keyboard.row(*back_row(route, "events"), icon_button("widget_icon", f"widget_{pack(route)}_{event['id']}"), icon_button("delete_icon", f"delask_{pack(route)}_{event['id']}"))
        return keyboard

    def event_screen(event, route=""):
        return messages["event_info"].format(title=event["title"], elapsed=describe_elapsed(event["date"])), event_keyboard(event, route)

    def reminder_items(scope):
        return [(f"{reminder_preview(reminder)} — {reminder_date(reminder)}", reminder["id"]) for reminder in get_reminders(scope)]

    def reminders_empty_screen(route=""):
        return empty_screen(messages["reminds_empty"], "create_remind", f"menu_addremind_{pack(route)}", back_row(route, "reminds"))

    def reminders_screen(scope, route=""):
        items = reminder_items(scope)
        if not items:
            return reminders_empty_screen(route)
        bottom_row = back_row(route, "reminds") + [icon_button("create_icon", f"menu_addremind_{pack(route)}")]
        return messages["reminds_list"], items_keyboard(items, f"remind_{pack('l' + route)}_", bottom_row)

    def reminders_delete_screen(scope):
        items = reminder_items(scope)
        if not items:
            return reminders_empty_screen()
        return messages["remind_delete_prompt"], items_keyboard(items, "rdelask_d_", [icon_button("back_icon", "nav_reminds_-")])

    def reminder_screen(reminder, route=""):
        text = messages["remind_info"].format(text=reminder["text"], date=reminder_date(reminder), repeat=describe_repeat(reminder["repeat"], reminder["every"]))
        keyboard = types.InlineKeyboardMarkup(row_width=1)
        keyboard.row(*back_row(route, "reminds"), icon_button("delete_icon", f"rdelask_{pack(route)}_{reminder['id']}"))
        return text, keyboard

    def widget_pick_screen(scope):
        events = get_events(scope)
        if not events:
            return events_empty_screen()
        return messages["widget_prompt"], items_keyboard([(event["title"], f"{PICK_TOKEN}_{event['id']}") for event in events], "widget_")

    # The token of the widget screens says where their back arrow leads: PICK_TOKEN - to the event list of
    # the /widget command, otherwise - to the event screen that was opened with that route.
    def widget_back(token, event_id):
        return "nav_widgets" if token == PICK_TOKEN else f"event_{token}_{event_id}"

    def widget_return_screen(scope, token):
        return widget_pick_screen(scope) if token == PICK_TOKEN else events_screen(scope, list_route(unpack(token)))

    def widget_format_screen(event, token):
        keyboard = types.InlineKeyboardMarkup(row_width=1)
        for index, mask in enumerate(settings["widget_format_masks"]):
            keyboard.add(types.InlineKeyboardButton(text=format_widget(event, mask), callback_data=f"wfmt_{index}_{token}_{event['id']}"))
        keyboard.add(types.InlineKeyboardButton(text=buttons["custom_format"], callback_data=f"wfmt_custom_{token}_{event['id']}"))
        keyboard.row(icon_button("back_icon", widget_back(token, event["id"])))
        return messages["widget_format_prompt"], keyboard

    def create_widget(ctx, event, mask, interval, first_update):
        chat_id = ctx[0]
        scope = scope_of(ctx)
        sent = send(ctx, format_widget(event, mask), silent=settings["silent_widget"])
        widget = add_widget(scope, event["id"], chat_id, sent.message_id, mask, interval, first_update)
        schedule_widget(scope, chat_id, sent.message_id, next_update_delay(widget))
        try:
            bot.pin_chat_message(chat_id, sent.message_id, disable_notification=settings["silent_pin"])
        except ApiTelegramException as e:
            logger.error(f"Failed to pin the widget in chat {chat_id}: {e}")
            send(ctx, messages["pin_failed"])

    def finish_widget_creation(ctx):
        if settings["notify_widget_created"]:
            send(ctx, messages["widget_created"], silent=settings["silent_widget_created"])

    # The widget setup (format, update period, first update) goes on in the main window - the message with the menu -
    # as long as only buttons are pressed (draft["window"]). Once a text has to be typed, the window goes back to the
    # list and the remaining questions are sent as new messages.
    def period_screen(draft):
        keyboard = types.InlineKeyboardMarkup(row_width=1)
        for key, label in buttons["periods"].items():
            keyboard.add(types.InlineKeyboardButton(text=label, callback_data=f"wper_{key}"))
        keyboard.add(types.InlineKeyboardButton(text=buttons["custom_period"], callback_data="wper_custom"))
        if draft["window"]:
            keyboard.row(icon_button("back_icon", f"widget_{draft['token']}_{draft['event_id']}"))
        return messages["widget_period_prompt"], keyboard

    def first_update_screen(draft):
        keyboard = types.InlineKeyboardMarkup(row_width=1)
        keyboard.add(
            types.InlineKeyboardButton(text=buttons["first_update_today"], callback_data="wtime_today"),
            types.InlineKeyboardButton(text=buttons["first_update_custom"], callback_data="wtime_custom"))
        if draft["window"]:
            keyboard.row(icon_button("back_icon", "wper_back"))
        return messages["widget_first_update_prompt"], keyboard

    def show_step(ctx, draft, screen):
        if draft["window"]:
            edit(draft["window"], *screen)
        else:
            send_setup(ctx, *screen)

    def leave_window(ctx, draft):
        if draft["window"]:
            edit(draft["window"], *widget_return_screen(scope_of(ctx), draft["token"]))
            draft["window"] = None

    # A question that was sent as a separate message is answered with its buttons, so they are taken off.
    def close_buttons(message):
        try:
            bot.edit_message_reply_markup(message.chat.id, message.message_id)
        except ApiTelegramException:
            pass

    def finish_widget_setup(ctx, draft, first_update):
        widget_drafts.pop(ctx, None)
        scope = scope_of(ctx)
        event = get_event(scope, draft["event_id"])
        if event is None:
            if draft["window"]:
                edit(draft["window"], *with_note(messages["event_not_found"], widget_return_screen(scope, draft["token"])))
            else:
                send(ctx, messages["event_not_found"])
            return
        create_widget(ctx, event, draft["mask"], draft["interval"], first_update)
        if draft["window"]:
            edit(draft["window"], *widget_return_screen(scope, draft["token"]))
        finish_widget_creation(ctx)

    def save_reminder(ctx, draft, repeat, every):
        chat_id, _, thread_id = ctx
        scope = scope_of(ctx)
        add_reminder(scope, draft["text"], draft["start"], repeat, every, chat_id, thread_id)
        drafts.pop(ctx, None)
        date = draft["start"].strftime(settings["datetime_format"])
        note = messages["remind_added"].format(date=date, repeat=describe_repeat(repeat, every))
        return with_note(note, reminders_screen(scope, draft.get("route", "")))

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
        add_event(scope_of(ctx), title, event_date)
        send_info(ctx, *with_note(messages["event_added"].format(title=title), events_screen(scope_of(ctx), route)))

    def receive_widget_mask(message, event_id, token):
        ctx = message_context(message)
        event = get_event(scope_of(ctx), event_id)
        if event is None:
            send(ctx, messages["event_not_found"])
            return
        mask = (message.text or "").strip()
        if len(mask) > settings["max_mask_length"] or not is_valid_mask(mask):
            ask(ctx, messages["invalid_mask"].format(max_length=settings["max_mask_length"]), receive_widget_mask, event_id, token)
            return
        draft = {"event_id": event_id, "token": token, "mask": mask, "window": None}
        widget_drafts[ctx] = draft
        show_step(ctx, draft, period_screen(draft))

    def receive_widget_interval(message):
        ctx = message_context(message)
        draft = widget_drafts.get(ctx)
        if draft is None:
            send(ctx, messages["widget_expired"])
            return
        number = (message.text or "").strip()
        if not (number.isascii() and number.isdigit() and 1 <= int(number) <= settings["max_widget_interval"]):
            ask(ctx, messages["widget_invalid_interval"].format(max_value=settings["max_widget_interval"]), receive_widget_interval)
            return
        draft["interval"] = int(number)
        show_step(ctx, draft, first_update_screen(draft))

    def receive_widget_first_update(message):
        ctx = message_context(message)
        draft = widget_drafts.get(ctx)
        if draft is None:
            send(ctx, messages["widget_expired"])
            return
        try:
            moment = parse_reminder_time(message.text or "")
        except ValueError:
            ask(ctx, messages["widget_invalid_time"], receive_widget_first_update)
            return
        finish_widget_setup(ctx, draft, moment.isoformat(timespec="minutes"))

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
        send_setup(ctx, messages["remind_repeat_prompt"], choice_keyboard(buttons["repeat"], "rrepeat_"))

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
        send_info(ctx, *save_reminder(ctx, draft, repeat, int(number)))

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

    @bot.message_handler(content_types=['pinned_message'])
    def handle_pin_service_message(message):
        if not settings["delete_pin_message"] or message.pinned_message is None:
            return
        try:
            if has_widget(message.chat.id, message.pinned_message.message_id):
                bot.delete_message(message.chat.id, message.message_id)
        except Exception as e:
            logger.error(f"Failed to delete the pin notice in chat {message.chat.id}: {e}")

    @bot.message_handler(commands=['start'])
    @safe_handler
    def start(message):
        send_info(message_context(message), *start_screen())

    @bot.message_handler(commands=['addevent'])
    @safe_handler
    def addevent(message):
        ask(message_context(message), messages["ask_title"], receive_title)

    @bot.message_handler(commands=['events'])
    @safe_handler
    def events(message):
        ctx = message_context(message)
        send_info(ctx, *events_screen(scope_of(ctx)))

    @bot.message_handler(commands=['delevent'])
    @safe_handler
    def delevent(message):
        ctx = message_context(message)
        send_info(ctx, *events_delete_screen(scope_of(ctx)))

    @bot.message_handler(commands=['widget'])
    @safe_handler
    def widget(message):
        ctx = message_context(message)
        send_info(ctx, *widget_pick_screen(scope_of(ctx)))

    @bot.message_handler(commands=['setremind'])
    @safe_handler
    def setremind(message):
        ask(message_context(message), messages["remind_ask_text"], receive_remind_text)

    @bot.message_handler(commands=['reminds'])
    @safe_handler
    def reminds(message):
        ctx = message_context(message)
        send_info(ctx, *reminders_screen(scope_of(ctx)))

    @bot.message_handler(commands=['delremind'])
    @safe_handler
    def delremind(message):
        ctx = message_context(message)
        send_info(ctx, *reminders_delete_screen(scope_of(ctx)))

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
            edit(call.message, *events_screen(scope_of(ctx), "s"))
        elif action == "reminds":
            edit(call.message, *reminders_screen(scope_of(ctx), "s"))
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
            "events": lambda: events_screen(scope_of(ctx), route),
            "reminds": lambda: reminders_screen(scope_of(ctx), route),
            "delevents": lambda: events_delete_screen(scope_of(ctx)),
            "delreminds": lambda: reminders_delete_screen(scope_of(ctx)),
            "widgets": lambda: widget_pick_screen(scope_of(ctx))}
        edit(call.message, *screens[parts[1]]())

    @bot.callback_query_handler(func=lambda call: call.data.startswith("event_"))
    @safe_handler
    def handle_event(call):
        ctx = begin(call)
        _, token, event_id = call.data.split("_", 2)
        route = unpack(token)
        event = get_event(scope_of(ctx), event_id)
        if event is None:
            edit(call.message, *with_note(messages["event_not_found"], events_screen(scope_of(ctx), list_route(route))))
            return
        edit(call.message, *event_screen(event, route))

    @bot.callback_query_handler(func=lambda call: call.data.startswith("unit_"))
    @safe_handler
    def handle_unit(call):
        ctx = begin(call)
        _, unit, token, event_id = call.data.split("_", 3)
        route = unpack(token)
        event = get_event(scope_of(ctx), event_id)
        if event is None:
            edit(call.message, *with_note(messages["event_not_found"], events_screen(scope_of(ctx), list_route(route))))
            return
        text = messages["unit_result"].format(title=event["title"], value=describe_in_unit(event["date"], unit))
        edit(call.message, text, event_keyboard(event, route))

    @bot.callback_query_handler(func=lambda call: call.data.startswith("widget_"))
    @safe_handler
    def handle_widget_event(call):
        ctx = begin(call)
        _, token, event_id = call.data.split("_", 2)
        event = get_event(scope_of(ctx), event_id)
        if event is None:
            edit(call.message, *with_note(messages["event_not_found"], widget_return_screen(scope_of(ctx), token)))
            return
        edit(call.message, *widget_format_screen(event, token))

    @bot.callback_query_handler(func=lambda call: call.data.startswith("wfmt_"))
    @safe_handler
    def handle_widget_format(call):
        ctx = begin(call)
        _, choice, token, event_id = call.data.split("_", 3)
        event = get_event(scope_of(ctx), event_id)
        if event is None:
            edit(call.message, *with_note(messages["event_not_found"], widget_return_screen(scope_of(ctx), token)))
            return
        masks = settings["widget_format_masks"]
        if choice != "custom" and not (choice.isdigit() and int(choice) < len(masks)):
            edit(call.message, *widget_format_screen(event, token))
            return
        if choice == "custom":
            edit(call.message, *widget_return_screen(scope_of(ctx), token))
            ask(ctx, messages["widget_mask_prompt"], receive_widget_mask, event_id, token)
            return
        draft = {"event_id": event_id, "token": token, "mask": masks[int(choice)], "window": call.message}
        widget_drafts[ctx] = draft
        edit(call.message, *period_screen(draft))

    @bot.callback_query_handler(func=lambda call: call.data.startswith("wper_"))
    @safe_handler
    def handle_widget_period(call):
        ctx = begin(call)
        draft = widget_drafts.get(ctx)
        if draft is None:
            send(ctx, messages["widget_expired"])
            return
        key = call.data[len("wper_"):]
        if key == "back":
            if draft["window"]:
                edit(draft["window"], *period_screen(draft))
            return
        if not draft["window"]:
            close_buttons(call.message)
        if key == "custom":
            leave_window(ctx, draft)
            ask(ctx, messages["widget_interval_prompt"], receive_widget_interval)
            return
        draft["interval"] = settings["widget_period_seconds"][key]
        show_step(ctx, draft, first_update_screen(draft))

    @bot.callback_query_handler(func=lambda call: call.data.startswith("wtime_"))
    @safe_handler
    def handle_widget_time(call):
        ctx = begin(call)
        draft = widget_drafts.get(ctx)
        if draft is None:
            send(ctx, messages["widget_expired"])
            return
        if not draft["window"]:
            close_buttons(call.message)
        if call.data == "wtime_custom":
            leave_window(ctx, draft)
            ask(ctx, messages["widget_time_prompt"], receive_widget_first_update)
            return
        finish_widget_setup(ctx, draft, default_first_update())

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
        reminder = get_reminder(scope_of(ctx), reminder_id)
        if reminder is None:
            edit(call.message, *with_note(messages["remind_not_found"], reminders_screen(scope_of(ctx), list_route(route))))
            return
        edit(call.message, *reminder_screen(reminder, route))

    @bot.callback_query_handler(func=lambda call: call.data.startswith("rdelask_"))
    @safe_handler
    def handle_remind_delete_ask(call):
        ctx = begin(call)
        _, token, reminder_id = call.data.split("_", 2)
        reminder = get_reminder(scope_of(ctx), reminder_id)
        if reminder is None:
            edit(call.message, *with_note(messages["remind_not_found"], reminders_screen(scope_of(ctx), list_route(unpack(token)))))
            return
        cancel = "nav_delreminds" if token == "d" else f"remind_{token}_{reminder_id}"
        text = messages["remind_delete_confirm"].format(text=reminder_preview(reminder), date=reminder_date(reminder))
        edit(call.message, *confirm_screen(text, f"rdel_{token}_{reminder_id}", cancel))

    @bot.callback_query_handler(func=lambda call: call.data.startswith("rdel_"))
    @safe_handler
    def handle_remind_delete(call):
        ctx = begin(call)
        _, token, reminder_id = call.data.split("_", 2)
        reminder = delete_reminder(scope_of(ctx), reminder_id)
        note = messages["remind_deleted"] if reminder else messages["remind_not_found"]
        edit(call.message, *with_note(note, reminders_screen(scope_of(ctx), list_route(unpack(token)))))

    @bot.callback_query_handler(func=lambda call: call.data.startswith("delask_"))
    @safe_handler
    def handle_delete_ask(call):
        ctx = begin(call)
        _, token, event_id = call.data.split("_", 2)
        event = get_event(scope_of(ctx), event_id)
        if event is None:
            edit(call.message, *with_note(messages["event_not_found"], events_screen(scope_of(ctx), list_route(unpack(token)))))
            return
        cancel = "nav_delevents" if token == "d" else f"event_{token}_{event_id}"
        edit(call.message, *confirm_screen(messages["delete_confirm"].format(title=event["title"]), f"delete_{token}_{event_id}", cancel))

    @bot.callback_query_handler(func=lambda call: call.data.startswith("delete_"))
    @safe_handler
    def handle_delete(call):
        ctx = begin(call)
        _, token, event_id = call.data.split("_", 2)
        route = list_route(unpack(token))
        event = delete_event(scope_of(ctx), event_id)
        if event is None:
            edit(call.message, *with_note(messages["event_not_found"], events_screen(scope_of(ctx), route)))
            return
        for pinned in pop_event_widgets(scope_of(ctx), event["id"]):
            unpin_widget(bot, pinned)
        edit(call.message, *with_note(messages["event_deleted"].format(title=event["title"]), events_screen(scope_of(ctx), route)))


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
