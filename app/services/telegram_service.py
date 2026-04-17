import os
import logging
import requests
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

_bot_username: str | None = None
_last_update_id: int = 0

REMINDER_OPTIONS = [
    (0,    "At event"),
    (5,    "5 min before"),
    (15,   "15 min before"),
    (30,   "30 min before"),
    (60,   "1 hour before"),
    (120,  "2 hours before"),
    (180,  "3 hours before"),
    (1440, "1 day before"),
]


def _mins_label(minutes: int) -> str:
    if minutes == 0:
        return "at event time"
    if minutes < 60:
        return f"{minutes} min before"
    hours = minutes // 60
    return f"{hours} hour{'s' if hours > 1 else ''} before"


# ── Bot helpers ───────────────────────────────────────────────────────────────

def get_bot_token() -> str:
    return os.getenv("TELEGRAM_BOT_TOKEN", "")


def get_bot_username() -> str | None:
    global _bot_username
    if _bot_username:
        return _bot_username
    token = get_bot_token()
    if not token:
        return None
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=5)
        if r.ok:
            _bot_username = r.json().get("result", {}).get("username")
            return _bot_username
    except Exception as e:
        logger.error(f"getMe failed: {e}")
    return None


# ── Sending ───────────────────────────────────────────────────────────────────

def send_telegram_to_chat(chat_id: str, text: str) -> bool:
    token = get_bot_token()
    if not token or not chat_id:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=5,
        )
        if not r.ok:
            logger.error(f"Telegram error {r.status_code}: {r.text[:200]}")
        return r.ok
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")
        return False


def send_telegram_message(text: str) -> bool:
    """Backward-compat: send to global TELEGRAM_CHAT_ID."""
    return send_telegram_to_chat(os.getenv("TELEGRAM_CHAT_ID", ""), text)


# ── Polling ───────────────────────────────────────────────────────────────────

def poll_telegram_updates():
    """Called every 10 seconds. Handles /start {token} from new users."""
    global _last_update_id
    token = get_bot_token()
    if not token:
        return
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={"offset": _last_update_id + 1, "limit": 100, "timeout": 0},
            timeout=10,
        )
        if not r.ok:
            return
        for update in r.json().get("result", []):
            _last_update_id = max(_last_update_id, update["update_id"])
            message = update.get("message") or {}
            text = message.get("text", "")
            chat_id = str(message.get("chat", {}).get("id", ""))
            if text.startswith("/start") and chat_id:
                parts = text.split(maxsplit=1)
                connect_token = parts[1].strip() if len(parts) > 1 else ""
                if connect_token:
                    _handle_connect_token(connect_token, chat_id)
                else:
                    send_telegram_to_chat(
                        chat_id,
                        "👋 Hello! To connect your Telegram to Parcel Manager, "
                        "click the <b>Connect Telegram</b> button in the app.",
                    )
    except Exception as e:
        logger.error(f"poll_telegram_updates error: {e}")


def _handle_connect_token(connect_token: str, chat_id: str):
    from app.database import SessionLocal
    from app.models.user import User

    db = SessionLocal()
    try:
        user = (
            db.query(User)
            .filter(
                User.telegram_token == connect_token,
                User.telegram_token_expires > datetime.utcnow(),
            )
            .first()
        )
        if user:
            user.telegram_chat_id = chat_id
            user.telegram_token = None
            user.telegram_token_expires = None
            db.commit()
            name = user.full_name or user.username
            send_telegram_to_chat(
                chat_id,
                f"✅ <b>Telegram connected!</b>\n\n"
                f"Hi, {name}! You will now receive reminders for meetings "
                f"and task deadlines from Parcel Manager.",
            )
        else:
            send_telegram_to_chat(
                chat_id,
                "❌ The link has expired or already been used.\n"
                "Please try again — click 'Connect Telegram' in the app.",
            )
    except Exception as e:
        logger.error(f"_handle_connect_token error: {e}")
    finally:
        db.close()


# ── Unified reminder checker ──────────────────────────────────────────────────

def check_reminders():
    """
    Called every minute by APScheduler.
    Checks all Reminder rows for tasks (deadline) and meetings (scheduled_at).
    Sends a Telegram message to the owner if it's time.
    """
    from app.database import SessionLocal
    from app.models.todo import Reminder, TodoTask, TodoMeeting
    from app.models.user import User

    db = SessionLocal()
    try:
        now = datetime.utcnow()
        pending = (
            db.query(Reminder)
            .filter(Reminder.telegram_notified == False)
            .all()
        )
        for reminder in pending:
            # Resolve the event
            if reminder.task_id:
                task = db.query(TodoTask).get(reminder.task_id)
                if not task or not task.deadline:
                    continue
                if task.status in ("done", "cancelled"):
                    reminder.telegram_notified = True
                    continue
                event_time = task.deadline
                event_title = task.title
                event_type = "Task deadline"
                icon = "📋"
            elif reminder.meeting_id:
                meeting = db.query(TodoMeeting).get(reminder.meeting_id)
                if not meeting:
                    continue
                event_time = meeting.scheduled_at
                event_title = meeting.title
                event_type = "Meeting"
                icon = "📅"
            else:
                reminder.telegram_notified = True
                continue

            remind_at = event_time - timedelta(minutes=reminder.minutes_before)

            # Not yet time
            if now < remind_at:
                continue

            # Event passed by more than 10 minutes — skip silently
            if now > event_time + timedelta(minutes=10):
                reminder.telegram_notified = True
                continue

            user = db.query(User).get(reminder.user_id)
            if not user or not user.telegram_chat_id:
                continue

            try:
                from zoneinfo import ZoneInfo
                user_tz = ZoneInfo(user.timezone or "UTC")
                local_event_time = event_time.replace(tzinfo=ZoneInfo("UTC")).astimezone(user_tz)
                tz_label = local_event_time.strftime("%Z")
            except Exception:
                local_event_time = event_time
                tz_label = "UTC"

            time_str = local_event_time.strftime("%d.%m.%Y %H:%M")
            when = _mins_label(reminder.minutes_before)
            text = (
                f"🔔 <b>Reminder</b>\n\n"
                f"{icon} <b>{event_title}</b>\n"
                f"⏰ {event_type}: {time_str} {tz_label}\n"
                f"📍 {when.capitalize()}"
            )
            if send_telegram_to_chat(user.telegram_chat_id, text):
                reminder.telegram_notified = True

        db.commit()
    except Exception as e:
        logger.error(f"check_reminders error: {e}")
    finally:
        db.close()
