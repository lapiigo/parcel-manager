import os
import uuid
from datetime import datetime
from typing import Optional, List

from fastapi import APIRouter, Depends, Request, Form, UploadFile, File, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import require_super_admin, require_admin_up
from app.models.todo import TodoProject, TodoTask, TaskAttachment, TodoMeeting, Reminder, Note
from app.permissions import can
from app.services.telegram_service import REMINDER_OPTIONS

router = APIRouter(prefix="/todo")
templates = Jinja2Templates(directory="app/templates")

UPLOAD_DIR = os.getenv("UPLOAD_DIR", "uploads")
TODO_UPLOAD_DIR = os.path.join(UPLOAD_DIR, "todo")
os.makedirs(TODO_UPLOAD_DIR, exist_ok=True)

PRIORITY_COLORS = {
    "low":    "bg-green-100 text-green-700",
    "medium": "bg-blue-100 text-blue-700",
    "high":   "bg-orange-100 text-orange-700",
    "urgent": "bg-red-100 text-red-700",
}
STATUS_COLORS = {
    "backlog":     "bg-gray-100 text-gray-600",
    "todo":        "bg-blue-100 text-blue-700",
    "in_progress": "bg-amber-100 text-amber-700",
    "done":        "bg-green-100 text-green-700",
    "cancelled":   "bg-red-100 text-red-600",
}
STATUS_LABELS = {
    "backlog":     "Backlog",
    "todo":        "To Do",
    "in_progress": "In Progress",
    "done":        "Done",
    "cancelled":   "Cancelled",
}
PROJECT_COLORS = [
    "#6366f1", "#8b5cf6", "#ec4899", "#ef4444",
    "#f97316", "#eab308", "#22c55e", "#14b8a6",
    "#3b82f6", "#06b6d4", "#64748b", "#1e293b",
]

NOTE_COLORS = {
    "yellow": "bg-yellow-50 border-yellow-200",
    "green":  "bg-green-50 border-green-200",
    "blue":   "bg-blue-50 border-blue-200",
    "pink":   "bg-pink-50 border-pink-200",
    "purple": "bg-purple-100 border-purple-200",
    "white":  "bg-white border-gray-200",
}
NOTE_DOT_COLORS = {
    "yellow": "bg-yellow-400",
    "green":  "bg-green-400",
    "blue":   "bg-blue-400",
    "pink":   "bg-pink-400",
    "purple": "bg-purple-400",
    "white":  "bg-gray-300",
}


# ── Dashboard ─────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
def todo_index(
    request: Request,
    tg: str = Query(""),
    tab: str = Query("overview"),
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_up),
):
    projects = (
        db.query(TodoProject)
        .filter(TodoProject.user_id == current_user.id)
        .order_by(TodoProject.created_at.desc())
        .all()
    )
    # Upcoming meetings (next 7 days)
    now = datetime.utcnow()
    from datetime import timedelta
    upcoming = (
        db.query(TodoMeeting)
        .filter(
            TodoMeeting.user_id == current_user.id,
            TodoMeeting.scheduled_at >= now,
            TodoMeeting.scheduled_at <= now + timedelta(days=7),
        )
        .order_by(TodoMeeting.scheduled_at)
        .all()
    )
    # Tasks due soon (next 48h, not done)
    due_soon = (
        db.query(TodoTask)
        .filter(
            TodoTask.user_id == current_user.id,
            TodoTask.deadline != None,
            TodoTask.deadline >= now,
            TodoTask.deadline <= now + timedelta(hours=48),
            TodoTask.status.notin_(["done", "cancelled"]),
            TodoTask.is_idea == False,
        )
        .order_by(TodoTask.deadline)
        .all()
    )
    total_tasks = db.query(TodoTask).filter(
        TodoTask.user_id == current_user.id, TodoTask.is_idea == False
    ).count()
    active_tasks = db.query(TodoTask).filter(
        TodoTask.user_id == current_user.id,
        TodoTask.is_idea == False,
        TodoTask.status.in_(["todo", "in_progress", "backlog"]),
    ).count()
    # Notes (not tied to any project)
    notes = (
        db.query(Note)
        .filter(Note.user_id == current_user.id)
        .order_by(Note.updated_at.desc())
        .all()
    )
    # Inbox: standalone tasks / ideas (no project)
    inbox_tasks = (
        db.query(TodoTask)
        .filter(
            TodoTask.user_id == current_user.id,
            TodoTask.project_id == None,
            TodoTask.is_idea == False,
        )
        .order_by(TodoTask.created_at.desc())
        .all()
    )
    inbox_ideas = (
        db.query(TodoTask)
        .filter(
            TodoTask.user_id == current_user.id,
            TodoTask.project_id == None,
            TodoTask.is_idea == True,
        )
        .order_by(TodoTask.created_at.desc())
        .all()
    )
    from app.services.telegram_service import get_bot_username
    bot_username = get_bot_username()

    return templates.TemplateResponse(
        request, "todo/index.html",
        {
            "current_user": current_user,
            "projects": projects,
            "upcoming": upcoming,
            "due_soon": due_soon,
            "total_tasks": total_tasks,
            "active_tasks": active_tasks,
            "notes": notes,
            "inbox_tasks": inbox_tasks,
            "inbox_ideas": inbox_ideas,
            "active_tab": tab,
            "project_colors": PROJECT_COLORS,
            "note_colors": NOTE_COLORS,
            "note_dot_colors": NOTE_DOT_COLORS,
            "bot_username": bot_username,
            "tg_flash": tg,
            "reminder_options": REMINDER_OPTIONS,
            "STATUS_COLORS": STATUS_COLORS,
            "STATUS_LABELS": STATUS_LABELS,
            "PRIORITY_COLORS": PRIORITY_COLORS,
            "can": can,
        },
    )


