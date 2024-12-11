import datetime
import uuid
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from vintasend.constants import NotificationStatus, NotificationTypes
from vintasend.exceptions import (
    NotificationCancelError,
    NotificationNotFoundError,
    NotificationUpdateError,
)
from vintasend.services.dataclasses import Notification, UpdateNotificationKwargs

from example_app.models import Notification as NotificationModel
from example_app.models import User
from vintasend_sqlalchemy.services.notification_backends.sqlalchemy_notification_backend import (
    SQLAlchemyAsyncIONotificationBackend,
)


@pytest_asyncio.fixture(autouse=True)
async def setup_fixture(async_db_session: async_sessionmaker[AsyncSession]):
    async with async_db_session.begin() as session:
        user = User(email="foo@example.com")
        session.add(user)
        await session.flush()
        session.expunge(user)
    yield user

@pytest.mark.asyncio
async def test_persist_notification(
    async_db_session: async_sessionmaker[AsyncSession], setup_fixture: User
):
    user = setup_fixture
    user_id = user.id
    notification = await SQLAlchemyAsyncIONotificationBackend(
        async_db_session, NotificationModel
    ).persist_notification(
        user_id=user_id,
        notification_type=NotificationTypes.EMAIL.value,
        title="test",
        body_template="test",
        context_name="test",
        context_kwargs={},
        send_after=None,
        subject_template="test",
        preheader_template="test",
    )

    assert isinstance(notification, Notification)
    assert notification.user_id == user_id
    assert notification.notification_type == NotificationTypes.EMAIL.value
    assert notification.title == "test"
    assert notification.body_template == "test"
    assert notification.context_name == "test"
    assert notification.context_kwargs == {}
    assert notification.send_after is None
    assert notification.subject_template == "test"
    assert notification.preheader_template == "test"
    assert notification.status == NotificationStatus.PENDING_SEND.value
    assert notification.id is not None

    notification_id = notification.id

    async with async_db_session.begin() as session:
        notification_db_record = await session.get(NotificationModel, notification_id)
        assert notification_db_record is not None
        assert notification_db_record.get_user_id() == user_id
        assert notification_db_record.notification_type == NotificationTypes.EMAIL.value
        assert notification_db_record.title == "test"
        assert notification_db_record.body_template == "test"
        assert notification_db_record.context_name == "test"
        assert notification_db_record.context_kwargs == {}
        assert notification_db_record.send_after is None
        assert notification_db_record.subject_template == "test"
        assert notification_db_record.preheader_template == "test"
        assert notification_db_record.status == NotificationStatus.PENDING_SEND.value

    async with async_db_session.begin() as session:
        await session.execute(
            delete(NotificationModel).where(NotificationModel.id == notification_id)
        )


@pytest.mark.asyncio
async def test_update_notification(
    async_db_session: async_sessionmaker[AsyncSession], setup_fixture: User
):
    user = setup_fixture
    user_id = user.id
    notification = await SQLAlchemyAsyncIONotificationBackend(
        async_db_session, NotificationModel
    ).persist_notification(
        user_id=user_id,
        notification_type=NotificationTypes.EMAIL.value,
        title="test",
        body_template="test",
        context_name="test",
        context_kwargs={},
        send_after=None,
        subject_template="test",
        preheader_template="test",
    )

    updated_data: UpdateNotificationKwargs = {"subject_template": "updated test subject"}
    updated_notification = await SQLAlchemyAsyncIONotificationBackend(
        async_db_session, NotificationModel
    ).persist_notification_update(
        notification_id=notification.id,
        updated_data=updated_data,
    )

    assert updated_notification.subject_template == "updated test subject"

    async with async_db_session.begin() as session:
        notification_db_record = await session.get(NotificationModel, notification.id)
        assert notification_db_record is not None
        assert notification_db_record.subject_template == "updated test subject"

    async with async_db_session.begin() as session:
        await session.execute(
            delete(NotificationModel).where(NotificationModel.id == notification.id)
        )


