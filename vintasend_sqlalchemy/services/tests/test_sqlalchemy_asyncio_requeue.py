"""``requeue_notification`` on the AsyncIO SQLAlchemy backend, and the service actions on it."""

import datetime

import pytest
import pytest_asyncio
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from vintasend.constants import (
    REQUEUEABLE_NOTIFICATION_STATUSES,
    NotificationStatus,
    NotificationTypes,
)
from vintasend.exceptions import NotificationNotFoundError, NotificationRequeueError
from vintasend.services.dataclasses import NotificationAttachment, OneOffNotification
from vintasend.services.notification_service import AsyncIONotificationService, register_context

from example_app.models import Notification as NotificationModel
from example_app.models import User
from vintasend_sqlalchemy.model_factory import AttachmentFileRecord as AttachmentFileRecordModel
from vintasend_sqlalchemy.model_factory import NotificationAttachment as NotificationAttachmentModel
from vintasend_sqlalchemy.services.notification_backends.sqlalchemy_notification_backend import (
    SQLAlchemyAsyncIONotificationBackend,
)


FAKE_EMAIL_ADAPTER = (
    "vintasend.services.notification_adapters.stubs.fake_adapter.FakeAsyncIOEmailAdapter"
)
FAKE_RENDERER = (
    "vintasend.services.notification_template_renderers.stubs."
    "fake_templated_email_renderer.FakeTemplateRenderer"
)


@register_context("sqlalchemy_asyncio_requeue_test_context")
def _asyncio_context():
    return {"greeting": "hi"}


@pytest_asyncio.fixture(loop_scope="session")
async def user_id(async_db_session: async_sessionmaker[AsyncSession]):
    async with async_db_session.begin() as session:
        user = User(email="foo@example.com")
        session.add(user)
        await session.flush()
        uid = user.id
        session.expunge(user)
    yield uid
    async with async_db_session.begin() as session:
        await session.execute(delete(NotificationAttachmentModel))
        await session.execute(delete(AttachmentFileRecordModel))
        await session.execute(delete(NotificationModel))
        await session.execute(delete(User))


def _backend(async_db_session) -> SQLAlchemyAsyncIONotificationBackend:
    return SQLAlchemyAsyncIONotificationBackend(async_db_session, NotificationModel)


def _naive(value: datetime.datetime | None) -> datetime.datetime | None:
    # SQLite hands timestamps back without a timezone.
    return None if value is None else value.replace(tzinfo=None)


async def _set(async_db_session, notification_id, **values):
    async with async_db_session.begin() as session:
        await session.execute(
            update(NotificationModel).where(NotificationModel.id == notification_id).values(values)
        )


async def _persist(backend, user_id, status=NotificationStatus.FAILED, **overrides):
    defaults = dict(
        user_id=user_id,
        notification_type=NotificationTypes.EMAIL.value,
        title="test",
        body_template="test",
        context_name="sqlalchemy_asyncio_requeue_test_context",
        context_kwargs={},
        send_after=None,
        subject_template="test",
        preheader_template="test",
    )
    defaults.update(overrides)
    notification = await backend.persist_notification(**defaults)
    await _set(backend.session_manager, notification.id, status=status.value)
    return notification


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("status", list(NotificationStatus))
async def test_moves_only_a_failed_or_cancelled_notification(async_db_session, user_id, status):
    backend = _backend(async_db_session)
    notification = await _persist(backend, user_id, status=status)

    if status.value in REQUEUEABLE_NOTIFICATION_STATUSES:
        requeued = await backend.requeue_notification(notification.id)
        assert requeued.status == NotificationStatus.PENDING_SEND.value
        return

    with pytest.raises(NotificationRequeueError) as raised:
        await backend.requeue_notification(notification.id)
    assert str(raised.value) == (
        f"Cannot requeue notification {notification.id}: it is not FAILED or CANCELLED"
    )
    assert (await backend.get_notification(notification.id)).status == status.value


@pytest.mark.asyncio(loop_scope="session")
async def test_the_status_check_is_part_of_the_write(async_db_session, user_id):
    backend = _backend(async_db_session)
    notification = await _persist(backend, user_id)

    await backend.requeue_notification(notification.id)

    # A second caller racing the first finds it PENDING_SEND and is refused.
    with pytest.raises(NotificationRequeueError):
        await backend.requeue_notification(notification.id)


@pytest.mark.asyncio(loop_scope="session")
async def test_an_unknown_id_is_not_found(async_db_session, user_id):
    with pytest.raises(NotificationNotFoundError) as raised:
        await _backend(async_db_session).requeue_notification(987654)

    assert str(raised.value) == "Notification 987654 not found"


