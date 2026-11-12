"""The 3.8 ``notification_version`` migration: the reference migration in ``migrations/versions``
and the ``upgrade_notification_table_to_3_8`` helper it calls, each against a database of its own
so the suite's shared database is never migrated down."""

import contextlib
import uuid

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations

from vintasend_sqlalchemy.alembic_initial_migration_ops import (
    finish_notification_table_upgrade_to_3_8,
    upgrade_notification_table_to_3_8,
)
from vintasend_sqlalchemy.services.notification_backends.sqlalchemy_notification_backend import (
    _version_token,
)


BEFORE_3_8 = "3c1a2b4d5e6f"
NOTIFICATIONS = sa.table(
    "notifications",
    sa.column("id", sa.Integer()),
    sa.column("notification_version", sa.Uuid()),
)


def _config(url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    return config


@contextlib.contextmanager
def _database_before_3_8(tmp_path, rows: int):
    """A database migrated to just before 3.8, holding ``rows`` notifications."""
    url = f"sqlite:///{tmp_path / 'migration.db'}"
    config = _config(url)
    command.upgrade(config, BEFORE_3_8)
    engine = sa.create_engine(url)
    with engine.begin() as connection:
        connection.execute(sa.text("INSERT INTO users (email) VALUES ('someone@example.com')"))
        for _ in range(rows):
            connection.execute(
                sa.text(
                    "INSERT INTO notifications "
                    "(notification_type, title, status, body_template, user_id) "
                    "VALUES ('EMAIL', 'title', 'PENDING_SEND', 'template', 1)"
                )
            )
    try:
        yield config, engine
    finally:
        engine.dispose()


def _tokens(engine) -> list[uuid.UUID]:
    with engine.connect() as connection:
        return list(
            connection.execute(
                sa.select(NOTIFICATIONS.c.notification_version).order_by(NOTIFICATIONS.c.id)
            ).scalars()
        )


def _version_column(engine) -> dict | None:
    columns = sa.inspect(engine).get_columns("notifications")
    return next((dict(c) for c in columns if c["name"] == "notification_version"), None)


def test_upgrade_downgrade_upgrade(tmp_path):
    with _database_before_3_8(tmp_path, rows=5) as (config, engine):
        command.upgrade(config, "head")

        first = _tokens(engine)
        column = _version_column(engine)
        assert column is not None
        assert column["nullable"] is False
        assert len(first) == 5
        assert len(set(first)) == 5
        assert all(token.version == 7 for token in first)

        command.downgrade(config, BEFORE_3_8)
        engine.dispose()
        assert _version_column(engine) is None

        command.upgrade(config, "head")
        engine.dispose()
        second = _tokens(engine)
        assert len(set(second)) == 5
        assert not set(first) & set(second)
        upgraded_again = _version_column(engine)
        assert upgraded_again is not None
        assert upgraded_again["nullable"] is False


def test_the_backfill_reads_only_ids_in_batches(tmp_path):
    with _database_before_3_8(tmp_path, rows=5) as (_, engine):
        statements: list[str] = []

        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        sa.event.listen(engine, "before_cursor_execute", record)
        try:
            with engine.begin() as connection:
                with Operations.context(MigrationContext.configure(connection)):
                    upgrade_notification_table_to_3_8(batch_size=2)
        finally:
            sa.event.remove(engine, "before_cursor_execute", record)

        backfill_reads = [
            s for s in statements if s.startswith("SELECT") and "notification_version IS NULL" in s
        ]
        # Three batches (2 + 2 + 1), then one read that finds nothing left.
        assert len(backfill_reads) == 4
        for statement in backfill_reads:
            assert " ".join(statement.split("FROM")[0].split()) == "SELECT notifications.id"
        tokens = _tokens(engine)
        assert len(set(tokens)) == 5
        assert all(token is not None for token in tokens)


def _run(engine, operation, **kwargs) -> None:
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            operation(**kwargs)


def _insert_row_without_token(engine) -> None:
    """A row as an instance of the previous release writes it: no ``notification_version``."""
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO notifications "
                "(notification_type, title, status, body_template, user_id) "
                "VALUES ('EMAIL', 'title', 'PENDING_SEND', 'template', 1)"
            )
        )


def test_running_the_upgrade_again_keeps_existing_tokens(tmp_path):
    with _database_before_3_8(tmp_path, rows=3) as (_, engine):
        _run(engine, upgrade_notification_table_to_3_8, not_null=False)
        first = _tokens(engine)

        # As after a failure past ``add_column`` on a database without transactional DDL.
        _insert_row_without_token(engine)
        _run(engine, upgrade_notification_table_to_3_8)

        tokens = _tokens(engine)
        assert tokens[:3] == first
        assert tokens[3] is not None
        assert len(set(tokens)) == 4
        column = _version_column(engine)
        assert column is not None
        assert column["nullable"] is False


def test_a_rolling_deploy_finishes_in_a_later_revision(tmp_path):
    with _database_before_3_8(tmp_path, rows=2) as (_, engine):
        _run(engine, upgrade_notification_table_to_3_8, not_null=False)
        column = _version_column(engine)
        assert column is not None
        assert column["nullable"] is True
        backfilled = _tokens(engine)
        assert all(token is not None for token in backfilled)

        _insert_row_without_token(engine)
        _run(engine, finish_notification_table_upgrade_to_3_8)

        tokens = _tokens(engine)
        assert tokens[:2] == backfilled
        assert tokens[2] is not None
        column = _version_column(engine)
        assert column is not None
        assert column["nullable"] is False


def test_the_upgrade_refuses_offline_mode(tmp_path):
    with _database_before_3_8(tmp_path, rows=1) as (config, engine):
        with pytest.raises(RuntimeError, match="live database connection"):
            command.upgrade(config, f"{BEFORE_3_8}:head", sql=True)
        assert _version_column(engine) is None


def test_a_row_without_a_token_is_listed_with_an_empty_version():
    """Between the two halves of a rolling deploy, a row the previous release inserted has no
    token. It is listed with ``""``, which no row holds, so a conditional move of it is refused
    as changed (see the malformed-token tests in the conditional-moves suites)."""
    assert _version_token(None) == ""
    token = uuid.uuid4()
    assert _version_token(token) == str(token)
