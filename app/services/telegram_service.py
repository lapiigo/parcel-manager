import os
import logging
import requests

logger = logging.getLogger(__name__)

# ── Bot info cache ────────────────────────────────────────────────────────────
_bot_username: str | None = None
_last_update_id: int = 0


def get_bot_token() -> str:
    return os.getenv("TELEGRAM_BOT_TOKEN", "")


def get_bot_username() -> str | None:
    """Fetch and cache the bot's @username. Returns None if bot not configured."""
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
    """Send a message to a specific Telegram chat_id."""
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
            logger.error(f"Telegram send error {r.status_code}: {r.text[:200]}")
        return r.ok
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")
        return False


def send_telegram_message(text: str) -> bool:
    """Backward-compat: send to global TELEGRAM_CHAT_ID (if set in .env)."""
    return send_telegram_to_chat(os.getenv("TELEGRAM_CHAT_ID", ""), text)


# ── Polling (handles /start {token} from new users) ──────────────────────────

def poll_telegram_updates():
    """Called by APScheduler every 10 seconds. Processes incoming bot messages."""
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
            message = update.get("message") or update.get("my_chat_member", {})
            text = message.get("text", "") if isinstance(message, dict) else ""
            chat = message.get("chat", {}) if isinstance(message, dict) else {}
            chat_id = str(chat.get("id", ""))
            if text.startswith("/start") and chat_id:
                parts = text.split(maxsplit=1)
                connect_token = parts[1].strip() if len(parts) > 1 else ""
                if connect_token:
                    _handle_connect_token(connect_token, chat_id)
                else:
                    send_telegram_to_chat(
                        chat_id,
                        "👋 Привіт! Щоб підключити Telegram до Parcel Manager, "
                        "скористайся кнопкою <b>«Підключити Telegram»</b> у додатку.",
                    )
    except Exception as e:
        logger.error(f"poll_telegram_updates error: {e}")


def _handle_connect_token(connect_token: str, chat_id: str):
    """Find user by token, save their chat_id, send confirmation."""
    from datetime import datetime
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
                f"✅ <b>Telegram підключено!</b>\n\n"
                f"Привіт, {name}! Тепер ти будеш отримувати сповіщення про "
                f"зустрічі та дедлайни з Parcel Manager.",
            )
        else:
            send_telegram_to_chat(
                chat_id,
                "❌ Посилання застаріло або вже використано.\n"
                "Спробуй ще раз — натисни «Підключити Telegram» в додатку.",
            )
    except Exception as e:
        logger.error(f"_handle_connect_token error: {e}")
    finally:
        db.close()


# ── Reminder logic (per-user) ─────────────────────────────────────────────────

def check_meeting_reminders():
    """Called every minute. Sends reminders to the meeting owner's Telegram."""
    from datetime import datetime, timedelta
    from app.database import SessionLocal
    from app.models.todo import TodoMeeting
    from app.models.user import User

    db = SessionLocal()
    try:
        now = datetime.utcnow()
        meetings = (
            db.query(TodoMeeting)
            .join(User, User.id == TodoMeeting.user_id)
            .filter(
                TodoMeeting.telegram_notified == False,
                TodoMeeting.scheduled_at > now,
                User.telegram_chat_id != None,
            )
            .all()
        )
        for meeting in meetings:
            remind_at = meeting.scheduled_at - timedelta(minutes=meeting.remind_minutes_before)
            if now < remind_at:
                continue
            user = db.query(User).filter(User.id == meeting.user_id).first()
            if not user or not user.telegram_chat_id:
                continue
            time_str = meeting.scheduled_at.strftime("%d.%m.%Y %H:%M")
            text = (
                f"🔔 <b>Нагадування про зустріч</b>\n\n"
                f"📅 <b>{meeting.title}</b>\n"
                f"🕐 {time_str} UTC  |  ⏱ через {meeting.remind_minutes_before} хв."
            )
            if meeting.description:
                text += f"\n\n📝 {meeting.description}"
            if meeting.project:
                text += f"\n📁 {meeting.project.name}"
            if send_telegram_to_chat(user.telegram_chat_id, text):
                meeting.telegram_notified = True
                db.commit()
    except Exception as e:
        logger.error(f"check_meeting_reminders error: {e}")
    finally:
        db.close()


def check_deadline_reminders():
    """Called daily at 9:00. Sends each user their tasks due in 48h."""
    from datetime import datetime, timedelta
    from app.database import SessionLocal
    from app.models.todo import TodoTask
    from app.models.user import User

    db = SessionLocal()
    try:
        now = datetime.utcnow()
        cutoff = now + timedelta(hours=48)
        users_with_tg = db.query(User).filter(User.telegram_chat_id != None).all()
        for user in users_with_tg:
            tasks = (
                db.query(TodoTask)
                .filter(
                    TodoTask.user_id == user.id,
                    TodoTask.deadline != None,
                    TodoTask.deadline >= now,
                    TodoTask.deadline <= cutoff,
                    TodoTask.status.notin_(["done", "cancelled"]),
                    TodoTask.is_idea == False,
                )
                .order_by(TodoTask.deadline)
                .all()
            )
            if not tasks:
                continue
            lines = [f"⚠️ <b>Дедлайни найближчі 48 год</b>\n"]
            for t in tasks:
                dl = t.deadline.strftime("%d.%m %H:%M")
                proj = f" [{t.project.name}]" if t.project else ""
                lines.append(f"• {t.title}{proj} — {dl}")
            send_telegram_to_chat(user.telegram_chat_id, "\n".join(lines))
    except Exception as e:
        logger.error(f"check_deadline_reminders error: {e}")
    finally:
        db.close()
