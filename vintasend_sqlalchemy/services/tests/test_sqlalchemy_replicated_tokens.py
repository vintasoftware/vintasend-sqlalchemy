"""The primary's version token on a replica, on the sync SQLAlchemy backend.

``apply_replication_snapshot_if_newer``, the ``notification_version`` keyword every write takes
(``writes.notificationVersion``), and a ``NotificationService`` replicating inline from one
database to a second one through the same model classes."""

import dataclasses
import datetime
import traceback
import uuid
from collections.abc import Callable
from typing import Any
from unittest import mock

import pytest
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.orm import Session, sessionmaker
from vintasend.constants import NotificationStatus, NotificationTypes
from vintasend.exceptions import (
    NotificationActionError,
    NotificationError,
    NotificationUpdateError,
    NotificationVersionChangedError,
)
from vintasend.services.dataclasses import ApplyResult, NotificationAttachment
from vintasend.services.git_commit_sha_providers.stubs.fake_git_commit_sha_provider import (
    FakeGitCommitShaProvider,
)
from vintasend.services.notification_service import NotificationService, register_context
from vintasend.utils.uuid_utils import uuid7

from example_app.models import Notification as NotificationModel
from example_app.models import User
from vintasend_sqlalchemy.model_factory import AttachmentFileRecord as AttachmentFileRecordModel
from vintasend_sqlalchemy.model_factory import NotificationAttachment as NotificationAttachmentModel
from vintasend_sqlalchemy.services.notification_backends.sqlalchemy_notification_backend import (
    SnapshotApplyDatabaseError,
    SQLAlchemyNotificationBackend,
    _user_notification_from_orm,
)


FAKE_EMAIL_ADAPTER = "vintasend.services.notification_adapters.stubs.fake_adapter.FakeEmailAdapter"
VERSIONED_RENDERER = (
    "vintasend.services.notification_template_renderers.stubs."
    "fake_templated_email_renderer.FakeVersionedTemplateRenderer"
)
CONTEXT = "sqlalchemy_replicated_tokens_test_context"
LATER = datetime.datetime(2030, 1, 1, tzinfo=datetime.timezone.utc)
EARLIER = datetime.datetime(2024, 5, 6, 7, 8, 9, 123456)
PENDING = NotificationStatus.PENDING_SEND.value
SENT = NotificationStatus.SENT.value
READ = NotificationStatus.READ.value
FAILED = NotificationStatus.FAILED.value
DECLINED = ApplyResult(applied=False, reason="declined for test")


@register_context(CONTEXT)
def _context():
    return {"greeting": "hi"}


def _clear(session_maker: sessionmaker[Session]) -> None:
    with session_maker.begin() as session:
        session.execute(delete(NotificationAttachmentModel))
        session.execute(delete(AttachmentFileRecordModel))
        session.execute(delete(NotificationModel))
        session.execute(delete(User))


@pytest.fixture
def user_id(db_session, replica_db_session):
    with db_session.begin() as session:
        user = User(email="foo@example.com")
        session.add(user)
        session.flush()
        uid = user.id
    # The same user in the replica database, so its rows point at a user that exists there too.
    with replica_db_session.begin() as session:
        session.add(User(id=uid, email="foo@example.com"))
    yield uid
    _clear(db_session)
    _clear(replica_db_session)


@pytest.fixture
def primary(db_session) -> SQLAlchemyNotificationBackend:
    return SQLAlchemyNotificationBackend(
        db_session, NotificationModel, backend_identifier="primary"
    )


@pytest.fixture
def replica(replica_db_session) -> SQLAlchemyNotificationBackend:
    return SQLAlchemyNotificationBackend(
        replica_db_session, NotificationModel, backend_identifier="replica"
    )


@pytest.fixture
def service(primary, replica) -> NotificationService:
    return NotificationService(
        notification_adapters=[(FAKE_EMAIL_ADAPTER, VERSIONED_RENDERER)],
        notification_backend=primary,
        # The adapters build their own backend from these.
        notification_backend_kwargs={
            "session": primary.session_manager,
            "notification_model_cls": NotificationModel,
        },
        additional_backends=[replica],
        git_commit_sha_provider=FakeGitCommitShaProvider(),
    )


def _create(service, user_id, **overrides):
    kwargs = dict(
        user_id=user_id,
        notification_type=NotificationTypes.EMAIL.value,
        title="test",
        body_template="test",
        context_name=CONTEXT,
        context_kwargs={},
        send_after=LATER,
        subject_template="test",
        preheader_template="test",
    )
    kwargs.update(overrides)
    return service.create_notification(**kwargs)


def _persist(backend, user_id, **overrides):
    kwargs = dict(
        user_id=user_id,
        notification_type=NotificationTypes.EMAIL.value,
        title="test",
        body_template="test",
        context_name=CONTEXT,
        context_kwargs={"order": 1},
        send_after=LATER,
        subject_template="subject",
        preheader_template="preheader",
        adapter_extra_parameters={"x": 1},
        tenant="acme",
        requested_template_version=2,
    )
    kwargs.update(overrides)
    return backend.persist_notification(**kwargs)


def _persist_one_off(backend, **overrides):
    kwargs = dict(
        email_or_phone="someone@example.com",
        first_name="Some",
        last_name="One",
        notification_type=NotificationTypes.EMAIL.value,
        title="test",
        body_template="test",
        context_name=CONTEXT,
        context_kwargs={},
        send_after=LATER,
        subject_template="subject",
        preheader_template="preheader",
    )
    kwargs.update(overrides)
    return backend.persist_one_off_notification(**kwargs)


def _set(session_maker, notification_id, **values):
    with session_maker.begin() as session:
        session.execute(
            update(NotificationModel).where(NotificationModel.id == notification_id).values(values)
        )


def _token(session_maker, notification_id) -> str:
    with session_maker.begin() as session:
        return str(
            session.execute(
                select(NotificationModel.notification_version).where(
                    NotificationModel.id == notification_id
                )
            ).scalar_one()
        )


