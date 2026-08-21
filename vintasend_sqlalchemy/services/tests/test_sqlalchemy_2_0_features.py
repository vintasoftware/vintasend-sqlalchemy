"""Regression coverage for the VintaSend 2.0 backend surface on the sync SQLAlchemy backend:
one-off notifications, sent_at/read_at, bulk read, the composable filter API, git-commit-sha,
and the attachment manager seam.
"""

import datetime
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session, sessionmaker
from vintasend.constants import NotificationStatus, NotificationTypes
from vintasend.exceptions import AttachmentFileNotFoundError, NotificationUpdateError
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
    SQLAlchemyNotificationBackend,
)


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


def _persist(backend, user_id, **overrides):
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
    return backend.persist_notification(**defaults)


def test_backend_instantiates(db_session):
    # Instantiating proves every abstract method added in 1.2/2.0 is implemented; a missing one
    # would raise TypeError here.
    backend = _backend(db_session)
    assert isinstance(backend, SQLAlchemyNotificationBackend)


def test_persist_one_off_and_get(db_session, user_id):
    backend = _backend(db_session)
    one_off = backend.persist_one_off_notification(
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
    assert one_off.email_or_phone == "recipient@example.com"
    assert one_off.first_name == "Jane"

    fetched = backend.get_notification(one_off.id)
    assert isinstance(fetched, OneOffNotification)
    assert fetched.email_or_phone == "recipient@example.com"


def test_mark_sent_sets_sent_at_and_read_at(db_session, user_id):
    backend = _backend(db_session)
    notification = _persist(backend, user_id)

    sent = backend.mark_pending_as_sent(notification.id)
    assert sent.status == NotificationStatus.SENT.value
    assert sent.sent_at is not None

    read = backend.mark_sent_as_read(notification.id)
    assert read.status == NotificationStatus.READ.value
    assert read.read_at is not None


def test_mark_sent_as_read_bulk_is_scoped_and_idempotent(db_session, user_id):
    backend = _backend(db_session)
    # A second user whose SENT notification must never be touched by a user_id-scoped bulk read.
    with db_session.begin() as session:
        other = User(email="other@example.com")
        session.add(other)
        session.flush()
        other_id = other.id

    n1 = _persist(backend, user_id)
    n2 = _persist(backend, user_id)
    other_notification = _persist(backend, other_id)
    for n in (n1, n2, other_notification):
        backend.mark_pending_as_sent(n.id)

    result = list(
        backend.mark_sent_as_read_bulk([n1.id, n2.id, other_notification.id], user_id=user_id)
    )
    read_ids = {r.id for r in result}
    assert n1.id in read_ids
    assert n2.id in read_ids
    # Scoped out: other user's row was neither read nor returned.
    assert other_notification.id not in read_ids
    assert backend.get_notification(other_notification.id).status == NotificationStatus.SENT.value

    # Idempotent: re-running never raises and still reports them read.
    again = list(backend.mark_sent_as_read_bulk([n1.id, n2.id], user_id=user_id))
    assert {r.id for r in again} == {n1.id, n2.id}


def test_filter_notifications_empty_matches_all_and_membership(db_session, user_id):
    backend = _backend(db_session)
    email = _persist(backend, user_id, notification_type=NotificationTypes.EMAIL.value)
    _persist(backend, user_id, notification_type=NotificationTypes.SMS.value)

    assert backend.count_notifications({}) == 2
    assert len(list(backend.filter_notifications({}, page=1, page_size=50))) == 2

    only_email = list(
        backend.filter_notifications(
            {"notification_type": NotificationTypes.EMAIL.value}, page=1, page_size=50
        )
    )
    assert [n.id for n in only_email] == [email.id]


def test_filter_notifications_negation_includes_null(db_session, user_id):
    backend = _backend(db_session)
    _persist(backend, user_id, tenant="acme")
    tenant_less = _persist(backend, user_id, tenant=None)

    # A positive tenant filter excludes the NULL-tenant row...
    acme = list(backend.filter_notifications({"tenant": "acme"}, page=1, page_size=50))
    assert tenant_less.id not in {n.id for n in acme}

    # ...and negation includes it, matching the reference NULL semantics.
    not_acme = list(backend.filter_notifications({"not": {"tenant": "acme"}}, page=1, page_size=50))
    assert tenant_less.id in {n.id for n in not_acme}


def test_filter_notifications_string_lookups(db_session, user_id):
    backend = _backend(db_session)
    welcome = _persist(backend, user_id, body_template="welcome_email.html")
    _persist(backend, user_id, body_template="receipt.txt")

    def ids(spec):
        return {n.id for n in backend.filter_notifications({"body_template": spec}, 1, 50)}

    # Bare string == exact, case-sensitive.
    assert ids("welcome_email.html") == {welcome.id}
    assert ids({"lookup": "starts_with", "value": "welcome"}) == {welcome.id}
    assert ids({"lookup": "ends_with", "value": ".html"}) == {welcome.id}
    assert ids({"lookup": "includes", "value": "_email"}) == {welcome.id}
    # Case-insensitive exact.
    assert ids({"lookup": "exact", "value": "WELCOME_EMAIL.HTML", "case_sensitive": False}) == {
        welcome.id
    }


def test_filter_notifications_membership_list_and_date_range(db_session, user_id):
    backend = _backend(db_session)
    email = _persist(backend, user_id, notification_type=NotificationTypes.EMAIL.value)
    sms = _persist(backend, user_id, notification_type=NotificationTypes.SMS.value)
    _persist(backend, user_id, notification_type=NotificationTypes.PUSH.value)

    # A list means membership.
    membership = backend.filter_notifications(
        {"notification_type": [NotificationTypes.EMAIL.value, NotificationTypes.SMS.value]}, 1, 50
    )
    assert {n.id for n in membership} == {email.id, sms.id}

    # Inclusive date range on created bounds every row created in the window.
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    windowed = backend.filter_notifications(
        {"created_at_range": {"from": now - timedelta(hours=1), "to": now + timedelta(hours=1)}},
        1,
        50,
    )
    assert len(list(windowed)) == 3
    # A window entirely in the past matches nothing.
    empty = backend.filter_notifications(
        {"created_at_range": {"from": now - timedelta(days=2), "to": now - timedelta(days=1)}},
        1,
        50,
    )
    assert list(empty) == []


def test_filter_notifications_by_template_version(db_session, user_id):
    """Which notifications are pinned to a version, and which one each actually rendered."""
    backend = _backend(db_session)
    pinned = _persist(backend, user_id, requested_template_version=3)
    other = _persist(backend, user_id, requested_template_version=4)
    backend.store_template_version(pinned.id, 3)

    requested = backend.filter_notifications({"requested_template_version": 3}, 1, 50)
    assert {n.id for n in requested} == {pinned.id}

    used = backend.filter_notifications({"used_template_version": 3}, 1, 50)
    assert {n.id for n in used} == {pinned.id}

    listed = backend.filter_notifications({"requested_template_version": [3, 4]}, 1, 50)
    assert {n.id for n in listed} == {pinned.id, other.id}
    assert backend.count_notifications({"requested_template_version": [3, 4]}) == 2


def test_filter_notifications_version_null_semantics(db_session, user_id):
    """No version is not version zero, and negation brings the unpinned rows back."""
    backend = _backend(db_session)
    pinned = _persist(backend, user_id, requested_template_version=1)
    unpinned = _persist(backend, user_id)

    positive = backend.filter_notifications({"requested_template_version": 1}, 1, 50)
    assert {n.id for n in positive} == {pinned.id}

    negated = backend.filter_notifications({"not": {"requested_template_version": 1}}, 1, 50)
    assert unpinned.id in {n.id for n in negated}


def test_filter_notifications_rejects_a_non_integer_version(db_session, user_id):
    """Forwarding a stringified version would raise on the bind rather than match nothing."""
    backend = _backend(db_session)
    _persist(backend, user_id, requested_template_version=3)

    assert list(backend.filter_notifications({"requested_template_version": "3"}, 1, 50)) == []
    assert list(backend.filter_notifications({"requested_template_version": [3, "x"]}, 1, 50)) == []


def test_filter_notifications_ordering_is_stable(db_session, user_id):
    backend = _backend(db_session)
    first = _persist(backend, user_id)
    second = _persist(backend, user_id)

    ascending = list(
        backend.filter_notifications(
            {}, page=1, page_size=50, order_by={"field": "created_at", "direction": "asc"}
        )
    )
    assert [n.id for n in ascending] == [first.id, second.id]

    descending = list(
        backend.filter_notifications(
            {}, page=1, page_size=50, order_by={"field": "created_at", "direction": "desc"}
        )
    )
    assert [n.id for n in descending] == [second.id, first.id]


def test_in_app_notifications_and_counts(db_session, user_id):
    backend = _backend(db_session)
    read_one = _persist(backend, user_id, notification_type=NotificationTypes.IN_APP.value)
    unread_one = _persist(backend, user_id, notification_type=NotificationTypes.IN_APP.value)
    backend.mark_pending_as_sent(read_one.id)
    backend.mark_pending_as_sent(unread_one.id)
    backend.mark_sent_as_read(read_one.id)

    assert backend.count_in_app_notifications(user_id) == 2
    assert backend.count_in_app_unread_notifications(user_id) == 1

    unread = list(backend.filter_all_in_app_unread_notifications(user_id))
    assert {n.id for n in unread} == {unread_one.id}
    both = list(backend.filter_all_in_app_notifications(user_id))
    assert {n.id for n in both} == {read_one.id, unread_one.id}


def test_store_git_commit_sha(db_session, user_id):
    backend = _backend(db_session)
    notification = _persist(backend, user_id)
    sha = "a" * 40
    backend.store_git_commit_sha(notification.id, sha)
    assert backend.get_notification(notification.id).git_commit_sha == sha


# --------------------------------------------------------------- template versions


def test_persist_notification_records_the_requested_template_version(db_session, user_id):
    backend = _backend(db_session)

    notification = _persist(backend, user_id, requested_template_version=3)

    assert notification.requested_template_version == 3
    assert backend.get_notification(notification.id).requested_template_version == 3


def test_a_notification_with_no_pin_stores_null(db_session, user_id):
    backend = _backend(db_session)

    notification = _persist(backend, user_id)

    assert notification.requested_template_version is None
    assert notification.used_template_version is None


def test_store_template_version(db_session, user_id):
    backend = _backend(db_session)
    notification = _persist(backend, user_id)

    backend.store_template_version(notification.id, 5)

    assert backend.get_notification(notification.id).used_template_version == 5


def test_a_one_off_notification_records_the_requested_template_version(db_session, user_id):
    backend = _backend(db_session)

    notification = backend.persist_one_off_notification(
        email_or_phone="someone@example.com",
        first_name="Some",
        last_name="One",
        notification_type=NotificationTypes.EMAIL.value,
        title="test",
        body_template="test",
        context_name="test",
        context_kwargs={},
        send_after=None,
        requested_template_version=2,
    )

    assert notification.requested_template_version == 2
    assert backend.get_notification(notification.id).requested_template_version == 2


def test_attachment_upload_dedup_and_get(db_session, user_id):
    backend = _backend(db_session)
    notification = _persist(
        backend,
        user_id,
        attachments=[NotificationAttachment(file=b"same bytes", filename="a.txt")],
    )
    assert len(notification.attachments) == 1
    assert notification.attachments[0].get_file_data() == b"same bytes"

    fetched = list(backend.get_attachments(notification.id))
    assert len(fetched) == 1
    assert fetched[0].filename == "a.txt"

    # Same bytes on a second notification reuse the existing file record (checksum, size) dedup:
    # a new join row, but the same underlying file_id.
    second = _persist(
        backend,
        user_id,
        attachments=[NotificationAttachment(file=b"same bytes", filename="a.txt")],
    )
    assert second.attachments[0].file_id == notification.attachments[0].file_id


def test_attachment_file_handle_stream_url_and_delete(db_session, user_id):
    backend = _backend(db_session)
    notification = _persist(
        backend,
        user_id,
        attachments=[NotificationAttachment(file=b"handle bytes", filename="h.txt")],
    )
    stored = next(iter(backend.get_attachments(notification.id)))

    # The StoredAttachment.file handle exposes the AttachmentFile API.
    assert stored.get_file_stream().read() == b"handle bytes"
    assert stored.get_file_url().startswith("file://")

    stored.delete()  # removes the underlying bytes on disk
    with pytest.raises(FileNotFoundError):
        stored.get_file_data()


def test_attachment_reference_and_missing_raises(db_session, user_id):
    backend = _backend(db_session)
    original = _persist(
        backend,
        user_id,
        attachments=[NotificationAttachment(file=b"ref bytes", filename="r.txt")],
    )
    file_id = original.attachments[0].file_id

    referencing = _persist(
        backend,
        user_id,
        attachments=[NotificationAttachmentReference(file_id=file_id)],
    )
    assert referencing.attachments[0].file_id == file_id

    with pytest.raises(AttachmentFileNotFoundError):
        _persist(
            backend,
            user_id,
            attachments=[NotificationAttachmentReference(file_id="999999")],
        )


def test_orphaned_files_and_delete_notification_attachment(db_session, user_id):
    backend = _backend(db_session)
    notification = _persist(
        backend,
        user_id,
        attachments=[NotificationAttachment(file=b"orphan test", filename="o.txt")],
    )
    join_id = notification.attachments[0].id

    assert list(backend.get_orphaned_attachment_files()) == []

    backend.delete_notification_attachment(join_id)
    assert list(backend.get_attachments(notification.id)) == []
    # With no join row left, the file record is now orphaned.
    orphaned = list(backend.get_orphaned_attachment_files())
    assert len(orphaned) == 1


def test_get_notification_not_found_still_raises(db_session, user_id):
    from vintasend.exceptions import NotificationNotFoundError

    backend = _backend(db_session)
    with pytest.raises(NotificationNotFoundError):
        backend.get_notification(uuid.uuid4())


def test_get_all_and_paginated_pending_notifications(db_session, user_id):
    backend = _backend(db_session)
    first = _persist(backend, user_id)
    second = _persist(backend, user_id)
    already_sent = _persist(backend, user_id)
    backend.mark_pending_as_sent(already_sent.id)

    all_pending = list(backend.get_all_pending_notifications())
    assert {n.id for n in all_pending} == {first.id, second.id}

    page_1 = list(backend.get_pending_notifications(page=1, page_size=1))
    page_2 = list(backend.get_pending_notifications(page=2, page_size=1))
    assert len(page_1) == 1 and len(page_2) == 1
    assert page_1[0].id != page_2[0].id


def test_future_notifications_all_paginated_and_by_user(db_session, user_id):
    backend = _backend(db_session)
    with db_session.begin() as session:
        other = User(email="other@example.com")
        session.add(other)
        session.flush()
        other_id = other.id

    future = datetime.datetime.now(tz=datetime.timezone.utc) + timedelta(days=1)
    mine_1 = _persist(backend, user_id, send_after=future)
    mine_2 = _persist(backend, user_id, send_after=future)
    theirs = _persist(backend, other_id, send_after=future)
    # A pending-now notification must not show up in the "future" listings.
    _persist(backend, user_id, send_after=None)

    all_future = list(backend.get_all_future_notifications())
    assert {mine_1.id, mine_2.id, theirs.id} <= {n.id for n in all_future}

    paged = list(backend.get_future_notifications(page=1, page_size=2))
    assert len(paged) == 2

    mine_all = list(backend.get_all_future_notifications_from_user(user_id))
    assert {n.id for n in mine_all} == {mine_1.id, mine_2.id}

    mine_paged = list(backend.get_future_notifications_from_user(user_id, page=1, page_size=1))
    assert len(mine_paged) == 1


def test_get_user_email_from_notification(db_session, user_id):
    backend = _backend(db_session)
    notification = _persist(backend, user_id)
    assert backend.get_user_email_from_notification(notification.id) == "foo@example.com"


def test_store_context_used(db_session, user_id):
    backend = _backend(db_session)
    notification = _persist(backend, user_id)
    backend.store_context_used(notification.id, {"key": "value"}, "myapp.adapters.Email")

    refetched = backend.get_notification(notification.id)
    assert refetched.context_used == {"key": "value"}
    assert refetched.adapter_used == "myapp.adapters.Email"


def test_attachment_file_record_crud(db_session, user_id):
    backend = _backend(db_session)
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    record = AttachmentFileRecord(
        id="ignored",  # store_ assigns its own id
        filename="doc.pdf",
        content_type="application/pdf",
        size=1234,
        checksum="deadbeef",
        created_at=now,
        updated_at=now,
        storage_identifiers={"id": "abc"},
    )

    stored = backend.store_attachment_file_record(record)
    assert stored.id != "ignored"
    assert stored.filename == "doc.pdf"

    fetched = backend.get_attachment_file_record(stored.id)
    assert fetched is not None
    assert fetched.checksum == "deadbeef"

    found = backend.find_attachment_file_by_checksum("deadbeef", 1234)
    assert found is not None and found.id == stored.id
    # A size mismatch degrades to a miss so a digest collision never serves the wrong file.
    assert backend.find_attachment_file_by_checksum("deadbeef", 9999) is None

    backend.delete_attachment_file(stored.id)
    assert backend.get_attachment_file_record(stored.id) is None
    # A non-numeric id is a miss, not a crash.
    assert backend.get_attachment_file_record("not-an-int") is None


def test_persist_notification_update_resend_and_already_sent(db_session, user_id):
    backend = _backend(db_session)
    original = _persist(
        backend,
        user_id,
        attachments=[NotificationAttachment(file=b"resend bytes", filename="r.txt")],
    )

    # The resend path links already-stored files by writing new join rows.
    target = _persist(backend, user_id)
    updated = backend.persist_notification_update(
        target.id, {"attachments": list(original.attachments)}
    )
    assert len(updated.attachments) == 1
    assert updated.attachments[0].file_id == original.attachments[0].file_id

    # An already-sent notification cannot be updated.
    backend.mark_pending_as_sent(target.id)
    with pytest.raises(NotificationUpdateError):
        backend.persist_notification_update(target.id, {"subject_template": "nope"})
    # Even an empty-scalar update raises when the row is no longer pending.
    with pytest.raises(NotificationUpdateError):
        backend.persist_notification_update(target.id, {})


def test_filter_in_app_paginated_and_unread_paginated(db_session, user_id):
    backend = _backend(db_session)
    read_one = _persist(backend, user_id, notification_type=NotificationTypes.IN_APP.value)
    unread_one = _persist(backend, user_id, notification_type=NotificationTypes.IN_APP.value)
    unread_two = _persist(backend, user_id, notification_type=NotificationTypes.IN_APP.value)
    for n in (read_one, unread_one, unread_two):
        backend.mark_pending_as_sent(n.id)
    backend.mark_sent_as_read(read_one.id)

    first_page = list(backend.filter_in_app_notifications(user_id, page=1, page_size=2))
    second_page = list(backend.filter_in_app_notifications(user_id, page=2, page_size=2))
    assert len(first_page) == 2 and len(second_page) == 1

    unread_page = list(backend.filter_in_app_unread_notifications(user_id, page=1, page_size=1))
    assert len(unread_page) == 1
    assert unread_page[0].id in {unread_one.id, unread_two.id}
