import datetime

import sqlalchemy as sa
from alembic import op


def create_notification_table(user_id_type: type):
    return op.create_table('notifications',
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True),
        sa.Column('notification_type', sa.String(50), nullable=False),
        sa.Column('title', sa.String(255), nullable=False),
        sa.Column('status', sa.String(50), nullable=False, default="PENDING_SEND"),
        sa.Column('body_template', sa.String(255), nullable=False),
        sa.Column(
            'created', sa.DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc)
        ),
        sa.Column(
            'updated',
            sa.DateTime,
            default=lambda: datetime.datetime.now(datetime.timezone.utc),
            onupdate=lambda: datetime.datetime.now(datetime.timezone.utc),
        ),
        sa.Column('subject_template', sa.String(255), nullable=True, default=""),
        sa.Column('preheader_template', sa.String(255), nullable=True, default=""),
        sa.Column('context_name', sa.String(255), nullable=True, default=""),
        sa.Column('context_kwargs', sa.JSON, default=dict),
        sa.Column('context_used', sa.JSON, nullable=True),
        sa.Column('adapter_used', sa.String(255), nullable=True),
        sa.Column('adapter_extra_parameters', sa.JSON, nullable=True),
        sa.Column('send_after', sa.DateTime(), nullable=True),
        sa.Column('user_id', user_id_type(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'])
    )


def upgrade_notification_table_to_2_0(user_id_type: type = sa.Integer):
    """Add the VintaSend 2.0 columns to an existing ``notifications`` table.

    Adds the one-off recipient fields (``email_or_phone`` / ``first_name`` / ``last_name``),
    the delivery/read timestamps (``sent_at`` / ``read_at``), the ``tenant`` partition key and
    the system-managed ``git_commit_sha``, and relaxes ``user_id`` to nullable so a one-off
    notification (no user, recipient carried inline) can live in the same table. Uses batch mode
    so it works on SQLite, which cannot ALTER a column's nullability in place.
    """
    with op.batch_alter_table("notifications") as batch_op:
        batch_op.add_column(sa.Column("email_or_phone", sa.String(255), nullable=True))
        batch_op.add_column(sa.Column("first_name", sa.String(255), nullable=True))
        batch_op.add_column(sa.Column("last_name", sa.String(255), nullable=True))
        batch_op.add_column(sa.Column("sent_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("read_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("tenant", sa.String(255), nullable=True))
        batch_op.add_column(sa.Column("git_commit_sha", sa.String(40), nullable=True))
        batch_op.alter_column("user_id", existing_type=user_id_type(), nullable=True)
    op.create_index("ix_notifications_tenant", "notifications", ["tenant"])


def create_attachment_tables():
    """Create the ``attachment_file_records`` and ``notification_attachments`` tables.

    ``attachment_file_records`` is the checksum-indexed blob description; the join table links a
    notification to a stored file, carrying the per-notification ``is_inline`` / ``description``.
    """
    op.create_table(
        "attachment_file_records",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(255), nullable=False, server_default=""),
        sa.Column("size", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("checksum", sa.String(64), nullable=False, server_default=""),
        sa.Column("storage_identifiers", sa.JSON, nullable=False),
        sa.Column("created", sa.DateTime()),
        sa.Column("updated", sa.DateTime()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_attachment_file_records_checksum", "attachment_file_records", ["checksum"]
    )
    op.create_index(
        "ix_attachment_file_records_checksum_size",
        "attachment_file_records",
        ["checksum", "size"],
    )

    op.create_table(
        "notification_attachments",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True),
        sa.Column(
            "notification_id",
            sa.BigInteger().with_variant(sa.Integer, "sqlite"),
            nullable=False,
        ),
        sa.Column(
            "file_id",
            sa.BigInteger().with_variant(sa.Integer, "sqlite"),
            nullable=False,
        ),
        sa.Column("description", sa.String(255), nullable=True),
        sa.Column("is_inline", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created", sa.DateTime()),
        sa.Column("updated", sa.DateTime()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["notification_id"], ["notifications.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["file_id"], ["attachment_file_records.id"]),
    )
    op.create_index(
        "ix_notification_attachments_notification_id",
        "notification_attachments",
        ["notification_id"],
    )
    op.create_index(
        "ix_notification_attachments_file_id", "notification_attachments", ["file_id"]
    )


def drop_attachment_tables():
    op.drop_index("ix_notification_attachments_file_id", table_name="notification_attachments")
    op.drop_index(
        "ix_notification_attachments_notification_id", table_name="notification_attachments"
    )
    op.drop_table("notification_attachments")
    op.drop_index(
        "ix_attachment_file_records_checksum_size", table_name="attachment_file_records"
    )
    op.drop_index("ix_attachment_file_records_checksum", table_name="attachment_file_records")
    op.drop_table("attachment_file_records")


def downgrade_notification_table_from_2_0(user_id_type: type = sa.Integer):
    op.drop_index("ix_notifications_tenant", table_name="notifications")
    with op.batch_alter_table("notifications") as batch_op:
        batch_op.alter_column("user_id", existing_type=user_id_type(), nullable=False)
        batch_op.drop_column("git_commit_sha")
        batch_op.drop_column("tenant")
        batch_op.drop_column("read_at")
        batch_op.drop_column("sent_at")
        batch_op.drop_column("last_name")
        batch_op.drop_column("first_name")
        batch_op.drop_column("email_or_phone")
