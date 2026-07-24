# VintaSend SQLAlchemy

SQLAlchemy backend implementation for [VintaSend](https://github.com/vintasoftware/vintasend).

Provides a synchronous (`SQLAlchemyNotificationBackend`) and an AsyncIO
(`SQLAlchemyAsyncIONotificationBackend`) notification backend, plus a filesystem-backed
attachment manager.

## Compatibility

- Python 3.10 - 3.14
- SQLAlchemy 2.0+
- vintasend 2.0+

This release implements the full vintasend 2.0 backend contract: one-off notifications, the
composable filtering / ordering API (`filter_notifications`), `sent_at` / `read_at`, the `tenant`
partition key, `git_commit_sha`, bulk read, and the attachment manager seam.

## Backends

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vintasend.services.notification_service import NotificationService
from vintasend_sqlalchemy.services.notification_backends.sqlalchemy_notification_backend import (
    SQLAlchemyNotificationBackend,
)

from myapp.models import Notification  # your GenericNotification subclass

engine = create_engine("postgresql://...")
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

engine = create_async_engine("postgresql+asyncpg://...")
Session = async_sessionmaker(bind=engine)
backend = SQLAlchemyAsyncIONotificationBackend(Session, Notification)
```

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
- `create_attachment_tables()` - the `attachment_file_records` and `notification_attachments`
  tables backing the attachment seam.

See `migrations/versions/` for the reference migrations used by the test suite.