def _state(session_maker, notification_id) -> tuple | None:
    """Everything a refused write could have touched, the token included."""
    with session_maker.begin() as session:
        row = session.execute(
            select(
                NotificationModel.title,
                NotificationModel.status,
                NotificationModel.requested_template_version,
                NotificationModel.read_at,
                NotificationModel.updated,
                NotificationModel.notification_version,
            ).where(NotificationModel.id == notification_id)
        ).one_or_none()
        return None if row is None else tuple(row)


def _count(session_maker, model) -> int:
    with session_maker.begin() as session:
        return session.execute(select(func.count()).select_from(model)).scalar_one()


def _listed(service, backend_identifier=None) -> dict[str, str]:
    return {
        str(entry.id): entry.version
        for entry in service.filter_notification_versions({}, 1, 1000, backend_identifier)
    }


def _attachment_shapes(notification) -> list[tuple]:
    return sorted(
        (
            a.filename,
            a.content_type,
            a.size,
            a.checksum,
            a.description,
            a.is_inline,
            tuple(sorted(a.storage_identifiers.items())),
        )
        for a in notification.attachments
    )


def _assert_on_primary_token(service, primary, replica, notification_id) -> str:
    token = _listed(service)[str(notification_id)]
    assert _listed(service, "replica")[str(notification_id)] == token
    assert primary.get_notification(notification_id).notification_version == token
    assert replica.get_notification(notification_id).notification_version == token
    return token


def _assert_replica_matches(service, primary, replica, notification_id) -> str:
    """The replica lists the primary's token and holds every field the primary does."""
    token = _assert_on_primary_token(service, primary, replica, notification_id)
    primary_row = primary.get_notification(notification_id)
    replica_row = replica.get_notification(notification_id)
    assert type(replica_row) is type(primary_row)
    for field in dataclasses.fields(primary_row):
        if field.name != "attachments":
            assert getattr(replica_row, field.name) == getattr(primary_row, field.name), field.name
    assert _attachment_shapes(replica_row) == _attachment_shapes(primary_row)
    return token


# --------------------------------------------------------------- the service, snapshots applied


def _send(service, primary, notification_id, user_id):
    service.send(primary.get_notification(notification_id))


def _failed_send(service, primary, notification_id, user_id):
    adapter = next(iter(service.notification_adapters))
    with mock.patch.object(adapter, "send", side_effect=RuntimeError("smtp down")):
        service.send(primary.get_notification(notification_id))


def _send_then_read(service, primary, notification_id, user_id):
    _send(service, primary, notification_id, user_id)
    service.mark_read(notification_id)


def _send_then_bulk_read(service, primary, notification_id, user_id):
    _send(service, primary, notification_id, user_id)
    assert [n.id for n in service.mark_read_bulk([notification_id], user_id=user_id)] == [
        notification_id
    ]


def _cancel_then_requeue(service, primary, notification_id, user_id):
    service.cancel_notification(notification_id)
    service.requeue_notification(notification_id, template_version=4, send=False)


REPLICATED_WRITES = {
    "update": lambda service, _p, id_, _u: service.update_notification(id_, title="Changed"),
    # Stores the git commit SHA, marks it sent, stores the context and the template version used.
    "send": _send,
    "failed send": _failed_send,
    "mark read": _send_then_read,
    "bulk read": _send_then_bulk_read,
    "cancel": lambda service, _p, id_, _u: service.cancel_notification(id_),
    "requeue": _cancel_then_requeue,
    "repin": lambda service, _p, id_, _u: service.repin_notification(id_, 4),
}
EXPECTED_STATUS = {
    "update": PENDING,
    "send": SENT,
    "failed send": FAILED,
    "mark read": READ,
    "bulk read": READ,
    "cancel": NotificationStatus.CANCELLED.value,
    "requeue": PENDING,
    "repin": PENDING,
}


@pytest.mark.parametrize("write", list(REPLICATED_WRITES))
def test_each_replicated_write_leaves_the_replica_on_the_primary_token(
    service, primary, replica, user_id, write
):
    created = _create(service, user_id)
    after_create = _assert_replica_matches(service, primary, replica, created.id)

    REPLICATED_WRITES[write](service, primary, created.id, user_id)

    after_write = _assert_replica_matches(service, primary, replica, created.id)
    assert after_write != after_create
    assert replica.get_notification(created.id).status == EXPECTED_STATUS[write]


def test_a_send_replicates_the_template_version_and_the_git_commit_sha(
    service, primary, replica, user_id
):
    created = _create(service, user_id)

    _send(service, primary, created.id, user_id)

    replicated = replica.get_notification(created.id)
    assert replicated.used_template_version == 7
    assert replicated.git_commit_sha == "a" * 40
    assert replicated.context_used == {"greeting": "hi"}
    _assert_replica_matches(service, primary, replica, created.id)


def test_a_one_off_and_its_attachments_are_replicated(service, primary, replica, user_id):
    one_off = service.create_one_off_notification(
        email_or_phone="someone@example.com",
        first_name="Some",
        last_name="One",
        notification_type=NotificationTypes.EMAIL.value,
        title="test",
        body_template="test",
        context_name=CONTEXT,
        context_kwargs={},
        send_after=LATER,
        subject_template="test",
        preheader_template="test",
        attachments=[
            NotificationAttachment(file=b"report", filename="report.txt", description="r"),
            NotificationAttachment(file=b"logo", filename="logo.png", is_inline=True),
        ],
    )

    _assert_replica_matches(service, primary, replica, one_off.id)
    assert len(replica.get_notification(one_off.id).attachments) == 2


