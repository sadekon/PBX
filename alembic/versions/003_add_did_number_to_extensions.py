"""Add did_number column to extensions table.

Revision ID: 003
Revises: 002
Create Date: 2026-07-20

This migration adds an optional, unique DID (Direct Inward Dial) number to each
extension, so a carrier-assigned number can be pointed straight at an extension
without requiring an explicit inbound_routes entry.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "003"
down_revision: str | None = "002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add did_number column and its unique index to extensions table."""
    from sqlalchemy import inspect as sa_inspect

    conn = op.get_bind()
    inspector = sa_inspect(conn)
    columns = [col["name"] for col in inspector.get_columns("extensions")]

    if "did_number" not in columns:
        op.add_column(
            "extensions",
            sa.Column("did_number", sa.String(20), nullable=True),
        )

    indexes = [idx["name"] for idx in inspector.get_indexes("extensions")]
    if "ix_extensions_did_number" not in indexes:
        op.create_index(
            "ix_extensions_did_number", "extensions", ["did_number"], unique=True
        )


def downgrade() -> None:
    """Remove did_number column and its index from extensions table."""
    op.drop_index("ix_extensions_did_number", table_name="extensions")
    op.drop_column("extensions", "did_number")
