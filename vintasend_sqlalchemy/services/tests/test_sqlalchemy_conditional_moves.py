"""Version tokens, conditional requeue / repin, ``filter_notification_versions`` and the presence
filters on the sync SQLAlchemy backend, and the service actions built on them."""

import contextlib
import datetime
import uuid

import pytest
from sqlalchemy import delete, event, select, text, update
from sqlalchemy.orm import Session, sessionmaker
from vintasend.constants import (
    REPINNABLE_NOTIFICATION_STATUSES,
    REQUEUEABLE_NOTIFICATION_STATUSES,
    NotificationStatus,
    NotificationTypes,
)
from vintasend.exceptions import (
    NotificationActionError,
    NotificationNotFoundError,
    NotificationRepinError,
    NotificationRequeueError,
    NotificationVersionChangedError,
)
from vintasend.services.dataclasses import NotificationAttachment, NotificationVersion
from vintasend.services.notification_backends.filters import matches_filter
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
ALL_ACTIONS = {
    "repin.expectedVersion": True,
    "requeue.expectedVersion": True,
    "requeue.templateVersion": True,
    "versions.filter": True,
    "writes.notificationVersion": True,
}
PRESENCE_CAPABILITIES = {
    "fields.hasSendAfter": True,
    "negation.hasSendAfter": True,
    "fields.hasRequestedTemplateVersion": True,
    "negation.hasRequestedTemplateVersion": True,
}
LATER = datetime.datetime(2030, 1, 1, tzinfo=datetime.timezone.utc)


@register_context("sqlalchemy_conditional_moves_test_context")
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


def _service(db_session: sessionmaker[Session]) -> NotificationService:
    return NotificationService(
        notification_adapters=[(FAKE_EMAIL_ADAPTER, FAKE_RENDERER)],
        notification_backend=_backend(db_session),
        # The adapters build their own backend from these.
        notification_backend_kwargs={
            "session": db_session,
            "notification_model_cls": NotificationModel,
        },
    )


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
        context_name="sqlalchemy_conditional_moves_test_context",
        context_kwargs={},
        send_after=None,
        subject_template="test",
        preheader_template="test",
    )
    defaults.update(overrides)
    notification = backend.persist_notification(**defaults)
    if status != NotificationStatus.PENDING_SEND:
        _set(backend.session_manager, notification.id, status=status.value)
    return notification


def _token(db_session, notification_id) -> str:
    with db_session.begin() as session:
        return str(
            session.execute(
                select(NotificationModel.notification_version).where(
                    NotificationModel.id == notification_id
                )
            ).scalar_one()
        )


def _snapshot(db_session, notification_id) -> tuple:
    """Everything a refused move could have touched, the version token included."""
    with db_session.begin() as session:
        return tuple(
            session.execute(
                select(
                    NotificationModel.status,
                    NotificationModel.requested_template_version,
                    NotificationModel.send_after,
                    NotificationModel.sent_at,
                    NotificationModel.updated,
                    NotificationModel.notification_version,
                ).where(NotificationModel.id == notification_id)
            ).one()
        )


@contextlib.contextmanager
def _statements(engine):
    seen: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", record)


# --------------------------------------------------------------------------- the version token


def test_a_new_notification_gets_a_uuid7_token(db_session, user_id):
    notification = _persist(_backend(db_session), user_id, status=NotificationStatus.PENDING_SEND)

    assert uuid.UUID(_token(db_session, notification.id)).version == 7


def _update(backend, notification, user_id):
    backend.persist_notification_update(notification.id, {"title": "changed"})


def _link_attachments(backend, notification, user_id):
    other = _persist(
        backend,
        user_id,
        status=NotificationStatus.PENDING_SEND,
        attachments=[NotificationAttachment(file=b"linked", filename="linked.txt")],
    )
    backend.persist_notification_update(notification.id, {"attachments": other.attachments})


def _delete_attachment(backend, notification, user_id):
    backend.delete_notification_attachment(notification.attachments[0].id)


