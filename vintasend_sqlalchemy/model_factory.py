import datetime
import uuid
from typing import Any, Generic, TypeVar

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.orm.decl_api import DeclarativeAttributeIntercept


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class Base(DeclarativeBase):
    pass


class NotificationMixin(Base):
    __abstract__ = True
    id: Mapped[int] = mapped_column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True)  # noqa: A003
    notification_type: Mapped[str] = mapped_column("notification_type", String(50), nullable=False)
    title: Mapped[str] = mapped_column("title", String(255), nullable=False)
    status: Mapped[str] = mapped_column("status", String(50), nullable=False, default="PENDING_SEND")
    body_template: Mapped[str] = mapped_column("body_template", String(255), nullable=False)

    # One-off recipient fields. A one-off notification has no ``user_id`` and instead carries
    # the recipient inline. Kept nullable/blank so a regular user notification simply leaves
    # them empty.
    email_or_phone: Mapped[str | None] = mapped_column(
        "email_or_phone", String(255), nullable=True, default=""
    )
    first_name: Mapped[str | None] = mapped_column(
        "first_name", String(255), nullable=True, default=""
    )
    last_name: Mapped[str | None] = mapped_column(
        "last_name", String(255), nullable=True, default=""
    )

    created: Mapped[datetime.datetime] = mapped_column("created", DateTime, default=_utcnow)
    updated: Mapped[datetime.datetime] = mapped_column(
        "updated", DateTime, default=_utcnow, onupdate=_utcnow
    )

    # Set by mark_pending_as_sent / mark_sent_as_read at delivery and read time. Kept nullable
    # so a pending or never-read notification carries no timestamp -- the filter vocabulary's
    # ``sent_at_range`` / ``read_at_range`` rely on a NULL never matching a positive range.
    sent_at: Mapped[datetime.datetime | None] = mapped_column("sent_at", DateTime, nullable=True)
    read_at: Mapped[datetime.datetime | None] = mapped_column("read_at", DateTime, nullable=True)

    # Optional multi-tenant partition key. Nullable (not "") so a tenant-less row is
    # distinguishable from one whose tenant is the empty string, which the filter NULL
    # semantics require.
    tenant: Mapped[str | None] = mapped_column("tenant", String(255), nullable=True, index=True)

    # System-managed: written only by NotificationService (through ``store_git_commit_sha``)
    # at send time, always already normalized to 40 lowercase hex characters.
    git_commit_sha: Mapped[str | None] = mapped_column(
        "git_commit_sha", String(40), nullable=True
    )

    # Email specific fields
    subject_template: Mapped[str] = mapped_column("subject_template", String(255), nullable=True, default="")
    preheader_template: Mapped[str] = mapped_column("preheader_template", String(255), nullable=True, default="")
    context_name: Mapped[str] = mapped_column("context_name", String(255), nullable=True, default="")
    context_kwargs: Mapped[dict] = mapped_column("context_kwargs", JSON, default=dict)
    adapter_used: Mapped[str] = mapped_column("adapter_used", String(255), nullable=True)
    context_used: Mapped[dict | None] = mapped_column("context_used", JSON, nullable=True)
    adapter_extra_parameters: Mapped[dict | None] = mapped_column("adapter_extra_parameters", JSON, nullable=True)

    send_after = mapped_column("send_after", DateTime, nullable=True)

    def __str__(self):
        return f"{self.get_user()} - {self.notification_type} - {self.title} - {self.status}{f' (scheduled to {self.send_after})' if self.send_after else ''}"

    def get_user(self) -> Any:
        raise NotImplementedError

    def get_user_id(self) -> Any:
        raise NotImplementedError

    def get_user_email(self) -> str:
        raise NotImplementedError

    @staticmethod
    def get_user_id_attr_name() -> str:
        raise NotImplementedError

    @staticmethod
    def get_user_attr_name() -> str:
        raise NotImplementedError

    def set_user_id(self, user_id: Any):
        raise NotImplementedError