def test_a_version_listed_from_the_replica_moves_the_primary(service, primary, replica, user_id):
    created = _create(service, user_id)
    service.cancel_notification(created.id)
    listed = _listed(service, "replica")[str(created.id)]

    requeued = service.requeue_notification(
        created.id, template_version=4, expected_version=listed, send=False
    )

    assert (requeued.status, requeued.requested_template_version) == (PENDING, 4)
    after_requeue = _assert_replica_matches(service, primary, replica, created.id)
    assert after_requeue != listed

    repinned = service.repin_notification(
        created.id, 5, expected_version=_listed(service, "replica")[str(created.id)]
    )

    assert repinned.requested_template_version == 5
    assert _assert_replica_matches(service, primary, replica, created.id) != after_requeue


def test_a_replica_that_missed_a_write_gives_a_version_the_primary_refuses(
    service, primary, replica, db_session, replica_db_session, user_id
):
    created = _create(service, user_id)
    service.cancel_notification(created.id)
    stale = _assert_on_primary_token(service, primary, replica, created.id)

    # The replica declines the snapshot, and is unreachable for the replayed write too.
    with (
        mock.patch.object(replica, "apply_replication_snapshot_if_newer", return_value=DECLINED),
        mock.patch.object(
            replica, "persist_notification_update", side_effect=RuntimeError("unavailable")
        ),
    ):
        service.repin_notification(created.id, 3)

    assert _listed(service, "replica")[str(created.id)] == stale
    assert _listed(service)[str(created.id)] != stale
    primary_before = _state(db_session, created.id)
    replica_before = _state(replica_db_session, created.id)

    with pytest.raises(NotificationActionError) as requeue:
        service.requeue_notification(created.id, expected_version=stale, send=False)
    with pytest.raises(NotificationActionError) as repin:
        service.repin_notification(created.id, 4, expected_version=stale)

    assert (requeue.value.reason, repin.value.reason) == ("CHANGED", "CHANGED")
    assert _state(db_session, created.id) == primary_before
    assert _state(replica_db_session, created.id) == replica_before


def test_the_replica_reports_versions_it_can_list(service):
    assert service.get_additional_backend_identifiers() == ["replica"]
    capabilities = service.get_backend_supported_action_capabilities("replica")

    assert capabilities["versions.filter"] is True
    assert capabilities["writes.notificationVersion"] is True


# --------------------------------------------- the service, snapshots declined and writes replayed


REPLAYED_WRITES = {
    "update": (_create, REPLICATED_WRITES["update"]),
    "failed send": (_create, _failed_send),
    "cancel": (_create, REPLICATED_WRITES["cancel"]),
    "requeue": (_create, _cancel_then_requeue),
    "repin": (_create, REPLICATED_WRITES["repin"]),
    "bulk read": (
        lambda service, user_id: _create(service, user_id, send_after=None),
        lambda service, _p, id_, user_id: service.mark_read_bulk([id_], user_id=user_id),
    ),
    # Stores the git commit SHA, marks it sent, stores the context used and the template version
    # used: each replayed on the replica with no token, and the converged row then stamped.
    "send": (_create, _send),
    "mark read": (
        lambda service, user_id: _create(service, user_id, send_after=None),
        lambda service, _p, id_, _u: service.mark_read(id_),
    ),
    "send then read": (_create, _send_then_read),
}


def _is_stamp(call) -> bool:
    return call.args[1:] == ({},) and "notification_version" in call.kwargs


@pytest.mark.parametrize("write", list(REPLAYED_WRITES))
def test_a_replica_declining_every_snapshot_is_stamped_with_the_primary_token_after_each_write(
    service, primary, replica, user_id, write
):
    seed, replicated_write = REPLAYED_WRITES[write]
    # The replay cannot create a row with the primary's id, so the row is replicated first.
    created = seed(service, user_id)
    before = _assert_on_primary_token(service, primary, replica, created.id)

    with (
        mock.patch.object(
            replica, "apply_replication_snapshot_if_newer", return_value=DECLINED
        ) as apply,
        mock.patch.object(
            replica, "persist_notification_update", wraps=replica.persist_notification_update
        ) as update_on_replica,
    ):
        replicated_write(service, primary, created.id, user_id)

    assert apply.called
    after = _assert_on_primary_token(service, primary, replica, created.id)
    assert after != before
    # The replica ends on the primary's token through the token-only stamp, made last; no
    # replayed write carries the token.
    stamps = [c for c in update_on_replica.call_args_list if _is_stamp(c)]
    assert stamps
    assert stamps[-1].kwargs["notification_version"] == after
    assert update_on_replica.call_args_list[-1] == stamps[-1]
    assert all(
        "notification_version" not in c.kwargs
        for c in update_on_replica.call_args_list
        if not _is_stamp(c)
    )
    primary_row = primary.get_notification(created.id)
    replica_row = replica.get_notification(created.id)
    for field in (
        "status",
        "title",
        "requested_template_version",
        "used_template_version",
        "git_commit_sha",
        "context_used",
        "adapter_used",
    ):
        assert getattr(replica_row, field) == getattr(primary_row, field), field
    if write in ("send", "mark read", "send then read"):
        assert replica_row.used_template_version == 7
        assert replica_row.context_used == {"greeting": "hi"}


@pytest.mark.parametrize("write", ["cancel", "requeue"])
def test_a_declining_replica_that_missed_a_write_is_converged_and_stamped(
    service, primary, replica, user_id, write
):
    created = _create(service, user_id)
    if write == "requeue":
        _failed_send(service, primary, created.id, user_id)
    unavailable = RuntimeError("unavailable")

    # The replica misses the write entirely.
    with mock.patch.object(replica, "apply_replication_snapshot_if_newer", side_effect=unavailable):
        if write == "cancel":
            service.cancel_notification(created.id)
        else:
            service.requeue_notification(created.id, send=False)
    assert replica.get_notification(created.id).status == (PENDING if write == "cancel" else FAILED)

    # Replicated again while it declines snapshots: converged through its primitives.
    with mock.patch.object(replica, "apply_replication_snapshot_if_newer", return_value=DECLINED):
        result = service.replicate_notification(created.id)

    assert result == {"successes": ["replica"], "failures": []}
    assert replica.get_notification(created.id).status == (
        NotificationStatus.CANCELLED.value if write == "cancel" else PENDING
    )
    _assert_on_primary_token(service, primary, replica, created.id)