WRITE_PATHS = [
    ("persist_notification_update", NotificationStatus.PENDING_SEND, _update),
    ("persist_notification_update_attachments", NotificationStatus.PENDING_SEND, _link_attachments),
    ("delete_notification_attachment", NotificationStatus.PENDING_SEND, _delete_attachment),
    (
        "mark_pending_as_sent",
        NotificationStatus.PENDING_SEND,
        lambda backend, n, _: backend.mark_pending_as_sent(n.id),
    ),
    (
        "mark_pending_as_failed",
        NotificationStatus.PENDING_SEND,
        lambda backend, n, _: backend.mark_pending_as_failed(n.id),
    ),
    (
        "mark_sent_as_read",
        NotificationStatus.SENT,
        lambda backend, n, _: backend.mark_sent_as_read(n.id),
    ),
    (
        "mark_sent_as_read_bulk",
        NotificationStatus.SENT,
        lambda backend, n, uid: backend.mark_sent_as_read_bulk([n.id], uid),
    ),
    (
        "cancel_notification",
        NotificationStatus.PENDING_SEND,
        lambda backend, n, _: backend.cancel_notification(n.id),
    ),
    (
        "requeue_notification",
        NotificationStatus.FAILED,
        lambda backend, n, _: backend.requeue_notification(n.id),
    ),
    (
        "repin_notification",
        NotificationStatus.FAILED,
        lambda backend, n, _: backend.repin_notification(n.id, 7),
    ),
    (
        "store_context_used",
        NotificationStatus.PENDING_SEND,
        lambda backend, n, _: backend.store_context_used(n.id, {"a": 1}, "some.adapter"),
    ),
    (
        "store_git_commit_sha",
        NotificationStatus.PENDING_SEND,
        lambda backend, n, _: backend.store_git_commit_sha(n.id, "a" * 40),
    ),
    (
        "store_template_version",
        NotificationStatus.PENDING_SEND,
        lambda backend, n, _: backend.store_template_version(n.id, 3),
    ),
]


@pytest.mark.parametrize(
    ("status", "write"),
    [(status, write) for _, status, write in WRITE_PATHS],
    ids=[name for name, _, _ in WRITE_PATHS],
)
def test_every_write_moves_the_token(db_session, user_id, status, write):
    backend = _backend(db_session)
    notification = _persist(
        backend,
        user_id,
        status=status,
        attachments=[NotificationAttachment(file=b"report", filename="report.txt")],
    )
    before = _token(db_session, notification.id)

    write(backend, notification, user_id)

    after = _token(db_session, notification.id)
    assert after != before
    assert uuid.UUID(after).version == 7


def test_a_bulk_read_moves_the_token_of_every_row_it_reads(db_session, user_id):
    backend = _backend(db_session)
    first = _persist(backend, user_id, status=NotificationStatus.SENT)
    second = _persist(backend, user_id, status=NotificationStatus.SENT)
    untouched = _persist(backend, user_id, status=NotificationStatus.PENDING_SEND)
    before = {n.id: _token(db_session, n.id) for n in (first, second, untouched)}

    backend.mark_sent_as_read_bulk([first.id, second.id, untouched.id], user_id)

    assert _token(db_session, first.id) != before[first.id]
    assert _token(db_session, second.id) != before[second.id]
    assert _token(db_session, untouched.id) == before[untouched.id]


def test_what_moves_the_token_outside_the_backend(db_session, user_id):
    notification = _persist(_backend(db_session), user_id, status=NotificationStatus.PENDING_SEND)
    initial = _token(db_session, notification.id)

    # A Core UPDATE through the model: the column's onupdate supplies a fresh token.
    _set(db_session, notification.id, title="core")
    after_core = _token(db_session, notification.id)
    assert after_core != initial

    # An ORM flush: the same onupdate.
    with db_session.begin() as session:
        instance = session.get(NotificationModel, notification.id)
        assert instance is not None
        instance.title = "orm"
    after_orm = _token(db_session, notification.id)
    assert after_orm != after_core

    # Raw SQL bypasses the model, so it leaves the token alone: such a write must set it itself.
    with db_session.begin() as session:
        session.execute(
            text("UPDATE notifications SET title = 'raw' WHERE id = :id"), {"id": notification.id}
        )
    assert _token(db_session, notification.id) == after_orm


