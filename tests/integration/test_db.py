from datetime import UTC, datetime
from decimal import Decimal

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.exc import IntegrityError

from chargeshare.clock import TAIPEI
from chargeshare.db.migrate import upgrade_to_head
from chargeshare.db.models import Base, Charger, Household, MeterSample, Transaction
from chargeshare.db.session import create_db_engine


def test_migrations_match_models(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'migrated.db'}")
    upgrade_to_head(engine)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    engine.dispose()
    assert diff == []


def _transaction(session) -> Transaction:
    household = Household(unit_no="A01-01", password_hash="x")
    charger = Charger(id="CP01")
    tx = Transaction(
        charger=charger,
        household=household,
        id_tag="CARD-1",
        start_ts=datetime(2026, 10, 22, 18, 0, tzinfo=TAIPEI),
        meter_start_wh=1000,
    )
    session.add(tx)
    session.flush()
    return tx


def test_datetimes_round_trip_as_taipei_aware(session):
    tx = _transaction(session)
    tx.stop_ts = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)  # = 台灣 20:00
    session.commit()
    session.expire_all()
    assert tx.start_ts == datetime(2026, 10, 22, 18, 0, tzinfo=TAIPEI)
    assert tx.stop_ts.utcoffset().total_seconds() == 8 * 3600
    assert tx.stop_ts.hour == 20


def test_naive_datetime_is_rejected(session):
    tx = _transaction(session)
    tx.stop_ts = datetime(2026, 10, 22, 20, 0)
    with pytest.raises(Exception, match="timezone-aware"):
        session.commit()


def test_amount_round_trips_as_exact_decimal(session):
    tx = _transaction(session)
    tx.amount = Decimal("154.7032")
    session.commit()
    session.expire_all()
    assert tx.amount == Decimal("154.7032")


def test_duplicate_meter_sample_timestamp_is_rejected(session):
    tx = _transaction(session)
    ts = datetime(2026, 10, 22, 18, 1, tzinfo=TAIPEI)
    session.add(MeterSample(transaction=tx, ts=ts, energy_wh=1100, power_w=6000))
    session.flush()
    session.add(MeterSample(transaction=tx, ts=ts, energy_wh=1100, power_w=6000))
    with pytest.raises(IntegrityError):
        session.flush()
