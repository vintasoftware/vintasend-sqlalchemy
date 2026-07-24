"""Regression coverage for the VintaSend 2.0 surface on the AsyncIO SQLAlchemy backend."""

import datetime
import uuid
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from vintasend.constants import NotificationTypes
from vintasend.exceptions import AttachmentFileNotFoundError, NotificationNotFoundError
from vintasend.services.dataclasses import (
    AttachmentFileRecord,
    NotificationAttachment,
    NotificationAttachmentReference,
    OneOffNotification,
)

from example_app.models import Notification as NotificationModel
from example_app.models import User
from vintasend_sqlalchemy.model_factory import AttachmentFileRecord as AttachmentFileRecordModel
from vintasend_sqlalchemy.model_factory import NotificationAttachment as NotificationAttachmentModel
from vintasend_sqlalchemy.services.notification_backends.sqlalchemy_notification_backend import (
    SQLAlchemyAsyncIONotificationBackend,
)


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


async def _persist(backend, user_id, **overrides):
    defaults = dict(
        user_id=user_id,
        notification_type=NotificationTypes.IN_APP.value,
        title="test",
        body_template="test",
        context_name="test",
        context_kwargs={},
        send_after=None,
        subject_template="test",
        preheader_template="test",
    )
    defaults.update(overrides)
    return await backend.persist_notification(**defaults)


@pytest.mark.asyncio(loop_scope="session")
async def test_backend_instantiates(async_db_session):
    backend = _backend(async_db_session)
    assert isinstance(backend, SQLAlchemyAsyncIONotificationBackend)


@pytest.mark.asyncio(loop_scope="session")
async def test_persist_one_off_and_get(async_db_session, user_id):
    backend = _backend(async_db_session)
    one_off = await backend.persist_one_off_notification(
        email_or_phone="recipient@example.com",
        first_name="Jane",
        last_name="Doe",
        notification_type=NotificationTypes.EMAIL.value,
        title="test",
        body_template="test",
        context_name="test",
        context_kwargs={},
        send_after=None,
        subject_template="test",
        preheader_template="test",
    )
    assert isinstance(one_off, OneOffNotification)
    fetched = await backend.get_notification(one_off.id)
    assert isinstance(fetched, OneOffNotification)
    assert fetched.email_or_phone == "recipient@example.com"


@pytest.mark.asyncio(loop_scope="session")
async def test_mark_sent_sets_sent_at_and_read_at(async_db_session, user_id):
    backend = _backend(async_db_session)
    notification = await _persist(backend, user_id)

    sent = await backend.mark_pending_as_sent(notification.id)
    assert sent.sent_at is not None
    read = await backend.mark_sent_as_read(notification.id)
    assert read.read_at is not None


@pytest.mark.asyncio(loop_scope="session")
async def test_mark_sent_as_read_bulk_scoped(async_db_session, user_id):
    backend = _backend(async_db_session)
    n1 = await _persist(backend, user_id)
    n2 = await _persist(backend, user_id)
    await backend.mark_pending_as_sent(n1.id)
    await backend.mark_pending_as_sent(n2.id)

    result = list(await backend.mark_sent_as_read_bulk([n1.id, n2.id], user_id=user_id))
    assert {r.id for r in result} == {n1.id, n2.id}
    # Idempotent re-run.
    again = list(await backend.mark_sent_as_read_bulk([n1.id, n2.id], user_id=user_id))
    assert {r.id for r in again} == {n1.id, n2.id}


@pytest.mark.asyncio(loop_scope="session")
async def test_filter_notifications_empty_and_negation(async_db_session, user_id):
    backend = _backend(async_db_session)
    await _persist(backend, user_id, tenant="acme")
    tenant_less = await _persist(backend, user_id, tenant=None)

    assert await backend.count_notifications({}) == 2
    not_acme = list(await backend.filter_notifications({"not": {"tenant": "acme"}}, 1, 50))
    assert tenant_less.id in {n.id for n in not_acme}


