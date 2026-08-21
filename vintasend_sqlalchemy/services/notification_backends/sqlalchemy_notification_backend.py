import asyncio
import datetime
import uuid
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast

from sqlalchemy import (
    ColumnElement,
    CursorResult,
    Result,
    and_,
    false,
    func,
    not_,
    or_,
    select,
    true,
    update,
)
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, joinedload, sessionmaker
from vintasend.app_settings import NotificationSettings
from vintasend.constants import NotificationStatus, NotificationTypes
from vintasend.exceptions import (
    AttachmentFileNotFoundError,
    NotificationCancelError,
    NotificationError,
    NotificationNotFoundError,
    NotificationUpdateError,
    UnconfirmedNotificationUpdateError,
)
from vintasend.services.attachment_managers.asyncio_base import AsyncIOBaseAttachmentManager
from vintasend.services.attachment_managers.base import BaseAttachmentManager
from vintasend.services.dataclasses import (
    AnyNotificationAttachment,
    Notification,
    NotificationAttachment,
    OneOffNotification,
    StoredAttachment,
    UpdateNotificationKwargs,
    is_attachment_reference,
)
from vintasend.services.dataclasses import (
    AttachmentFileRecord as AttachmentFileRecordDataclass,
)
from vintasend.services.notification_backends.asyncio_base import AsyncIOBaseNotificationBackend
from vintasend.services.notification_backends.base import BaseNotificationBackend
from vintasend.services.notification_backends.filters import (
    NotificationFilter,
    NotificationOrderBy,
    is_field_filter,
    is_template_version_value,
)

from vintasend_sqlalchemy.model_factory import (
    AttachmentFileRecord as AttachmentFileRecordModel,
)
from vintasend_sqlalchemy.model_factory import (
    NotificationAttachment as NotificationAttachmentModel,
)
from vintasend_sqlalchemy.services.attachment_managers.filesystem import (
    FilesystemAsyncIOAttachmentManager,
    FilesystemAttachmentManager,
)


if TYPE_CHECKING:
    from vintasend_sqlalchemy.model_factory import NotificationMixin


NotificationModel = TypeVar("NotificationModel", bound="NotificationMixin")


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _rowcount(result: "Result[Any]") -> int:
    # AsyncSession.execute is typed as returning a plain Result, but an UPDATE comes back as a
    # CursorResult, which is where rowcount lives. Check instead of assuming, so a dialect that
    # ever returns something else fails here rather than as an AttributeError further down.
    if not isinstance(result, CursorResult):
        raise UnconfirmedNotificationUpdateError(
            f"Expected an UPDATE to return a CursorResult, got {type(result).__name__}"
        )
    return result.rowcount


# ---------------------------------------------------------------------------- filter translation
#
# Filter-field name -> notification ORM column, split by how each is matched. Mirrors the maps in
# ``vintasend.services.notification_backends.filters`` so this SQLAlchemy translation stays
# faithful to the reference in-memory evaluator. The one divergence from the core dataclass maps
# is ``updated_at`` -> ``updated`` (the ORM column) rather than ``modified`` (the dataclass field).
_MEMBERSHIP_FIELDS: dict[str, str] = {
    "status": "status",
    "notification_type": "notification_type",
    "adapter_used": "adapter_used",
    "user_id": "user_id",
    "tenant": "tenant",
}
_STRING_LOOKUP_FIELDS = frozenset({"body_template", "subject_template", "context_name"})
# Integer membership, kept apart from ``_MEMBERSHIP_FIELDS`` because these columns are
# ``Integer``: a candidate that is not an ``int`` has to be rejected before it reaches
# ``IN``, where the driver would raise on the bind rather than return no rows.
_VERSION_FIELDS: dict[str, str] = {
    "requested_template_version": "requested_template_version",
    "used_template_version": "used_template_version",
}
_RANGE_FIELDS: dict[str, str] = {
    "send_after_range": "send_after",
    "created_at_range": "created",
    "sent_at_range": "sent_at",
    "read_at_range": "read_at",
}
_ORDER_FIELD_TO_COLUMN: dict[str, str] = {
    "send_after": "send_after",
    "sent_at": "sent_at",
    "read_at": "read_at",
    "created_at": "created",
    "updated_at": "updated",
}


def _normalize_membership_value(value: object) -> object:
    return value.value if hasattr(value, "value") else value


def _string_lookup_expr(column: "ColumnElement", spec: object) -> "ColumnElement":
    if isinstance(spec, dict):
        lookup = spec.get("lookup", "exact")
        value = spec.get("value", "")
        case_sensitive = spec.get("case_sensitive", True)
    else:
        lookup = "exact"
        value = spec
        case_sensitive = True

    if lookup == "exact":
        if case_sensitive:
            return column == value
        return func.lower(column) == str(value).lower()

    if lookup == "starts_with":
        pattern = f"{value}%"
    elif lookup == "ends_with":
        pattern = f"%{value}"
    elif lookup == "includes":
        pattern = f"%{value}%"
    else:
        # Unknown lookup: mirror the reference evaluator, which never matches.
        return false()

    return column.ilike(pattern) if not case_sensitive else column.like(pattern)


def _range_expr(column: "ColumnElement", spec: object) -> "ColumnElement":
    if not isinstance(spec, dict):
        return true()
    conditions: list[ColumnElement] = []
    lower = spec.get("from")
    upper = spec.get("to")
    if lower is not None:
        conditions.append(column >= lower)
    if upper is not None:
        conditions.append(column <= upper)
    if not conditions:
        return true()
    return and_(*conditions)


def _field_leaf(
    model: "type[NotificationModel]", field: str, value: object
) -> "tuple[ColumnElement, ColumnElement | None]":
    """Positive expression for one field filter, plus the column to OR ``IS NULL`` on when this
    leaf is negated. Returns the match-nothing expression (and no null column) for an unknown
    field, mirroring the reference evaluator's "unknown field never matches"."""
    if field in _RANGE_FIELDS:
        column = getattr(model, _RANGE_FIELDS[field])
        return _range_expr(column, value), column
    if field in _STRING_LOOKUP_FIELDS:
        column = getattr(model, field)
        return _string_lookup_expr(column, value), column
    if field in _VERSION_FIELDS:
        column = getattr(model, _VERSION_FIELDS[field])
        versions = list(value) if isinstance(value, (list, tuple, set)) else [value]
        if not all(is_template_version_value(version) for version in versions):
            # One bad candidate rejects the whole leaf, mirroring the reference evaluator.
            return false(), None
        return column.in_(versions), column
    if field in _MEMBERSHIP_FIELDS:
        column = getattr(model, _MEMBERSHIP_FIELDS[field])
        values = value if isinstance(value, (list, tuple, set)) else [value]
        normalized = [_normalize_membership_value(v) for v in values]
        return column.in_(normalized), column
    return false(), None


def _and_all(expressions: "list[ColumnElement]") -> "ColumnElement":
    if not expressions:
        return true()
    return and_(*expressions)


def _or_all(expressions: "list[ColumnElement]") -> "ColumnElement":
    if not expressions:
        return false()
    return or_(*expressions)


def build_filter_expression(
    model: "type[NotificationModel]",
    filter: NotificationFilter,  # noqa: A002
    negated: bool = False,
) -> "ColumnElement":
    """Translate a composable filter to a SQLAlchemy boolean expression, pushing negation to the
    leaves so NULL semantics stay correct: a positive leaf excludes NULL rows, while a negated
    leaf ORs in ``column IS NULL`` so NULL rows ARE included under ``not`` -- exactly what the
    reference in-memory evaluator does.
    """
    if "and" in filter:
        subs = [build_filter_expression(model, sub, negated) for sub in filter["and"]]  # type: ignore[typeddict-item]
        return _or_all(subs) if negated else _and_all(subs)  # De Morgan under negation
    if "or" in filter:
        subs = [build_filter_expression(model, sub, negated) for sub in filter["or"]]  # type: ignore[typeddict-item]
        return _and_all(subs) if negated else _or_all(subs)
    if "not" in filter:
        return build_filter_expression(model, filter["not"], not negated)  # type: ignore[typeddict-item]

    # Field filter. Empty ``{}`` matches everything (or nothing when negated). Multiple keys are
    # an implicit AND (OR under negation, by De Morgan).
    if not is_field_filter(filter):
        return true() if negated else false()
    items = list(filter.items())
    if not items:
        return false() if negated else true()

    leaf_expressions: list[ColumnElement] = []
    for key, value in items:
        positive, null_column = _field_leaf(model, key, value)
        if not negated:
            leaf_expressions.append(positive)
        else:
            negated_expr = not_(positive)
            if null_column is not None:
                negated_expr = or_(negated_expr, null_column.is_(None))
            leaf_expressions.append(negated_expr)
    return _or_all(leaf_expressions) if negated else _and_all(leaf_expressions)


