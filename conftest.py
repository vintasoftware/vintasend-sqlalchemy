import os

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from example_app.models import Notification as NotificationModel
from example_app.models import User


@pytest.fixture(scope="session")
def db_filename(worker_id):
    # Each xdist worker gets its own SQLite file so parallel workers never race on the same
    # database while running migrations or mutating rows. ``worker_id`` is "master" when the
    # suite runs without xdist.
    return f"test_{worker_id}.db"


@pytest.fixture(scope="session")
def db_engine(db_filename):
    db_url = os.environ.get("TEST_DATABASE_URL", f"sqlite:///{db_filename}")
    return create_engine(db_url)


@pytest.fixture(scope="session")
def async_db_engine(db_filename):
    db_url = os.environ.get("TEST_ASYNC_DATABASE_URL", f"sqlite+aiosqlite:///{db_filename}")
    return create_async_engine(db_url)


@pytest.fixture(scope="session", autouse=True)
def db_session(db_engine) -> sessionmaker[Session]:
    return sessionmaker(bind=db_engine)


@pytest.fixture(scope="session", autouse=True)
def async_db_session(async_db_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=async_db_engine)


@pytest.fixture(scope="session", autouse=True)
def setup_db(db_engine, db_filename):
    # Run Alembic migrations against this worker's database file. Pointing the Alembic config at
    # the same per-worker URL keeps migrations and the ORM engines on one file.
    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_filename}")
    command.upgrade(alembic_cfg, "head")

    yield

    db_engine.dispose()


@pytest_asyncio.fixture(scope="session", autouse=True, loop_scope="session")
async def setup_async_db(async_db_session):
    # The sync ``setup_db`` fixture already migrated this worker's shared file to head; the async
    # half only needs to clean up and dispose its engine.
    yield

    async with async_db_session.begin() as session:
        await session.execute(delete(User))
        await session.execute(delete(NotificationModel))
