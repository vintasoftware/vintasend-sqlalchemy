# VintaSend SQLAlchemy

SQLAlchemy backend implementation for [VintaSend](https://github.com/vintasoftware/vintasend).

Provides a synchronous (`SQLAlchemyNotificationBackend`) and an AsyncIO
(`SQLAlchemyAsyncIONotificationBackend`) notification backend, plus a filesystem-backed
attachment manager.

## Compatibility

- Python 3.10 - 3.14
- SQLAlchemy 2.0+
- vintasend 2.0+ (3.8.0+ for the `notification_version` column and the conditional moves)

This release implements the full vintasend 2.0 backend contract: one-off notifications, the
composable filtering / ordering API (`filter_notifications`), `sent_at` / `read_at`, the `tenant`
partition key, `git_commit_sha`, bulk read, and the attachment manager seam.

It also persists vintasend's template-version fields: `requested_template_version` (which version
of its template a notification renders) and `used_template_version` (which version the renderer
reported it actually used, written through `store_template_version` at send time). Both are
filterable -- `{"requested_template_version": 3}`, `{"used_template_version": [1, 2]}` -- as
integer membership, with the same NULL semantics as every other field: a notification with no
version never matches positively and is included under negation. See
[Template Version Pinning](https://github.com/vintasoftware/vintasend#template-version-pinning).

### Requeueing and repinning

Both backends implement `requeue_notification` and `repin_notification` as single conditional
`UPDATE`s, so two callers racing cannot both succeed:

- `requeue_notification` moves a `FAILED` or `CANCELLED` notification back to `PENDING_SEND` with
  one `UPDATE ... WHERE id = ? AND status IN ('FAILED', 'CANCELLED')`. It clears `sent_at`,
  applies `send_after` only when it is passed (`None` included), pins `template_version` in the
  same `UPDATE` when one is passed, and returns the notification with its attachments.
- `repin_notification` changes `requested_template_version` only. Its `WHERE` carries the
  statuses a pin may still change in (`PENDING_SEND`, `FAILED`, `CANCELLED`) and "not already on
  this version" (an unpinned row included). A notification a worker sends in between is never
  repinned, and one already on the version is returned as it is, with nothing written.

When an `UPDATE` matches nothing, the row is read once to say why, and nothing is written: an
unknown id raises `NotificationNotFoundError`, a notification written since `expected_version`
raises `NotificationVersionChangedError` (see below), and the wrong status raises
`NotificationRequeueError` or `NotificationRepinError`. The service maps these to
`NotificationActionError` reasons `NOT_FOUND`, `CHANGED` and `INVALID_STATUS`.

A cancelled notification keeps its row with status `CANCELLED`, and `get_notification` returns
it, so it can be requeued. `persist_notification_update` accepts a `FAILED` or `CANCELLED`
notification as well as a pending one; a `SENT` or `READ` one still raises
`NotificationUpdateError`.

### Moving notifications safely

Both backends implement every optional action vintasend 3.8.0 offers for moving notifications,
and report all five in `get_action_capabilities()` (`repin.expectedVersion`,
`requeue.expectedVersion`, `requeue.templateVersion`, `versions.filter`,
`writes.notificationVersion`). They also evaluate the
presence filters `has_send_after` and `has_requested_template_version` (`IS NOT NULL` /
`IS NULL`, negation included) and report them in `get_filter_capabilities()`.

```python
failed = {"and": [{"body_template": "welcome"}, {"status": NotificationStatus.FAILED}]}

# Selects the id and notification_version columns only: no recipient, context or content.
for listed in service.filter_notification_versions(failed, page=1, page_size=100):
    # One UPDATE: pin, requeue, and only if the row is unchanged since it was listed.
    service.requeue_notification(
        listed.id, template_version=4, expected_version=listed.version, send=False
    )
```

Moving notifications changes which ones match, so list again from page 1 rather than moving on to
page 2.

#### The version token

The token is a `notification_version` column (`sqlalchemy.Uuid`: native on PostgreSQL,
`CHAR(32)` elsewhere) on `NotificationMixin`. It is a new UUIDv7 on **every** write to the row,
and `filter_notification_versions` hands it out as `str(uuid)`. `expected_version` joins the
`UPDATE`'s `WHERE`, so a notification written since it was listed is refused with
`NotificationVersionChangedError` (reason `CHANGED`) and nothing is written. A token that is not a
UUID is refused the same way, after an unknown id is reported as not found.

Every `UPDATE` the backends issue sets the token, attachment changes included. The column's
`default` and `onupdate` cover an insert and an `UPDATE` made through your model, by an ORM flush
or a Core `update()` against its table. A write that bypasses the model, such as raw SQL or
another service writing to the same table, must set `notification_version` to a fresh UUID in the
same statement (`vintasend.utils.uuid_utils.uuid7()`), or `expected_version` cannot see it.

Every `Notification` / `OneOffNotification` the backends return carries the row's token in
`notification_version` (`None` for a row inserted by a pre-3.8 instance during a rolling deploy),
so a caller holding one can pass it as `expected_version` without listing.

#### Replicas

A second database can serve listings as a replica of the primary: give each backend a stable
`backend_identifier` and pass the replica in `additional_backends`.

```python
primary = SQLAlchemyNotificationBackend(PrimarySession, Notification, backend_identifier="primary")
replica = SQLAlchemyNotificationBackend(ReplicaSession, Notification, backend_identifier="replica")
service = NotificationService(
    notification_backend=primary,
    additional_backends=[replica],
    notification_adapters=[...],
)

for listed in service.filter_notification_versions(failed, 1, 100, backend_identifier="replica"):
    service.requeue_notification(listed.id, expected_version=listed.version, send=False)
```

Moves always run on the primary, and the token a replica lists is the **primary's**. After every
replicated write (requeue and repin included) the service hands the replica the primary's
snapshot, and `apply_replication_snapshot_if_newer` upserts it under the primary's id in one
transaction: a conditional `UPDATE` that only applies when the snapshot's `modified` is at least
the row's (so a newer row is never overwritten), or an insert with the primary's `created` and
`modified`, then the snapshot's `notification_version` as the row's token. Datetimes are compared
and stored as naive UTC, so a snapshot whose `modified` carries another offset is still ordered by
its instant. A row newer than the snapshot is declined as `superseded`, and the service replays
nothing over it. The insert runs in a savepoint: if a concurrent apply inserts the same id first,
the conditional `UPDATE` runs again against that row, newer-wins as before.

A replica that declines a snapshot for any other reason has the write replayed through its own
methods instead, with no token; the service then checks the row against the snapshot and, only
when they match, stamps it with `persist_notification_update(id, {}, notification_version=...)`.
That token-only write is accepted in any status, `SENT` and `READ` included, and changes nothing
but the token (not `updated`); an update that changes anything else is still refused once a
notification is delivered. If the replay or the stamp fails, the replica keeps a token of its own,
which the primary refuses. Either way `get_backend_supported_action_capabilities("replica")`
reports `versions.filter` and `writes.notificationVersion`.

A replica lags its primary. A version listed from it before the latest write reached it is
refused with reason `CHANGED`, and nothing is written; list again and retry.

Snapshot apply copies attachment **metadata** only: each file is matched on the replica by
`(checksum, size)` or recorded from the snapshot's `storage_identifiers`, and join rows are added
and removed to match. The bytes are never read or copied, so both backends must use the same
attachment storage (the same attachment manager configuration, e.g. one bucket or one shared
`VINTASEND_ATTACHMENTS_PATH`).

A snapshot whose id (or user id) this backend's column cannot hold, such as a UUID string for the
integer primary key, is declined and nothing is written, so primary and replica should share an id
type. Both databases need the same migrations, and the replica's `users` table must hold the users
its rows point at where foreign keys are enforced.

Any other database error while applying a snapshot (a foreign key, a `NOT NULL`, a lost
connection) is raised as `SnapshotApplyDatabaseError`, a `NotificationError` whose message names
the notification id and the error class only, with SQLAlchemy's original neither chained nor kept
as context: its text lists the statement's parameters, which hold titles, recipients and contexts.

## Backends

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vintasend.services.notification_service import NotificationService
from vintasend_sqlalchemy.services.notification_backends.sqlalchemy_notification_backend import (
    SQLAlchemyNotificationBackend,
)

from myapp.models import Notification  # your GenericNotification subclass

engine = create_engine("postgresql://...", hide_parameters=True)
Session = sessionmaker(bind=engine)

backend = SQLAlchemyNotificationBackend(Session, Notification)
service = NotificationService(notification_backend=backend, notification_adapters=[...])
```

The AsyncIO backend takes an `async_sessionmaker` instead:

```python
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from vintasend_sqlalchemy.services.notification_backends.sqlalchemy_notification_backend import (
    SQLAlchemyAsyncIONotificationBackend,
)

engine = create_async_engine("postgresql+asyncpg://...", hide_parameters=True)
Session = async_sessionmaker(bind=engine)
backend = SQLAlchemyAsyncIONotificationBackend(Session, Notification)
```

Create every engine a backend uses, primary and replicas alike, with `hide_parameters=True`.
Without it, a database error from any write renders the statement's bound parameters into the
exception text (`[parameters: ...]`), and a notification's parameters are its title, recipient
and context, which then reach whatever logs the exception. Only snapshot apply wraps its errors
itself; every other write raises SQLAlchemy's error as is.

## Attachments

Each backend defaults to a `FilesystemAttachmentManager` (async: `FilesystemAsyncIOAttachmentManager`)
that stores uploaded files under a base directory (`VINTASEND_ATTACHMENTS_PATH`, default
`vintasend_attachments/`). Configure a different manager on the service to store bytes elsewhere;
the backend only persists the `attachment_file_records` / `notification_attachments` rows.

## Migrations

The package ships Alembic helper ops so your migrations stay in sync with the models:

- `create_notification_table(user_id_type)` - the base `notifications` table.
- `upgrade_notification_table_to_2_0()` - adds the 2.0 columns (one-off recipient fields,
  `sent_at`, `read_at`, `tenant`, `git_commit_sha`) and relaxes `user_id` to nullable.
- `upgrade_notification_table_to_2_1()` - adds the 2.1 template-version columns
  (`requested_template_version`, `used_template_version`). Both nullable, no backfill: a
  notification written before them was rendered against whatever its template said at the time,
  and there is no honest value to invent for it.
- `upgrade_notification_table_to_3_8(batch_size=1000, *, not_null=True)` - adds the 3.8
  `notification_version` column. It adds the column as nullable (unless it is already there),
  gives every row without a token its own UUIDv7, then makes it `NOT NULL`. The backfill reads
  `id` only, `batch_size` rows at a time, and touches only rows that have no token yet, so a run
  that failed part-way can be repeated. It needs a live connection and refuses Alembic's offline
  (`--sql`) mode.
- `finish_notification_table_upgrade_to_3_8(batch_size=1000)` - the second half, for a rolling
  deploy: it backfills rows still without a token, then makes the column `NOT NULL`.

The models declare `notification_version` as `NOT NULL`, and code from before 3.8 does not know
the column. So with every writer stopped, run `upgrade_notification_table_to_3_8()` and then
deploy. In a rolling deploy, where instances of the previous release keep inserting while the new
code rolls out, run `upgrade_notification_table_to_3_8(not_null=False)` first, deploy, and run
`finish_notification_table_upgrade_to_3_8()` in a later revision once no old instance is left;
it also backfills the rows they inserted in between. Until then, `filter_notification_versions`
lists such a row with the version `""`, and a conditional move of it is refused with
`CHANGED`.
- `create_attachment_tables()` - the `attachment_file_records` and `notification_attachments`
  tables backing the attachment seam.

Each has a matching `downgrade_notification_table_from_*` / `drop_attachment_tables`.
`downgrade_notification_table_from_3_8()` drops `notification_version`; the tokens are not
needed by anything older.

See `migrations/versions/` for the reference migrations used by the test suite.