# ----------------------------------------------------------------------------------- requeue


def test_requeue_pins_the_template_version_in_the_same_write(db_session, user_id):
    backend = _backend(db_session)
    notification = _persist(backend, user_id, requested_template_version=1)
    token = _token(db_session, notification.id)

    with _statements(db_session.kw["bind"]) as statements:
        requeued = backend.requeue_notification(
            notification.id, LATER, template_version=4, expected_version=token
        )

    assert requeued.status == NotificationStatus.PENDING_SEND.value
    assert requeued.requested_template_version == 4
    assert requeued.send_after is not None
    assert _token(db_session, notification.id) != token
    updates = [s for s in statements if s.lstrip().upper().startswith("UPDATE")]
    assert len(updates) == 1


def test_requeue_refuses_a_stale_token_and_writes_nothing(db_session, user_id):
    backend = _backend(db_session)
    notification = _persist(backend, user_id)
    stale = _token(db_session, notification.id)
    backend.store_context_used(notification.id, {"a": 1}, "some.adapter")
    before = _snapshot(db_session, notification.id)

    with pytest.raises(NotificationVersionChangedError) as raised:
        backend.requeue_notification(
            notification.id, LATER, template_version=4, expected_version=stale
        )

    assert str(raised.value) == (
        f"Cannot requeue notification {notification.id}: it changed since that version was read"
    )
    assert _snapshot(db_session, notification.id) == before


@pytest.mark.parametrize("token", ["not-a-uuid", "", "1234"])
def test_requeue_refuses_a_malformed_token_as_changed(db_session, user_id, token):
    backend = _backend(db_session)
    notification = _persist(backend, user_id)
    before = _snapshot(db_session, notification.id)

    with _statements(db_session.kw["bind"]) as statements:
        with pytest.raises(NotificationVersionChangedError):
            backend.requeue_notification(notification.id, expected_version=token)

    # The UPDATE matches nothing and one read finds the row: no other statement runs.
    assert len(statements) == 2
    assert _snapshot(db_session, notification.id) == before


@pytest.mark.parametrize(
    "status",
    [s for s in NotificationStatus if s.value not in REQUEUEABLE_NOTIFICATION_STATUSES],
)
def test_requeue_refuses_the_wrong_status_at_the_expected_version(db_session, user_id, status):
    backend = _backend(db_session)
    notification = _persist(backend, user_id, status=status)
    token = _token(db_session, notification.id)
    before = _snapshot(db_session, notification.id)

    with pytest.raises(NotificationRequeueError):
        backend.requeue_notification(
            notification.id, LATER, template_version=4, expected_version=token
        )

    assert _snapshot(db_session, notification.id) == before


def test_requeue_reports_a_changed_version_before_the_wrong_status(db_session, user_id):
    backend = _backend(db_session)
    notification = _persist(backend, user_id, status=NotificationStatus.SENT)
    before = _snapshot(db_session, notification.id)

    with pytest.raises(NotificationVersionChangedError):
        backend.requeue_notification(notification.id, expected_version=str(uuid.uuid4()))

    assert _snapshot(db_session, notification.id) == before


@pytest.mark.parametrize("action", ["requeue", "repin"])
def test_an_unknown_id_with_a_malformed_token_is_not_found(db_session, action):
    backend = _backend(db_session)
    with pytest.raises(NotificationNotFoundError):
        if action == "requeue":
            backend.requeue_notification(987654, expected_version="not-a-uuid")
        else:
            backend.repin_notification(987654, 5, expected_version="not-a-uuid")


