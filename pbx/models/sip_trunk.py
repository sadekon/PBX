"""
SIP trunk ORM model for outbound/inbound carrier trunk definitions.
"""

from sqlalchemy import JSON, Boolean, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from pbx.models.base import Base, TimestampMixin


class SipTrunk(TimestampMixin, Base):
    """Represents a configured SIP trunk to an upstream carrier/provider."""

    __tablename__ = "sip_trunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trunk_id: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, default=5060, server_default="5060")
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    password: Mapped[str] = mapped_column(String(255), nullable=False)
    codec_preferences: Mapped[list | None] = mapped_column(JSON, nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=100, server_default="100")
    max_channels: Mapped[int] = mapped_column(Integer, default=10, server_default="10")
    health_check_interval: Mapped[int] = mapped_column(Integer, default=60, server_default="60")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")

    __table_args__ = (
        Index("ix_sip_trunks_trunk_id", "trunk_id"),
        Index("ix_sip_trunks_enabled", "enabled"),
    )

    def __repr__(self) -> str:
        return f"<SipTrunk(id={self.id}, trunk_id='{self.trunk_id}', name='{self.name}')>"
