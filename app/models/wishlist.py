from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base


class WishlistItem(Base):
    __tablename__ = "wishlist_items"

    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    asin = Column(String(20), nullable=False, index=True)
    title = Column(String(500), nullable=True)       # fetched from Keepa on add
    qty_per_month = Column(Integer, default=1, nullable=False)
    notes = Column(String(500), nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    client = relationship("Client", back_populates="wishlist_items")


class ClientShipXAddress(Base):
    """Maps a ShipX address.name (e.g. 'JSM21') to a client for a specific supplier."""
    __tablename__ = "client_shipx_addresses"

    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    supplier_id = Column(Integer, ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False)
    address_name = Column(String(255), nullable=False)  # matches order.address.name in ShipX

    client = relationship("Client", back_populates="shipx_addresses")
    supplier = relationship("Supplier")