def test_requeue_of_an_unknown_id_with_a_token_is_not_found(db_session, user_id):
    with pytest.raises(NotificationNotFoundError):
        _backend(db_session).requeue_notification(
            987654, template_version=1, expected_version=str(uuid.uuid4())
        )


# ------------------------------------------------------------------------------------- repin


@pytest.mark.parametrize("status", REPINNABLE_NOTIFICATION_STATUSES)
def test_repin_changes_only_the_pin_and_the_token(db_session, user_id, status):
    backend = _backend(db_session)
    notification = _persist(backend, user_id, status=NotificationStatus(status), send_after=LATER)
    token = _token(db_session, notification.id)
    before = _snapshot(db_session, notification.id)

    with _statements(db_session.kw["bind"]) as statements:
        repinned = backend.repin_notification(notification.id, 5, expected_version=token)

    assert repinned.requested_template_version == 5
    after = _snapshot(db_session, notification.id)
    assert after[0] == before[0]  # status
    assert after[1] == 5
    assert after[2:4] == before[2:4]  # send_after, sent_at
    assert after[5] != before[5]
    assert len([s for s in statements if s.lstrip().upper().startswith("UPDATE")]) == 1


def test_repin_moves_an_existing_pin(db_session, user_id):
    backend = _backend(db_session)
    notification = _persist(backend, user_id, requested_template_version=2)

    assert backend.repin_notification(notification.id, 3).requested_template_version == 3


@pytest.mark.parametrize(
    "status",
    [s for s in NotificationStatus if s.value not in REPINNABLE_NOTIFICATION_STATUSES],
)
def test_repin_refuses_a_delivered_notification(db_session, user_id, status):
    backend = _backend(db_session)
    notification = _persist(backend, user_id, status=status)
    token = _token(db_session, notification.id)
    before = _snapshot(db_session, notification.id)

    with pytest.raises(NotificationRepinError) as raised:
        backend.repin_notification(notification.id, 5, expected_version=token)

    assert str(raised.value) == (
        f"Cannot repin notification {notification.id}: it has already been delivered"
    )
    assert _snapshot(db_session, notification.id) == before


@pytest.mark.parametrize("with_token", [False, True])
def test_repin_to_the_current_pin_writes_nothing(db_session, user_id, with_token):
    backend = _backend(db_session)
    notification = _persist(backend, user_id, requested_template_version=5)
    token = _token(db_session, notification.id) if with_token else None
    before = _snapshot(db_session, notification.id)

    repinned = backend.repin_notification(notification.id, 5, expected_version=token)

    assert repinned.requested_template_version == 5
    assert repinned.id == notification.id
    assert _snapshot(db_session, notification.id) == before


@pytest.mark.parametrize("pinned", [None, 5])
def test_repin_refuses_a_stale_token_and_writes_nothing(db_session, user_id, pinned):
    backend = _backend(db_session)
    notification = _persist(backend, user_id, requested_template_version=pinned)
    stale = _token(db_session, notification.id)
    backend.store_git_commit_sha(notification.id, "b" * 40)
    before = _snapshot(db_session, notification.id)

    with pytest.raises(NotificationVersionChangedError) as raised:
        backend.repin_notification(notification.id, 5, expected_version=stale)

    assert str(raised.value) == (
        f"Cannot repin notification {notification.id}: it changed since that version was read"
    )
    assert _snapshot(db_session, notification.id) == before


@pytest.mark.parametrize("token", ["not-a-uuid", "", "1234"])
def test_repin_refuses_a_malformed_token_as_changed(db_session, user_id, token):
    backend = _backend(db_session)
    notification = _persist(backend, user_id)
    before = _snapshot(db_session, notification.id)

    with _statements(db_session.kw["bind"]) as statements:
        with pytest.raises(NotificationVersionChangedError):
            backend.repin_notification(notification.id, 5, expected_version=token)

    # The UPDATE matches nothing and one read finds the row: no other statement runs.
    assert len(statements) == 2
    assert _snapshot(db_session, notification.id) == before