@pytest.mark.asyncio(loop_scope="session")
async def test_store_git_commit_sha(async_db_session, user_id):
    backend = _backend(async_db_session)
    notification = await _persist(backend, user_id)
    sha = "b" * 40
    await backend.store_git_commit_sha(notification.id, sha)
    assert (await backend.get_notification(notification.id)).git_commit_sha == sha


@pytest.mark.asyncio(loop_scope="session")
async def test_attachment_upload_dedup_and_reference(async_db_session, user_id):
    backend = _backend(async_db_session)
    notification = await _persist(
        backend,
        user_id,
        attachments=[NotificationAttachment(file=b"async bytes", filename="a.txt")],
    )
    assert notification.attachments[0].get_file_data() == b"async bytes"
    file_id = notification.attachments[0].file_id

    # Dedup on identical bytes.
    second = await _persist(
        backend,
        user_id,
        attachments=[NotificationAttachment(file=b"async bytes", filename="a.txt")],
    )
    assert second.attachments[0].file_id == file_id

    # Reference an already-stored file by id.
    referencing = await _persist(
        backend,
        user_id,
        attachments=[NotificationAttachmentReference(file_id=file_id)],
    )
    assert referencing.attachments[0].file_id == file_id

    with pytest.raises(AttachmentFileNotFoundError):
        await _persist(
            backend,
            user_id,
            attachments=[NotificationAttachmentReference(file_id="999999")],
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_orphaned_and_delete_notification_attachment(async_db_session, user_id):
    backend = _backend(async_db_session)
    notification = await _persist(
        backend,
        user_id,
        attachments=[NotificationAttachment(file=b"orphan", filename="o.txt")],
    )
    join_id = notification.attachments[0].id
    assert list(await backend.get_orphaned_attachment_files()) == []

    await backend.delete_notification_attachment(join_id)
    assert list(await backend.get_attachments(notification.id)) == []
    assert len(list(await backend.get_orphaned_attachment_files())) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_get_notification_not_found(async_db_session, user_id):
    backend = _backend(async_db_session)
    with pytest.raises(NotificationNotFoundError):
        await backend.get_notification(uuid.uuid4())


@pytest.mark.asyncio(loop_scope="session")
async def test_get_all_pending_notifications(async_db_session, user_id):
    backend = _backend(async_db_session)
    first = await _persist(backend, user_id)
    second = await _persist(backend, user_id)
    already_sent = await _persist(backend, user_id)
    await backend.mark_pending_as_sent(already_sent.id)

    all_pending = list(await backend.get_all_pending_notifications())
    assert {n.id for n in all_pending} == {first.id, second.id}


@pytest.mark.asyncio(loop_scope="session")
async def test_future_notifications_all_paginated_and_by_user(async_db_session, user_id):
    backend = _backend(async_db_session)
    async with async_db_session.begin() as session:
        other = User(email="other@example.com")
        session.add(other)
        await session.flush()
        other_id = other.id
        session.expunge(other)

    future = datetime.datetime.now(tz=datetime.timezone.utc) + timedelta(days=1)
    mine_1 = await _persist(backend, user_id, send_after=future)
    mine_2 = await _persist(backend, user_id, send_after=future)
    theirs = await _persist(backend, other_id, send_after=future)
    await _persist(backend, user_id, send_after=None)

    all_future = list(await backend.get_all_future_notifications())
    assert {mine_1.id, mine_2.id, theirs.id} <= {n.id for n in all_future}

    paged = list(await backend.get_future_notifications(page=1, page_size=2))
    assert len(paged) == 2

    mine_all = list(await backend.get_all_future_notifications_from_user(user_id))
    assert {n.id for n in mine_all} == {mine_1.id, mine_2.id}

    mine_paged = list(
        await backend.get_future_notifications_from_user(user_id, page=1, page_size=1)
    )
    assert len(mine_paged) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_in_app_notifications_paginated_and_counts(async_db_session, user_id):
    backend = _backend(async_db_session)
    read_one = await _persist(backend, user_id, notification_type=NotificationTypes.IN_APP.value)
    unread_one = await _persist(backend, user_id, notification_type=NotificationTypes.IN_APP.value)
    unread_two = await _persist(backend, user_id, notification_type=NotificationTypes.IN_APP.value)
    for n in (read_one, unread_one, unread_two):
        await backend.mark_pending_as_sent(n.id)
    await backend.mark_sent_as_read(read_one.id)

    assert await backend.count_in_app_notifications(user_id) == 3
    assert await backend.count_in_app_unread_notifications(user_id) == 2

    all_unread = list(await backend.filter_all_in_app_unread_notifications(user_id))
    assert {n.id for n in all_unread} == {unread_one.id, unread_two.id}

    all_in_app = list(await backend.filter_all_in_app_notifications(user_id))
    assert {n.id for n in all_in_app} == {read_one.id, unread_one.id, unread_two.id}

    first_page = list(await backend.filter_in_app_notifications(user_id, page=1, page_size=2))
    second_page = list(await backend.filter_in_app_notifications(user_id, page=2, page_size=2))
    assert len(first_page) == 2 and len(second_page) == 1

    unread_page = list(
        await backend.filter_in_app_unread_notifications(user_id, page=1, page_size=1)
    )
    assert len(unread_page) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_filter_notifications_ordering_is_stable(async_db_session, user_id):
    backend = _backend(async_db_session)
    first = await _persist(backend, user_id)
    second = await _persist(backend, user_id)

    ascending = list(
        await backend.filter_notifications(
            {}, page=1, page_size=50, order_by={"field": "created_at", "direction": "asc"}
        )
    )
    assert [n.id for n in ascending] == [first.id, second.id]

    descending = list(
        await backend.filter_notifications(
            {}, page=1, page_size=50, order_by={"field": "created_at", "direction": "desc"}
        )
    )
    assert [n.id for n in descending] == [second.id, first.id]


@pytest.mark.asyncio(loop_scope="session")
async def test_get_user_email_from_notification(async_db_session, user_id):
    backend = _backend(async_db_session)
    notification = await _persist(backend, user_id)
    assert await backend.get_user_email_from_notification(notification.id) == "foo@example.com"


@pytest.mark.asyncio(loop_scope="session")
async def test_store_context_used(async_db_session, user_id):
    backend = _backend(async_db_session)
    notification = await _persist(backend, user_id)
    await backend.store_context_used(notification.id, {"key": "value"}, "myapp.adapters.Email")

    refetched = await backend.get_notification(notification.id)
    assert refetched.context_used == {"key": "value"}
    assert refetched.adapter_used == "myapp.adapters.Email"


@pytest.mark.asyncio(loop_scope="session")
async def test_attachment_file_record_crud(async_db_session, user_id):
    backend = _backend(async_db_session)
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    record = AttachmentFileRecord(
        id="ignored",
        filename="doc.pdf",
        content_type="application/pdf",
        size=1234,
        checksum="deadbeef",
        created_at=now,
        updated_at=now,
        storage_identifiers={"id": "abc"},
    )

    stored = await backend.store_attachment_file_record(record)
    assert stored.id != "ignored"

    fetched = await backend.get_attachment_file_record(stored.id)
    assert fetched is not None and fetched.checksum == "deadbeef"

    found = await backend.find_attachment_file_by_checksum("deadbeef", 1234)
    assert found is not None and found.id == stored.id
    assert await backend.find_attachment_file_by_checksum("deadbeef", 9999) is None

    await backend.delete_attachment_file(stored.id)
    assert await backend.get_attachment_file_record(stored.id) is None


@pytest.mark.asyncio(loop_scope="session")
async def test_persist_notification_update_and_already_sent(async_db_session, user_id):
    backend = _backend(async_db_session)
    notification = await _persist(backend, user_id)

    updated = await backend.persist_notification_update(
        notification.id, {"subject_template": "changed"}
    )
    assert updated.subject_template == "changed"

    # An already-sent notification cannot be updated.
    from vintasend.exceptions import NotificationUpdateError

    await backend.mark_pending_as_sent(notification.id)
    with pytest.raises(NotificationUpdateError):
        await backend.persist_notification_update(notification.id, {"subject_template": "again"})