# ------------------------------------------------------------------------- dataclass serialization


def _serialize_file_record(record: AttachmentFileRecordModel) -> AttachmentFileRecordDataclass:
    return AttachmentFileRecordDataclass(
        id=str(record.id),
        filename=record.filename,
        content_type=record.content_type,
        size=record.size,
        checksum=record.checksum,
        created_at=record.created,
        updated_at=record.updated,
        storage_identifiers=record.storage_identifiers or {},
    )


def _stored_attachment(
    manager, join_row: NotificationAttachmentModel, record: AttachmentFileRecordModel
) -> StoredAttachment:
    attachment_file = manager.reconstruct_attachment_file(record.storage_identifiers or {})
    return StoredAttachment(
        id=str(join_row.id),
        filename=record.filename,
        content_type=record.content_type,
        size=record.size,
        checksum=record.checksum,
        created_at=record.created,
        file=attachment_file,
        description=join_row.description,
        is_inline=join_row.is_inline,
        file_id=str(record.id),
        storage_identifiers=record.storage_identifiers or {},
    )


def _user_notification_from_orm(
    notification: "NotificationModel", attachments: list[StoredAttachment]
) -> Notification:
    return Notification(
        id=notification.id,
        user_id=notification.get_user_id(),
        notification_type=notification.notification_type,
        title=notification.title,
        body_template=notification.body_template,
        context_name=notification.context_name,
        context_kwargs=notification.context_kwargs,
        send_after=notification.send_after,
        subject_template=notification.subject_template,
        preheader_template=notification.preheader_template,
        status=notification.status,
        context_used=notification.context_used,
        adapter_used=notification.adapter_used or None,
        adapter_extra_parameters=notification.adapter_extra_parameters,
        created=notification.created,
        modified=notification.updated,
        sent_at=notification.sent_at,
        read_at=notification.read_at,
        tenant=notification.tenant,
        git_commit_sha=notification.git_commit_sha,
        requested_template_version=notification.requested_template_version,
        used_template_version=notification.used_template_version,
        attachments=attachments,
    )


def _one_off_notification_from_orm(
    notification: "NotificationModel", attachments: list[StoredAttachment]
) -> OneOffNotification:
    return OneOffNotification(
        id=notification.id,
        email_or_phone=notification.email_or_phone or "",
        first_name=notification.first_name or "",
        last_name=notification.last_name or "",
        notification_type=notification.notification_type,
        title=notification.title,
        body_template=notification.body_template,
        context_name=notification.context_name,
        context_kwargs=notification.context_kwargs,
        send_after=notification.send_after,
        subject_template=notification.subject_template,
        preheader_template=notification.preheader_template,
        status=notification.status,
        context_used=notification.context_used,
        adapter_used=notification.adapter_used or None,
        adapter_extra_parameters=notification.adapter_extra_parameters,
        created=notification.created,
        modified=notification.updated,
        sent_at=notification.sent_at,
        read_at=notification.read_at,
        tenant=notification.tenant,
        git_commit_sha=notification.git_commit_sha,
        requested_template_version=notification.requested_template_version,
        used_template_version=notification.used_template_version,
        attachments=attachments,
    )