@pytest.mark.asyncio
async def test_get_pending_notifications(
    async_db_session: async_sessionmaker[AsyncSession], setup_fixture: User
):
    user = setup_fixture
    user_id = user.id
    await SQLAlchemyAsyncIONotificationBackend(
        async_db_session, NotificationModel
    ).persist_notification(
        user_id=user_id,
        notification_type=NotificationTypes.EMAIL.value,
        title="test",
        body_template="test",
        context_name="test",
        context_kwargs={},
        send_after=None,
        subject_template="test",
        preheader_template="test",
    )

    await SQLAlchemyAsyncIONotificationBackend(
        async_db_session, NotificationModel
    ).persist_notification(
        user_id=user_id,
        notification_type=NotificationTypes.EMAIL.value,
        title="test 2",
        body_template="test 2",
        context_name="test 2",
        context_kwargs={},
        send_after=None,
        subject_template="test 2",
        preheader_template="test 2",
    )

    already_sent = await SQLAlchemyAsyncIONotificationBackend(
        async_db_session, NotificationModel
    ).persist_notification(
        user_id=user_id,
        notification_type=NotificationTypes.EMAIL.value,
        title="test already sent",
        body_template="test already sent",
        context_name="test already sent",
        context_kwargs={},
        send_after=None,
        subject_template="test already sent",
        preheader_template="test already sent",
    )

    await SQLAlchemyAsyncIONotificationBackend(
        async_db_session, NotificationModel
    ).mark_pending_as_sent(
        notification_id=already_sent.id,
    )

    notifications = list(
        await SQLAlchemyAsyncIONotificationBackend(
            async_db_session, NotificationModel
        ).get_pending_notifications(page=1, page_size=1)
    )
    assert len(notifications) == 1
    notification_1 = notifications[0]
    assert isinstance(notification_1, Notification)
    assert notification_1.user_id == user_id
    assert notification_1.notification_type == NotificationTypes.EMAIL.value
    assert notification_1.title == "test"
    assert notification_1.body_template == "test"
    assert notification_1.context_name == "test"
    assert notification_1.context_kwargs == {}
    assert notification_1.send_after is None
    assert notification_1.subject_template == "test"
    assert notification_1.preheader_template == "test"
    assert notification_1.status == NotificationStatus.PENDING_SEND.value

    notifications = list(
        await SQLAlchemyAsyncIONotificationBackend(
            async_db_session, NotificationModel
        ).get_pending_notifications(page=2, page_size=1)
    )
    assert len(notifications) == 1
    notification_2 = notifications[0]
    assert isinstance(notification_2, Notification)
    assert notification_2.user_id == user_id
    assert notification_2.notification_type == NotificationTypes.EMAIL.value
    assert notification_2.title == "test 2"
    assert notification_2.body_template == "test 2"
    assert notification_2.context_name == "test 2"
    assert notification_2.context_kwargs == {}
    assert notification_2.send_after is None
    assert notification_2.subject_template == "test 2"
    assert notification_2.preheader_template == "test 2"
    assert notification_2.status == NotificationStatus.PENDING_SEND.value

    async with async_db_session.begin() as session:
        await session.execute(
            delete(NotificationModel).where(NotificationModel.id == notification_1.id)
        )
        await session.execute(
            delete(NotificationModel).where(NotificationModel.id == notification_2.id)
        )
        await session.execute(
            delete(NotificationModel).where(NotificationModel.id == already_sent.id)
        )

@pytest.mark.asyncio
async def test_mark_pending_as_sent(
    async_db_session: async_sessionmaker[AsyncSession], setup_fixture: User
):
    user = setup_fixture
    user_id = user.id
    notification = await SQLAlchemyAsyncIONotificationBackend(
        async_db_session, NotificationModel
    ).persist_notification(
        user_id=user_id,
        notification_type=NotificationTypes.EMAIL.value,
        title="test",
        body_template="test",
        context_name="test",
        context_kwargs={},
        send_after=None,
        subject_template="test",
        preheader_template="test",
    )

    notification = await SQLAlchemyAsyncIONotificationBackend(
        async_db_session, NotificationModel
    ).mark_pending_as_sent(notification.id)
    assert notification.status == NotificationStatus.SENT.value
    async with async_db_session.begin() as session:
        notification_db_record = await session.get(NotificationModel, notification.id)
        assert notification_db_record is not None
        assert notification_db_record.status == NotificationStatus.SENT.value

    async with async_db_session.begin() as session:
        await session.execute(
            delete(NotificationModel).where(NotificationModel.id == notification.id)
        )