def test_an_out_of_order_snapshot_through_the_service_leaves_the_later_state_and_token(
    service, primary, replica, user_id
):
    created = _create(service, user_id)
    earlier_snapshot = primary.get_notification(created.id)
    service.cancel_notification(created.id)
    later_token = _assert_replica_matches(service, primary, replica, created.id)

    outcomes: list[ApplyResult] = []
    apply = replica.apply_replication_snapshot_if_newer

    def recording_apply(snapshot):
        outcomes.append(apply(snapshot))
        return outcomes[-1]

    # A late replication of the earlier state, say a queued worker that read the primary before
    # the cancel and applies after it.
    with (
        mock.patch.object(primary, "get_notification", return_value=earlier_snapshot),
        mock.patch.object(
            replica, "apply_replication_snapshot_if_newer", side_effect=recording_apply
        ),
        mock.patch.object(
            replica, "persist_notification_update", wraps=replica.persist_notification_update
        ) as update_on_replica,
        mock.patch.object(
            replica, "requeue_notification", wraps=replica.requeue_notification
        ) as requeue_on_replica,
    ):
        service.replicate_notification(created.id)

    assert [(o.applied, o.superseded) for o in outcomes] == [(False, True)]
    # Superseded: nothing is replayed over the newer row.
    assert update_on_replica.call_args_list == []
    assert requeue_on_replica.call_args_list == []
    replicated = replica.get_notification(created.id)
    assert replicated.status == NotificationStatus.CANCELLED.value
    assert replicated.notification_version == later_token


def test_a_partial_replay_leaves_the_replica_on_its_own_token(service, primary, replica, user_id):
    created = _create(service, user_id)
    service.cancel_notification(created.id)
    before = _assert_on_primary_token(service, primary, replica, created.id)
    replica_update = replica.persist_notification_update

    def refuse_the_stamp(notification_id, update_data, **kwargs):
        if not update_data:
            raise RuntimeError("unavailable")
        return replica_update(notification_id, update_data, **kwargs)

    with (
        mock.patch.object(replica, "apply_replication_snapshot_if_newer", return_value=DECLINED),
        mock.patch.object(replica, "persist_notification_update", side_effect=refuse_the_stamp),
    ):
        service.repin_notification(created.id, 3)

    primary_token = _listed(service)[str(created.id)]
    replica_token = _listed(service, "replica")[str(created.id)]
    # The replayed writes landed, each issuing the replica's own token; the stamp did not.
    assert replica.get_notification(created.id).requested_template_version == 3
    assert replica_token not in (before, primary_token)
    with pytest.raises(NotificationActionError) as refused:
        service.repin_notification(created.id, 4, expected_version=replica_token)
    assert refused.value.reason == "CHANGED"


# ------------------------------------------------------- apply_replication_snapshot_if_newer


def _old_snapshot(primary, db_session, notification):
    """The primary's record, written at a fixed past instant so its clock is not the replica's."""
    _set(db_session, notification.id, created=EARLIER, updated=EARLIER)
    return primary.get_notification(notification.id)


@pytest.mark.parametrize("kind", ["user", "one-off"])
def test_apply_inserts_with_the_snapshot_id_timestamps_and_token(
    primary, replica, db_session, user_id, kind
):
    persisted = (
        _persist(primary, user_id) if kind == "user" else _persist_one_off(primary, tenant="acme")
    )
    _set(db_session, persisted.id, context_used={"a": 1}, adapter_used="some.adapter")
    snapshot = _old_snapshot(primary, db_session, persisted)

    result = replica.apply_replication_snapshot_if_newer(snapshot)

    assert result == ApplyResult(applied=True)
    stored = replica.get_notification(snapshot.id)
    assert (stored.id, stored.created, stored.modified) == (snapshot.id, EARLIER, EARLIER)
    assert stored.notification_version == snapshot.notification_version
    assert stored == dataclasses.replace(snapshot, attachments=stored.attachments)


def test_an_older_snapshot_is_declined_and_nothing_is_written(
    primary, replica, db_session, replica_db_session, user_id
):
    snapshot = _old_snapshot(primary, db_session, _persist(primary, user_id))
    replica.apply_replication_snapshot_if_newer(snapshot)
    before = _state(replica_db_session, snapshot.id)
    older = dataclasses.replace(
        snapshot,
        title="older",
        modified=EARLIER - datetime.timedelta(microseconds=1),
        notification_version=str(uuid7()),
    )

    result = replica.apply_replication_snapshot_if_newer(older)

    assert (result.applied, result.superseded) == (False, True)
    assert str(snapshot.id) in (result.reason or "")
    assert "older" not in (result.reason or "")
    assert _state(replica_db_session, snapshot.id) == before


def test_an_equal_modified_snapshot_with_a_new_token_applies(primary, replica, db_session, user_id):
    snapshot = _old_snapshot(primary, db_session, _persist(primary, user_id))
    replica.apply_replication_snapshot_if_newer(snapshot)
    new_token = str(uuid7())

    result = replica.apply_replication_snapshot_if_newer(
        dataclasses.replace(snapshot, title="same instant", notification_version=new_token)
    )

    assert result.applied is True
    stored = replica.get_notification(snapshot.id)
    assert (stored.title, stored.notification_version, stored.modified) == (
        "same instant",
        new_token,
        EARLIER,
    )


def test_a_newer_snapshot_applies(primary, replica, db_session, user_id):
    snapshot = _old_snapshot(primary, db_session, _persist(primary, user_id))
    replica.apply_replication_snapshot_if_newer(snapshot)
    newer = dataclasses.replace(
        snapshot,
        status=READ,
        read_at=EARLIER,
        modified=EARLIER + datetime.timedelta(seconds=1),
        notification_version=str(uuid7()),
    )

    assert replica.apply_replication_snapshot_if_newer(newer).applied is True
    assert replica.get_notification(snapshot.id) == dataclasses.replace(newer, attachments=[])


