import datetime

import sqlalchemy as sa
from alembic import op


def create_notification_table(user_id_type: type):
    return op.create_table(
        "notifications",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True),
        sa.Column("notification_type", sa.String(50), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("status", sa.String(50), nullable=False, default="PENDING_SEND"),
        sa.Column("body_template", sa.String(255), nullable=False),
        sa.Column(
            "created", sa.DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc)
        ),
        sa.Column(
            "updated",
            sa.DateTime,
            default=lambda: datetime.datetime.now(datetime.timezone.utc),
            onupdate=lambda: datetime.datetime.now(datetime.timezone.utc),
        ),
        sa.Column("subject_template", sa.String(255), nullable=True, default=""),
        sa.Column("preheader_template", sa.String(255), nullable=True, default=""),
        sa.Column("context_name", sa.String(255), nullable=True, default=""),
        sa.Column("context_kwargs", sa.JSON, default=dict),
        sa.Column("context_used", sa.JSON, nullable=True),
        sa.Column("adapter_used", sa.String(255), nullable=True),
        sa.Column("adapter_extra_parameters", sa.JSON, nullable=True),
        sa.Column("send_after", sa.DateTime(), nullable=True),
        sa.Column("user_id", user_id_type(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
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


def upgrade_notification_table_to_2_1(user_id_type: type = sa.Integer):
    """Add the VintaSend 2.1 template-version columns to an existing ``notifications`` table.

    ``requested_template_version`` is which version of its template a notification renders --
    NULL meaning "whatever is current at send time", which is how every notification written
    before this behaves. ``used_template_version`` is the version the renderer reported it
    actually used, written by the service at send time.

    Both are nullable with no backfill: a notification that predates the columns was rendered
    against whatever the template said at the time, and there is no honest value to invent for
    it. ``user_id_type`` is unused here and kept only so this reads like its 2.0 sibling.
    """
    with op.batch_alter_table("notifications") as batch_op:
        batch_op.add_column(sa.Column("requested_template_version", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("used_template_version", sa.Integer(), nullable=True))


def downgrade_notification_table_from_2_1(user_id_type: type = sa.Integer):
    with op.batch_alter_table("notifications") as batch_op:
        batch_op.drop_column("used_template_version")
        batch_op.drop_column("requested_template_version")


def upgrade_notification_table_to_3_8(batch_size: int = 1000, *, not_null: bool = True):
    """Add the ``notification_version`` column to an existing ``notifications`` table.

    ``notification_version`` is the version token ``filter_notification_versions`` hands out and
    ``requeue_notification`` / ``repin_notification`` check as ``expected_version``: a UUIDv7
    that every write to the notification replaces.

    Three steps: add the column as nullable (skipped when it already exists, so a run that failed
    part-way on a database without transactional DDL can be repeated), give every row without a
    token its own fresh UUIDv7, then make the column ``NOT NULL``. The backfill works
    ``batch_size`` rows at a time and reads only ``id`` -- no recipient, context or content is
    selected. It needs a live connection, so it refuses Alembic's offline (``--sql``) mode.

    For a rolling deploy, where instances running the previous release keep inserting rows without
    the column, pass ``not_null=False`` here and call
    ``finish_notification_table_upgrade_to_3_8()`` in a later revision, once none is left.
    """
    _require_online_migration()
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("notifications")}
    if "notification_version" not in columns:
        with op.batch_alter_table("notifications") as batch_op:
            batch_op.add_column(sa.Column("notification_version", sa.Uuid(), nullable=True))
    if not_null:
        finish_notification_table_upgrade_to_3_8(batch_size)
    else:
        _backfill_notification_versions(batch_size)


def finish_notification_table_upgrade_to_3_8(batch_size: int = 1000):
    """Give every row still without a ``notification_version`` one, then make it ``NOT NULL``.

    The second half of ``upgrade_notification_table_to_3_8(not_null=False)``: run it once no
    instance of the previous release is writing, so it also covers the rows they inserted in
    between. Running it on a table that is already done changes nothing but the constraint.
    """
    _require_online_migration()
    _backfill_notification_versions(batch_size)
    with op.batch_alter_table("notifications") as batch_op:
        batch_op.alter_column("notification_version", existing_type=sa.Uuid(), nullable=False)


def _require_online_migration() -> None:
    if op.get_context().as_sql:
        raise RuntimeError(
            "The notification_version backfill reads and writes rows, so it needs a live "
            "database connection: run this migration online, not with --sql."
        )


def _backfill_notification_versions(batch_size: int) -> None:
    from vintasend.utils.uuid_utils import uuid7

    notifications = sa.table(
        "notifications",
        sa.column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite")),
        sa.column("notification_version", sa.Uuid()),
    )
    backfill = (
        sa.update(notifications)
        .where(notifications.c.id == sa.bindparam("row_id"))
        .where(notifications.c.notification_version.is_(None))
        .values(notification_version=sa.bindparam("row_version"))
    )
    bind = op.get_bind()
    while True:
        ids = (
            bind.execute(
                sa.select(notifications.c.id)
                .where(notifications.c.notification_version.is_(None))
                .order_by(notifications.c.id)
                .limit(batch_size)
            )
            .scalars()
            .all()
        )
        if not ids:
            break
        bind.execute(backfill, [{"row_id": row_id, "row_version": uuid7()} for row_id in ids])


def downgrade_notification_table_from_3_8():
    with op.batch_alter_table("notifications") as batch_op:
        batch_op.drop_column("notification_version")


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
    op.create_index("ix_attachment_file_records_checksum", "attachment_file_records", ["checksum"])
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
        sa.ForeignKeyConstraint(["notification_id"], ["notifications.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["file_id"], ["attachment_file_records.id"]),
    )
    op.create_index(
        "ix_notification_attachments_notification_id",
        "notification_attachments",
        ["notification_id"],
    )
    op.create_index("ix_notification_attachments_file_id", "notification_attachments", ["file_id"])


def drop_attachment_tables():
    op.drop_index("ix_notification_attachments_file_id", table_name="notification_attachments")
    op.drop_index(
        "ix_notification_attachments_notification_id", table_name="notification_attachments"
    )
    op.drop_table("notification_attachments")
    op.drop_index("ix_attachment_file_records_checksum_size", table_name="attachment_file_records")
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
