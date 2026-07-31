"""Add sip_port to registered_phones.

Revision ID: 005
Revises: 004
Create Date: 2026-07-31

Registrations record the phone's IP but not the port it registered from, so recovering a
registration after a restart had to assume 5060. Any phone registering from another port was
then sent INVITEs at the wrong port: it appeared registered and never rang.

Nullable, because rows written before this column exists have no port to record. The call
router falls back to 5060 for those rather than refusing to recover them.
"""

import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "005"
down_revision: str | None = "004"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add sip_port column to registered_phones table."""
    conn = op.get_bind()
    inspector = sa_inspect(conn)
    if "registered_phones" not in inspector.get_table_names():
        return

    columns = [col["name"] for col in inspector.get_columns("registered_phones")]
    if "sip_port" not in columns:
        op.add_column("registered_phones", sa.Column("sip_port", sa.Integer, nullable=True))


def downgrade() -> None:
    """Remove sip_port column from registered_phones table."""
    conn = op.get_bind()
    inspector = sa_inspect(conn)
    if "registered_phones" not in inspector.get_table_names():
        return

    columns = [col["name"] for col in inspector.get_columns("registered_phones")]
    if "sip_port" in columns:
        op.drop_column("registered_phones", "sip_port")
