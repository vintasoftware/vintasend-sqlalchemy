"""``requeue_notification`` on the sync SQLAlchemy backend, and the service actions built on it."""

import datetime

import pytest
from sqlalchemy import delete, update
from sqlalchemy.orm import Session, sessionmaker
from vintasend.constants import (
    REQUEUEABLE_NOTIFICATION_STATUSES,
    NotificationStatus,
    NotificationTypes,
)
from vintasend.exceptions import NotificationNotFoundError, NotificationRequeueError
from vintasend.services.dataclasses import NotificationAttachment, OneOffNotification
from vintasend.services.notification_service import NotificationService, register_context

from example_app.models import Notification as NotificationModel
from example_app.models import User
from vintasend_sqlalchemy.model_factory import AttachmentFileRecord as AttachmentFileRecordModel
from vintasend_sqlalchemy.model_factory import NotificationAttachment as NotificationAttachmentModel
from vintasend_sqlalchemy.services.notification_backends.sqlalchemy_notification_backend import (
    SQLAlchemyNotificationBackend,
)


FAKE_EMAIL_ADAPTER = "vintasend.services.notification_adapters.stubs.fake_adapter.FakeEmailAdapter"
FAKE_RENDERER = (
    "vintasend.services.notification_template_renderers.stubs."
    "fake_templated_email_renderer.FakeTemplateRenderer"
)


@register_context("sqlalchemy_requeue_test_context")
def _context():
    return {"greeting": "hi"}


@pytest.fixture
def user_id(db_session: sessionmaker[Session]):
    with db_session.begin() as session:
        user = User(email="foo@example.com")
        session.add(user)
        session.flush()
        uid = user.id
    yield uid
    with db_session.begin() as session:
        session.execute(delete(NotificationAttachmentModel))
        session.execute(delete(AttachmentFileRecordModel))
        session.execute(delete(NotificationModel))
        session.execute(delete(User))


def _backend(db_session: sessionmaker[Session]) -> SQLAlchemyNotificationBackend:
    return SQLAlchemyNotificationBackend(db_session, NotificationModel)


def _naive(value: datetime.datetime | None) -> datetime.datetime | None:
    # SQLite hands timestamps back without a timezone.
    return None if value is None else value.replace(tzinfo=None)


def _set(db_session, notification_id, **values):
    with db_session.begin() as session:
        session.execute(
            update(NotificationModel).where(NotificationModel.id == notification_id).values(values)
        )


def _persist(backend, user_id, status=NotificationStatus.FAILED, **overrides):
    defaults = dict(
        user_id=user_id,
        notification_type=NotificationTypes.EMAIL.value,
        title="test",
        body_template="test",
        context_name="sqlalchemy_requeue_test_context",
        context_kwargs={},
        send_after=None,
        subject_template="test",
        preheader_template="test",
    )
    defaults.update(overrides)
    notification = backend.persist_notification(**defaults)
    _set(backend.session_manager, notification.id, status=status.value)
    return notification


@pytest.mark.parametrize("status", list(NotificationStatus))
def test_moves_only_a_failed_or_cancelled_notification(db_session, user_id, status):
    backend = _backend(db_session)
    notification = _persist(backend, user_id, status=status)

    if status.value in REQUEUEABLE_NOTIFICATION_STATUSES:
        requeued = backend.requeue_notification(notification.id)
        assert requeued.status == NotificationStatus.PENDING_SEND.value
        return

    with pytest.raises(NotificationRequeueError) as raised:
        backend.requeue_notification(notification.id)
    assert str(raised.value) == (
        f"Cannot requeue notification {notification.id}: it is not FAILED or CANCELLED"
    )
    assert backend.get_notification(notification.id).status == status.value


def test_the_status_check_is_part_of_the_write(db_session, user_id):
    backend = _backend(db_session)
    notification = _persist(backend, user_id)

    backend.requeue_notification(notification.id)

    # A second caller racing the first finds it PENDING_SEND and is refused.
    with pytest.raises(NotificationRequeueError):
        backend.requeue_notification(notification.id)


def test_an_unknown_id_is_not_found(db_session, user_id):
    with pytest.raises(NotificationNotFoundError) as raised:
        _backend(db_session).requeue_notification(987654)

    assert str(raised.value) == "Notification 987654 not found"


