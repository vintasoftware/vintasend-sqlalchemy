"""Add the VintaSend 3.8 notification_version column

Adds notification_version, the version token that requeue_notification and repin_notification
check as expected_version. Every existing row is backfilled with its own UUIDv7, then the column
is made NOT NULL.

Revision ID: 4d2e3f5a6b7c
Revises: 3c1a2b4d5e6f
Create Date: 2026-11-11 00:00:00.000000

"""

from collections.abc import Sequence

from vintasend_sqlalchemy.alembic_initial_migration_ops import (
    downgrade_notification_table_from_3_8,
    upgrade_notification_table_to_3_8,
)


revision: str = "4d2e3f5a6b7c"
down_revision: str | None = "3c1a2b4d5e6f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    upgrade_notification_table_to_3_8()


def downgrade() -> None:
    downgrade_notification_table_from_3_8()