@pytest.mark.asyncio
async def test_mark_pending_as_failed(
    async_db_session: async_sessionmaker[AsyncSession], setup_fixture: User
):
    user = setup_fixture
    user_id = user.id
    notification = await SQLAlchemyAsyncIONotificationBackend(
        async_db_session, NotificationModel
    ).persist_notification(
        user_id=user_id,
        notification_type=NotificationTypes.EMAIL.value,
        title="test",
        body_template="test",
        context_name="test",
        context_kwargs={},
        send_after=None,
        subject_template="test",
        preheader_template="test",
    )

    notification = await SQLAlchemyAsyncIONotificationBackend(
        async_db_session, NotificationModel
    ).mark_pending_as_failed(notification.id)
    assert notification.status == NotificationStatus.FAILED.value
    async with async_db_session.begin() as session:
        notification_db_record = await session.get(NotificationModel, notification.id)
        assert notification_db_record is not None
        assert notification_db_record.status == NotificationStatus.FAILED.value

    async with async_db_session.begin() as session:
        await session.execute(
            delete(NotificationModel).where(NotificationModel.id == notification.id)
        )

@pytest.mark.asyncio
async def test_mark_pending_as_failed_already_sent(
    async_db_session: async_sessionmaker[AsyncSession], setup_fixture: User
):
    user = setup_fixture
    user_id = user.id
    notification = await SQLAlchemyAsyncIONotificationBackend(
        async_db_session, NotificationModel
    ).persist_notification(
        user_id=user_id,
        notification_type=NotificationTypes.EMAIL.value,
        title="test",
        body_template="test",
        context_name="test",
        context_kwargs={},
        send_after=None,
        subject_template="test",
        preheader_template="test",
    )
    await SQLAlchemyAsyncIONotificationBackend(
        async_db_session, NotificationModel
    ).mark_pending_as_sent(notification.id)

    with pytest.raises(NotificationUpdateError):
        await SQLAlchemyAsyncIONotificationBackend(
            async_db_session, NotificationModel
        ).mark_pending_as_failed(notification.id)
    async with async_db_session.begin() as session:
        notification_db_record = await session.get(NotificationModel, notification.id)
        assert notification_db_record is not None
        assert notification_db_record.status == NotificationStatus.SENT.value

    async with async_db_session.begin() as session:
        await session.execute(
            delete(NotificationModel).where(NotificationModel.id == notification.id)
        )

@pytest.mark.asyncio
async def test_mark_sent_as_read(
    async_db_session: async_sessionmaker[AsyncSession], setup_fixture: User
):
    user = setup_fixture
    user_id = user.id
    notification = await SQLAlchemyAsyncIONotificationBackend(
        async_db_session, NotificationModel
    ).persist_notification(
        user_id=user_id,
        notification_type=NotificationTypes.EMAIL.value,
        title="test",
        body_template="test",
        context_name="test",
        context_kwargs={},
        send_after=None,
        subject_template="test",
        preheader_template="test",
    )
    await SQLAlchemyAsyncIONotificationBackend(
        async_db_session, NotificationModel
    ).mark_pending_as_sent(notification.id)

    notification = await SQLAlchemyAsyncIONotificationBackend(
        async_db_session, NotificationModel
    ).mark_sent_as_read(notification.id)
    assert notification.status == NotificationStatus.READ.value
    async with async_db_session.begin() as session:
        notification_db_record = await session.get(NotificationModel, notification.id)
        assert notification_db_record is not None
        assert notification_db_record.status == NotificationStatus.READ.value

    async with async_db_session.begin() as session:
        await session.execute(
            delete(NotificationModel).where(NotificationModel.id == notification.id)
        )

@pytest.mark.asyncio
async def test_cancel_notification(
    async_db_session: async_sessionmaker[AsyncSession], setup_fixture: User
):
    user = setup_fixture
    user_id = user.id
    notification = await SQLAlchemyAsyncIONotificationBackend(
        async_db_session, NotificationModel
    ).persist_notification(
        user_id=user_id,
        notification_type=NotificationTypes.EMAIL.value,
        title="test",
        body_template="test",
        context_name="test",
        context_kwargs={},
        send_after=datetime.datetime.now(tz=datetime.timezone.utc) + timedelta(days=1),
        subject_template="test",
        preheader_template="test",
    )

    await SQLAlchemyAsyncIONotificationBackend(
        async_db_session, NotificationModel
    ).cancel_notification(notification.id)
    async with async_db_session.begin() as session:
        notification_db_record = await session.get(NotificationModel, notification.id)
        assert notification_db_record is not None
        assert notification_db_record.status == NotificationStatus.CANCELLED.value

    async with async_db_session.begin() as session:
        await session.execute(
            delete(NotificationModel).where(NotificationModel.id == notification.id)
        )