# ── Projects ──────────────────────────────────────────────────────────────────

@router.post("/projects/create")
def create_project(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    color: str = Form("#6366f1"),
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_up),
):
    project = TodoProject(
        name=name.strip(),
        description=description.strip() or None,
        color=color,
        user_id=current_user.id,
    )
    db.add(project)
    db.commit()
    return RedirectResponse(f"/todo/projects/{project.id}", status_code=302)


@router.post("/projects/{project_id}/edit")
def edit_project(
    project_id: int,
    name: str = Form(...),
    description: str = Form(""),
    color: str = Form("#6366f1"),
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_up),
):
    project = db.query(TodoProject).filter(
        TodoProject.id == project_id, TodoProject.user_id == current_user.id
    ).first()
    if project:
        project.name = name.strip()
        project.description = description.strip() or None
        project.color = color
        db.commit()
    return RedirectResponse(f"/todo/projects/{project_id}", status_code=302)


@router.post("/projects/{project_id}/delete")
def delete_project(
    project_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_up),
):
    project = db.query(TodoProject).filter(
        TodoProject.id == project_id, TodoProject.user_id == current_user.id
    ).first()
    if project:
        db.delete(project)
        db.commit()
    return RedirectResponse("/todo", status_code=302)


# ── Project detail ────────────────────────────────────────────────────────────

@router.get("/projects/{project_id}", response_class=HTMLResponse)
def project_detail(
    project_id: int,
    request: Request,
    tab: str = Query("tasks"),
    status_filter: str = Query(""),
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_up),
):
    project = db.query(TodoProject).filter(
        TodoProject.id == project_id, TodoProject.user_id == current_user.id
    ).first()
    if not project:
        return RedirectResponse("/todo", status_code=302)

    tasks_q = db.query(TodoTask).filter(
        TodoTask.project_id == project_id,
        TodoTask.is_idea == False,
    )
    if status_filter:
        tasks_q = tasks_q.filter(TodoTask.status == status_filter)
    tasks = tasks_q.order_by(
        TodoTask.deadline.asc().nullslast(), TodoTask.created_at.desc()
    ).all()

    ideas = db.query(TodoTask).filter(
        TodoTask.project_id == project_id,
        TodoTask.is_idea == True,
    ).order_by(TodoTask.created_at.desc()).all()

    meetings = db.query(TodoMeeting).filter(
        TodoMeeting.project_id == project_id,
    ).order_by(TodoMeeting.scheduled_at).all()

    all_projects = db.query(TodoProject).filter(TodoProject.user_id == current_user.id).all()

    now = datetime.utcnow()
    return templates.TemplateResponse(
        request, "todo/project.html",
        {
            "current_user": current_user,
            "project": project,
            "tasks": tasks,
            "ideas": ideas,
            "meetings": meetings,
            "all_projects": all_projects,
            "tab": tab,
            "status_filter": status_filter,
            "project_colors": PROJECT_COLORS,
            "PRIORITY_COLORS": PRIORITY_COLORS,
            "STATUS_COLORS": STATUS_COLORS,
            "STATUS_LABELS": STATUS_LABELS,
            "reminder_options": REMINDER_OPTIONS,
            "now": now,
            "can": can,
        },
    )


# ── Tasks ─────────────────────────────────────────────────────────────────────