@pytest.mark.parametrize("token", [None, "not-a-uuid"])
def test_a_snapshot_without_a_usable_token_gets_a_fresh_one(
    primary, replica, db_session, replica_db_session, user_id, token
):
    snapshot = _old_snapshot(primary, db_session, _persist(primary, user_id))

    replica.apply_replication_snapshot_if_newer(
        dataclasses.replace(snapshot, notification_version=token)
    )

    stored = _token(replica_db_session, snapshot.id)
    assert uuid.UUID(stored).version == 7
    assert stored != snapshot.notification_version


@pytest.mark.parametrize(
    "notification_id",
    [str(uuid.uuid4()), uuid.uuid4(), "012", "abc", True],
    ids=["uuid string", "uuid", "zero padded", "word", "bool"],
)
def test_an_id_this_backend_cannot_hold_is_declined_and_nothing_is_written(
    primary, replica, db_session, replica_db_session, user_id, notification_id
):
    snapshot = _old_snapshot(primary, db_session, _persist(primary, user_id))

    result = replica.apply_replication_snapshot_if_newer(
        dataclasses.replace(snapshot, id=notification_id)
    )

    assert (result.applied, result.superseded) == (False, False)
    assert str(notification_id) in (result.reason or "")
    assert _count(replica_db_session, NotificationModel) == 0


def test_a_user_id_this_backend_cannot_hold_is_declined(
    primary, replica, db_session, replica_db_session, user_id
):
    snapshot = _old_snapshot(primary, db_session, _persist(primary, user_id))

    result = replica.apply_replication_snapshot_if_newer(
        dataclasses.replace(snapshot, user_id=str(uuid.uuid4()))
    )

    assert (result.applied, result.superseded) == (False, False)
    assert _count(replica_db_session, NotificationModel) == 0


def test_an_id_given_as_a_digit_string_lands_on_that_row(primary, replica, db_session, user_id):
    snapshot = _old_snapshot(primary, db_session, _persist(primary, user_id))

    result = replica.apply_replication_snapshot_if_newer(
        dataclasses.replace(snapshot, id=str(snapshot.id), user_id=str(user_id))
    )

    assert result.applied is True
    stored = replica.get_notification(snapshot.id)
    assert (stored.id, stored.user_id) == (snapshot.id, user_id)


def test_attachments_are_reconciled_without_touching_bytes(
    primary, replica, db_session, replica_db_session, user_id
):
    persisted = _persist(
        primary,
        user_id,
        attachments=[
            NotificationAttachment(file=b"report", filename="report.txt", description="r"),
            NotificationAttachment(file=b"logo", filename="logo.png", is_inline=True),
        ],
    )
    snapshot = _old_snapshot(primary, db_session, persisted)
    manager = mock.Mock()
    replica._attachment_manager = manager

    assert replica.apply_replication_snapshot_if_newer(snapshot).applied is True

    assert manager.mock_calls == []
    assert _attachment_shapes(replica.get_notification(snapshot.id)) == _attachment_shapes(snapshot)

    report = next(a for a in snapshot.attachments if a.filename == "report.txt")
    trimmed = dataclasses.replace(
        snapshot,
        attachments=[dataclasses.replace(report, description="changed", is_inline=True)],
        modified=EARLIER + datetime.timedelta(seconds=1),
    )
    manager.reset_mock()

    assert replica.apply_replication_snapshot_if_newer(trimmed).applied is True

    assert [c for c in manager.mock_calls if "reconstruct" not in str(c)] == []
    assert _attachment_shapes(replica.get_notification(snapshot.id)) == _attachment_shapes(trimmed)
    # The removed file keeps its record (another notification may use it); none is duplicated.
    assert _count(replica_db_session, AttachmentFileRecordModel) == 2
    assert _count(replica_db_session, NotificationAttachmentModel) == 1

    restored = dataclasses.replace(snapshot, modified=EARLIER + datetime.timedelta(seconds=2))
    assert replica.apply_replication_snapshot_if_newer(restored).applied is True
    assert _attachment_shapes(replica.get_notification(snapshot.id)) == _attachment_shapes(restored)
    assert _count(replica_db_session, AttachmentFileRecordModel) == 2
    assert _token(replica_db_session, snapshot.id) == restored.notification_version


@pytest.mark.parametrize("present", [False, True], ids=["insert", "update"])
def test_an_apply_that_fails_part_way_writes_nothing(
    primary, replica, db_session, replica_db_session, user_id, present
):
    snapshot = _old_snapshot(primary, db_session, _persist(primary, user_id))
    if present:
        replica.apply_replication_snapshot_if_newer(snapshot)
    before = _state(replica_db_session, snapshot.id)
    newer = dataclasses.replace(
        snapshot,
        title="newer",
        modified=EARLIER + datetime.timedelta(seconds=1),
        notification_version=str(uuid7()),
    )

    with (
        mock.patch.object(
            SQLAlchemyNotificationBackend,
            "_reconcile_snapshot_attachments",
            side_effect=RuntimeError("boom"),
        ),
        pytest.raises(RuntimeError),
    ):
        replica.apply_replication_snapshot_if_newer(newer)

    assert _state(replica_db_session, snapshot.id) == before


MINUS_THREE = datetime.timezone(datetime.timedelta(hours=-3))
PLUS_THREE = datetime.timezone(datetime.timedelta(hours=3))


def _aware(naive_utc: datetime.datetime, tz: datetime.timezone) -> datetime.datetime:
    return naive_utc.replace(tzinfo=datetime.timezone.utc).astimezone(tz)


