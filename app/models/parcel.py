from sqlalchemy import Column, Integer, String, Float, Text, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base


class Parcel(Base):
    __tablename__ = "parcels"

    id = Column(Integer, primary_key=True, index=True)
    external_order_id = Column(String(255), nullable=True, index=True)  # Order ID from supplier
    tracking_number = Column(String(255), nullable=False, index=True)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=True)
    qty = Column(Integer, nullable=True, default=1)
    asin = Column(String(20), nullable=True, index=True)  # Amazon Standard Identification Number
    title = Column(String(500), nullable=True)             # Product title from Keepa
    amazon_price = Column(Float, nullable=True)            # Amazon NEW price at delivery date
    purchase_price = Column(Float, nullable=True)          # cost = amazon_price × 0.45, rounded to int
    # Status flow: unidentified → in_transit / in_forwarding / disposed
    #              in_transit → delivered → in_warehouse → disposed | sold
    status = Column(String(50), nullable=False, default="in_transit")
    # unidentified | in_transit | delivered | in_warehouse | in_forwarding | disposed | sold
    is_wrong_address = Column(Boolean, default=False, nullable=False)
    match_source = Column(String(20), nullable=True)  # 'address' | 'wishlist' | 'manual' | None
    description = Column(String(500), nullable=True)
    weight_kg = Column(Float, nullable=True)
    length_cm = Column(Float, nullable=True)
    width_cm = Column(Float, nullable=True)
    height_cm = Column(Float, nullable=True)
    notes = Column(Text, nullable=True)
    arrived_at = Column(DateTime, nullable=True)
    # Payment: when set → parcel is paid; value = report date (tag)
    payment_report_date = Column(String(10), nullable=True, index=True)  # "YYYY-MM-DD"
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    supplier = relationship("Supplier", back_populates="parcels")
    client = relationship("Client", back_populates="parcels", foreign_keys=[client_id])
    photos = relationship("ParcelPhoto", back_populates="parcel", cascade="all, delete-orphan")
    comments = relationship("ParcelComment", back_populates="parcel", cascade="all, delete-orphan", order_by="ParcelComment.created_at")
    status_logs = relationship("ParcelStatusLog", back_populates="parcel", cascade="all, delete-orphan", order_by="ParcelStatusLog.changed_at.desc()")
    orders = relationship("Order", back_populates="parcel")


class ParcelPhoto(Base):
    __tablename__ = "parcel_photos"

    id = Column(Integer, primary_key=True, index=True)
    parcel_id = Column(Integer, ForeignKey("parcels.id", ondelete="CASCADE"), nullable=False)
    file_path = Column(String(500), nullable=False)
    caption = Column(String(255), nullable=True)
    uploaded_at = Column(DateTime, server_default=func.now())

    parcel = relationship("Parcel", back_populates="photos")


class ParcelComment(Base):
    __tablename__ = "parcel_comments"

    id = Column(Integer, primary_key=True, index=True)
    parcel_id = Column(Integer, ForeignKey("parcels.id", ondelete="CASCADE"), nullable=False)
    body = Column(Text, nullable=False)
    author = Column(String(100), nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    parcel = relationship("Parcel", back_populates="comments")


class ParcelStatusLog(Base):
    __tablename__ = "parcel_status_log"

    id = Column(Integer, primary_key=True, index=True)
    parcel_id = Column(Integer, ForeignKey("parcels.id", ondelete="CASCADE"), nullable=False)
    old_status = Column(String(50), nullable=True)
    new_status = Column(String(50), nullable=False)
    changed_by = Column(String(100), nullable=True)
    changed_at = Column(DateTime, server_default=func.now())
    notes = Column(Text, nullable=True)

    parcel = relationship("Parcel", back_populates="status_logs")