@router.post("/tasks/create")
def create_task(
    project_id: Optional[str] = Form(None),
    title: str = Form(...),
    description: str = Form(""),
    priority: str = Form("medium"),
    status: str = Form("todo"),
    is_idea: str = Form("0"),
    deadline: str = Form(""),
    redirect_tab: str = Form("tasks"),
    reminder_minutes: List[int] = Form(default=[]),
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_up),
):
    project_id_int: Optional[int] = None
    if project_id and str(project_id).strip().isdigit():
        project_id_int = int(project_id)

    deadline_dt = None
    if deadline:
        try:
            deadline_dt = datetime.fromisoformat(deadline)
        except ValueError:
            pass
    task = TodoTask(
        title=title.strip(),
        description=description.strip() or None,
        project_id=project_id_int,
        user_id=current_user.id,
        status=status,
        priority=priority,
        is_idea=(is_idea == "1"),
        deadline=deadline_dt,
    )
    db.add(task)
    db.flush()  # get task.id
    if deadline_dt and reminder_minutes:
        for mins in reminder_minutes:
            db.add(Reminder(task_id=task.id, user_id=current_user.id, minutes_before=mins))
    db.commit()
    if project_id_int:
        tab = "ideas" if is_idea == "1" else redirect_tab
        return RedirectResponse(f"/todo/projects/{project_id_int}?tab={tab}", status_code=302)
    return RedirectResponse("/todo?tab=inbox", status_code=302)


@router.post("/tasks/{task_id}/status")
def update_task_status(
    task_id: int,
    new_status: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_up),
):
    task = db.query(TodoTask).filter(
        TodoTask.id == task_id, TodoTask.user_id == current_user.id
    ).first()
    if task:
        task.status = new_status
        db.commit()
        if task.project_id:
            tab = "ideas" if task.is_idea else "tasks"
            return RedirectResponse(f"/todo/projects/{task.project_id}?tab={tab}", status_code=302)
        return RedirectResponse("/todo?tab=inbox", status_code=302)
    return RedirectResponse("/todo", status_code=302)


@router.post("/tasks/{task_id}/edit")
def edit_task(
    task_id: int,
    title: str = Form(...),
    description: str = Form(""),
    priority: str = Form("medium"),
    status: str = Form("todo"),
    deadline: str = Form(""),
    reminder_minutes: List[int] = Form(default=[]),
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_up),
):
    task = db.query(TodoTask).filter(
        TodoTask.id == task_id, TodoTask.user_id == current_user.id
    ).first()
    if task:
        task.title = title.strip()
        task.description = description.strip() or None
        task.priority = priority
        task.status = status
        if deadline:
            try:
                task.deadline = datetime.fromisoformat(deadline)
            except ValueError:
                pass
        else:
            task.deadline = None
        # Replace reminders
        db.query(Reminder).filter(Reminder.task_id == task_id).delete()
        if task.deadline and reminder_minutes:
            for mins in reminder_minutes:
                db.add(Reminder(task_id=task.id, user_id=current_user.id, minutes_before=mins))
        db.commit()
        if task.project_id:
            tab = "ideas" if task.is_idea else "tasks"
            return RedirectResponse(f"/todo/projects/{task.project_id}?tab={tab}", status_code=302)
        return RedirectResponse("/todo?tab=inbox", status_code=302)
    return RedirectResponse("/todo", status_code=302)


@router.post("/tasks/{task_id}/delete")
def delete_task(
    task_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_up),
):
    task = db.query(TodoTask).filter(
        TodoTask.id == task_id, TodoTask.user_id == current_user.id
    ).first()
    if task:
        project_id = task.project_id
        is_idea = task.is_idea
        db.delete(task)
        db.commit()
        if project_id:
            tab = "ideas" if is_idea else "tasks"
            return RedirectResponse(f"/todo/projects/{project_id}?tab={tab}", status_code=302)
        return RedirectResponse("/todo?tab=inbox", status_code=302)
    return RedirectResponse("/todo", status_code=302)


@router.post("/tasks/{task_id}/upload")
async def upload_attachment(
    task_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_up),
):
    task = db.query(TodoTask).filter(
        TodoTask.id == task_id, TodoTask.user_id == current_user.id
    ).first()
    if not task:
        return RedirectResponse("/todo", status_code=302)

    ext = os.path.splitext(file.filename or "file")[1]
    filename = f"{uuid.uuid4().hex}{ext}"
    file_path = os.path.join(TODO_UPLOAD_DIR, filename)
    content = await file.read()
    with open(file_path, "wb") as f:
        f.write(content)

    att = TaskAttachment(
        task_id=task_id,
        file_path=f"uploads/todo/{filename}",
        filename=file.filename or filename,
        file_size=len(content),
    )
    db.add(att)
    db.commit()
    if task.project_id:
        tab = "ideas" if task.is_idea else "tasks"
        return RedirectResponse(f"/todo/projects/{task.project_id}?tab={tab}", status_code=302)
    return RedirectResponse("/todo?tab=inbox", status_code=302)