class SQLAlchemyNotificationBackend(Generic[NotificationModel], BaseNotificationBackend):
    session: sessionmaker[Session]
    notification_model_cls: "type[NotificationModel]"

    def __init__(
        self, session: sessionmaker[Session], notification_model_cls: "type[NotificationModel]"
    ) -> None:
        super().__init__(session == session, notification_model_cls=notification_model_cls)
        self.session_manager = session
        self.notification_model_cls = (
            notification_model_cls if notification_model_cls else self._get_notification_model_cls()
        )
        # Default to a filesystem-backed manager so the backend is usable standalone; the
        # service replaces this through inject_attachment_manager when one is configured.
        self._attachment_manager: BaseAttachmentManager = FilesystemAttachmentManager()

    def _get_notification_model_cls(self) -> "type[NotificationModel]":
        notification_model_cls = NotificationSettings().get_notification_model_cls()
        if notification_model_cls is None:
            raise NotificationError("Notification model class not set in settings")

        return notification_model_cls

    # ---------------------------------------------------------------------- queries

    def _get_all_in_app_unread_notifications_query(
        self, session: Session, user_id: int | str | uuid.UUID
    ):
        return (
            session.query(self.notification_model_cls)
            .where(
                getattr(
                    self.notification_model_cls, self.notification_model_cls.get_user_id_attr_name()
                )
                == user_id,
                self.notification_model_cls.status == NotificationStatus.SENT.value,
                self.notification_model_cls.notification_type == NotificationTypes.IN_APP.value,
            )
            .order_by(
                self.notification_model_cls.created.desc(), self.notification_model_cls.id.desc()
            )
        )

    def _get_all_in_app_notifications_query(self, session: Session, user_id: int | str | uuid.UUID):
        return (
            session.query(self.notification_model_cls)
            .where(
                getattr(
                    self.notification_model_cls, self.notification_model_cls.get_user_id_attr_name()
                )
                == user_id,
                self.notification_model_cls.status.in_(
                    [NotificationStatus.SENT.value, NotificationStatus.READ.value]
                ),
                self.notification_model_cls.notification_type == NotificationTypes.IN_APP.value,
            )
            .order_by(
                self.notification_model_cls.created.desc(), self.notification_model_cls.id.desc()
            )
        )

    def _get_all_future_notifications_query(self, session: Session):
        return (
            session.query(self.notification_model_cls)
            .where(
                self.notification_model_cls.status == NotificationStatus.PENDING_SEND.value,
                self.notification_model_cls.send_after > datetime.datetime.now(),
            )
            .order_by(self.notification_model_cls.created)
        )

    def _get_all_future_notifications_from_user_query(
        self, session: Session, user_id: int | str | uuid.UUID
    ):
        return (
            session.query(self.notification_model_cls)
            .where(
                self.notification_model_cls.status == NotificationStatus.PENDING_SEND.value,
                self.notification_model_cls.send_after > datetime.datetime.now(),
                getattr(
                    self.notification_model_cls, self.notification_model_cls.get_user_id_attr_name()
                )
                == user_id,
            )
            .order_by(self.notification_model_cls.created)
        )

    # ---------------------------------------------------------------------- serialization

    def serialize_notification(
        self, notification: "NotificationModel"
    ) -> Notification | OneOffNotification:
        attachments = list(self.get_attachments(notification.id))
        if getattr(notification, "user_id", None):
            return _user_notification_from_orm(notification, attachments)
        return _one_off_notification_from_orm(notification, attachments)

    def serialize_user_notification(self, notification: "NotificationModel") -> Notification:
        return _user_notification_from_orm(
            notification, list(self.get_attachments(notification.id))
        )

    # ---------------------------------------------------------------------- attachments

    def _store_attachments(
        self,
        attachments: list[AnyNotificationAttachment],
        notification_id: int | str | uuid.UUID,
    ) -> list[StoredAttachment]:
        """Persist attachments, delegating every byte operation to the injected manager.

        Uploads are deduplicated on (checksum, size): a matching existing file record is reused
        and no upload happens; otherwise the manager stores the bytes and a new record is
        persisted. A reference attaches an already-stored file by id, raising
        ``AttachmentFileNotFoundError`` if that id is unknown. Either path writes one join row.
        """
        manager = self._attachment_manager
        stored_attachments: list[StoredAttachment] = []

        with self.session_manager.begin() as session:
            for attachment in attachments:
                if is_attachment_reference(attachment):
                    record = self._load_file_record(session, attachment.file_id)
                    join_row = NotificationAttachmentModel(
                        notification_id=notification_id,
                        file_id=record.id,
                        description=attachment.description,
                        is_inline=attachment.is_inline,
                    )
                    session.add(join_row)
                    session.flush()
                    stored_attachments.append(_stored_attachment(manager, join_row, record))
                    continue

                # TypeGuard narrows only the reference branch, so restate the upload type.
                assert isinstance(attachment, NotificationAttachment)  # noqa: S101

                # Read the bytes once, up front, so the checksum lookup and (on a miss) the
                # upload never re-read the same path/URL/stream twice.
                data = manager.file_to_bytes(attachment.file)
                checksum = manager.calculate_checksum(data)
                existing = (
                    session.query(AttachmentFileRecordModel)
                    .filter(
                        AttachmentFileRecordModel.checksum == checksum,
                        AttachmentFileRecordModel.size == len(data),
                    )
                    .first()
                )
                if existing is not None:
                    record = existing
                else:
                    file_record = manager.upload_file(
                        data, attachment.filename, attachment.content_type
                    )
                    record = AttachmentFileRecordModel(
                        filename=file_record.filename,
                        content_type=file_record.content_type or "",
                        size=file_record.size,
                        checksum=file_record.checksum,
                        storage_identifiers=file_record.storage_identifiers,
                    )
                    session.add(record)
                    session.flush()

                join_row = NotificationAttachmentModel(
                    notification_id=notification_id,
                    file_id=record.id,
                    description=attachment.description,
                    is_inline=attachment.is_inline,
                )
                session.add(join_row)
                session.flush()
                stored_attachments.append(_stored_attachment(manager, join_row, record))

        return stored_attachments

    def _attach_stored_attachments(
        self,
        notification_id: int | str | uuid.UUID,
        attachments: list[StoredAttachment],
    ) -> None:
        """Link already-stored files to a notification by writing join rows only (resend path)."""
        with self.session_manager.begin() as session:
            for attachment in attachments:
                file_id = attachment.file_id or attachment.id
                record = self._load_file_record(session, file_id)
                session.add(
                    NotificationAttachmentModel(
                        notification_id=notification_id,
                        file_id=record.id,
                        description=attachment.description,
                        is_inline=attachment.is_inline,
                    )
                )

    def _load_file_record(self, session: Session, file_id: object) -> AttachmentFileRecordModel:
        record = None
        try:
            record = session.get(AttachmentFileRecordModel, int(str(file_id)))
        except (TypeError, ValueError):
            record = None
        if record is None:
            raise AttachmentFileNotFoundError(
                f"No attachment file record found for file_id={file_id!r}"
            )
        return record

    def store_attachment_file_record(
        self, record: AttachmentFileRecordDataclass
    ) -> AttachmentFileRecordDataclass:
        with self.session_manager.begin() as session:
            instance = AttachmentFileRecordModel(
                filename=record.filename,
                content_type=record.content_type or "",
                size=record.size,
                checksum=record.checksum,
                storage_identifiers=record.storage_identifiers,
            )
            session.add(instance)
            session.flush()
            serialized = _serialize_file_record(instance)
        return serialized

    def get_attachment_file_record(self, file_id: str) -> AttachmentFileRecordDataclass | None:
        with self.session_manager.begin() as session:
            try:
                instance = session.get(AttachmentFileRecordModel, int(file_id))
            except (TypeError, ValueError):
                return None
            if instance is None:
                return None
            return _serialize_file_record(instance)

    def find_attachment_file_by_checksum(
        self, checksum: str, size: int
    ) -> AttachmentFileRecordDataclass | None:
        with self.session_manager.begin() as session:
            instance = (
                session.query(AttachmentFileRecordModel)
                .filter(
                    AttachmentFileRecordModel.checksum == checksum,
                    AttachmentFileRecordModel.size == size,
                )
                .first()
            )
            if instance is None:
                return None
            return _serialize_file_record(instance)

    def delete_attachment_file(self, file_id: str) -> None:
        with self.session_manager.begin() as session:
            try:
                instance = session.get(AttachmentFileRecordModel, int(file_id))
            except (TypeError, ValueError):
                return
            if instance is not None:
                session.delete(instance)

    def get_orphaned_attachment_files(self) -> Iterable[AttachmentFileRecordDataclass]:
        """Return file records no longer referenced by any notification join row.

        Reclaiming one is a caller-driven, two-step operation this only surfaces candidates for:
        ``manager.delete_file_by_identifiers(record.storage_identifiers)`` to remove the bytes,
        then ``backend.delete_attachment_file(record.id)`` to drop the row.
        """
        with self.session_manager.begin() as session:
            referenced = select(NotificationAttachmentModel.file_id)
            records = (
                session.query(AttachmentFileRecordModel)
                .filter(AttachmentFileRecordModel.id.not_in(referenced))
                .all()
            )
            return [_serialize_file_record(record) for record in records]

    def get_attachments(self, notification_id: int | str | uuid.UUID) -> Iterable[StoredAttachment]:
        manager = self._attachment_manager
        with self.session_manager.begin() as session:
            join_rows = (
                session.query(NotificationAttachmentModel)
                .options(joinedload(NotificationAttachmentModel.file))
                .filter(NotificationAttachmentModel.notification_id == notification_id)
                .all()
            )
            return [_stored_attachment(manager, join_row, join_row.file) for join_row in join_rows]

    def delete_notification_attachment(self, attachment_id: int | str | uuid.UUID) -> None:
        """Delete a single notification attachment join row by its own id.

        Drops only the join row, never the ``AttachmentFileRecord`` or its bytes -- a file may
        still back other notifications.
        """
        with self.session_manager.begin() as session:
            try:
                instance = session.get(NotificationAttachmentModel, int(attachment_id))
            except (TypeError, ValueError):
                return
            if instance is not None:
                session.delete(instance)

    # ---------------------------------------------------------------------- persistence

    def get_all_pending_notifications(self) -> Iterable[Notification | OneOffNotification]:
        with self.session_manager.begin() as session:
            notifications = (
                session.query(self.notification_model_cls)
                .filter(
                    (self.notification_model_cls.send_after <= datetime.datetime.now())
                    | (self.notification_model_cls.send_after == None),  # noqa: E711
                    self.notification_model_cls.status == NotificationStatus.PENDING_SEND.value,
                )
                .order_by(self.notification_model_cls.created)
                .all()
            )
            session.expunge_all()
        return [self.serialize_notification(n) for n in notifications]

    def get_pending_notifications(
        self, page: int, page_size: int
    ) -> Iterable[Notification | OneOffNotification]:
        with self.session_manager.begin() as session:
            notifications = (
                session.query(self.notification_model_cls)
                .filter(self.notification_model_cls.status == NotificationStatus.PENDING_SEND.value)
                .order_by(self.notification_model_cls.created)
                .offset((page - 1) * page_size)
                .limit(page_size)
                .all()
            )
            session.expunge_all()
        return [self.serialize_notification(n) for n in notifications]

    def persist_notification(
        self,
        user_id: int | str | uuid.UUID,
        notification_type: str,
        title: str,
        body_template: str,
        context_name: str,
        context_kwargs: dict[str, int | str | uuid.UUID],
        send_after: datetime.datetime | None,
        subject_template: str | None = None,
        preheader_template: str | None = None,
        adapter_extra_parameters: dict | None = None,
        attachments: list[AnyNotificationAttachment] | None = None,
        tenant: str | None = None,
        requested_template_version: int | None = None,
    ) -> Notification:
        with self.session_manager.begin() as session:
            notification_instance = self.notification_model_cls(
                notification_type=notification_type,
                user_id=user_id,
                title=title,
                body_template=body_template,
                context_name=context_name,
                context_kwargs=context_kwargs,
                send_after=send_after,
                subject_template=subject_template or "",
                preheader_template=preheader_template or "",
                status=NotificationStatus.PENDING_SEND.value,
                adapter_extra_parameters=adapter_extra_parameters,
                tenant=tenant,
                requested_template_version=requested_template_version,
            )
            session.add(notification_instance)
            session.flush()
            session.expunge(notification_instance)

        stored_attachments: list[StoredAttachment] = []
        if attachments:
            stored_attachments = self._store_attachments(attachments, notification_instance.id)
        return _user_notification_from_orm(notification_instance, stored_attachments)

    def persist_one_off_notification(
        self,
        email_or_phone: str,
        first_name: str,
        last_name: str,
        notification_type: str,
        title: str,
        body_template: str,
        context_name: str,
        context_kwargs: dict[str, int | str | uuid.UUID],
        send_after: datetime.datetime | None = None,
        subject_template: str | None = None,
        preheader_template: str | None = None,
        adapter_extra_parameters: dict | None = None,
        attachments: list[AnyNotificationAttachment] | None = None,
        tenant: str | None = None,
        requested_template_version: int | None = None,
    ) -> OneOffNotification:
        with self.session_manager.begin() as session:
            notification_instance = self.notification_model_cls(
                notification_type=notification_type,
                user_id=None,
                email_or_phone=email_or_phone,
                first_name=first_name,
                last_name=last_name,
                title=title,
                body_template=body_template,
                context_name=context_name,
                context_kwargs=context_kwargs,
                send_after=send_after,
                subject_template=subject_template or "",
                preheader_template=preheader_template or "",
                status=NotificationStatus.PENDING_SEND.value,
                adapter_extra_parameters=adapter_extra_parameters,
                tenant=tenant,
                requested_template_version=requested_template_version,
            )
            session.add(notification_instance)
            session.flush()
            session.expunge(notification_instance)

        stored_attachments: list[StoredAttachment] = []
        if attachments:
            stored_attachments = self._store_attachments(attachments, notification_instance.id)
        return _one_off_notification_from_orm(notification_instance, stored_attachments)

    def persist_notification_update(
        self, notification_id: int | str | uuid.UUID, updated_data: UpdateNotificationKwargs
    ) -> Notification | OneOffNotification:
        # ``attachments`` is not a scalar column; it is a set of already-stored files to link via
        # join rows (the resend path), so pull it out before the row update.
        update_values = dict(updated_data)
        attachments = cast("list[StoredAttachment] | None", update_values.pop("attachments", None))

        with self.session_manager.begin() as session:
            if update_values:
                records_updated = (
                    session.query(self.notification_model_cls)
                    .filter(
                        self.notification_model_cls.id == notification_id,
                        self.notification_model_cls.status == NotificationStatus.PENDING_SEND.value,
                    )
                    .update(
                        {
                            getattr(self.notification_model_cls, k): v
                            for k, v in update_values.items()
                        }
                    )
                )
                if records_updated == 0:
                    raise NotificationUpdateError(
                        "Failed to update notification, it may have already been sent"
                    )
            else:
                exists = (
                    session.query(self.notification_model_cls)
                    .filter(
                        self.notification_model_cls.id == notification_id,
                        self.notification_model_cls.status == NotificationStatus.PENDING_SEND.value,
                    )
                    .count()
                )
                if exists == 0:
                    raise NotificationUpdateError(
                        "Failed to update notification, it may have already been sent"
                    )

        if attachments:
            self._attach_stored_attachments(notification_id, attachments)

        return self.get_notification(notification_id)

    def mark_pending_as_sent(
        self, notification_id: int | str | uuid.UUID
    ) -> Notification | OneOffNotification:
        return self._update_notification_status(
            notification_id,
            [NotificationStatus.PENDING_SEND.value],
            NotificationStatus.SENT.value,
            extra_values={"sent_at": _utcnow()},
        )

    def mark_pending_as_failed(
        self, notification_id: int | str | uuid.UUID
    ) -> Notification | OneOffNotification:
        return self._update_notification_status(
            notification_id,
            [NotificationStatus.PENDING_SEND.value],
            NotificationStatus.FAILED.value,
        )

    def mark_sent_as_read(
        self, notification_id: int | str | uuid.UUID
    ) -> Notification | OneOffNotification:
        return self._update_notification_status(
            notification_id,
            [NotificationStatus.SENT.value],
            NotificationStatus.READ.value,
            extra_values={"read_at": _utcnow()},
        )

    def mark_sent_as_read_bulk(
        self,
        notification_ids: Iterable[int | str | uuid.UUID],
        user_id: int | str | uuid.UUID | None = None,
    ) -> Iterable[Notification]:
        ids = list(notification_ids)
        if not ids:
            return []

        with self.session_manager.begin() as session:
            base_filters = [self.notification_model_cls.id.in_(ids)]
            if user_id is not None:
                base_filters.append(
                    getattr(
                        self.notification_model_cls,
                        self.notification_model_cls.get_user_id_attr_name(),
                    )
                    == user_id
                )
            session.query(self.notification_model_cls).filter(
                *base_filters,
                self.notification_model_cls.status == NotificationStatus.SENT.value,
            ).update(
                {"status": NotificationStatus.READ.value, "read_at": _utcnow()},
                synchronize_session=False,
            )

            read_rows = (
                session.query(self.notification_model_cls)
                .filter(
                    *base_filters,
                    self.notification_model_cls.status == NotificationStatus.READ.value,
                )
                .order_by(
                    self.notification_model_cls.created.desc(),
                    self.notification_model_cls.id.desc(),
                )
                .all()
            )
            session.expunge_all()
        return [_user_notification_from_orm(n, list(self.get_attachments(n.id))) for n in read_rows]

    def cancel_notification(self, notification_id: int | str | uuid.UUID) -> None:
        with self.session_manager.begin() as session:
            records_updated = (
                session.query(self.notification_model_cls)
                .filter(
                    self.notification_model_cls.id == notification_id,
                    self.notification_model_cls.status == NotificationStatus.PENDING_SEND.value,
                )
                .update({"status": NotificationStatus.CANCELLED.value})
            )

        if records_updated == 0:
            raise NotificationCancelError("Failed to delete notification")

    def get_notification(
        self, notification_id: int | str | uuid.UUID, for_update=False
    ) -> Notification | OneOffNotification:
        with self.session_manager.begin() as session:
            query = session.query(self.notification_model_cls).filter(
                self.notification_model_cls.status != NotificationStatus.CANCELLED.value,
                self.notification_model_cls.id == notification_id,
            )
            if for_update:
                query = query.with_for_update()
            try:
                notification_instance = query.one()
            except NoResultFound as e:
                raise NotificationNotFoundError("Notification not found") from e
            session.expunge(notification_instance)
        return self.serialize_notification(notification_instance)

    def _update_notification_status(
        self,
        notification_id: int | str | uuid.UUID,
        expected_current_statuses: list[str],
        new_status: str,
        extra_values: dict | None = None,
    ) -> Notification | OneOffNotification:
        with self.session_manager.begin() as session:
            values: dict = {"status": new_status}
            if extra_values:
                values.update(extra_values)
            records_updated = (
                session.query(self.notification_model_cls)
                .filter(
                    self.notification_model_cls.id == notification_id,
                    self.notification_model_cls.status.in_(expected_current_statuses),
                )
                .update(values)
            )
            if records_updated == 0:
                raise NotificationUpdateError("Failed to update notification status")

        with self.session_manager.begin() as session:
            notification_instance = (
                session.query(self.notification_model_cls)
                .filter(self.notification_model_cls.id == notification_id)
                .one()
            )
            session.expunge(notification_instance)

        return self.serialize_notification(notification_instance)

    # ---------------------------------------------------------------------- in-app + filtering

    def filter_all_in_app_unread_notifications(
        self, user_id: int | str | uuid.UUID
    ) -> Iterable[Notification]:
        with self.session_manager.begin() as session:
            notifications = self._get_all_in_app_unread_notifications_query(session, user_id).all()
            session.expunge_all()
        return [
            _user_notification_from_orm(n, list(self.get_attachments(n.id))) for n in notifications
        ]

    def filter_in_app_unread_notifications(
        self, user_id: int | str | uuid.UUID, page: int = 1, page_size: int = 10
    ) -> Iterable[Notification]:
        with self.session_manager.begin() as session:
            notifications = (
                self._get_all_in_app_unread_notifications_query(session, user_id)
                .offset((page - 1) * page_size)
                .limit(page_size)
                .all()
            )
            session.expunge_all()
        return [
            _user_notification_from_orm(n, list(self.get_attachments(n.id))) for n in notifications
        ]

    def filter_all_in_app_notifications(
        self, user_id: int | str | uuid.UUID
    ) -> Iterable[Notification]:
        with self.session_manager.begin() as session:
            notifications = self._get_all_in_app_notifications_query(session, user_id).all()
            session.expunge_all()
        return [
            _user_notification_from_orm(n, list(self.get_attachments(n.id))) for n in notifications
        ]

    def filter_in_app_notifications(
        self, user_id: int | str | uuid.UUID, page: int = 1, page_size: int = 10
    ) -> Iterable[Notification]:
        with self.session_manager.begin() as session:
            notifications = (
                self._get_all_in_app_notifications_query(session, user_id)
                .offset((page - 1) * page_size)
                .limit(page_size)
                .all()
            )
            session.expunge_all()
        return [
            _user_notification_from_orm(n, list(self.get_attachments(n.id))) for n in notifications
        ]

    def count_in_app_notifications(self, user_id: int | str | uuid.UUID) -> int:
        with self.session_manager.begin() as session:
            return self._get_all_in_app_notifications_query(session, user_id).count()

    def count_in_app_unread_notifications(self, user_id: int | str | uuid.UUID) -> int:
        with self.session_manager.begin() as session:
            return self._get_all_in_app_unread_notifications_query(session, user_id).count()

    def _order_columns(self, order_by: NotificationOrderBy | None):
        model = self.notification_model_cls
        if order_by is None:
            return [model.created.desc(), model.id.desc()]
        column = getattr(model, _ORDER_FIELD_TO_COLUMN[order_by["field"]])
        primary = column.desc() if order_by["direction"] == "desc" else column.asc()
        tiebreaker = model.id.desc() if order_by["direction"] == "desc" else model.id.asc()
        return [primary, tiebreaker]

    def filter_notifications(
        self,
        filter: NotificationFilter,  # noqa: A002
        page: int,
        page_size: int,
        order_by: NotificationOrderBy | None = None,
    ) -> Iterable[Notification | OneOffNotification]:
        expression = build_filter_expression(self.notification_model_cls, filter)
        with self.session_manager.begin() as session:
            notifications = (
                session.query(self.notification_model_cls)
                .filter(expression)
                .order_by(*self._order_columns(order_by))
                .offset((page - 1) * page_size)
                .limit(page_size)
                .all()
            )
            session.expunge_all()
        return [self.serialize_notification(n) for n in notifications]

    def count_notifications(self, filter: NotificationFilter) -> int:  # noqa: A002
        expression = build_filter_expression(self.notification_model_cls, filter)
        with self.session_manager.begin() as session:
            return session.query(self.notification_model_cls).filter(expression).count()

    def get_filter_capabilities(self) -> dict[str, bool]:
        # This backend translates the full vocabulary into SQL, so it declines nothing.
        return {}

    def get_all_future_notifications(self) -> Iterable["Notification | OneOffNotification"]:
        with self.session_manager.begin() as session:
            notifications = self._get_all_future_notifications_query(session).all()
            session.expunge_all()
        return [self.serialize_notification(n) for n in notifications]

    def get_future_notifications(
        self, page: int, page_size: int
    ) -> Iterable["Notification | OneOffNotification"]:
        with self.session_manager.begin() as session:
            notifications = (
                self._get_all_future_notifications_query(session)
                .offset((page - 1) * page_size)
                .limit(page_size)
                .all()
            )
            session.expunge_all()
        return [self.serialize_notification(n) for n in notifications]

    def get_all_future_notifications_from_user(
        self, user_id: int | str | uuid.UUID
    ) -> Iterable["Notification | OneOffNotification"]:
        with self.session_manager.begin() as session:
            notifications = self._get_all_future_notifications_from_user_query(
                session, user_id
            ).all()
            session.expunge_all()
        return [self.serialize_notification(n) for n in notifications]

    def get_future_notifications_from_user(
        self, user_id: int | str | uuid.UUID, page: int, page_size: int
    ) -> Iterable["Notification | OneOffNotification"]:
        with self.session_manager.begin() as session:
            notifications = (
                self._get_all_future_notifications_from_user_query(session, user_id)
                .offset((page - 1) * page_size)
                .limit(page_size)
                .all()
            )
            session.expunge_all()
        return [self.serialize_notification(n) for n in notifications]

    def get_user_email_from_notification(self, notification_id: int | str | uuid.UUID) -> str:
        with self.session_manager.begin() as session:
            notification = (
                session.query(self.notification_model_cls)
                .options(
                    joinedload(
                        getattr(
                            self.notification_model_cls,
                            self.notification_model_cls.get_user_attr_name(),
                        )
                    )
                )
                .filter(self.notification_model_cls.id == notification_id)
                .one()
            )
            email = notification.get_user_email()
        return email

    def store_context_used(
        self,
        notification_id: int | str | uuid.UUID,
        context: dict,
        adapter_import_str: str,
    ) -> None:
        with self.session_manager.begin() as session:
            session.query(self.notification_model_cls).filter(
                self.notification_model_cls.id == notification_id
            ).update({"context_used": context, "adapter_used": adapter_import_str})

    def store_git_commit_sha(
        self,
        notification_id: int | str | uuid.UUID,
        git_commit_sha: str,
    ) -> None:
        with self.session_manager.begin() as session:
            session.query(self.notification_model_cls).filter(
                self.notification_model_cls.id == notification_id
            ).update({"git_commit_sha": git_commit_sha})

    def store_template_version(
        self,
        notification_id: int | str | uuid.UUID,
        template_version: int,
    ) -> None:
        # Overridden rather than inherited: the seam's default is a no-op so a backend with
        # nowhere to put this keeps working, and there is a column for it here.
        with self.session_manager.begin() as session:
            session.query(self.notification_model_cls).filter(
                self.notification_model_cls.id == notification_id
            ).update({"used_template_version": template_version})


