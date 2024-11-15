import datetime
from typing import TYPE_CHECKING
import uuid
from collections.abc import Iterable

from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session, sessionmaker, joinedload

from vintasend.app_settings import NotificationSettings
from vintasend.constants import NotificationStatus, NotificationTypes
from vintasend.exceptions import (
    NotificationCancelError,
    NotificationNotFoundError,
    NotificationUpdateError,
    NotificationError,
)
from vintasend.services.dataclasses import (
    Notification,
    UpdateNotificationKwargs,
)
from vintasend.services.notification_backends.base import BaseNotificationBackend

if TYPE_CHECKING:
    from vintasend_sqlalchemy.model_factory import NotificationMixin as NotificationModel


class SQLAlchemyNotificationBackend(BaseNotificationBackend):
    session: sessionmaker[Session]
    notification_model_cls: "type[NotificationModel]"

    def __init__(self, session: sessionmaker[Session], notification_model_cls: "type[NotificationModel]") -> None:
        self.session_manager = session
        self.notification_model_cls = (
            notification_model_cls if notification_model_cls else self._get_notification_model_cls()
        )

    def _get_notification_model_cls(self) -> "type[NotificationModel]":
        notification_model_cls = NotificationSettings().get_notification_model_cls()
        if notification_model_cls is None:
            raise NotificationError("Notification model class not set in settings")

        return notification_model_cls
    
    def _get_all_in_app_unread_notifications_query(self, session: Session, user_id: int | str | uuid.UUID):
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
            .order_by(self.notification_model_cls.created)
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
    
    def _get_all_future_notifications_from_user_query(self, session: Session, user_id: int | str | uuid.UUID):
        return (
            session.query(self.notification_model_cls)
            .where(
                self.notification_model_cls.status == NotificationStatus.PENDING_SEND.value,
                self.notification_model_cls.send_after > datetime.datetime.now(),
                getattr(
                    self.notification_model_cls, self.notification_model_cls.get_user_id_attr_name()
                ) == user_id,
            )
            .order_by(self.notification_model_cls.created)
        )

    def serialize_notification(self, notification: "NotificationModel") -> Notification:
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
        )

    def get_all_pending_notifications(self) -> Iterable[Notification]:
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
        return (self.serialize_notification(n) for n in notifications)

    def get_pending_notifications(self, page: int, page_size: int) -> Iterable[Notification]:
        with self.session_manager.begin() as session:
            notifications = (
                session.query(self.notification_model_cls)
                .filter(self.notification_model_cls.status == NotificationStatus.PENDING_SEND.value)
                .order_by(self.notification_model_cls.created)
                .offset((page - 1) * page_size)
                .limit(page_size)
                .all()
            )
            session.flush()
            session.expunge_all()
        return (self.serialize_notification(n) for n in notifications)

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
            )
            session.add(notification_instance)
            session.flush()
            session.expunge(notification_instance)
        return self.serialize_notification(notification_instance)

    def persist_notification_update(
        self, notification_id: int | str | uuid.UUID, updated_data: UpdateNotificationKwargs
    ) -> Notification:
        with self.session_manager.begin() as session:
            records_updated = (
                session.query(self.notification_model_cls)
                .filter(
                    self.notification_model_cls.id == notification_id,
                    self.notification_model_cls.status == NotificationStatus.PENDING_SEND.value,
                )
                .update({getattr(self.notification_model_cls, k): v for k, v in updated_data.items()})
            )
            session.commit()
            session.flush()
            if records_updated == 0:
                raise NotificationUpdateError(
                    "Failed to update notification, it may have already been sent"
                )
        
        with self.session_manager.begin() as session:
            notification_instance = session.query(
                self.notification_model_cls
            ).filter(self.notification_model_cls.id==notification_id).one()
            session.flush()
            session.expunge(notification_instance)

        if notification_instance is None:
            raise NotificationNotFoundError("Notification not found after update")

        return self.serialize_notification(notification_instance)

    def mark_pending_as_sent(self, notification_id: int | str | uuid.UUID) -> Notification:
        return self._update_notification_status(notification_id, [NotificationStatus.PENDING_SEND.value], NotificationStatus.SENT.value)

    def mark_pending_as_failed(self, notification_id: int | str | uuid.UUID) -> Notification:
        return self._update_notification_status(notification_id, [NotificationStatus.PENDING_SEND.value], NotificationStatus.FAILED.value)

    def mark_sent_as_read(self, notification_id: int | str | uuid.UUID) -> Notification:
        return self._update_notification_status(notification_id, [NotificationStatus.SENT.value], NotificationStatus.READ.value)

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
            session.commit()

        if records_updated == 0:
            raise NotificationCancelError("Failed to delete notification")

    def get_notification(
        self, notification_id: int | str | uuid.UUID, for_update=False
    ) -> Notification:
        with self.session_manager.begin() as session:
            query = session.query(self.notification_model_cls).filter(
                self.notification_model_cls.status != NotificationStatus.CANCELLED.value,
                self.notification_model_cls.id == notification_id,
            )
            if for_update:
                query = query.with_for_update()
            try:
                notification_instance = query.one()
                session.flush()
                session.expunge(notification_instance)
            except NoResultFound as e:
                raise NotificationNotFoundError("Notification not found") from e
        return self.serialize_notification(notification_instance)

    def _update_notification_status(
        self, notification_id: int | str | uuid.UUID, expected_current_statuses: list[str], new_status: str
    ) -> Notification:
        with self.session_manager.begin() as session:
            records_updated = (
                session.query(self.notification_model_cls)
                .filter(
                    self.notification_model_cls.id == notification_id,
                    self.notification_model_cls.status.in_(expected_current_statuses),
                )
                .update({"status": new_status})
            )
            session.commit()

            if records_updated == 0:
                raise NotificationUpdateError("Failed to update notification status")

        with self.session_manager.begin() as session:
            notification_instance = session.query(
                self.notification_model_cls
            ).filter(self.notification_model_cls.id==notification_id).one()
            session.flush()
            session.expunge(notification_instance)

        if notification_instance is None:
            raise NotificationNotFoundError("Notification not found after update")

        return self.serialize_notification(notification_instance)

    def filter_all_in_app_unread_notifications(
        self, user_id: int | str | uuid.UUID, page: int, page_size: int
    ) -> Iterable[Notification]:
        with self.session_manager.begin() as session:
            query = (
                self._get_all_in_app_unread_notifications_query(session, user_id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            notifications = query.all()
        return (self.serialize_notification(notification) for notification in notifications)

    def filter_in_app_unread_notifications(
        self, user_id: int | str | uuid.UUID, page: int, page_size: int
    ) -> Iterable[Notification]:
        with self.session_manager.begin() as session:
            query = (
                self._get_all_in_app_unread_notifications_query(session, user_id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            notifications = query.all()
        return (self.serialize_notification(notification) for notification in notifications)

    def get_all_future_notifications(self) -> Iterable["Notification"]:
        with self.session_manager.begin() as session:
            query = self._get_all_future_notifications_query(session)
            notifications = query.all()
        return (self.serialize_notification(notification) for notification in notifications)
    
    def get_future_notifications(self, page: int, page_size: int) -> Iterable["Notification"]:
        with self.session_manager.begin() as session:
            query = self._get_all_future_notifications_query(session)
            notifications = query.offset((page - 1) * page_size).limit(page_size).all()
            session.flush()
            session.expunge_all()
        return (self.serialize_notification(notification) for notification in notifications)
    
    def get_all_future_notifications_from_user(self, user_id: int | str | uuid.UUID) -> Iterable["Notification"]:
        with self.session_manager.begin() as session:
            query = self._get_all_future_notifications_from_user_query(session, user_id)
            notifications = query.all()
            session.flush()
            session.expunge_all()
        return (self.serialize_notification(notification) for notification in notifications)
    
    def get_future_notifications_from_user(self, user_id: int | str | uuid.UUID, page: int, page_size: int) -> Iterable["Notification"]:
        with self.session_manager.begin() as session:
            query = self._get_all_future_notifications_from_user_query(session, user_id)
            notifications = query.offset((page - 1) * page_size).limit(page_size).all()
            session.flush()
            session.expunge_all()
        return (self.serialize_notification(notification) for notification in notifications)

    def get_user_email_from_notification(self, notification_id: int | str | uuid.UUID) -> str | None:
        with self.session_manager.begin() as session:
            notification = (
                session.query(self.notification_model_cls)
                .options(joinedload(getattr(self.notification_model_cls, self.notification_model_cls.get_user_attr_name())))
                .filter(
                    getattr(
                        self.notification_model_cls,
                        self.notification_model_cls.get_user_id_attr_name()
                    ) == notification_id
                )
                .one()
            )
            session.flush()
            session.expunge(notification)
        return notification.get_user_email()

    def store_context_used(self, notification_id: int | str | uuid.UUID, context: dict) -> None:
        with self.session_manager.begin() as session:
            notification = (
                session.query(self.notification_model_cls)
                .filter(
                    getattr(
                        self.notification_model_cls,
                        self.notification_model_cls.get_user_id_attr_name()
                    ) == notification_id
                )
                .one()
            )
            notification.context_kwargs = context
            session.commit()
            session.flush()