@pytest.mark.asyncio(loop_scope="session")
async def test_send_after_is_applied_only_when_passed(async_db_session, user_id):
    backend = _backend(async_db_session)
    original = datetime.datetime(2030, 1, 1, tzinfo=datetime.timezone.utc)
    later = datetime.datetime(2031, 1, 1, tzinfo=datetime.timezone.utc)
    notification = await _persist(backend, user_id, send_after=original)

    assert _naive((await backend.requeue_notification(notification.id)).send_after) == _naive(
        original
    )
    await backend.mark_pending_as_failed(notification.id)
    assert _naive(
        (await backend.requeue_notification(notification.id, later)).send_after
    ) == _naive(later)
    await backend.mark_pending_as_failed(notification.id)
    assert (await backend.requeue_notification(notification.id, None)).send_after is None


@pytest.mark.asyncio(loop_scope="session")
async def test_clears_sent_at_and_keeps_the_rest(async_db_session, user_id):
    backend = _backend(async_db_session)
    notification = await _persist(backend, user_id, requested_template_version=3)
    await _set(
        async_db_session,
        notification.id,
        sent_at=datetime.datetime.now(datetime.timezone.utc),
        context_used={"stored": True},
        adapter_used="some.adapter",
    )

    requeued = await backend.requeue_notification(notification.id)

    assert requeued.sent_at is None
    assert requeued.requested_template_version == 3
    assert requeued.context_used == {"stored": True}
    assert requeued.adapter_used == "some.adapter"


@pytest.mark.asyncio(loop_scope="session")
async def test_returns_the_attachments(async_db_session, user_id):
    backend = _backend(async_db_session)
    notification = await _persist(
        backend,
        user_id,
        status=NotificationStatus.CANCELLED,
        attachments=[NotificationAttachment(file=b"report", filename="report.txt")],
    )

    requeued = await backend.requeue_notification(notification.id)

    assert [attachment.filename for attachment in requeued.attachments] == ["report.txt"]


@pytest.mark.asyncio(loop_scope="session")
async def test_requeues_a_one_off_notification(async_db_session, user_id):
    backend = _backend(async_db_session)
    notification = await backend.persist_one_off_notification(
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
    await _set(async_db_session, notification.id, status=NotificationStatus.FAILED.value)

    requeued = await backend.requeue_notification(notification.id)

    assert isinstance(requeued, OneOffNotification)
    assert requeued.status == NotificationStatus.PENDING_SEND.value


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("status", [NotificationStatus.FAILED, NotificationStatus.CANCELLED])
async def test_a_failed_or_cancelled_notification_can_be_updated(async_db_session, user_id, status):
    backend = _backend(async_db_session)
    notification = await _persist(backend, user_id, status=status)

    updated = await backend.persist_notification_update(
        notification.id, {"requested_template_version": 4}
    )

    assert updated.requested_template_version == 4
    assert updated.status == status.value


@pytest.mark.asyncio(loop_scope="session")
async def test_the_service_requeues_a_cancelled_notification_and_sends_it(
    async_db_session, user_id
):
    backend = _backend(async_db_session)
    service = AsyncIONotificationService(
        notification_adapters=[(FAKE_EMAIL_ADAPTER, FAKE_RENDERER)],
        notification_backend=backend,
        # The adapters build their own backend from these.
        notification_backend_kwargs={
            "session": async_db_session,
            "notification_model_cls": NotificationModel,
        },
    )
    notification = await _persist(backend, user_id, status=NotificationStatus.CANCELLED)

    requeued = await service.requeue_notification(notification.id, send_after=None)

    # The service returns the notification read again after the send it started.
    assert requeued.status == NotificationStatus.SENT.value
    assert (await backend.get_notification(notification.id)).status == NotificationStatus.SENT.value


@pytest.mark.asyncio(loop_scope="session")
async def test_the_service_repins_a_failed_notification(async_db_session, user_id):
    backend = _backend(async_db_session)
    service = AsyncIONotificationService(
        notification_adapters=[(FAKE_EMAIL_ADAPTER, FAKE_RENDERER)],
        notification_backend=backend,
        # The adapters build their own backend from these.
        notification_backend_kwargs={
            "session": async_db_session,
            "notification_model_cls": NotificationModel,
        },
    )
    notification = await _persist(backend, user_id)

    repinned = await service.repin_notification(notification.id, template_version=0)

    assert repinned.requested_template_version == 0
    stored = await backend.get_notification(notification.id)
    assert stored.requested_template_version == 0
    assert stored.status == NotificationStatus.FAILED.value