@pytest.mark.asyncio
async def test_cancel_notification_already_sent(
    async_db_session: async_sessionmaker[AsyncSession], setup_fixture: User
):
    user = setup_fixture
    user_id = user.id
    notification = await SQLAlchemyAsyncIONotificationBackend(
        async_db_session, NotificationModel
    ).persist_notification(
        user_id=user_id,
        notification_type=NotificationTypes.EMAIL.value,
        title="test",
        body_template="test",
        context_name="test",
        context_kwargs={},
        send_after=None,
        subject_template="test",
        preheader_template="test",
    )
    await SQLAlchemyAsyncIONotificationBackend(
        async_db_session, NotificationModel
    ).mark_pending_as_sent(notification.id)

    with pytest.raises(NotificationCancelError):
        await SQLAlchemyAsyncIONotificationBackend(
            async_db_session, NotificationModel
        ).cancel_notification(notification.id)

    async with async_db_session.begin() as session:
        notification_db_record = await session.get(NotificationModel, notification.id)
        assert notification_db_record is not None
        assert notification_db_record.status != NotificationStatus.CANCELLED.value

    async with async_db_session.begin() as session:
        await session.execute(
            delete(NotificationModel).where(NotificationModel.id == notification.id)
        )

@pytest.mark.asyncio
async def test_get_notification(
    async_db_session: async_sessionmaker[AsyncSession], setup_fixture: User
):
    user = setup_fixture
    user_id = user.id
    notification = await SQLAlchemyAsyncIONotificationBackend(
        async_db_session, NotificationModel
    ).persist_notification(
        user_id=user_id,
        notification_type=NotificationTypes.EMAIL.value,
        title="test",
        body_template="test",
        context_name="test",
        context_kwargs={},
        send_after=None,
        subject_template="test",
        preheader_template="test",
    )

    notification_retrieved = await SQLAlchemyAsyncIONotificationBackend(
        async_db_session, NotificationModel
    ).get_notification(notification.id)
    assert notification_retrieved.id == notification.id
    assert notification_retrieved.user_id == user_id
    assert notification_retrieved.notification_type == NotificationTypes.EMAIL.value
    assert notification_retrieved.title == "test"
    assert notification_retrieved.body_template == "test"
    assert notification_retrieved.context_name == "test"
    assert notification_retrieved.context_kwargs == {}
    assert notification_retrieved.send_after is None
    assert notification_retrieved.subject_template == "test"
    assert notification_retrieved.preheader_template == "test"
    assert notification_retrieved.status == NotificationStatus.PENDING_SEND.value

    async with async_db_session.begin() as session:
        await session.execute(
            delete(NotificationModel).where(NotificationModel.id == notification.id)
        )

@pytest.mark.asyncio
async def test_get_notification_not_found(
    async_db_session: async_sessionmaker[AsyncSession], setup_fixture: User
):
    with pytest.raises(NotificationNotFoundError):
        await SQLAlchemyAsyncIONotificationBackend(
            async_db_session, NotificationModel
        ).get_notification(uuid.uuid4())


@pytest.mark.asyncio
async def test_get_notification_cancelled(
    async_db_session: async_sessionmaker[AsyncSession], setup_fixture: User
):
    user = setup_fixture
    user_id = user.id
    notification = await SQLAlchemyAsyncIONotificationBackend(
        async_db_session, NotificationModel
    ).persist_notification(
        user_id=user_id,
        notification_type=NotificationTypes.EMAIL.value,
        title="test",
        body_template="test",
        context_name="test",
        context_kwargs={},
        send_after=None,
        subject_template="test",
        preheader_template="test",
    )
    await SQLAlchemyAsyncIONotificationBackend(
        async_db_session, NotificationModel
    ).cancel_notification(notification.id)
    with pytest.raises(NotificationNotFoundError):
        await SQLAlchemyAsyncIONotificationBackend(
            async_db_session, NotificationModel
        ).get_notification(notification.id)

    async with async_db_session.begin() as session:
        await session.execute(
            delete(NotificationModel).where(NotificationModel.id == notification.id)
        )
