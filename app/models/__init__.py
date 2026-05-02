# Import all models so SQLAlchemy registers them and Alembic can detect them
from app.models.supplier import Supplier
from app.models.client import Client, ClientDeposit
from app.models.user import User, Session
from app.models.parcel import Parcel, ParcelPhoto, ParcelComment, ParcelStatusLog, ParcelNegotiation
from app.models.order import Order
from app.models.report import Report
from app.models.wishlist import WishlistItem, ClientShipXAddress
from app.models.todo import TodoProject, TodoTask, TaskAttachment, TodoMeeting, Reminder, Note

__all__ = [
    "Supplier",
    "Client",
    "ClientDeposit",
    "User",
    "Session",
    "Parcel",
    "ParcelPhoto",
    "ParcelComment",
    "ParcelStatusLog",
    "ParcelNegotiation",
    "Order",
    "Report",
    "WishlistItem",
    "ClientShipXAddress",
    "TodoProject",
    "TodoTask",
    "TaskAttachment",
    "TodoMeeting",
    "Reminder",
    "Note",
]
