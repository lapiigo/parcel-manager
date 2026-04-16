import os
import logging
import requests

logger = logging.getLogger(__name__)


def send_telegram_message(text: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        logger.warning("Telegram not configured (TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing)")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=5,
        )
        if not r.ok:
            logger.error(f"Telegram API error: {r.status_code} {r.text}")
        return r.ok
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")
        return False


def check_meeting_reminders():
    """Called by APScheduler every minute. Sends reminders for upcoming meetings."""
    from datetime import datetime, timedelta
    from app.database import SessionLocal
    from app.models.todo import TodoMeeting

    db = SessionLocal()
    try:
        now = datetime.utcnow()
        meetings = (
            db.query(TodoMeeting)
            .filter(
                TodoMeeting.telegram_notified == False,
                TodoMeeting.scheduled_at > now,
            )
            .all()
        )
        for meeting in meetings:
            remind_at = meeting.scheduled_at - timedelta(minutes=meeting.remind_minutes_before)
            if now >= remind_at:
                time_str = meeting.scheduled_at.strftime("%d.%m.%Y %H:%M")
                mins = meeting.remind_minutes_before
                text = (
                    f"🔔 <b>Нагадування</b>\n\n"
                    f"📅 <b>{meeting.title}</b>\n"
                    f"🕐 {time_str} UTC\n"
                    f"⏱ Через {mins} хв."
                )
                if meeting.description:
                    text += f"\n\n📝 {meeting.description}"
                if meeting.project:
                    text += f"\n📁 Проєкт: {meeting.project.name}"
                if send_telegram_message(text):
                    meeting.telegram_notified = True
                    db.commit()
    except Exception as e:
        logger.error(f"check_meeting_reminders error: {e}")
    finally:
        db.close()


def check_deadline_reminders():
    """Called by APScheduler daily. Sends reminders for tasks with deadlines today or tomorrow."""
    from datetime import datetime, timedelta
    from app.database import SessionLocal
    from app.models.todo import TodoTask

    db = SessionLocal()
    try:
        now = datetime.utcnow()
        tomorrow_end = now + timedelta(days=2)
        tasks = (
            db.query(TodoTask)
            .filter(
                TodoTask.deadline != None,
                TodoTask.deadline <= tomorrow_end,
                TodoTask.deadline >= now,
                TodoTask.status.notin_(["done", "cancelled"]),
                TodoTask.is_idea == False,
            )
            .all()
        )
        if not tasks:
            return
        lines = ["⚠️ <b>Дедлайни найближчі 48 год</b>\n"]
        for task in tasks:
            dl = task.deadline.strftime("%d.%m %H:%M")
            proj = f" [{task.project.name}]" if task.project else ""
            lines.append(f"• {task.title}{proj} — {dl}")
        send_telegram_message("\n".join(lines))
    except Exception as e:
        logger.error(f"check_deadline_reminders error: {e}")
    finally:
        db.close()