@pytest.mark.parametrize("token", [None, "0190a7a5-0000-7000-8000-000000000000"])
def test_repin_of_an_unknown_id_is_not_found(db_session, user_id, token):
    with pytest.raises(NotificationNotFoundError):
        _backend(db_session).repin_notification(987654, 5, expected_version=token)


def test_repin_refuses_a_notification_that_changed_between_the_two_statements(db_session, user_id):
    backend = _backend(db_session)
    notification = _persist(backend, user_id, status=NotificationStatus.SENT)
    engine = db_session.kw["bind"]
    fired = []

    # A writer that lands between the conditional UPDATE (which matches nothing: the row is
    # SENT) and the read that says why, so the read finds a row the UPDATE would have matched.
    def concurrent_write(conn, cursor, statement, parameters, context, executemany):
        if fired or not statement.lstrip().upper().startswith("UPDATE NOTIFICATIONS"):
            return
        fired.append(True)
        conn.execute(
            update(NotificationModel.__table__)
            .where(NotificationModel.__table__.c.id == notification.id)
            .values(status=NotificationStatus.FAILED.value)
        )

    event.listen(engine, "after_cursor_execute", concurrent_write)
    try:
        with pytest.raises(NotificationVersionChangedError):
            backend.repin_notification(notification.id, 5)
    finally:
        event.remove(engine, "after_cursor_execute", concurrent_write)

    assert fired
    # The backend wrote nothing; its transaction, the simulated write included, rolled back.
    stored = backend.get_notification(notification.id)
    assert stored.status == NotificationStatus.SENT.value
    assert stored.requested_template_version is None


# ------------------------------------------------------------------- filter_notification_versions


def test_versions_are_ordered_by_id_and_paged_from_one(db_session, user_id):
    backend = _backend(db_session)
    notifications = [_persist(backend, user_id) for _ in range(5)]
    _persist(backend, user_id, status=NotificationStatus.SENT)
    failed = {"status": NotificationStatus.FAILED}

    first = backend.filter_notification_versions(failed, 1, 2)
    second = backend.filter_notification_versions(failed, 2, 2)
    third = backend.filter_notification_versions(failed, 3, 2)

    assert first + second + third == [
        NotificationVersion(id=n.id, version=_token(db_session, n.id)) for n in notifications
    ]
    assert [len(first), len(second), len(third)] == [2, 2, 1]
    assert backend.filter_notification_versions(failed, 4, 2) == []


def test_versions_select_only_the_id_and_the_token(db_session, user_id):
    backend = _backend(db_session)
    _persist(
        backend,
        user_id,
        attachments=[NotificationAttachment(file=b"secret", filename="secret.txt")],
    )

    with _statements(db_session.kw["bind"]) as statements:
        backend.filter_notification_versions(
            {"and": [{"status": NotificationStatus.FAILED}, {"user_id": user_id}]}, 1, 10
        )

    assert len(statements) == 1
    select_list = statements[0].split("FROM")[0]
    assert " ".join(select_list.split()) == (
        "SELECT notifications.id, notifications.notification_version"
    )
    assert "notification_attachments" not in statements[0]


def test_a_listed_token_moves_the_notification_once(db_session, user_id):
    backend = _backend(db_session)
    notification = _persist(backend, user_id)
    [listed] = backend.filter_notification_versions({"status": NotificationStatus.FAILED}, 1, 10)

    backend.repin_notification(notification.id, 2, expected_version=listed.version)

    with pytest.raises(NotificationVersionChangedError):
        backend.requeue_notification(notification.id, expected_version=listed.version)


# ---------------------------------------------------------------------------- presence filters


def _presence_fixture(backend, user_id):
    return [
        _persist(backend, user_id, send_after=None, requested_template_version=None),
        _persist(backend, user_id, send_after=LATER, requested_template_version=None),
        _persist(backend, user_id, send_after=None, requested_template_version=0),
        _persist(backend, user_id, send_after=LATER, requested_template_version=3),
    ]