def test_send_after_is_applied_only_when_passed(db_session, user_id):
    backend = _backend(db_session)
    original = datetime.datetime(2030, 1, 1, tzinfo=datetime.timezone.utc)
    later = datetime.datetime(2031, 1, 1, tzinfo=datetime.timezone.utc)
    notification = _persist(backend, user_id, send_after=original)

    assert _naive(backend.requeue_notification(notification.id).send_after) == _naive(original)
    backend.mark_pending_as_failed(notification.id)
    assert _naive(backend.requeue_notification(notification.id, later).send_after) == _naive(later)
    backend.mark_pending_as_failed(notification.id)
    assert backend.requeue_notification(notification.id, None).send_after is None


def test_clears_sent_at_and_keeps_the_rest(db_session, user_id):
    backend = _backend(db_session)
    notification = _persist(backend, user_id, requested_template_version=3)
    _set(
        db_session,
        notification.id,
        sent_at=datetime.datetime.now(datetime.timezone.utc),
        context_used={"stored": True},
        adapter_used="some.adapter",
    )

    requeued = backend.requeue_notification(notification.id)

    assert requeued.sent_at is None
    assert requeued.requested_template_version == 3
    assert requeued.context_used == {"stored": True}
    assert requeued.adapter_used == "some.adapter"


def test_returns_the_attachments(db_session, user_id):
    backend = _backend(db_session)
    notification = _persist(
        backend,
        user_id,
        status=NotificationStatus.CANCELLED,
        attachments=[NotificationAttachment(file=b"report", filename="report.txt")],
    )

    requeued = backend.requeue_notification(notification.id)

    assert [attachment.filename for attachment in requeued.attachments] == ["report.txt"]


def test_requeues_a_one_off_notification(db_session, user_id):
    backend = _backend(db_session)
    notification = backend.persist_one_off_notification(
        email_or_phone="someone@example.com",
        first_name="Some",
        last_name="One",
        notification_type=NotificationTypes.EMAIL.value,
        title="One-off",
        body_template="test",
        context_name="test",
        context_kwargs={},
        send_after=None,
        subject_template="",
        preheader_template="",
    )
    _set(db_session, notification.id, status=NotificationStatus.FAILED.value)

    requeued = backend.requeue_notification(notification.id)

    assert isinstance(requeued, OneOffNotification)
    assert requeued.status == NotificationStatus.PENDING_SEND.value


@pytest.mark.parametrize("status", [NotificationStatus.FAILED, NotificationStatus.CANCELLED])
def test_a_failed_or_cancelled_notification_can_be_updated(db_session, user_id, status):
    backend = _backend(db_session)
    notification = _persist(backend, user_id, status=status)

    updated = backend.persist_notification_update(
        notification.id, {"requested_template_version": 4}
    )

    assert updated.requested_template_version == 4
    assert updated.status == status.value


def test_the_service_requeues_a_cancelled_notification_and_sends_it(db_session, user_id):
    backend = _backend(db_session)
    service = NotificationService(
        notification_adapters=[(FAKE_EMAIL_ADAPTER, FAKE_RENDERER)],
        notification_backend=backend,
        # The adapters build their own backend from these.
        notification_backend_kwargs={
            "session": db_session,
            "notification_model_cls": NotificationModel,
        },
    )
    notification = _persist(backend, user_id, status=NotificationStatus.CANCELLED)

    requeued = service.requeue_notification(notification.id, send_after=None)

    assert requeued.status == NotificationStatus.PENDING_SEND.value
    assert backend.get_notification(notification.id).status == NotificationStatus.SENT.value


def test_the_service_repins_a_failed_notification(db_session, user_id):
    backend = _backend(db_session)
    service = NotificationService(
        notification_adapters=[(FAKE_EMAIL_ADAPTER, FAKE_RENDERER)],
        notification_backend=backend,
        # The adapters build their own backend from these.
        notification_backend_kwargs={
            "session": db_session,
            "notification_model_cls": NotificationModel,
        },
    )
    notification = _persist(backend, user_id)

    repinned = service.repin_notification(notification.id, template_version=0)

    assert repinned.requested_template_version == 0
    stored = backend.get_notification(notification.id)
    assert stored.requested_template_version == 0
    assert stored.status == NotificationStatus.FAILED.value
