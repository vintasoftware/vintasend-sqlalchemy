import os
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from alembic.config import Config
from alembic import command

@pytest.fixture(scope="session")
def db_engine():
    # Set the database URL
    db_url = os.environ.get("TEST_DATABASE_URL", "sqlite:///test.db")
    engine = create_engine(db_url)
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
    os.remove("test.db")