@pytest.mark.parametrize(
    ("offset", "tz", "applies"),
    [
        # A newer instant whose -03:00 wall clock is earlier than the row's.
        (datetime.timedelta(seconds=1), MINUS_THREE, True),
        # An older instant whose +03:00 wall clock is later than the row's.
        (-datetime.timedelta(seconds=1), PLUS_THREE, False),
    ],
    ids=["newer at -03:00", "older at +03:00"],
)
def test_a_non_utc_modified_is_ordered_by_its_instant(
    primary, replica, db_session, replica_db_session, user_id, offset, tz, applies
):
    snapshot = _old_snapshot(primary, db_session, _persist(primary, user_id))
    replica.apply_replication_snapshot_if_newer(snapshot)
    before = _state(replica_db_session, snapshot.id)
    moved = dataclasses.replace(
        snapshot,
        title="moved",
        modified=_aware(EARLIER + offset, tz),
        notification_version=str(uuid7()),
    )

    result = replica.apply_replication_snapshot_if_newer(moved)

    assert (result.applied, result.superseded) == (applies, not applies)
    if applies:
        stored = replica.get_notification(snapshot.id)
        assert (stored.title, stored.modified) == ("moved", EARLIER + offset)
    else:
        assert _state(replica_db_session, snapshot.id) == before


def test_every_snapshot_datetime_is_stored_as_naive_utc(primary, replica, db_session, user_id):
    snapshot = _old_snapshot(primary, db_session, _persist(primary, user_id))
    instants = {
        "created": EARLIER,
        "modified": EARLIER + datetime.timedelta(seconds=1),
        "send_after": EARLIER + datetime.timedelta(days=1),
        "sent_at": EARLIER + datetime.timedelta(seconds=2),
        "read_at": EARLIER + datetime.timedelta(seconds=3),
    }
    aware = {field: _aware(value, MINUS_THREE) for field, value in instants.items()}

    replica.apply_replication_snapshot_if_newer(dataclasses.replace(snapshot, status=READ, **aware))

    stored = replica.get_notification(snapshot.id)
    assert {field: getattr(stored, field) for field in instants} == instants


def _racing_insert(user_id, row_modified: datetime.datetime):
    """A ``_snapshot_row_exists`` that, on its first call, inserts the row another apply raced
    in (through the same transaction and around the ORM, standing in for one that committed
    meanwhile) and reports it absent, as a check made just before that insert would have."""
    real = SQLAlchemyNotificationBackend._snapshot_row_exists
    calls = []

    def exists(backend, session, notification_id):
        calls.append(notification_id)
        if len(calls) == 1:
            session.execute(
                insert(NotificationModel).values(
                    id=notification_id,
                    user_id=user_id,
                    notification_type=NotificationTypes.EMAIL.value,
                    title="raced",
                    body_template="raced",
                    updated=row_modified,
                    created=row_modified,
                    notification_version=uuid7(),
                )
            )
            return False
        return real(backend, session, notification_id)

    return exists


@pytest.mark.parametrize("raced_row_is_newer", [False, True], ids=["older", "newer"])
def test_an_insert_that_loses_a_race_re_runs_the_conditional_update(
    primary, replica, db_session, user_id, raced_row_is_newer
):
    snapshot = _old_snapshot(primary, db_session, _persist(primary, user_id))
    raced_at = EARLIER + datetime.timedelta(seconds=1 if raced_row_is_newer else -1)
    insert_row = SQLAlchemyNotificationBackend._insert_snapshot_row
    inserted: list[bool] = []

    def recording_insert(*args):
        inserted.append(insert_row(*args))
        return inserted[-1]

    with (
        mock.patch.object(
            SQLAlchemyNotificationBackend,
            "_snapshot_row_exists",
            autospec=True,
            side_effect=_racing_insert(user_id, raced_at),
        ),
        mock.patch.object(
            SQLAlchemyNotificationBackend,
            "_insert_snapshot_row",
            autospec=True,
            side_effect=recording_insert,
        ),
    ):
        result = replica.apply_replication_snapshot_if_newer(snapshot)

    # The insert was attempted, and lost to the row raced in.
    assert inserted == [False]

    stored = replica.get_notification(snapshot.id)
    if raced_row_is_newer:
        assert (result.applied, result.superseded) == (False, True)
        assert (stored.title, stored.modified) == ("raced", raced_at)
        assert stored.notification_version != snapshot.notification_version
    else:
        assert result == ApplyResult(applied=True)
        assert stored == dataclasses.replace(snapshot, attachments=stored.attachments)


SECRETS = ("Secret title", "secret@example.com", "secret-diagnosis")


@pytest.mark.parametrize("present", [False, True], ids=["insert", "update"])
def test_a_database_error_in_apply_carries_no_field_value(
    primary, replica, db_session, replica_db_session, present
):
    snapshot = _old_snapshot(
        primary,
        db_session,
        _persist_one_off(
            primary,
            title=SECRETS[0],
            email_or_phone=SECRETS[1],
            context_kwargs={"note": SECRETS[2]},
        ),
    )
    if present:
        replica.apply_replication_snapshot_if_newer(snapshot)
    # ``body_template`` is NOT NULL: the INSERT or the UPDATE fails in the database, with every
    # bound parameter in SQLAlchemy's error text.
    broken = dataclasses.replace(
        snapshot, body_template=None, modified=EARLIER + datetime.timedelta(seconds=1)
    )

    with pytest.raises(SnapshotApplyDatabaseError) as raised:
        replica.apply_replication_snapshot_if_newer(broken)

    error = raised.value
    assert isinstance(error, NotificationError)
    assert str(snapshot.id) in str(error)
    assert "IntegrityError" in str(error)
    assert (error.__cause__, error.__context__, error.__suppress_context__) == (None, None, True)
    rendered = "".join(traceback.format_exception(error))
    for secret in SECRETS:
        assert secret not in rendered
    assert "parameters" not in rendered


# ---------------------------------------------------------------- writes.notificationVersion


def _update_fields(backend, n, uid, **kw):
    return backend.persist_notification_update(n.id, {"title": "changed"}, **kw)


def _update_nothing(backend, n, uid, **kw):
    return backend.persist_notification_update(n.id, {}, **kw)


