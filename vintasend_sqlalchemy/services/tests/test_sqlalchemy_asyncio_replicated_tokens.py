"""The primary's version token on a replica, on the AsyncIO SQLAlchemy backend.

The AsyncIO twin of ``test_sqlalchemy_replicated_tokens``: ``apply_replication_snapshot_if_newer``,
the ``notification_version`` keyword every write takes (``writes.notificationVersion``), and an
``AsyncIONotificationService`` replicating inline from one database to a second one."""

import dataclasses
import datetime
import traceback
import uuid
from collections.abc import Callable
from typing import Any
from unittest import mock

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, insert, select, update
from vintasend.constants import NotificationStatus, NotificationTypes
from vintasend.exceptions import (
    NotificationActionError,
    NotificationError,
    NotificationUpdateError,
    NotificationVersionChangedError,
)
from vintasend.services.dataclasses import ApplyResult, NotificationAttachment
from vintasend.services.git_commit_sha_providers.stubs.fake_git_commit_sha_provider import (
    FakeAsyncIOGitCommitShaProvider,
)
from vintasend.services.notification_service import AsyncIONotificationService, register_context
from vintasend.utils.uuid_utils import uuid7

from example_app.models import Notification as NotificationModel
from example_app.models import User
from vintasend_sqlalchemy.model_factory import AttachmentFileRecord as AttachmentFileRecordModel
from vintasend_sqlalchemy.model_factory import NotificationAttachment as NotificationAttachmentModel
from vintasend_sqlalchemy.services.notification_backends.sqlalchemy_notification_backend import (
    SnapshotApplyDatabaseError,
    SQLAlchemyAsyncIONotificationBackend,
)


FAKE_EMAIL_ADAPTER = (
    "vintasend.services.notification_adapters.stubs.fake_adapter.FakeAsyncIOEmailAdapter"
)
VERSIONED_RENDERER = (
    "vintasend.services.notification_template_renderers.stubs."
    "fake_templated_email_renderer.FakeVersionedTemplateRenderer"
)
CONTEXT = "sqlalchemy_asyncio_replicated_tokens_test_context"
LATER = datetime.datetime(2030, 1, 1, tzinfo=datetime.timezone.utc)
EARLIER = datetime.datetime(2024, 5, 6, 7, 8, 9, 123456)
PENDING = NotificationStatus.PENDING_SEND.value
SENT = NotificationStatus.SENT.value
READ = NotificationStatus.READ.value
FAILED = NotificationStatus.FAILED.value
CANCELLED = NotificationStatus.CANCELLED.value
DECLINED = ApplyResult(applied=False, reason="declined for test")

asyncio_test = pytest.mark.asyncio(loop_scope="session")


@register_context(CONTEXT)
def _asyncio_context():
    return {"greeting": "hi"}


async def _clear(session_maker) -> None:
    async with session_maker.begin() as session:
        await session.execute(delete(NotificationAttachmentModel))
        await session.execute(delete(AttachmentFileRecordModel))
        await session.execute(delete(NotificationModel))
        await session.execute(delete(User))


@pytest_asyncio.fixture(loop_scope="session")
async def user_id(async_db_session, replica_async_db_session):
    async with async_db_session.begin() as session:
        user = User(email="foo@example.com")
        session.add(user)
        await session.flush()
        uid = user.id
    # The same user in the replica database, so its rows point at a user that exists there too.
    async with replica_async_db_session.begin() as session:
        session.add(User(id=uid, email="foo@example.com"))
    yield uid
    await _clear(async_db_session)
    await _clear(replica_async_db_session)


@pytest.fixture
def primary(async_db_session) -> SQLAlchemyAsyncIONotificationBackend:
    return SQLAlchemyAsyncIONotificationBackend(
        async_db_session, NotificationModel, backend_identifier="primary"
    )


@pytest.fixture
def replica(replica_async_db_session) -> SQLAlchemyAsyncIONotificationBackend:
    return SQLAlchemyAsyncIONotificationBackend(
        replica_async_db_session, NotificationModel, backend_identifier="replica"
    )


@pytest.fixture
def service(primary, replica) -> AsyncIONotificationService:
    return AsyncIONotificationService(
        notification_adapters=[(FAKE_EMAIL_ADAPTER, VERSIONED_RENDERER)],
        notification_backend=primary,
        # The adapters build their own backend from these.
        notification_backend_kwargs={
            "session": primary.session_manager,
            "notification_model_cls": NotificationModel,
        },
        additional_backends=[replica],
        git_commit_sha_provider=FakeAsyncIOGitCommitShaProvider(),
    )


async def _create(service, user_id, **overrides):
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
    return await service.create_notification(**kwargs)


async def _persist(backend, user_id, **overrides):
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
    return await backend.persist_notification(**kwargs)


async def _persist_one_off(backend, **overrides):
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
    return await backend.persist_one_off_notification(**kwargs)


async def _set(session_maker, notification_id, **values):
    async with session_maker.begin() as session:
        await session.execute(
            update(NotificationModel).where(NotificationModel.id == notification_id).values(values)
        )


async def _token(session_maker, notification_id) -> str:
    async with session_maker.begin() as session:
        return str(
            (
                await session.execute(
                    select(NotificationModel.notification_version).where(
                        NotificationModel.id == notification_id
                    )
                )
            ).scalar_one()
        )


async def _state(session_maker, notification_id) -> tuple | None:
    """Everything a refused write could have touched, the token included."""
    async with session_maker.begin() as session:
        row = (
            await session.execute(
                select(
                    NotificationModel.title,
                    NotificationModel.status,
                    NotificationModel.requested_template_version,
                    NotificationModel.read_at,
                    NotificationModel.updated,
                    NotificationModel.notification_version,
                ).where(NotificationModel.id == notification_id)
            )
        ).one_or_none()
        return None if row is None else tuple(row)


