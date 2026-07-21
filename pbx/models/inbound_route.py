"""
Inbound DID routing ORM model: maps a carrier-assigned DID number (optionally
scoped to one trunk) to an internal destination.
"""

from sqlalchemy import Boolean, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from pbx.models.base import Base, TimestampMixin


class InboundRoute(TimestampMixin, Base):
    """A DID -> internal-destination mapping for calls arriving on a SIP trunk."""

    __tablename__ = "inbound_routes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    did_number: Mapped[str] = mapped_column(String(20), nullable=False)
    trunk_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    destination_type: Mapped[str] = mapped_column(String(20), nullable=False)
    destination_value: Mapped[str] = mapped_column(String(50), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    priority: Mapped[int] = mapped_column(Integer, default=100, server_default="100")

    __table_args__ = (
        Index("ix_inbound_routes_did_number", "did_number"),
        UniqueConstraint("did_number", "trunk_id", name="uq_inbound_routes_did_trunk"),
    )

    def __repr__(self) -> str:
        return (
            f"<InboundRoute(id={self.id}, did_number='{self.did_number}', "
            f"trunk_id={self.trunk_id!r}, destination_type='{self.destination_type}', "
            f"destination_value='{self.destination_value}')>"
        )