class SQLAlchemyAsyncIONotificationBackend(
    Generic[NotificationModel], AsyncIOBaseNotificationBackend
):
    session: async_sessionmaker[AsyncSession]
    notification_model_cls: "type[NotificationModel]"

    def __init__(
        self,
        session: async_sessionmaker[AsyncSession],
        notification_model_cls: "type[NotificationModel]",
    ) -> None:
        super().__init__(session=session, notification_model_cls=notification_model_cls)
        self.session_manager = session
        self.notification_model_cls = (
            notification_model_cls if notification_model_cls else self._get_notification_model_cls()
        )
        self._attachment_manager: AsyncIOBaseAttachmentManager = (
            FilesystemAsyncIOAttachmentManager()
        )

    def _get_notification_model_cls(self) -> "type[NotificationModel]":
        notification_model_cls = NotificationSettings().get_notification_model_cls()
        if notification_model_cls is None:
            raise NotificationError("Notification model class not set in settings")

        return notification_model_cls

    # ---------------------------------------------------------------------- queries

    def _get_all_in_app_unread_notifications_query(self, user_id: int | str | uuid.UUID):
        return (
            select(self.notification_model_cls)
            .where(
                getattr(
                    self.notification_model_cls, self.notification_model_cls.get_user_id_attr_name()
                )
                == user_id,
                self.notification_model_cls.status == NotificationStatus.SENT.value,
                self.notification_model_cls.notification_type == NotificationTypes.IN_APP.value,
            )
            .order_by(
                self.notification_model_cls.created.desc(), self.notification_model_cls.id.desc()
            )
        )

    def _get_all_in_app_notifications_query(self, user_id: int | str | uuid.UUID):
        return (
            select(self.notification_model_cls)
            .where(
                getattr(
                    self.notification_model_cls, self.notification_model_cls.get_user_id_attr_name()
                )
                == user_id,
                self.notification_model_cls.status.in_(
                    [NotificationStatus.SENT.value, NotificationStatus.READ.value]
                ),
                self.notification_model_cls.notification_type == NotificationTypes.IN_APP.value,
            )
            .order_by(
                self.notification_model_cls.created.desc(), self.notification_model_cls.id.desc()
            )
        )

    def _get_all_future_notifications_query(self):
        return (
            select(self.notification_model_cls)
            .where(
                self.notification_model_cls.status == NotificationStatus.PENDING_SEND.value,
                self.notification_model_cls.send_after > datetime.datetime.now(),
            )
            .order_by(self.notification_model_cls.created)
        )

    def _get_all_future_notifications_from_user_query(self, user_id: int | str | uuid.UUID):
        return (
            select(self.notification_model_cls)
            .where(
                self.notification_model_cls.status == NotificationStatus.PENDING_SEND.value,
                self.notification_model_cls.send_after > datetime.datetime.now(),
                getattr(
                    self.notification_model_cls, self.notification_model_cls.get_user_id_attr_name()
                )
                == user_id,
            )
            .order_by(self.notification_model_cls.created)
        )

    # ---------------------------------------------------------------------- serialization

    async def _serialize_notification(
        self, notification: "NotificationModel"
    ) -> Notification | OneOffNotification:
        attachments = list(await self.get_attachments(notification.id))
        if getattr(notification, "user_id", None):
            return _user_notification_from_orm(notification, attachments)
        return _one_off_notification_from_orm(notification, attachments)

    async def _serialize_user_notification(self, notification: "NotificationModel") -> Notification:
        attachments = list(await self.get_attachments(notification.id))
        return _user_notification_from_orm(notification, attachments)

    async def _serialize_many(
        self, notifications: "list[NotificationModel]"
    ) -> list[Notification | OneOffNotification]:
        return [await self._serialize_notification(n) for n in notifications]

    async def _serialize_many_user(
        self, notifications: "list[NotificationModel]"
    ) -> list[Notification]:
        return [await self._serialize_user_notification(n) for n in notifications]

    # ---------------------------------------------------------------------- attachments

    async def _store_attachments(
        self,
        attachments: list[AnyNotificationAttachment],
        notification_id: int | str | uuid.UUID,
    ) -> list[StoredAttachment]:
        manager = self._attachment_manager
        stored_attachments: list[StoredAttachment] = []

        async with self.session_manager() as session:
            async with session.begin():
                for attachment in attachments:
                    if is_attachment_reference(attachment):
                        record = await self._load_file_record(session, attachment.file_id)
                        join_row = NotificationAttachmentModel(
                            notification_id=notification_id,
                            file_id=record.id,
                            description=attachment.description,
                            is_inline=attachment.is_inline,
                        )
                        session.add(join_row)
                        await session.flush()
                        stored_attachments.append(_stored_attachment(manager, join_row, record))
                        continue

                    assert isinstance(attachment, NotificationAttachment)  # noqa: S101

                    data = manager.file_to_bytes(attachment.file)
                    checksum = manager.calculate_checksum(data)
                    existing = (
                        (
                            await session.execute(
                                select(AttachmentFileRecordModel).where(
                                    AttachmentFileRecordModel.checksum == checksum,
                                    AttachmentFileRecordModel.size == len(data),
                                )
                            )
                        )
                        .scalars()
                        .first()
                    )
                    if existing is not None:
                        record = existing
                    else:
                        file_record = await manager.upload_file(
                            data, attachment.filename, attachment.content_type
                        )
                        record = AttachmentFileRecordModel(
                            filename=file_record.filename,
                            content_type=file_record.content_type or "",
                            size=file_record.size,
                            checksum=file_record.checksum,
                            storage_identifiers=file_record.storage_identifiers,
                        )
                        session.add(record)
                        await session.flush()

                    join_row = NotificationAttachmentModel(
                        notification_id=notification_id,
                        file_id=record.id,
                        description=attachment.description,
                        is_inline=attachment.is_inline,
                    )
                    session.add(join_row)
                    await session.flush()
                    stored_attachments.append(_stored_attachment(manager, join_row, record))

        return stored_attachments

    async def _attach_stored_attachments(
        self,
        notification_id: int | str | uuid.UUID,
        attachments: list[StoredAttachment],
    ) -> None:
        async with self.session_manager() as session:
            async with session.begin():
                for attachment in attachments:
                    file_id = attachment.file_id or attachment.id
                    record = await self._load_file_record(session, file_id)
                    session.add(
                        NotificationAttachmentModel(
                            notification_id=notification_id,
                            file_id=record.id,
                            description=attachment.description,
                            is_inline=attachment.is_inline,
                        )
                    )

    async def _load_file_record(
        self, session: AsyncSession, file_id: object
    ) -> AttachmentFileRecordModel:
        record = None
        try:
            record = await session.get(AttachmentFileRecordModel, int(str(file_id)))
        except (TypeError, ValueError):
            record = None
        if record is None:
            raise AttachmentFileNotFoundError(
                f"No attachment file record found for file_id={file_id!r}"
            )
        return record

    async def store_attachment_file_record(
        self, record: AttachmentFileRecordDataclass, lock: asyncio.Lock | None = None
    ) -> AttachmentFileRecordDataclass:
        async with self.session_manager() as session:
            async with session.begin():
                instance = AttachmentFileRecordModel(
                    filename=record.filename,
                    content_type=record.content_type or "",
                    size=record.size,
                    checksum=record.checksum,
                    storage_identifiers=record.storage_identifiers,
                )
                session.add(instance)
                await session.flush()
                serialized = _serialize_file_record(instance)
        return serialized

    async def get_attachment_file_record(
        self, file_id: str
    ) -> AttachmentFileRecordDataclass | None:
        async with self.session_manager() as session:
            try:
                instance = await session.get(AttachmentFileRecordModel, int(file_id))
            except (TypeError, ValueError):
                return None
            if instance is None:
                return None
            return _serialize_file_record(instance)

    async def find_attachment_file_by_checksum(
        self, checksum: str, size: int
    ) -> AttachmentFileRecordDataclass | None:
        async with self.session_manager() as session:
            instance = (
                (
                    await session.execute(
                        select(AttachmentFileRecordModel).where(
                            AttachmentFileRecordModel.checksum == checksum,
                            AttachmentFileRecordModel.size == size,
                        )
                    )
                )
                .scalars()
                .first()
            )
            if instance is None:
                return None
            return _serialize_file_record(instance)

    async def delete_attachment_file(self, file_id: str, lock: asyncio.Lock | None = None) -> None:
        async with self.session_manager() as session:
            async with session.begin():
                try:
                    instance = await session.get(AttachmentFileRecordModel, int(file_id))
                except (TypeError, ValueError):
                    return
                if instance is not None:
                    await session.delete(instance)

    async def get_orphaned_attachment_files(self) -> Iterable[AttachmentFileRecordDataclass]:
        async with self.session_manager() as session:
            referenced = select(NotificationAttachmentModel.file_id)
            records = (
                (
                    await session.execute(
                        select(AttachmentFileRecordModel).where(
                            AttachmentFileRecordModel.id.not_in(referenced)
                        )
                    )
                )
                .scalars()
                .all()
            )
            return [_serialize_file_record(record) for record in records]

    async def get_attachments(
        self, notification_id: int | str | uuid.UUID
    ) -> Iterable[StoredAttachment]:
        manager = self._attachment_manager
        async with self.session_manager() as session:
            join_rows = (
                (
                    await session.execute(
                        select(NotificationAttachmentModel)
                        .options(joinedload(NotificationAttachmentModel.file))
                        .where(NotificationAttachmentModel.notification_id == notification_id)
                    )
                )
                .scalars()
                .all()
            )
            return [_stored_attachment(manager, join_row, join_row.file) for join_row in join_rows]

    async def delete_notification_attachment(
        self, attachment_id: int | str | uuid.UUID, lock: asyncio.Lock | None = None
    ) -> None:
        async with self.session_manager() as session:
            async with session.begin():
                try:
                    instance = await session.get(NotificationAttachmentModel, int(attachment_id))
                except (TypeError, ValueError):
                    return
                if instance is not None:
                    await session.delete(instance)

    # ---------------------------------------------------------------------- persistence

    async def get_all_pending_notifications(self) -> Iterable[Notification | OneOffNotification]:
        async with self.session_manager() as session:
            notifications = (
                (
                    await session.execute(
                        select(self.notification_model_cls)
                        .filter(
                            (self.notification_model_cls.send_after <= datetime.datetime.now())
                            | (self.notification_model_cls.send_after == None),  # noqa: E711
                            self.notification_model_cls.status
                            == NotificationStatus.PENDING_SEND.value,
                        )
                        .order_by(self.notification_model_cls.created)
                    )
                )
                .scalars()
                .all()
            )
            session.expunge_all()
        return await self._serialize_many(list(notifications))

    async def get_pending_notifications(
        self, page: int, page_size: int
    ) -> Iterable[Notification | OneOffNotification]:
        async with self.session_manager() as session:
            notifications = (
                (
                    await session.execute(
                        select(self.notification_model_cls)
                        .filter(
                            self.notification_model_cls.status
                            == NotificationStatus.PENDING_SEND.value
                        )
                        .order_by(self.notification_model_cls.created)
                        .offset((page - 1) * page_size)
                        .limit(page_size)
                    )
                )
                .scalars()
                .all()
            )
            session.expunge_all()
        return await self._serialize_many(list(notifications))

    async def persist_notification(
        self,
        user_id: int | str | uuid.UUID,
        notification_type: str,
        title: str,
        body_template: str,
        context_name: str,
        context_kwargs: dict[str, int | str | uuid.UUID],
        send_after: datetime.datetime | None,
        subject_template: str | None = None,
        preheader_template: str | None = None,
        adapter_extra_parameters: dict | None = None,
        attachments: list[AnyNotificationAttachment] | None = None,
        tenant: str | None = None,
        requested_template_version: int | None = None,
        lock: asyncio.Lock | None = None,
    ) -> Notification:
        async with self.session_manager() as session:
            notification_instance = self.notification_model_cls(
                notification_type=notification_type,
                user_id=user_id,
                title=title,
                body_template=body_template,
                context_name=context_name,
                context_kwargs=context_kwargs,
                send_after=send_after,
                subject_template=subject_template or "",
                preheader_template=preheader_template or "",
                status=NotificationStatus.PENDING_SEND.value,
                adapter_extra_parameters=adapter_extra_parameters,
                tenant=tenant,
                requested_template_version=requested_template_version,
            )
            session.add(notification_instance)
            await session.flush()
            await session.commit()
            await session.refresh(notification_instance)
            session.expunge(notification_instance)

        stored_attachments: list[StoredAttachment] = []
        if attachments:
            stored_attachments = await self._store_attachments(
                attachments, notification_instance.id
            )
        return _user_notification_from_orm(notification_instance, stored_attachments)

    async def persist_one_off_notification(
        self,
        email_or_phone: str,
        first_name: str,
        last_name: str,
        notification_type: str,
        title: str,
        body_template: str,
        context_name: str,
        context_kwargs: dict[str, int | str | uuid.UUID],
        send_after: datetime.datetime | None = None,
        subject_template: str | None = None,
        preheader_template: str | None = None,
        adapter_extra_parameters: dict | None = None,
        attachments: list[AnyNotificationAttachment] | None = None,
        tenant: str | None = None,
        requested_template_version: int | None = None,
        lock: asyncio.Lock | None = None,
    ) -> OneOffNotification:
        async with self.session_manager() as session:
            notification_instance = self.notification_model_cls(
                notification_type=notification_type,
                user_id=None,
                email_or_phone=email_or_phone,
                first_name=first_name,
                last_name=last_name,
                title=title,
                body_template=body_template,
                context_name=context_name,
                context_kwargs=context_kwargs,
                send_after=send_after,
                subject_template=subject_template or "",
                preheader_template=preheader_template or "",
                status=NotificationStatus.PENDING_SEND.value,
                adapter_extra_parameters=adapter_extra_parameters,
                tenant=tenant,
                requested_template_version=requested_template_version,
            )
            session.add(notification_instance)
            await session.flush()
            await session.commit()
            await session.refresh(notification_instance)
            session.expunge(notification_instance)

        stored_attachments: list[StoredAttachment] = []
        if attachments:
            stored_attachments = await self._store_attachments(
                attachments, notification_instance.id
            )
        return _one_off_notification_from_orm(notification_instance, stored_attachments)

    async def persist_notification_update(
        self,
        notification_id: int | str | uuid.UUID,
        updated_data: UpdateNotificationKwargs,
        lock: asyncio.Lock | None = None,
    ) -> Notification | OneOffNotification:
        update_values = dict(updated_data)
        attachments = cast("list[StoredAttachment] | None", update_values.pop("attachments", None))

        async with self.session_manager() as session:
            if update_values:
                records_updated = _rowcount(
                    await session.execute(
                        update(self.notification_model_cls)
                        .where(
                            self.notification_model_cls.id == notification_id,
                            self.notification_model_cls.status
                            == NotificationStatus.PENDING_SEND.value,
                        )
                        .values(
                            {
                                getattr(self.notification_model_cls, k): v
                                for k, v in update_values.items()
                            }
                        )
                    )
                )
                await session.commit()
                if records_updated == 0:
                    raise NotificationUpdateError(
                        "Failed to update notification, it may have already been sent"
                    )
            else:
                exists = (
                    await session.execute(
                        select(func.count())
                        .select_from(self.notification_model_cls)
                        .where(
                            self.notification_model_cls.id == notification_id,
                            self.notification_model_cls.status
                            == NotificationStatus.PENDING_SEND.value,
                        )
                    )
                ).scalar()
                if not exists:
                    raise NotificationUpdateError(
                        "Failed to update notification, it may have already been sent"
                    )

        if attachments:
            await self._attach_stored_attachments(notification_id, attachments)

        return await self.get_notification(notification_id)

    async def mark_pending_as_sent(
        self, notification_id: int | str | uuid.UUID, lock: asyncio.Lock | None = None
    ) -> Notification | OneOffNotification:
        return await self._update_notification_status(
            notification_id,
            [NotificationStatus.PENDING_SEND.value],
            NotificationStatus.SENT.value,
            extra_values={"sent_at": _utcnow()},
        )

    async def mark_pending_as_failed(
        self, notification_id: int | str | uuid.UUID, lock: asyncio.Lock | None = None
    ) -> Notification | OneOffNotification:
        return await self._update_notification_status(
            notification_id,
            [NotificationStatus.PENDING_SEND.value],
            NotificationStatus.FAILED.value,
        )

    async def mark_sent_as_read(
        self, notification_id: int | str | uuid.UUID, lock: asyncio.Lock | None = None
    ) -> Notification | OneOffNotification:
        return await self._update_notification_status(
            notification_id,
            [NotificationStatus.SENT.value],
            NotificationStatus.READ.value,
            extra_values={"read_at": _utcnow()},
        )

    async def mark_sent_as_read_bulk(
        self,
        notification_ids: Iterable[int | str | uuid.UUID],
        user_id: int | str | uuid.UUID | None = None,
        lock: asyncio.Lock | None = None,
    ) -> Iterable[Notification]:
        ids = list(notification_ids)
        if not ids:
            return []

        base_filters = [self.notification_model_cls.id.in_(ids)]
        if user_id is not None:
            base_filters.append(
                getattr(
                    self.notification_model_cls,
                    self.notification_model_cls.get_user_id_attr_name(),
                )
                == user_id
            )

        async with self.session_manager() as session:
            await session.execute(
                update(self.notification_model_cls)
                .where(
                    *base_filters,
                    self.notification_model_cls.status == NotificationStatus.SENT.value,
                )
                .values(status=NotificationStatus.READ.value, read_at=_utcnow())
            )
            await session.commit()

            read_rows = (
                (
                    await session.execute(
                        select(self.notification_model_cls)
                        .where(
                            *base_filters,
                            self.notification_model_cls.status == NotificationStatus.READ.value,
                        )
                        .order_by(
                            self.notification_model_cls.created.desc(),
                            self.notification_model_cls.id.desc(),
                        )
                    )
                )
                .scalars()
                .all()
            )
            session.expunge_all()
        return await self._serialize_many_user(list(read_rows))

    async def cancel_notification(
        self, notification_id: int | str | uuid.UUID, lock: asyncio.Lock | None = None
    ) -> None:
        async with self.session_manager() as session:
            records_updated = _rowcount(
                await session.execute(
                    update(self.notification_model_cls)
                    .where(
                        self.notification_model_cls.id == notification_id,
                        self.notification_model_cls.status == NotificationStatus.PENDING_SEND.value,
                    )
                    .values({"status": NotificationStatus.CANCELLED.value})
                )
            )
            await session.commit()
        if records_updated == 0:
            raise NotificationCancelError("Failed to delete notification")

    async def get_notification(
        self, notification_id: int | str | uuid.UUID, for_update=False
    ) -> Notification | OneOffNotification:
        async with self.session_manager() as session:
            query = select(self.notification_model_cls).where(
                self.notification_model_cls.status != NotificationStatus.CANCELLED.value,
                self.notification_model_cls.id == notification_id,
            )
            if for_update:
                query = query.with_for_update()
            try:
                notification_instance = (await session.execute(query)).scalars().one()
            except NoResultFound as e:
                raise NotificationNotFoundError("Notification not found") from e
            session.expunge(notification_instance)
        return await self._serialize_notification(notification_instance)

    async def _update_notification_status(
        self,
        notification_id: int | str | uuid.UUID,
        expected_current_statuses: list[str],
        new_status: str,
        extra_values: dict | None = None,
    ) -> Notification | OneOffNotification:
        values: dict = {"status": new_status}
        if extra_values:
            values.update(extra_values)
        async with self.session_manager() as session:
            records_updated = _rowcount(
                await session.execute(
                    update(self.notification_model_cls)
                    .where(
                        self.notification_model_cls.id == notification_id,
                        self.notification_model_cls.status.in_(expected_current_statuses),
                    )
                    .values(values)
                )
            )
            await session.commit()
            if records_updated == 0:
                raise NotificationUpdateError("Failed to update notification status")

        async with self.session_manager() as session:
            notification_instance = (
                (
                    await session.execute(
                        select(self.notification_model_cls).where(
                            self.notification_model_cls.id == notification_id
                        )
                    )
                )
                .scalars()
                .one()
            )
            session.expunge(notification_instance)
        return await self._serialize_notification(notification_instance)

    # ---------------------------------------------------------------------- in-app + filtering

    async def filter_all_in_app_unread_notifications(
        self, user_id: int | str | uuid.UUID
    ) -> Iterable[Notification]:
        async with self.session_manager() as session:
            notifications = (
                (await session.execute(self._get_all_in_app_unread_notifications_query(user_id)))
                .scalars()
                .all()
            )
            session.expunge_all()
        return await self._serialize_many_user(list(notifications))

    async def filter_in_app_unread_notifications(
        self, user_id: int | str | uuid.UUID, page: int = 1, page_size: int = 10
    ) -> Iterable[Notification]:
        async with self.session_manager() as session:
            notifications = (
                (
                    await session.execute(
                        self._get_all_in_app_unread_notifications_query(user_id)
                        .offset((page - 1) * page_size)
                        .limit(page_size)
                    )
                )
                .scalars()
                .all()
            )
            session.expunge_all()
        return await self._serialize_many_user(list(notifications))

    async def filter_all_in_app_notifications(
        self, user_id: int | str | uuid.UUID
    ) -> Iterable[Notification]:
        async with self.session_manager() as session:
            notifications = (
                (await session.execute(self._get_all_in_app_notifications_query(user_id)))
                .scalars()
                .all()
            )
            session.expunge_all()
        return await self._serialize_many_user(list(notifications))

    async def filter_in_app_notifications(
        self, user_id: int | str | uuid.UUID, page: int = 1, page_size: int = 10
    ) -> Iterable[Notification]:
        async with self.session_manager() as session:
            notifications = (
                (
                    await session.execute(
                        self._get_all_in_app_notifications_query(user_id)
                        .offset((page - 1) * page_size)
                        .limit(page_size)
                    )
                )
                .scalars()
                .all()
            )
            session.expunge_all()
        return await self._serialize_many_user(list(notifications))

    async def count_in_app_notifications(self, user_id: int | str | uuid.UUID) -> int:
        async with self.session_manager() as session:
            return (
                await session.execute(
                    select(func.count()).select_from(
                        self._get_all_in_app_notifications_query(user_id).subquery()
                    )
                )
            ).scalar() or 0

    async def count_in_app_unread_notifications(self, user_id: int | str | uuid.UUID) -> int:
        async with self.session_manager() as session:
            return (
                await session.execute(
                    select(func.count()).select_from(
                        self._get_all_in_app_unread_notifications_query(user_id).subquery()
                    )
                )
            ).scalar() or 0

    def _order_columns(self, order_by: NotificationOrderBy | None):
        model = self.notification_model_cls
        if order_by is None:
            return [model.created.desc(), model.id.desc()]
        column = getattr(model, _ORDER_FIELD_TO_COLUMN[order_by["field"]])
        primary = column.desc() if order_by["direction"] == "desc" else column.asc()
        tiebreaker = model.id.desc() if order_by["direction"] == "desc" else model.id.asc()
        return [primary, tiebreaker]

    async def filter_notifications(
        self,
        filter: NotificationFilter,  # noqa: A002
        page: int,
        page_size: int,
        order_by: NotificationOrderBy | None = None,
    ) -> Iterable[Notification | OneOffNotification]:
        expression = build_filter_expression(self.notification_model_cls, filter)
        async with self.session_manager() as session:
            notifications = (
                (
                    await session.execute(
                        select(self.notification_model_cls)
                        .where(expression)
                        .order_by(*self._order_columns(order_by))
                        .offset((page - 1) * page_size)
                        .limit(page_size)
                    )
                )
                .scalars()
                .all()
            )
            session.expunge_all()
        return await self._serialize_many(list(notifications))

    async def count_notifications(self, filter: NotificationFilter) -> int:  # noqa: A002
        expression = build_filter_expression(self.notification_model_cls, filter)
        async with self.session_manager() as session:
            return (
                await session.execute(
                    select(func.count()).select_from(self.notification_model_cls).where(expression)
                )
            ).scalar() or 0

    async def get_filter_capabilities(self) -> dict[str, bool]:
        return {}

    async def get_all_future_notifications(self) -> Iterable["Notification | OneOffNotification"]:
        async with self.session_manager() as session:
            notifications = (
                (await session.execute(self._get_all_future_notifications_query())).scalars().all()
            )
            session.expunge_all()
        return await self._serialize_many(list(notifications))

    async def get_future_notifications(
        self, page: int, page_size: int
    ) -> Iterable["Notification | OneOffNotification"]:
        async with self.session_manager() as session:
            notifications = (
                (
                    await session.execute(
                        self._get_all_future_notifications_query()
                        .offset((page - 1) * page_size)
                        .limit(page_size)
                    )
                )
                .scalars()
                .all()
            )
            session.expunge_all()
        return await self._serialize_many(list(notifications))

    async def get_all_future_notifications_from_user(
        self, user_id: int | str | uuid.UUID
    ) -> Iterable["Notification | OneOffNotification"]:
        async with self.session_manager() as session:
            notifications = (
                (await session.execute(self._get_all_future_notifications_from_user_query(user_id)))
                .scalars()
                .all()
            )
            session.expunge_all()
        return await self._serialize_many(list(notifications))

    async def get_future_notifications_from_user(
        self, user_id: int | str | uuid.UUID, page: int, page_size: int
    ) -> Iterable["Notification | OneOffNotification"]:
        async with self.session_manager() as session:
            notifications = (
                (
                    await session.execute(
                        self._get_all_future_notifications_from_user_query(user_id)
                        .offset((page - 1) * page_size)
                        .limit(page_size)
                    )
                )
                .scalars()
                .all()
            )
            session.expunge_all()
        return await self._serialize_many(list(notifications))

    async def get_user_email_from_notification(self, notification_id: int | str | uuid.UUID) -> str:
        async with self.session_manager() as session:
            notification = (
                (
                    await session.execute(
                        select(self.notification_model_cls)
                        .options(
                            joinedload(
                                getattr(
                                    self.notification_model_cls,
                                    self.notification_model_cls.get_user_attr_name(),
                                )
                            )
                        )
                        .where(self.notification_model_cls.id == notification_id)
                    )
                )
                .scalars()
                .one()
            )
            email = notification.get_user_email()
        return email

    async def store_context_used(
        self,
        notification_id: int | str | uuid.UUID,
        context: dict,
        adapter_import_str: str,
        lock: asyncio.Lock | None = None,
    ) -> None:
        async with self.session_manager() as session:
            await session.execute(
                update(self.notification_model_cls)
                .where(self.notification_model_cls.id == notification_id)
                .values(context_used=context, adapter_used=adapter_import_str)
            )
            await session.commit()

    async def store_git_commit_sha(
        self,
        notification_id: int | str | uuid.UUID,
        git_commit_sha: str,
        lock: asyncio.Lock | None = None,
    ) -> None:
        async with self.session_manager() as session:
            await session.execute(
                update(self.notification_model_cls)
                .where(self.notification_model_cls.id == notification_id)
                .values(git_commit_sha=git_commit_sha)
            )
            await session.commit()

    async def store_template_version(
        self,
        notification_id: int | str | uuid.UUID,
        template_version: int,
        lock: asyncio.Lock | None = None,
    ) -> None:
        # See the sync twin.
        async with self.session_manager() as session:
            await session.execute(
                update(self.notification_model_cls)
                .where(self.notification_model_cls.id == notification_id)
                .values(used_template_version=template_version)
            )
            await session.commit()