async def _count(session_maker, model) -> int:
    async with session_maker.begin() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def _listed(service, backend_identifier=None) -> dict[str, str]:
    return {
        str(entry.id): entry.version
        for entry in await service.filter_notification_versions({}, 1, 1000, backend_identifier)
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


async def _assert_on_primary_token(service, primary, replica, notification_id) -> str:
    token = (await _listed(service))[str(notification_id)]
    assert (await _listed(service, "replica"))[str(notification_id)] == token
    assert (await primary.get_notification(notification_id)).notification_version == token
    assert (await replica.get_notification(notification_id)).notification_version == token
    return token


async def _assert_replica_matches(service, primary, replica, notification_id) -> str:
    """The replica lists the primary's token and holds every field the primary does."""
    token = await _assert_on_primary_token(service, primary, replica, notification_id)
    primary_row = await primary.get_notification(notification_id)
    replica_row = await replica.get_notification(notification_id)
    assert type(replica_row) is type(primary_row)
    for field in dataclasses.fields(primary_row):
        if field.name != "attachments":
            assert getattr(replica_row, field.name) == getattr(primary_row, field.name), field.name
    assert _attachment_shapes(replica_row) == _attachment_shapes(primary_row)
    return token


# --------------------------------------------------------------- the service, snapshots applied


async def _send(service, primary, notification_id, user_id):
    await service.send(await primary.get_notification(notification_id))


async def _failed_send(service, primary, notification_id, user_id):
    adapter = next(iter(service.notification_adapters))
    with mock.patch.object(adapter, "send", side_effect=RuntimeError("smtp down")):
        await service.send(await primary.get_notification(notification_id))


async def _send_then_read(service, primary, notification_id, user_id):
    await _send(service, primary, notification_id, user_id)
    await service.mark_read(notification_id)


async def _send_then_bulk_read(service, primary, notification_id, user_id):
    await _send(service, primary, notification_id, user_id)
    read = await service.mark_read_bulk([notification_id], user_id=user_id)
    assert [n.id for n in read] == [notification_id]


async def _update(service, primary, notification_id, user_id):
    await service.update_notification(notification_id, title="Changed")


async def _cancel(service, primary, notification_id, user_id):
    await service.cancel_notification(notification_id)


async def _cancel_then_requeue(service, primary, notification_id, user_id):
    await service.cancel_notification(notification_id)
    await service.requeue_notification(notification_id, template_version=4, send=False)


async def _repin(service, primary, notification_id, user_id):
    await service.repin_notification(notification_id, 4)


async def _mark_read(service, primary, notification_id, user_id):
    await service.mark_read(notification_id)


async def _bulk_read(service, primary, notification_id, user_id):
    await service.mark_read_bulk([notification_id], user_id=user_id)


REPLICATED_WRITES = {
    "update": (_update, PENDING),
    # Stores the git commit SHA, marks it sent, stores the context and the template version used.
    "send": (_send, SENT),
    "failed send": (_failed_send, FAILED),
    "mark read": (_send_then_read, READ),
    "bulk read": (_send_then_bulk_read, READ),
    "cancel": (_cancel, CANCELLED),
    "requeue": (_cancel_then_requeue, PENDING),
    "repin": (_repin, PENDING),
}


@asyncio_test
@pytest.mark.parametrize("write", list(REPLICATED_WRITES))
async def test_each_replicated_write_leaves_the_replica_on_the_primary_token(
    service, primary, replica, user_id, write
):
    replicated_write, expected_status = REPLICATED_WRITES[write]
    created = await _create(service, user_id)
    after_create = await _assert_replica_matches(service, primary, replica, created.id)

    await replicated_write(service, primary, created.id, user_id)

    after_write = await _assert_replica_matches(service, primary, replica, created.id)
    assert after_write != after_create
    assert (await replica.get_notification(created.id)).status == expected_status


@asyncio_test
async def test_a_send_replicates_the_template_version_and_the_git_commit_sha(
    service, primary, replica, user_id
):
    created = await _create(service, user_id)

    await _send(service, primary, created.id, user_id)

    replicated = await replica.get_notification(created.id)
    assert replicated.used_template_version == 7
    assert replicated.git_commit_sha == "a" * 40
    assert replicated.context_used == {"greeting": "hi"}
    await _assert_replica_matches(service, primary, replica, created.id)


@asyncio_test
async def test_a_one_off_and_its_attachments_are_replicated(service, primary, replica, user_id):
    one_off = await service.create_one_off_notification(
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

    await _assert_replica_matches(service, primary, replica, one_off.id)
    assert len((await replica.get_notification(one_off.id)).attachments) == 2


@asyncio_test
async def test_a_version_listed_from_the_replica_moves_the_primary(
    service, primary, replica, user_id
):
    created = await _create(service, user_id)
    await service.cancel_notification(created.id)
    listed = (await _listed(service, "replica"))[str(created.id)]

    requeued = await service.requeue_notification(
        created.id, template_version=4, expected_version=listed, send=False
    )

    assert (requeued.status, requeued.requested_template_version) == (PENDING, 4)
    after_requeue = await _assert_replica_matches(service, primary, replica, created.id)
    assert after_requeue != listed

    repinned = await service.repin_notification(
        created.id, 5, expected_version=(await _listed(service, "replica"))[str(created.id)]
    )

    assert repinned.requested_template_version == 5
    assert await _assert_replica_matches(service, primary, replica, created.id) != after_requeue


@asyncio_test
async def test_a_replica_that_missed_a_write_gives_a_version_the_primary_refuses(
    service, primary, replica, async_db_session, replica_async_db_session, user_id
):
    created = await _create(service, user_id)
    await service.cancel_notification(created.id)
    stale = await _assert_on_primary_token(service, primary, replica, created.id)

    # The replica declines the snapshot, and is unreachable for the replayed write too.
    with (
        mock.patch.object(replica, "apply_replication_snapshot_if_newer", return_value=DECLINED),
        mock.patch.object(
            replica, "persist_notification_update", side_effect=RuntimeError("unavailable")
        ),
    ):
        await service.repin_notification(created.id, 3)

    assert (await _listed(service, "replica"))[str(created.id)] == stale
    assert (await _listed(service))[str(created.id)] != stale
    primary_before = await _state(async_db_session, created.id)
    replica_before = await _state(replica_async_db_session, created.id)

    with pytest.raises(NotificationActionError) as requeue:
        await service.requeue_notification(created.id, expected_version=stale, send=False)
    with pytest.raises(NotificationActionError) as repin:
        await service.repin_notification(created.id, 4, expected_version=stale)

    assert (requeue.value.reason, repin.value.reason) == ("CHANGED", "CHANGED")
    assert await _state(async_db_session, created.id) == primary_before
    assert await _state(replica_async_db_session, created.id) == replica_before


@asyncio_test
async def test_the_replica_reports_versions_it_can_list(service):
    assert service.get_additional_backend_identifiers() == ["replica"]
    capabilities = await service.get_backend_supported_action_capabilities("replica")

    assert capabilities["versions.filter"] is True
    assert capabilities["writes.notificationVersion"] is True


# --------------------------------------------- the service, snapshots declined and writes replayed


async def _create_sent(service, user_id):
    return await _create(service, user_id, send_after=None)


REPLAYED_WRITES = {
    "update": (_create, _update),
    "failed send": (_create, _failed_send),
    "cancel": (_create, _cancel),
    "requeue": (_create, _cancel_then_requeue),
    "repin": (_create, _repin),
    "bulk read": (_create_sent, _bulk_read),
    # Stores the git commit SHA, marks it sent, stores the context used and the template version
    # used: each replayed on the replica with no token, and the converged row then stamped.
    "send": (_create, _send),
    "mark read": (_create_sent, _mark_read),
    "send then read": (_create, _send_then_read),
}


def _is_stamp(call) -> bool:
    return call.args[1:] == ({},) and "notification_version" in call.kwargs


@asyncio_test
@pytest.mark.parametrize("write", list(REPLAYED_WRITES))
async def test_a_replica_declining_every_snapshot_is_stamped_with_the_primary_token_after_each_write(
    service, primary, replica, user_id, write
):
    seed, replicated_write = REPLAYED_WRITES[write]
    # The replay cannot create a row with the primary's id, so the row is replicated first.
    created = await seed(service, user_id)
    before = await _assert_on_primary_token(service, primary, replica, created.id)

    with (
        mock.patch.object(
            replica, "apply_replication_snapshot_if_newer", return_value=DECLINED
        ) as apply,
        mock.patch.object(
            replica, "persist_notification_update", wraps=replica.persist_notification_update
        ) as update_on_replica,
    ):
        await replicated_write(service, primary, created.id, user_id)

    assert apply.called
    after = await _assert_on_primary_token(service, primary, replica, created.id)
    assert after != before
    # See the sync twin: the token arrives through the stamp, made last, and only through it.
    stamps = [c for c in update_on_replica.call_args_list if _is_stamp(c)]
    assert stamps
    assert stamps[-1].kwargs["notification_version"] == after
    assert update_on_replica.call_args_list[-1] == stamps[-1]
    assert all(
        "notification_version" not in c.kwargs
        for c in update_on_replica.call_args_list
        if not _is_stamp(c)
    )
    primary_row = await primary.get_notification(created.id)
    replica_row = await replica.get_notification(created.id)
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


@asyncio_test
@pytest.mark.parametrize("write", ["cancel", "requeue"])
async def test_a_declining_replica_that_missed_a_write_is_converged_and_stamped(
    service, primary, replica, user_id, write
):
    created = await _create(service, user_id)
    if write == "requeue":
        await _failed_send(service, primary, created.id, user_id)

    # The replica misses the write entirely.
    with mock.patch.object(
        replica, "apply_replication_snapshot_if_newer", side_effect=RuntimeError("unavailable")
    ):
        if write == "cancel":
            await service.cancel_notification(created.id)
        else:
            await service.requeue_notification(created.id, send=False)
    assert (await replica.get_notification(created.id)).status == (
        PENDING if write == "cancel" else FAILED
    )

    # Replicated again while it declines snapshots: converged through its primitives.
    with mock.patch.object(replica, "apply_replication_snapshot_if_newer", return_value=DECLINED):
        result = await service.replicate_notification(created.id)

    assert result == {"successes": ["replica"], "failures": []}
    assert (await replica.get_notification(created.id)).status == (
        CANCELLED if write == "cancel" else PENDING
    )
    await _assert_on_primary_token(service, primary, replica, created.id)


@asyncio_test
async def test_an_out_of_order_snapshot_through_the_service_leaves_the_later_state_and_token(
    service, primary, replica, user_id
):
    created = await _create(service, user_id)
    earlier_snapshot = await primary.get_notification(created.id)
    await service.cancel_notification(created.id)
    later_token = await _assert_replica_matches(service, primary, replica, created.id)
    outcomes: list[ApplyResult] = []
    apply = replica.apply_replication_snapshot_if_newer

    async def recording_apply(snapshot):
        outcomes.append(await apply(snapshot))
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
        await service.replicate_notification(created.id)

    assert [(o.applied, o.superseded) for o in outcomes] == [(False, True)]
    # Superseded: nothing is replayed over the newer row.
    assert update_on_replica.call_args_list == []
    assert requeue_on_replica.call_args_list == []
    replicated = await replica.get_notification(created.id)
    assert replicated.status == CANCELLED
    assert replicated.notification_version == later_token


@asyncio_test
async def test_a_partial_replay_leaves_the_replica_on_its_own_token(
    service, primary, replica, user_id
):
    created = await _create(service, user_id)
    await service.cancel_notification(created.id)
    before = await _assert_on_primary_token(service, primary, replica, created.id)
    replica_update = replica.persist_notification_update

    async def refuse_the_stamp(notification_id, update_data, **kwargs):
        if not update_data:
            raise RuntimeError("unavailable")
        return await replica_update(notification_id, update_data, **kwargs)

    with (
        mock.patch.object(replica, "apply_replication_snapshot_if_newer", return_value=DECLINED),
        mock.patch.object(replica, "persist_notification_update", side_effect=refuse_the_stamp),
    ):
        await service.repin_notification(created.id, 3)

    primary_token = (await _listed(service))[str(created.id)]
    replica_token = (await _listed(service, "replica"))[str(created.id)]
    # The replayed writes landed, each issuing the replica's own token; the stamp did not.
    assert (await replica.get_notification(created.id)).requested_template_version == 3
    assert replica_token not in (before, primary_token)
    with pytest.raises(NotificationActionError) as refused:
        await service.repin_notification(created.id, 4, expected_version=replica_token)
    assert refused.value.reason == "CHANGED"


# ------------------------------------------------------- apply_replication_snapshot_if_newer


async def _old_snapshot(primary, async_db_session, notification):
    """The primary's record, written at a fixed past instant so its clock is not the replica's."""
    await _set(async_db_session, notification.id, created=EARLIER, updated=EARLIER)
    return await primary.get_notification(notification.id)


@asyncio_test
@pytest.mark.parametrize("kind", ["user", "one-off"])
async def test_apply_inserts_with_the_snapshot_id_timestamps_and_token(
    primary, replica, async_db_session, user_id, kind
):
    persisted = (
        await _persist(primary, user_id)
        if kind == "user"
        else await _persist_one_off(primary, tenant="acme")
    )
    await _set(async_db_session, persisted.id, context_used={"a": 1}, adapter_used="some.adapter")
    snapshot = await _old_snapshot(primary, async_db_session, persisted)

    result = await replica.apply_replication_snapshot_if_newer(snapshot)

    assert result == ApplyResult(applied=True)
    stored = await replica.get_notification(snapshot.id)
    assert (stored.id, stored.created, stored.modified) == (snapshot.id, EARLIER, EARLIER)
    assert stored.notification_version == snapshot.notification_version
    assert stored == dataclasses.replace(snapshot, attachments=stored.attachments)


@asyncio_test
async def test_an_older_snapshot_is_declined_and_nothing_is_written(
    primary, replica, async_db_session, replica_async_db_session, user_id
):
    snapshot = await _old_snapshot(primary, async_db_session, await _persist(primary, user_id))
    await replica.apply_replication_snapshot_if_newer(snapshot)
    before = await _state(replica_async_db_session, snapshot.id)
    older = dataclasses.replace(
        snapshot,
        title="older",
        modified=EARLIER - datetime.timedelta(microseconds=1),
        notification_version=str(uuid7()),
    )

    result = await replica.apply_replication_snapshot_if_newer(older)

    assert (result.applied, result.superseded) == (False, True)
    assert str(snapshot.id) in (result.reason or "")
    assert "older" not in (result.reason or "")
    assert await _state(replica_async_db_session, snapshot.id) == before


@asyncio_test
async def test_an_equal_modified_snapshot_with_a_new_token_applies(
    primary, replica, async_db_session, user_id
):
    snapshot = await _old_snapshot(primary, async_db_session, await _persist(primary, user_id))
    await replica.apply_replication_snapshot_if_newer(snapshot)
    new_token = str(uuid7())

    result = await replica.apply_replication_snapshot_if_newer(
        dataclasses.replace(snapshot, title="same instant", notification_version=new_token)
    )

    assert result.applied is True
    stored = await replica.get_notification(snapshot.id)
    assert (stored.title, stored.notification_version, stored.modified) == (
        "same instant",
        new_token,
        EARLIER,
    )


@asyncio_test
async def test_a_newer_snapshot_applies(primary, replica, async_db_session, user_id):
    snapshot = await _old_snapshot(primary, async_db_session, await _persist(primary, user_id))
    await replica.apply_replication_snapshot_if_newer(snapshot)
    newer = dataclasses.replace(
        snapshot,
        status=READ,
        read_at=EARLIER,
        modified=EARLIER + datetime.timedelta(seconds=1),
        notification_version=str(uuid7()),
    )

    assert (await replica.apply_replication_snapshot_if_newer(newer)).applied is True
    assert await replica.get_notification(snapshot.id) == dataclasses.replace(newer, attachments=[])


@asyncio_test
@pytest.mark.parametrize("token", [None, "not-a-uuid"])
async def test_a_snapshot_without_a_usable_token_gets_a_fresh_one(
    primary, replica, async_db_session, replica_async_db_session, user_id, token
):
    snapshot = await _old_snapshot(primary, async_db_session, await _persist(primary, user_id))

    await replica.apply_replication_snapshot_if_newer(
        dataclasses.replace(snapshot, notification_version=token)
    )

    stored = await _token(replica_async_db_session, snapshot.id)
    assert uuid.UUID(stored).version == 7
    assert stored != snapshot.notification_version


@asyncio_test
@pytest.mark.parametrize(
    "notification_id",
    [str(uuid.uuid4()), uuid.uuid4(), "012", "abc", True],
    ids=["uuid string", "uuid", "zero padded", "word", "bool"],
)
async def test_an_id_this_backend_cannot_hold_is_declined_and_nothing_is_written(
    primary, replica, async_db_session, replica_async_db_session, user_id, notification_id
):
    snapshot = await _old_snapshot(primary, async_db_session, await _persist(primary, user_id))

    result = await replica.apply_replication_snapshot_if_newer(
        dataclasses.replace(snapshot, id=notification_id)
    )

    assert (result.applied, result.superseded) == (False, False)
    assert str(notification_id) in (result.reason or "")
    assert await _count(replica_async_db_session, NotificationModel) == 0


@asyncio_test
async def test_a_user_id_this_backend_cannot_hold_is_declined(
    primary, replica, async_db_session, replica_async_db_session, user_id
):
    snapshot = await _old_snapshot(primary, async_db_session, await _persist(primary, user_id))

    result = await replica.apply_replication_snapshot_if_newer(
        dataclasses.replace(snapshot, user_id=str(uuid.uuid4()))
    )

    assert (result.applied, result.superseded) == (False, False)
    assert await _count(replica_async_db_session, NotificationModel) == 0


@asyncio_test
async def test_an_id_given_as_a_digit_string_lands_on_that_row(
    primary, replica, async_db_session, user_id
):
    snapshot = await _old_snapshot(primary, async_db_session, await _persist(primary, user_id))

    result = await replica.apply_replication_snapshot_if_newer(
        dataclasses.replace(snapshot, id=str(snapshot.id), user_id=str(user_id))
    )

    assert result.applied is True
    stored = await replica.get_notification(snapshot.id)
    assert (stored.id, stored.user_id) == (snapshot.id, user_id)


@asyncio_test
async def test_attachments_are_reconciled_without_touching_bytes(
    primary, replica, async_db_session, replica_async_db_session, user_id
):
    persisted = await _persist(
        primary,
        user_id,
        attachments=[
            NotificationAttachment(file=b"report", filename="report.txt", description="r"),
            NotificationAttachment(file=b"logo", filename="logo.png", is_inline=True),
        ],
    )
    snapshot = await _old_snapshot(primary, async_db_session, persisted)
    manager = mock.Mock()
    replica._attachment_manager = manager

    assert (await replica.apply_replication_snapshot_if_newer(snapshot)).applied is True

    assert manager.mock_calls == []
    assert _attachment_shapes(await replica.get_notification(snapshot.id)) == _attachment_shapes(
        snapshot
    )

    report = next(a for a in snapshot.attachments if a.filename == "report.txt")
    trimmed = dataclasses.replace(
        snapshot,
        attachments=[dataclasses.replace(report, description="changed", is_inline=True)],
        modified=EARLIER + datetime.timedelta(seconds=1),
    )
    manager.reset_mock()

    assert (await replica.apply_replication_snapshot_if_newer(trimmed)).applied is True

    assert [c for c in manager.mock_calls if "reconstruct" not in str(c)] == []
    assert _attachment_shapes(await replica.get_notification(snapshot.id)) == _attachment_shapes(
        trimmed
    )
    # The removed file keeps its record (another notification may use it); none is duplicated.
    assert await _count(replica_async_db_session, AttachmentFileRecordModel) == 2
    assert await _count(replica_async_db_session, NotificationAttachmentModel) == 1

    restored = dataclasses.replace(snapshot, modified=EARLIER + datetime.timedelta(seconds=2))
    assert (await replica.apply_replication_snapshot_if_newer(restored)).applied is True
    assert _attachment_shapes(await replica.get_notification(snapshot.id)) == _attachment_shapes(
        restored
    )
    assert await _count(replica_async_db_session, AttachmentFileRecordModel) == 2
    assert await _token(replica_async_db_session, snapshot.id) == restored.notification_version


@asyncio_test
@pytest.mark.parametrize("present", [False, True], ids=["insert", "update"])
async def test_an_apply_that_fails_part_way_writes_nothing(
    primary, replica, async_db_session, replica_async_db_session, user_id, present
):
    snapshot = await _old_snapshot(primary, async_db_session, await _persist(primary, user_id))
    if present:
        await replica.apply_replication_snapshot_if_newer(snapshot)
    before = await _state(replica_async_db_session, snapshot.id)
    newer = dataclasses.replace(
        snapshot,
        title="newer",
        modified=EARLIER + datetime.timedelta(seconds=1),
        notification_version=str(uuid7()),
    )

    with (
        mock.patch.object(
            SQLAlchemyAsyncIONotificationBackend,
            "_reconcile_snapshot_attachments",
            side_effect=RuntimeError("boom"),
        ),
        pytest.raises(RuntimeError),
    ):
        await replica.apply_replication_snapshot_if_newer(newer)

    assert await _state(replica_async_db_session, snapshot.id) == before


MINUS_THREE = datetime.timezone(datetime.timedelta(hours=-3))
PLUS_THREE = datetime.timezone(datetime.timedelta(hours=3))


def _aware(naive_utc: datetime.datetime, tz: datetime.timezone) -> datetime.datetime:
    return naive_utc.replace(tzinfo=datetime.timezone.utc).astimezone(tz)


@asyncio_test
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
async def test_a_non_utc_modified_is_ordered_by_its_instant(
    primary, replica, async_db_session, replica_async_db_session, user_id, offset, tz, applies
):
    snapshot = await _old_snapshot(primary, async_db_session, await _persist(primary, user_id))
    await replica.apply_replication_snapshot_if_newer(snapshot)
    before = await _state(replica_async_db_session, snapshot.id)
    moved = dataclasses.replace(
        snapshot,
        title="moved",
        modified=_aware(EARLIER + offset, tz),
        notification_version=str(uuid7()),
    )

    result = await replica.apply_replication_snapshot_if_newer(moved)

    assert (result.applied, result.superseded) == (applies, not applies)
    if applies:
        stored = await replica.get_notification(snapshot.id)
        assert (stored.title, stored.modified) == ("moved", EARLIER + offset)
    else:
        assert await _state(replica_async_db_session, snapshot.id) == before


@asyncio_test
async def test_every_snapshot_datetime_is_stored_as_naive_utc(
    primary, replica, async_db_session, user_id
):
    snapshot = await _old_snapshot(primary, async_db_session, await _persist(primary, user_id))
    instants = {
        "created": EARLIER,
        "modified": EARLIER + datetime.timedelta(seconds=1),
        "send_after": EARLIER + datetime.timedelta(days=1),
        "sent_at": EARLIER + datetime.timedelta(seconds=2),
        "read_at": EARLIER + datetime.timedelta(seconds=3),
    }
    aware = {field: _aware(value, MINUS_THREE) for field, value in instants.items()}

    await replica.apply_replication_snapshot_if_newer(
        dataclasses.replace(snapshot, status=READ, **aware)
    )

    stored = await replica.get_notification(snapshot.id)
    assert {field: getattr(stored, field) for field in instants} == instants


def _racing_insert(user_id, row_modified: datetime.datetime):
    """See the sync twin: on its first call, inserts the row another apply raced in and reports
    it absent."""
    real = SQLAlchemyAsyncIONotificationBackend._snapshot_row_exists
    calls = []

    async def exists(backend, session, notification_id):
        calls.append(notification_id)
        if len(calls) == 1:
            await session.execute(
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
        return await real(backend, session, notification_id)

    return exists


@asyncio_test
@pytest.mark.parametrize("raced_row_is_newer", [False, True], ids=["older", "newer"])
async def test_an_insert_that_loses_a_race_re_runs_the_conditional_update(
    primary, replica, async_db_session, user_id, raced_row_is_newer
):
    snapshot = await _old_snapshot(primary, async_db_session, await _persist(primary, user_id))
    raced_at = EARLIER + datetime.timedelta(seconds=1 if raced_row_is_newer else -1)
    insert_row = SQLAlchemyAsyncIONotificationBackend._insert_snapshot_row
    inserted: list[bool] = []

    async def recording_insert(*args):
        inserted.append(await insert_row(*args))
        return inserted[-1]

    with (
        mock.patch.object(
            SQLAlchemyAsyncIONotificationBackend,
            "_snapshot_row_exists",
            autospec=True,
            side_effect=_racing_insert(user_id, raced_at),
        ),
        mock.patch.object(
            SQLAlchemyAsyncIONotificationBackend,
            "_insert_snapshot_row",
            autospec=True,
            side_effect=recording_insert,
        ),
    ):
        result = await replica.apply_replication_snapshot_if_newer(snapshot)

    # The insert was attempted, and lost to the row raced in.
    assert inserted == [False]
    stored = await replica.get_notification(snapshot.id)
    if raced_row_is_newer:
        assert (result.applied, result.superseded) == (False, True)
        assert (stored.title, stored.modified) == ("raced", raced_at)
        assert stored.notification_version != snapshot.notification_version
    else:
        assert result == ApplyResult(applied=True)
        assert stored == dataclasses.replace(snapshot, attachments=stored.attachments)


SECRETS = ("Secret title", "secret@example.com", "secret-diagnosis")


@asyncio_test
@pytest.mark.parametrize("present", [False, True], ids=["insert", "update"])
async def test_a_database_error_in_apply_carries_no_field_value(
    primary, replica, async_db_session, replica_async_db_session, present
):
    snapshot = await _old_snapshot(
        primary,
        async_db_session,
        await _persist_one_off(
            primary,
            title=SECRETS[0],
            email_or_phone=SECRETS[1],
            context_kwargs={"note": SECRETS[2]},
        ),
    )
    if present:
        await replica.apply_replication_snapshot_if_newer(snapshot)
    # See the sync twin: ``body_template`` is NOT NULL.
    broken = dataclasses.replace(
        snapshot, body_template=None, modified=EARLIER + datetime.timedelta(seconds=1)
    )

    with pytest.raises(SnapshotApplyDatabaseError) as raised:
        await replica.apply_replication_snapshot_if_newer(broken)

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


async def _update_fields(backend, n, uid, **kw):
    return await backend.persist_notification_update(n.id, {"title": "changed"}, **kw)


async def _update_nothing(backend, n, uid, **kw):
    return await backend.persist_notification_update(n.id, {}, **kw)


async def _update_attachments(backend, n, uid, **kw):
    other = await _persist(
        backend, uid, attachments=[NotificationAttachment(file=b"linked", filename="linked.txt")]
    )
    return await backend.persist_notification_update(n.id, {"attachments": other.attachments}, **kw)


async def _delete_attachment(backend, n, uid, **kw):
    return await backend.delete_notification_attachment(n.attachments[0].id, **kw)


async def _mark_pending_as_sent(backend, n, uid, **kw):
    return await backend.mark_pending_as_sent(n.id, **kw)


async def _mark_pending_as_failed(backend, n, uid, **kw):
    return await backend.mark_pending_as_failed(n.id, **kw)


async def _mark_sent_as_read(backend, n, uid, **kw):
    return await backend.mark_sent_as_read(n.id, **kw)


async def _cancel_notification(backend, n, uid, **kw):
    return await backend.cancel_notification(n.id, **kw)


async def _requeue_notification(backend, n, uid, **kw):
    return await backend.requeue_notification(n.id, **kw)


async def _requeue_conditionally(backend, n, uid, **kw):
    current = await _token(backend.session_manager, n.id)
    return await backend.requeue_notification(
        n.id, template_version=5, expected_version=current, **kw
    )


async def _repin_notification(backend, n, uid, **kw):
    return await backend.repin_notification(n.id, 9, **kw)


async def _repin_to_current_pin(backend, n, uid, **kw):
    return await backend.repin_notification(n.id, 2, **kw)


async def _store_context_used(backend, n, uid, **kw):
    return await backend.store_context_used(n.id, {"a": 1}, "some.adapter", **kw)


async def _store_git_commit_sha(backend, n, uid, **kw):
    return await backend.store_git_commit_sha(n.id, "b" * 40, **kw)


async def _store_template_version(backend, n, uid, **kw):
    return await backend.store_template_version(n.id, 3, **kw)


TOKEN_WRITES: list[tuple[str, str, Callable[..., Any]]] = [
    ("persist_notification_update", PENDING, _update_fields),
    ("persist_notification_update_nothing", PENDING, _update_nothing),
    ("persist_notification_update_attachments", PENDING, _update_attachments),
    ("delete_notification_attachment", PENDING, _delete_attachment),
    ("mark_pending_as_sent", PENDING, _mark_pending_as_sent),
    ("mark_pending_as_failed", PENDING, _mark_pending_as_failed),
    ("mark_sent_as_read", SENT, _mark_sent_as_read),
    ("cancel_notification", PENDING, _cancel_notification),
    ("requeue_notification", FAILED, _requeue_notification),
    ("requeue_notification_conditionally", FAILED, _requeue_conditionally),
    ("repin_notification", FAILED, _repin_notification),
    ("repin_notification_to_its_pin", FAILED, _repin_to_current_pin),
    ("store_context_used", PENDING, _store_context_used),
    ("store_git_commit_sha", PENDING, _store_git_commit_sha),
    ("store_template_version", PENDING, _store_template_version),
]
NO_OP_WITHOUT_A_TOKEN = (_update_nothing, _repin_to_current_pin)


@asyncio_test
@pytest.mark.parametrize("given", ["token", "none", "malformed"])
@pytest.mark.parametrize(
    ("status", "write"),
    [(status, write) for _, status, write in TOKEN_WRITES],
    ids=[name for name, _, _ in TOKEN_WRITES],
)
async def test_every_write_takes_the_token_it_is_given(
    primary, async_db_session, user_id, status, write, given
):
    notification = await _persist(
        primary, user_id, attachments=[NotificationAttachment(file=b"r", filename="r.txt")]
    )
    await _set(async_db_session, notification.id, status=status)
    before = await _token(async_db_session, notification.id)
    token = {"token": str(uuid7()), "none": None, "malformed": "not-a-uuid"}[given]
    kwargs = {} if given == "none" else {"notification_version": token}

    returned = await write(primary, notification, user_id, **kwargs)

    after = await _token(async_db_session, notification.id)
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


async def _bulk_read_one(backend, n, uid, **kw):
    versions = {"notification_versions": {n.id: kw["notification_version"]}} if kw else {}
    return await backend.mark_sent_as_read_bulk([n.id], uid, **versions)


UPDATED_MOVING_WRITES = [
    (name, status, write)
    for name, status, write in TOKEN_WRITES
    if write not in NO_OP_WITHOUT_A_TOKEN
] + [("mark_sent_as_read_bulk", SENT, _bulk_read_one)]


@asyncio_test
@pytest.mark.parametrize("with_token", [False, True], ids=["minted", "given"])
@pytest.mark.parametrize(
    ("status", "write"),
    [(status, write) for _, status, write in UPDATED_MOVING_WRITES],
    ids=[name for name, _, _ in UPDATED_MOVING_WRITES],
)
async def test_every_write_moves_updated_forward(
    primary, async_db_session, user_id, status, write, with_token
):
    # See the sync twin: newer-wins compares ``updated``, which every UPDATE's onupdate moves.
    notification = await _persist(
        primary, user_id, attachments=[NotificationAttachment(file=b"r", filename="r.txt")]
    )
    await _set(async_db_session, notification.id, status=status, updated=EARLIER)
    kwargs = {"notification_version": str(uuid7())} if with_token else {}

    await write(primary, notification, user_id, **kwargs)

    state = await _state(async_db_session, notification.id)
    assert state is not None
    assert state[4] > EARLIER


@asyncio_test
@pytest.mark.parametrize("given", ["token", "none", "malformed"])
@pytest.mark.parametrize("with_attachments", [False, True], ids=["plain", "attachments"])
@pytest.mark.parametrize("kind", ["user", "one-off"])
async def test_a_created_row_takes_the_token_it_is_given(
    primary, async_db_session, user_id, kind, with_attachments, given
):
    token = {"token": str(uuid7()), "none": None, "malformed": "not-a-uuid"}[given]
    kwargs = {} if given == "none" else {"notification_version": token}
    if with_attachments:
        kwargs["attachments"] = [NotificationAttachment(file=b"r", filename="r.txt")]

    created = (
        await _persist(primary, user_id, **kwargs)
        if kind == "user"
        else await _persist_one_off(primary, **kwargs)
    )

    stored = await _token(async_db_session, created.id)
    assert created.notification_version == stored
    if given == "token":
        assert stored == token
    else:
        assert uuid.UUID(stored).version == 7


@asyncio_test
async def test_a_bulk_read_takes_the_token_given_for_each_row(primary, async_db_session, user_id):
    mapped = await _persist(primary, user_id)
    unmapped = await _persist(primary, user_id)
    already_read = await _persist(primary, user_id)
    await _set(async_db_session, mapped.id, status=SENT)
    await _set(async_db_session, unmapped.id, status=SENT)
    await _set(async_db_session, already_read.id, status=READ, read_at=EARLIER, updated=EARLIER)
    tokens = {str(mapped.id): str(uuid7()), str(already_read.id): str(uuid7())}
    before_unmapped = await _token(async_db_session, unmapped.id)

    # Keyed by the ids as strings, as a primary of another kind would return them.
    read = await primary.mark_sent_as_read_bulk(
        [mapped.id, unmapped.id, already_read.id], user_id, notification_versions=tokens
    )

    by_id = {n.id: n for n in read}
    assert set(by_id) == {mapped.id, unmapped.id, already_read.id}
    assert await _token(async_db_session, mapped.id) == tokens[str(mapped.id)]
    assert await _token(async_db_session, already_read.id) == tokens[str(already_read.id)]
    assert await _token(async_db_session, unmapped.id) not in (
        before_unmapped,
        *tokens.values(),
    )
    for notification_id, notification in by_id.items():
        assert notification.notification_version == await _token(async_db_session, notification_id)
    # The row that was already read keeps its read_at and its updated.
    assert (by_id[already_read.id].read_at, by_id[already_read.id].modified) == (EARLIER, EARLIER)


@asyncio_test
async def test_a_bulk_read_mapping_reaches_no_row_outside_the_call(
    primary, async_db_session, user_id
):
    target = await _persist(primary, user_id)
    other = await _persist(primary, user_id)
    await _set(async_db_session, target.id, status=SENT)
    await _set(async_db_session, other.id, status=READ)
    before_other = await _token(async_db_session, other.id)

    await primary.mark_sent_as_read_bulk(
        [target.id], user_id, notification_versions={other.id: str(uuid7()), "abc": "x"}
    )

    assert await _token(async_db_session, other.id) == before_other


@asyncio_test
async def test_a_conditional_requeue_checks_the_current_token_before_taking_the_given_one(
    primary, async_db_session, user_id
):
    notification = await _persist(primary, user_id)
    await _set(async_db_session, notification.id, status=FAILED)
    stale = await _token(async_db_session, notification.id)
    await _set(async_db_session, notification.id, title="moved on")
    before = await _state(async_db_session, notification.id)

    with pytest.raises(NotificationVersionChangedError):
        await primary.requeue_notification(
            notification.id, expected_version=stale, notification_version=str(uuid7())
        )
    with pytest.raises(NotificationVersionChangedError):
        await primary.repin_notification(
            notification.id, 9, expected_version=stale, notification_version=str(uuid7())
        )

    assert await _state(async_db_session, notification.id) == before


@asyncio_test
@pytest.mark.parametrize("status", [PENDING, FAILED, CANCELLED, SENT, READ])
async def test_a_token_only_update_is_accepted_in_any_status_and_changes_only_the_token(
    primary, async_db_session, user_id, status
):
    notification = await _persist(primary, user_id)
    await _set(async_db_session, notification.id, status=status, read_at=EARLIER, updated=EARLIER)
    before = await _state(async_db_session, notification.id)
    token = str(uuid7())

    returned = await primary.persist_notification_update(
        notification.id, {}, notification_version=token
    )

    assert (returned.notification_version, returned.modified) == (token, EARLIER)
    assert await _state(async_db_session, notification.id) == (*before[:-1], uuid.UUID(token))


@asyncio_test
@pytest.mark.parametrize("status", [SENT, READ])
@pytest.mark.parametrize("with_token", [False, True], ids=["minted", "given"])
async def test_an_update_that_changes_anything_on_a_delivered_row_is_still_refused(
    primary, async_db_session, user_id, status, with_token
):
    notification = await _persist(primary, user_id)
    await _set(async_db_session, notification.id, status=status)
    before = await _state(async_db_session, notification.id)
    kwargs = {"notification_version": str(uuid7())} if with_token else {}

    with pytest.raises(NotificationUpdateError):
        await primary.persist_notification_update(notification.id, {"title": "changed"}, **kwargs)

    assert await _state(async_db_session, notification.id) == before


@asyncio_test
async def test_a_token_only_update_of_a_missing_row_is_refused(primary, user_id):
    with pytest.raises(NotificationUpdateError):
        await primary.persist_notification_update(987654, {}, notification_version=str(uuid7()))


@asyncio_test
async def test_a_repin_to_the_current_pin_takes_the_token_and_keeps_updated(
    primary, async_db_session, user_id
):
    notification = await _persist(primary, user_id)
    await _set(async_db_session, notification.id, status=FAILED, updated=EARLIER)
    token = str(uuid7())

    repinned = await primary.repin_notification(notification.id, 2, notification_version=token)

    assert (repinned.notification_version, repinned.modified) == (token, EARLIER)
    assert await _token(async_db_session, notification.id) == token


# ------------------------------------------------------------------ identifier, serialization


def test_the_backend_identifier_is_optional_and_kept(async_db_session):
    unnamed = SQLAlchemyAsyncIONotificationBackend(async_db_session, NotificationModel)
    named = SQLAlchemyAsyncIONotificationBackend(
        async_db_session, NotificationModel, backend_identifier="eu-west"
    )

    assert unnamed.get_backend_identifier() is None
    assert "backend_identifier" not in unnamed.backend_kwargs
    assert named.get_backend_identifier() == "eu-west"
    assert named.backend_kwargs["backend_identifier"] == "eu-west"


@asyncio_test
async def test_every_returned_notification_carries_the_row_token(
    primary, async_db_session, user_id
):
    notification = await _persist(primary, user_id)
    token = await _token(async_db_session, notification.id)

    assert notification.notification_version == token
    assert (await primary.get_notification(notification.id)).notification_version == token
    listed = await primary.filter_notifications({}, 1, 100)
    assert {n.notification_version for n in listed if n.id == notification.id} == {token}