@router.post("/attachments/{att_id}/delete")
def delete_attachment(
    att_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_up),
):
    att = db.query(TaskAttachment).filter(TaskAttachment.id == att_id).first()
    if att:
        task = att.task
        project_id = task.project_id
        is_idea = task.is_idea
        try:
            os.remove(att.file_path)
        except Exception:
            pass
        db.delete(att)
        db.commit()
        if project_id:
            tab = "ideas" if is_idea else "tasks"
            return RedirectResponse(f"/todo/projects/{project_id}?tab={tab}", status_code=302)
        return RedirectResponse("/todo?tab=inbox", status_code=302)
    return RedirectResponse("/todo", status_code=302)


# ── Meetings ──────────────────────────────────────────────────────────────────

@router.post("/meetings/create")
def create_meeting(
    title: str = Form(...),
    description: str = Form(""),
    scheduled_at: str = Form(...),
    duration_minutes: int = Form(60),
    reminder_minutes: List[int] = Form(default=[]),
    project_id: str = Form(""),
    redirect_to: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_up),
):
    try:
        scheduled_dt = datetime.fromisoformat(scheduled_at)
    except ValueError:
        return RedirectResponse(redirect_to or "/todo", status_code=302)

    meeting = TodoMeeting(
        title=title.strip(),
        description=description.strip() or None,
        scheduled_at=scheduled_dt,
        duration_minutes=duration_minutes,
        project_id=int(project_id) if project_id.isdigit() else None,
        user_id=current_user.id,
    )
    db.add(meeting)
    db.flush()
    for mins in reminder_minutes:
        db.add(Reminder(meeting_id=meeting.id, user_id=current_user.id, minutes_before=mins))
    db.commit()
    if redirect_to:
        return RedirectResponse(redirect_to, status_code=302)
    return RedirectResponse("/todo", status_code=302)


@router.post("/meetings/{meeting_id}/delete")
def delete_meeting(
    meeting_id: int,
    redirect_to: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_up),
):
    meeting = db.query(TodoMeeting).filter(
        TodoMeeting.id == meeting_id, TodoMeeting.user_id == current_user.id
    ).first()
    if meeting:
        db.delete(meeting)
        db.commit()
    if redirect_to:
        return RedirectResponse(redirect_to, status_code=302)
    return RedirectResponse("/todo", status_code=302)


# ── Telegram connect / disconnect ─────────────────────────────────────────────

@router.post("/telegram/connect")
def telegram_connect(
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_up),
):
    import secrets
    from datetime import timedelta
    from app.services.telegram_service import get_bot_username

    bot_username = get_bot_username()
    if not bot_username:
        return RedirectResponse("/todo?tg=no_bot", status_code=302)

    token = secrets.token_hex(16)
    current_user.telegram_token = token
    current_user.telegram_token_expires = datetime.utcnow() + timedelta(minutes=10)
    db.commit()

    tg_url = f"https://t.me/{bot_username}?start={token}"
    return RedirectResponse(tg_url, status_code=302)


@router.post("/telegram/disconnect")
def telegram_disconnect(
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_up),
):
    current_user.telegram_chat_id = None
    current_user.telegram_token = None
    current_user.telegram_token_expires = None
    db.commit()
    return RedirectResponse("/todo?tg=disconnected", status_code=302)


# ── Notes ─────────────────────────────────────────────────────────────────────

@router.post("/notes/create")
def create_note(
    title: str = Form(""),
    content: str = Form(""),
    color: str = Form("yellow"),
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_up),
):
    note = Note(
        title=title.strip() or None,
        content=content.strip() or None,
        color=color if color in NOTE_COLORS else "yellow",
        user_id=current_user.id,
    )
    db.add(note)
    db.commit()
    return RedirectResponse("/todo?tab=notes", status_code=302)


@router.post("/notes/{note_id}/edit")
def edit_note(
    note_id: int,
    title: str = Form(""),
    content: str = Form(""),
    color: str = Form("yellow"),
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_up),
):
    note = db.query(Note).filter(
        Note.id == note_id, Note.user_id == current_user.id
    ).first()
    if note:
        note.title = title.strip() or None
        note.content = content.strip() or None
        note.color = color if color in NOTE_COLORS else "yellow"
        db.commit()
    return RedirectResponse("/todo?tab=notes", status_code=302)


@router.post("/notes/{note_id}/delete")
def delete_note(
    note_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_up),
):
    note = db.query(Note).filter(
        Note.id == note_id, Note.user_id == current_user.id
    ).first()
    if note:
        db.delete(note)
        db.commit()
    return RedirectResponse("/todo?tab=notes", status_code=302)