def _update_attachments(backend, n, uid, **kw):
    other = _persist(
        backend, uid, attachments=[NotificationAttachment(file=b"linked", filename="linked.txt")]
    )
    return backend.persist_notification_update(n.id, {"attachments": other.attachments}, **kw)


def _requeue_conditionally(backend, n, uid, **kw):
    current = _token(backend.session_manager, n.id)
    return backend.requeue_notification(n.id, template_version=5, expected_version=current, **kw)


def _repin_to_current_pin(backend, n, uid, **kw):
    return backend.repin_notification(n.id, 2, **kw)


TOKEN_WRITES: list[tuple[str, str, Callable[..., Any]]] = [
    ("persist_notification_update", PENDING, _update_fields),
    ("persist_notification_update_nothing", PENDING, _update_nothing),
    ("persist_notification_update_attachments", PENDING, _update_attachments),
    (
        "delete_notification_attachment",
        PENDING,
        lambda backend, n, uid, **kw: backend.delete_notification_attachment(
            n.attachments[0].id, **kw
        ),
    ),
    ("mark_pending_as_sent", PENDING, lambda b, n, uid, **kw: b.mark_pending_as_sent(n.id, **kw)),
    (
        "mark_pending_as_failed",
        PENDING,
        lambda b, n, uid, **kw: b.mark_pending_as_failed(n.id, **kw),
    ),
    ("mark_sent_as_read", SENT, lambda b, n, uid, **kw: b.mark_sent_as_read(n.id, **kw)),
    ("cancel_notification", PENDING, lambda b, n, uid, **kw: b.cancel_notification(n.id, **kw)),
    ("requeue_notification", FAILED, lambda b, n, uid, **kw: b.requeue_notification(n.id, **kw)),
    ("requeue_notification_conditionally", FAILED, _requeue_conditionally),
    ("repin_notification", FAILED, lambda b, n, uid, **kw: b.repin_notification(n.id, 9, **kw)),
    ("repin_notification_to_its_pin", FAILED, _repin_to_current_pin),
    (
        "store_context_used",
        PENDING,
        lambda b, n, uid, **kw: b.store_context_used(n.id, {"a": 1}, "some.adapter", **kw),
    ),
    (
        "store_git_commit_sha",
        PENDING,
        lambda b, n, uid, **kw: b.store_git_commit_sha(n.id, "b" * 40, **kw),
    ),
    (
        "store_template_version",
        PENDING,
        lambda b, n, uid, **kw: b.store_template_version(n.id, 3, **kw),
    ),
]


NO_OP_WITHOUT_A_TOKEN = (_update_nothing, _repin_to_current_pin)


@pytest.mark.parametrize("given", ["token", "none", "malformed"])
@pytest.mark.parametrize(
    ("status", "write"),
    [(status, write) for _, status, write in TOKEN_WRITES],
    ids=[name for name, _, _ in TOKEN_WRITES],
)
def test_every_write_takes_the_token_it_is_given(
    primary, db_session, user_id, status, write, given
):
    notification = _persist(
        primary, user_id, attachments=[NotificationAttachment(file=b"r", filename="r.txt")]
    )
    _set(db_session, notification.id, status=status)
    before = _token(db_session, notification.id)
    token = {"token": str(uuid7()), "none": None, "malformed": "not-a-uuid"}[given]
    kwargs = {} if given == "none" else {"notification_version": token}

    returned = write(primary, notification, user_id, **kwargs)

    after = _token(db_session, notification.id)
    if given == "token":
        assert after == token
    elif write in NO_OP_WITHOUT_A_TOKEN:
        # Nothing to write, and no token it could store: nothing is written.
        assert after == before
    else:
        assert after not in (before, token)
        assert uuid.UUID(after).version == 7
    if returned is not None:
        assert returned.notification_version == after


def _bulk_read_one(backend, n, uid, **kw):
    versions = {"notification_versions": {n.id: kw["notification_version"]}} if kw else {}
    return backend.mark_sent_as_read_bulk([n.id], uid, **versions)


UPDATED_MOVING_WRITES = [
    (name, status, write)
    for name, status, write in TOKEN_WRITES
    if write not in NO_OP_WITHOUT_A_TOKEN
] + [("mark_sent_as_read_bulk", SENT, _bulk_read_one)]


@pytest.mark.parametrize("with_token", [False, True], ids=["minted", "given"])
@pytest.mark.parametrize(
    ("status", "write"),
    [(status, write) for _, status, write in UPDATED_MOVING_WRITES],
    ids=[name for name, _, _ in UPDATED_MOVING_WRITES],
)
def test_every_write_moves_updated_forward(primary, db_session, user_id, status, write, with_token):
    # ``updated`` is what newer-wins compares, so a write that left it alone would let an older
    # snapshot overwrite it. None of these sets it: its onupdate does, on every UPDATE.
    notification = _persist(
        primary, user_id, attachments=[NotificationAttachment(file=b"r", filename="r.txt")]
    )
    _set(db_session, notification.id, status=status, updated=EARLIER)
    kwargs = {"notification_version": str(uuid7())} if with_token else {}

    write(primary, notification, user_id, **kwargs)

    assert _state(db_session, notification.id)[4] > EARLIER


@pytest.mark.parametrize("given", ["token", "none", "malformed"])
@pytest.mark.parametrize("with_attachments", [False, True], ids=["plain", "attachments"])
@pytest.mark.parametrize("kind", ["user", "one-off"])
def test_a_created_row_takes_the_token_it_is_given(
    primary, db_session, user_id, kind, with_attachments, given
):
    token = {"token": str(uuid7()), "none": None, "malformed": "not-a-uuid"}[given]
    kwargs = {} if given == "none" else {"notification_version": token}
    if with_attachments:
        kwargs["attachments"] = [NotificationAttachment(file=b"r", filename="r.txt")]

    created = (
        _persist(primary, user_id, **kwargs)
        if kind == "user"
        else _persist_one_off(primary, **kwargs)
    )

    stored = _token(db_session, created.id)
    assert created.notification_version == stored
    if given == "token":
        assert stored == token
    else:
        assert uuid.UUID(stored).version == 7


