from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base


class TodoProject(Base):
    __tablename__ = "todo_projects"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    color = Column(String(7), default="#6366f1")
    is_archived = Column(Boolean, default=False, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    tasks = relationship("TodoTask", back_populates="project", cascade="all, delete-orphan",
                         order_by="TodoTask.created_at.desc()")
    meetings = relationship("TodoMeeting", back_populates="project", cascade="all, delete-orphan")

    @property
    def task_count(self):
        return len([t for t in self.tasks if not t.is_idea])

    @property
    def done_count(self):
        return len([t for t in self.tasks if not t.is_idea and t.status == "done"])

    @property
    def idea_count(self):
        return len([t for t in self.tasks if t.is_idea])

    @property
    def progress(self):
        tc = self.task_count
        if tc == 0:
            return 0
        return round(self.done_count / tc * 100)


class TodoTask(Base):
    __tablename__ = "todo_tasks"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(500), nullable=False)
    description = Column(Text, nullable=True)
    project_id = Column(Integer, ForeignKey("todo_projects.id", ondelete="CASCADE"), nullable=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    status = Column(String(20), default="todo", nullable=False)   # backlog|todo|in_progress|done|cancelled
    priority = Column(String(10), default="medium", nullable=False)  # low|medium|high|urgent
    is_idea = Column(Boolean, default=False, nullable=False)
    deadline = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    project = relationship("TodoProject", back_populates="tasks")
    attachments = relationship("TaskAttachment", back_populates="task",
                               cascade="all, delete-orphan", order_by="TaskAttachment.created_at")
    reminders = relationship("Reminder", back_populates="task",
                             cascade="all, delete-orphan", order_by="Reminder.minutes_before")


class TaskAttachment(Base):
    __tablename__ = "task_attachments"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("todo_tasks.id", ondelete="CASCADE"), nullable=False)
    file_path = Column(String(500), nullable=False)
    filename = Column(String(255), nullable=False)
    file_size = Column(Integer, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    task = relationship("TodoTask", back_populates="attachments")


class TodoMeeting(Base):
    __tablename__ = "todo_meetings"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    project_id = Column(Integer, ForeignKey("todo_projects.id", ondelete="SET NULL"), nullable=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    scheduled_at = Column(DateTime, nullable=False)
    duration_minutes = Column(Integer, default=60)
    created_at = Column(DateTime, server_default=func.now())

    project = relationship("TodoProject", back_populates="meetings")
    reminders = relationship("Reminder", back_populates="meeting",
                             cascade="all, delete-orphan", order_by="Reminder.minutes_before")


class Reminder(Base):
    """A single Telegram alert linked to either a task (deadline) or a meeting."""
    __tablename__ = "reminders"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    task_id = Column(Integer, ForeignKey("todo_tasks.id", ondelete="CASCADE"), nullable=True)
    meeting_id = Column(Integer, ForeignKey("todo_meetings.id", ondelete="CASCADE"), nullable=True)
    minutes_before = Column(Integer, default=0)   # 0 = at event time
    telegram_notified = Column(Boolean, default=False)
    created_at = Column(DateTime, server_default=func.now())

    task = relationship("TodoTask", back_populates="reminders")
    meeting = relationship("TodoMeeting", back_populates="reminders")


class Note(Base):
    """Simple freeform note / idea / reminder not tied to any project."""
    __tablename__ = "notes"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(255), nullable=True)
    content = Column(Text, nullable=True)
    color = Column(String(20), default="yellow")  # yellow|green|blue|pink|purple|white
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