class AttachmentFileRecord(Base):
    """A checksum-indexed, stored blob. One record can back many notifications.

    The bytes live wherever the injected attachment manager put them; this row only
    describes the blob and carries the manager's opaque ``storage_identifiers`` back to
    it for reconstruction and deletion. The backend never opens the file.
    """

    __tablename__ = "attachment_file_records"

    id: Mapped[int] = mapped_column(  # noqa: A003
        "id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True
    )
    filename: Mapped[str] = mapped_column("filename", String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(
        "content_type", String(255), nullable=False, default=""
    )
    size: Mapped[int] = mapped_column("size", BigInteger, nullable=False, default=0)
    # sha256 hex digest, indexed for the (checksum, size) dedup lookup.
    checksum: Mapped[str] = mapped_column(
        "checksum", String(64), nullable=False, default="", index=True
    )
    # Opaque, manager-defined identifiers. Must carry a non-empty ``id``; every other key
    # belongs to whichever manager wrote the bytes.
    storage_identifiers: Mapped[dict] = mapped_column(
        "storage_identifiers", JSON, nullable=False, default=dict
    )
    created: Mapped[datetime.datetime] = mapped_column("created", DateTime, default=_utcnow)
    updated: Mapped[datetime.datetime] = mapped_column(
        "updated", DateTime, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (Index("ix_attachment_file_records_checksum_size", "checksum", "size"),)

    def __str__(self):
        return f"{self.filename} ({self.checksum[:12]})"


class NotificationAttachment(Base):
    """Join row linking a notification to a stored ``AttachmentFileRecord``.

    ``is_inline`` / ``description`` live here rather than on the file record because they
    describe how *this* notification uses the file, not the file itself. Deleting this row
    drops one reference; the file record survives until nothing references it.
    """

    __tablename__ = "notification_attachments"

    id: Mapped[int] = mapped_column(  # noqa: A003
        "id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True
    )
    notification_id: Mapped[int] = mapped_column(
        "notification_id",
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("notifications.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    file_id: Mapped[int] = mapped_column(
        "file_id",
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("attachment_file_records.id"),
        nullable=False,
        index=True,
    )
    description: Mapped[str | None] = mapped_column("description", String(255), nullable=True)
    is_inline: Mapped[bool] = mapped_column("is_inline", Boolean, nullable=False, default=False)
    created: Mapped[datetime.datetime] = mapped_column("created", DateTime, default=_utcnow)
    updated: Mapped[datetime.datetime] = mapped_column(
        "updated", DateTime, default=_utcnow, onupdate=_utcnow
    )

    file: Mapped["AttachmentFileRecord"] = relationship("AttachmentFileRecord")


UserType = TypeVar('UserType', bound=DeclarativeBase)
UserPrimaryKeyType = TypeVar('UserPrimaryKeyType', int, str, uuid.UUID)


class NotificationMeta(DeclarativeAttributeIntercept):
    def __new__(cls, name, bases, dct, user_model, user_primary_key_field_name, user_primary_key_field_type):
        # user_id is nullable so a one-off notification (no user, recipient carried inline
        # via email_or_phone/first_name/last_name) can live in the same table.
        if user_primary_key_field_type == int:
            dct['user_id'] = mapped_column(ForeignKey(getattr(user_model, user_primary_key_field_name)), nullable=True)
            dct['set_user_id'] = lambda self, user_id: setattr(self, 'user_id', user_id)
        elif user_primary_key_field_type == str:
            dct['user_id'] = mapped_column(ForeignKey(getattr(user_model, user_primary_key_field_name)), nullable=True)
            dct['set_user_id'] = lambda self, user_id: setattr(self, 'user_id', user_id)
        elif user_primary_key_field_type == uuid.UUID:
            dct['user_id'] = mapped_column(ForeignKey(getattr(user_model, user_primary_key_field_name)), nullable=True)
            dct['set_user_id'] = lambda self, user_id: setattr(self, 'user_id', user_id)

        dct['user'] = relationship(user_model, backref="notifications")
        dct['get_user_id'] = lambda self: self.user_id
        dct['get_user'] = lambda self: self.user
        dct['__tablename__'] = "notifications"
        dct['__tableargs__'] = {"extend_existing": True}

        return super().__new__(cls, name, bases, dct)


class GenericNotification(
    NotificationMixin,
    Generic[UserType, UserPrimaryKeyType],
):
    __abstract__ = True

    user: Mapped[UserType]
    user_id: Mapped[UserPrimaryKeyType]

    def get_user_id(self) -> UserPrimaryKeyType:
        raise NotImplementedError

    def set_user_id(self, user_id: UserPrimaryKeyType) -> None:
        raise NotImplementedError

    def get_user(self) -> UserType:
        raise NotImplementedError

    @staticmethod
    def get_user_id_attr_name() -> str:
        return "user_id"

    @staticmethod
    def get_user_attr_name() -> str:
        return "user"