PRESENCE_FILTERS = [
    {"has_send_after": True},
    {"has_send_after": False},
    {"not": {"has_send_after": True}},
    {"not": {"has_send_after": False}},
    {"has_requested_template_version": True},
    {"has_requested_template_version": False},
    {"not": {"has_requested_template_version": True}},
    {"not": {"has_requested_template_version": False}},
    {"has_send_after": True, "has_requested_template_version": True},
    {"not": {"has_send_after": True, "has_requested_template_version": True}},
    {"has_send_after": "yes"},
    {"not": {"has_send_after": 1}},
    {"has_requested_template_version": None},
    {"and": [{"has_requested_template_version": True}, {"not": {"requested_template_version": 3}}]},
]


@pytest.mark.parametrize("notification_filter", PRESENCE_FILTERS)
def test_presence_filters_match_the_reference_evaluator(db_session, user_id, notification_filter):
    backend = _backend(db_session)
    notifications = _presence_fixture(backend, user_id)
    stored = [backend.get_notification(n.id) for n in notifications]
    expected = sorted(n.id for n in stored if matches_filter(n, notification_filter))

    listed = backend.filter_notifications(notification_filter, 1, 100)
    versions = backend.filter_notification_versions(notification_filter, 1, 100)

    assert sorted(n.id for n in listed) == expected
    assert [v.id for v in versions] == expected
    assert backend.count_notifications(notification_filter) == len(expected)


def test_presence_filters_use_real_nulls(db_session, user_id):
    backend = _backend(db_session)
    unscheduled, scheduled, pinned_to_zero, _ = _presence_fixture(backend, user_id)

    with db_session.begin() as session:
        null_send_after = session.execute(
            text("SELECT id FROM notifications WHERE send_after IS NULL ORDER BY id")
        ).scalars()
        assert list(null_send_after) == [unscheduled.id, pinned_to_zero.id]

    assert sorted(n.id for n in backend.filter_notifications({"has_send_after": False}, 1, 10)) == [
        unscheduled.id,
        pinned_to_zero.id,
    ]
    # Version 0 is a pin, not "no pin".
    assert pinned_to_zero.id in [
        v.id
        for v in backend.filter_notification_versions(
            {"has_requested_template_version": True}, 1, 10
        )
    ]
    assert scheduled.id in [
        v.id
        for v in backend.filter_notification_versions(
            {"has_requested_template_version": False}, 1, 10
        )
    ]


def test_the_stranded_filter_runs(db_session, user_id):
    backend = _backend(db_session)
    v = 4
    stranded_pending = _persist(
        backend,
        user_id,
        status=NotificationStatus.PENDING_SEND,
        body_template="welcome",
        send_after=LATER,
        requested_template_version=3,
    )
    failed = _persist(backend, user_id, body_template="welcome")
    # None of these match.
    _persist(backend, user_id, status=NotificationStatus.PENDING_SEND, body_template="welcome")
    _persist(
        backend,
        user_id,
        status=NotificationStatus.PENDING_SEND,
        body_template="welcome",
        send_after=LATER,
    )
    _persist(
        backend,
        user_id,
        status=NotificationStatus.PENDING_SEND,
        body_template="welcome",
        send_after=LATER,
        requested_template_version=v,
    )
    _persist(
        backend,
        user_id,
        status=NotificationStatus.PENDING_SEND,
        body_template="welcome",
        requested_template_version=3,
    )
    _persist(backend, user_id, body_template="other")
    _persist(backend, user_id, status=NotificationStatus.SENT, body_template="welcome")
    stranded = {
        "and": [
            {"body_template": "welcome"},
            {
                "or": [
                    {
                        "and": [
                            {"status": NotificationStatus.PENDING_SEND},
                            {"has_send_after": True},
                            {"has_requested_template_version": True},
                            {"not": {"requested_template_version": v}},
                        ]
                    },
                    {"status": NotificationStatus.FAILED},
                ]
            },
        ]
    }

    versions = backend.filter_notification_versions(stranded, 1, 100)

    assert [version.id for version in versions] == [stranded_pending.id, failed.id]
    assert backend.count_notifications(stranded) == 2
    assert sorted(n.id for n in backend.filter_notifications(stranded, 1, 100)) == [
        stranded_pending.id,
        failed.id,
    ]


