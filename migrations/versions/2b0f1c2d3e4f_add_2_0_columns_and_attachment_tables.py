"""Add VintaSend 2.0 columns and attachment tables

Adds the one-off recipient fields, sent_at/read_at, tenant, git_commit_sha (and relaxes
user_id to nullable) to the notifications table, and creates the attachment_file_records +
notification_attachments tables backing the attachment manager seam.

Revision ID: 2b0f1c2d3e4f
Revises: 8b1baef54852
Create Date: 2026-07-23 00:00:00.000000

"""
from typing import Sequence, Union

from vintasend_sqlalchemy.alembic_initial_migration_ops import (
    create_attachment_tables,
    downgrade_notification_table_from_2_0,
    drop_attachment_tables,
    upgrade_notification_table_to_2_0,
)


# revision identifiers, used by Alembic.
revision: str = '2b0f1c2d3e4f'
down_revision: Union[str, None] = '8b1baef54852'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    upgrade_notification_table_to_2_0()
    create_attachment_tables()


def downgrade() -> None:
    drop_attachment_tables()
    downgrade_notification_table_from_2_0()
