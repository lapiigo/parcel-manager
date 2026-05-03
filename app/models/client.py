from sqlalchemy import Column, Integer, String, Float, Text, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base


class ClientDeposit(Base):
    __tablename__ = "client_deposits"

    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False)
    amount = Column(Float, nullable=False)
    transaction_ref = Column(String(255), nullable=True)
    screenshot_path = Column(String(500), nullable=True)
    notes = Column(Text, nullable=True)
    status = Column(String(20), default="pending")   # pending | confirmed
    created_at = Column(DateTime, server_default=func.now())
    confirmed_at = Column(DateTime, nullable=True)
    confirmed_by = Column(String(100), nullable=True)

    client = relationship("Client", back_populates="deposits")


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

    # ShipX integration: each client has their own ShipX account
    shipx_supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=True)
    shipx_username = Column(String(255), nullable=True)
    shipx_password_encrypted = Column(Text, nullable=True)

    # Cost coefficient for estimated purchase price (e.g. 0.45 = 45% of Amazon price)
    # None → use default 0.45
    cost_coefficient = Column(Float, nullable=True)

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
    shipx_supplier = relationship("Supplier", foreign_keys=[shipx_supplier_id])
    deposits = relationship("ClientDeposit", back_populates="client", cascade="all, delete-orphan",
                            order_by="ClientDeposit.created_at.desc()")
