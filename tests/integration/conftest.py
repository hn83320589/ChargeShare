from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from chargeshare.db.models import Base
from chargeshare.db.session import create_db_engine, make_session_factory


@pytest.fixture
def engine(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine) -> Iterator[Session]:
    factory = make_session_factory(engine)
    with factory() as session:
        yield session
