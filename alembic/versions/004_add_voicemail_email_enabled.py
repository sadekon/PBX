"""Add voicemail_email_enabled field to extensions table.

Revision ID: 004
Revises: 003
Create Date: 2026-07-31

Adds a per-extension subscription switch for voicemail notification email. This is separate
from voicemail_enabled, which is about whether the extension has a mailbox at all; this
column only governs whether new voicemail is emailed to the address in `email`.

Defaults to true so that extensions already receiving notifications keep receiving them.
Before this column existed, any extension with an email address was emailed unconditionally,
so defaulting to false would silently stop every existing notification on upgrade.
"""

import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "004"
down_revision: str | None = "003"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add voicemail_email_enabled column to extensions table."""
    conn = op.get_bind()
    inspector = sa_inspect(conn)
    columns = [col["name"] for col in inspector.get_columns("extensions")]

    if "voicemail_email_enabled" not in columns:
        op.add_column(
            "extensions",
            sa.Column(
                "voicemail_email_enabled",
                sa.Boolean,
                server_default="1",
                nullable=False,
            ),
        )


def downgrade() -> None:
    """Remove voicemail_email_enabled column from extensions table."""
    conn = op.get_bind()
    inspector = sa_inspect(conn)
    columns = [col["name"] for col in inspector.get_columns("extensions")]

    if "voicemail_email_enabled" in columns:
        op.drop_column("extensions", "voicemail_email_enabled")