def test_a_bulk_read_takes_the_token_given_for_each_row(primary, db_session, user_id):
    mapped, unmapped, already_read = (_persist(primary, user_id) for _ in range(3))
    _set(db_session, mapped.id, status=SENT)
    _set(db_session, unmapped.id, status=SENT)
    _set(db_session, already_read.id, status=READ, read_at=EARLIER, updated=EARLIER)
    tokens = {str(mapped.id): str(uuid7()), str(already_read.id): str(uuid7())}
    before_unmapped = _token(db_session, unmapped.id)

    # Keyed by the ids as strings, as a primary of another kind would return them.
    read = primary.mark_sent_as_read_bulk(
        [mapped.id, unmapped.id, already_read.id], user_id, notification_versions=tokens
    )

    by_id = {n.id: n for n in read}
    assert set(by_id) == {mapped.id, unmapped.id, already_read.id}
    assert _token(db_session, mapped.id) == tokens[str(mapped.id)]
    assert _token(db_session, already_read.id) == tokens[str(already_read.id)]
    assert _token(db_session, unmapped.id) not in (before_unmapped, *tokens.values())
    for notification_id, notification in by_id.items():
        assert notification.notification_version == _token(db_session, notification_id)
    # The row that was already read keeps its read_at and its updated.
    assert (by_id[already_read.id].read_at, by_id[already_read.id].modified) == (EARLIER, EARLIER)


def test_a_bulk_read_mapping_reaches_no_row_outside_the_call(primary, db_session, user_id):
    target, other = (_persist(primary, user_id) for _ in range(2))
    _set(db_session, target.id, status=SENT)
    _set(db_session, other.id, status=READ)
    before_other = _token(db_session, other.id)

    primary.mark_sent_as_read_bulk(
        [target.id], user_id, notification_versions={other.id: str(uuid7()), "abc": "x"}
    )

    assert _token(db_session, other.id) == before_other


def test_a_conditional_requeue_checks_the_current_token_before_taking_the_given_one(
    primary, db_session, user_id
):
    notification = _persist(primary, user_id)
    _set(db_session, notification.id, status=FAILED)
    stale = _token(db_session, notification.id)
    _set(db_session, notification.id, title="moved on")
    before = _state(db_session, notification.id)

    with pytest.raises(NotificationVersionChangedError):
        primary.requeue_notification(
            notification.id, expected_version=stale, notification_version=str(uuid7())
        )
    with pytest.raises(NotificationVersionChangedError):
        primary.repin_notification(
            notification.id, 9, expected_version=stale, notification_version=str(uuid7())
        )

    assert _state(db_session, notification.id) == before


@pytest.mark.parametrize(
    "status", [PENDING, FAILED, NotificationStatus.CANCELLED.value, SENT, READ]
)
def test_a_token_only_update_is_accepted_in_any_status_and_changes_only_the_token(
    primary, db_session, user_id, status
):
    notification = _persist(primary, user_id)
    _set(db_session, notification.id, status=status, read_at=EARLIER, updated=EARLIER)
    before = _state(db_session, notification.id)
    token = str(uuid7())

    returned = primary.persist_notification_update(notification.id, {}, notification_version=token)

    assert (returned.notification_version, returned.modified) == (token, EARLIER)
    assert _state(db_session, notification.id) == (*before[:-1], uuid.UUID(token))


@pytest.mark.parametrize("status", [SENT, READ])
@pytest.mark.parametrize("with_token", [False, True], ids=["minted", "given"])
def test_an_update_that_changes_anything_on_a_delivered_row_is_still_refused(
    primary, db_session, user_id, status, with_token
):
    notification = _persist(primary, user_id)
    _set(db_session, notification.id, status=status)
    before = _state(db_session, notification.id)
    kwargs = {"notification_version": str(uuid7())} if with_token else {}

    with pytest.raises(NotificationUpdateError):
        primary.persist_notification_update(notification.id, {"title": "changed"}, **kwargs)

    assert _state(db_session, notification.id) == before


def test_a_token_only_update_of_a_missing_row_is_refused(primary, user_id):
    with pytest.raises(NotificationUpdateError):
        primary.persist_notification_update(987654, {}, notification_version=str(uuid7()))


def test_a_repin_to_the_current_pin_takes_the_token_and_keeps_updated(primary, db_session, user_id):
    notification = _persist(primary, user_id)
    _set(db_session, notification.id, status=FAILED, updated=EARLIER)
    token = str(uuid7())

    repinned = primary.repin_notification(notification.id, 2, notification_version=token)

    assert (repinned.notification_version, repinned.modified) == (token, EARLIER)
    assert _token(db_session, notification.id) == token


# ------------------------------------------------------------------ identifier, serialization


def test_the_backend_identifier_is_optional_and_kept(db_session):
    unnamed = SQLAlchemyNotificationBackend(db_session, NotificationModel)
    named = SQLAlchemyNotificationBackend(
        db_session, NotificationModel, backend_identifier="eu-west"
    )

    assert unnamed.get_backend_identifier() is None
    assert "backend_identifier" not in unnamed.backend_kwargs
    assert named.get_backend_identifier() == "eu-west"
    assert named.backend_kwargs["backend_identifier"] == "eu-west"


def test_every_returned_notification_carries_the_row_token(primary, db_session, user_id):
    notification = _persist(primary, user_id)
    token = _token(db_session, notification.id)

    assert notification.notification_version == token
    assert primary.get_notification(notification.id).notification_version == token
    listed = primary.filter_notifications({}, 1, 100)
    assert {n.notification_version for n in listed if n.id == notification.id} == {token}


def test_a_row_without_a_token_serializes_none():
    row = NotificationModel(id=1, user_id=1, notification_type="EMAIL", title="t")
    row.notification_version = None  # type: ignore[assignment]

    assert _user_notification_from_orm(row, []).notification_version is None
