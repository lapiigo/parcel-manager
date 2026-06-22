from sqlalchemy import Column, Integer, String, Float, Text, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base


class WarehouseItem(Base):
    __tablename__ = "warehouse_items"

    id = Column(Integer, primary_key=True, index=True)
    parcel_id = Column(Integer, ForeignKey("parcels.id", ondelete="CASCADE"), nullable=False, unique=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=True)
    # available | reserved | sold | fba_sent | returned
    status = Column(String(50), nullable=False, default="available")
    location = Column(String(100), nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    parcel = relationship("Parcel", back_populates="warehouse_item")
    client = relationship("Client")


WAREHOUSE_STATUS_LABELS = {
    "available":  "Available",
    "reserved":   "Reserved",
    "sold":       "Sold",
    "fba_sent":   "FBA Sent",
    "returned":   "Returned",
}

WAREHOUSE_STATUS_COLORS = {
    "available": "bg-green-100 text-green-800",
    "reserved":  "bg-amber-100 text-amber-800",
    "sold":      "bg-purple-100 text-purple-800",
    "fba_sent":  "bg-blue-100 text-blue-800",
    "returned":  "bg-gray-100 text-gray-600",
}
