from sqlalchemy import Column, Integer, String, Float, Text, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base


class Client(Base):
    __tablename__ = "clients"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    email = Column(String(255), nullable=True)
    phone = Column(String(100), nullable=True)
    type = Column(String(50), default="direct")  # direct | amazon_seller
    balance = Column(Float, default=0.0)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    # HouseCargo integration: each client has their own HC account
    housecargo_supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=True)
    housecargo_username = Column(String(255), nullable=True)
    housecargo_password_encrypted = Column(Text, nullable=True)

    # Prime Prep integration: UUID of this client in prime-prep's system
    prime_prep_client_id = Column(String(36), nullable=True)

    parcels = relationship("Parcel", back_populates="client", foreign_keys="Parcel.client_id")
    orders = relationship("Order", back_populates="client")
    reports = relationship("Report", back_populates="client")
    user = relationship("User", back_populates="client", foreign_keys="User.client_id", uselist=False)
    wishlist_items = relationship("WishlistItem", back_populates="client", cascade="all, delete-orphan",
                                  order_by="WishlistItem.created_at")
    shipx_addresses = relationship("ClientShipXAddress", back_populates="client", cascade="all, delete-orphan")
    housecargo_supplier = relationship("Supplier", foreign_keys=[housecargo_supplier_id])
