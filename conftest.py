import os
import pytest
import pytest_asyncio
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession, create_async_engine
from alembic.config import Config
from alembic import command

@pytest.fixture(scope="session")
def db_engine():
    # Set the database URL
    db_url = os.environ.get("TEST_DATABASE_URL", "sqlite:///test.db")
    engine = create_engine(db_url)
    return engine

@pytest.fixture(scope="session")
def async_db_engine():
    # Set the database URL
    db_url = os.environ.get("TEST_DATABASE_URL", "sqlite+aiosqlite:///test.db")
    engine = create_async_engine(db_url)
    return engine

@pytest.fixture(scope="session", autouse=True)
def db_session(db_engine) -> sessionmaker[Session]:
    return sessionmaker(bind=db_engine)

@pytest.fixture(scope="session", autouse=True)
def setup_db(db_engine):
    # Run Alembic migrations
    alembic_cfg = Config("alembic.ini")
    command.upgrade(alembic_cfg, "head")

    yield

    # Tear down the database after the tests
    db_engine.dispose()

@pytest_asyncio.fixture(scope="session", autouse=True)
@pytest.mark.asyncio
async def setup_async_db(async_db_engine):
    # Run Alembic migrations
    alembic_cfg = Config("alembic.ini")
    command.upgrade(alembic_cfg, "head")

    yield

    # Tear down the database after the tests
    await async_db_engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def async_db_session(async_db_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=async_db_engine)
