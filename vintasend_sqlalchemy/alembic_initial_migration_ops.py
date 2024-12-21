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