# ------------------------------------------------------------------------------- capabilities


def test_reports_every_action_and_the_presence_filters(db_session):
    backend = _backend(db_session)

    assert backend.get_action_capabilities() == ALL_ACTIONS
    assert backend.get_filter_capabilities() == PRESENCE_CAPABILITIES


def test_the_service_sees_every_capability(db_session):
    service = _service(db_session)

    assert service.get_backend_supported_action_capabilities() == ALL_ACTIONS
    filter_capabilities = service.get_backend_supported_filter_capabilities()
    for key in PRESENCE_CAPABILITIES:
        assert filter_capabilities[key] is True


# ------------------------------------------------------------------------------------ service


def test_the_service_requeues_on_a_new_pin_only_while_unchanged(db_session, user_id):
    service = _service(db_session)
    backend = service.notification_backend
    notification = _persist(backend, user_id, requested_template_version=1)
    [listed] = service.filter_notification_versions({"status": NotificationStatus.FAILED}, 1, 10)

    requeued = service.requeue_notification(
        notification.id, template_version=2, expected_version=listed.version, send=False
    )

    assert requeued.status == NotificationStatus.PENDING_SEND.value
    assert requeued.requested_template_version == 2
    _set(db_session, notification.id, status=NotificationStatus.FAILED.value)
    before = _snapshot(db_session, notification.id)
    with pytest.raises(NotificationActionError) as raised:
        service.requeue_notification(
            notification.id, template_version=3, expected_version=listed.version, send=False
        )
    assert raised.value.reason == "CHANGED"
    assert _snapshot(db_session, notification.id) == before


def test_the_service_refuses_a_malformed_token_as_changed(db_session, user_id):
    service = _service(db_session)
    notification = _persist(service.notification_backend, user_id)
    before = _snapshot(db_session, notification.id)

    for move in (
        lambda: service.requeue_notification(
            notification.id, expected_version="not-a-uuid", send=False
        ),
        lambda: service.repin_notification(notification.id, 2, expected_version="not-a-uuid"),
    ):
        with pytest.raises(NotificationActionError) as raised:
            move()
        assert raised.value.reason == "CHANGED"

    assert _snapshot(db_session, notification.id) == before


def test_the_service_repins_only_while_unchanged(db_session, user_id):
    service = _service(db_session)
    notification = _persist(service.notification_backend, user_id)
    token = _token(db_session, notification.id)

    repinned = service.repin_notification(notification.id, 2, expected_version=token)

    assert repinned.requested_template_version == 2
    before = _snapshot(db_session, notification.id)
    with pytest.raises(NotificationActionError) as raised:
        service.repin_notification(notification.id, 3, expected_version=token)
    assert raised.value.reason == "CHANGED"
    assert _snapshot(db_session, notification.id) == before


def test_the_service_refuses_a_delivered_or_unknown_notification(db_session, user_id):
    service = _service(db_session)
    sent = _persist(service.notification_backend, user_id, status=NotificationStatus.SENT)
    before = _snapshot(db_session, sent.id)

    with pytest.raises(NotificationActionError) as repin_raised:
        service.repin_notification(sent.id, 2, expected_version=_token(db_session, sent.id))
    with pytest.raises(NotificationActionError) as requeue_raised:
        service.requeue_notification(
            sent.id, expected_version=_token(db_session, sent.id), send=False
        )
    with pytest.raises(NotificationActionError) as missing_raised:
        service.requeue_notification(987654, expected_version=str(uuid.uuid4()), send=False)

    assert repin_raised.value.reason == "INVALID_STATUS"
    assert requeue_raised.value.reason == "INVALID_STATUS"
    assert missing_raised.value.reason == "NOT_FOUND"
    assert _snapshot(db_session, sent.id) == before
