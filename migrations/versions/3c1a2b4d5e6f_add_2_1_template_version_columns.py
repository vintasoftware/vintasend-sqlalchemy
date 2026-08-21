"""Add VintaSend 2.1 template-version columns

Adds requested_template_version (which version of its template a notification renders) and
used_template_version (the version the renderer reported it actually used) to the
notifications table. Both nullable, with no backfill: a notification that predates them was
rendered against whatever the template said at the time.

Revision ID: 3c1a2b4d5e6f
Revises: 2b0f1c2d3e4f
Create Date: 2026-08-21 00:00:00.000000

"""

from collections.abc import Sequence

from vintasend_sqlalchemy.alembic_initial_migration_ops import (
    downgrade_notification_table_from_2_1,
    upgrade_notification_table_to_2_1,
)


# revision identifiers, used by Alembic.
revision: str = "3c1a2b4d5e6f"
down_revision: str | None = "2b0f1c2d3e4f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    upgrade_notification_table_to_2_1()


def downgrade() -> None:
    downgrade_notification_table_from_2_1()
