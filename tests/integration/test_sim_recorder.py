from datetime import datetime

from chargeshare.clock import TAIPEI
from chargeshare.db.models import Charger, Household, Transaction
from chargeshare.simulator.recorder import SessionRecord, verify


def test_verify_reports_energy_mismatch_and_missing(session):
    household = Household(unit_no="01-01", password_hash="x")
    start = datetime(2026, 10, 22, 18, 0, tzinfo=TAIPEI)
    tx = Transaction(
        charger=Charger(id="CP01"),
        household=household,
        id_tag="T",
        start_ts=start,
        meter_start_wh=100,
        stop_ts=start,
        meter_stop_wh=600,
        peak_wh=500,
        offpeak_wh=0,
    )
    session.add(tx)
    session.flush()
    ok = SessionRecord(
        "CP01", tx.id, "T", start.isoformat(), 100, start.isoformat(), 600, reported_wh=500
    )
    wrong = SessionRecord(
        "CP01", tx.id, "T", start.isoformat(), 100, start.isoformat(), 700, reported_wh=600
    )
    missing = SessionRecord("CP02", 999, "T", start.isoformat(), 0, start.isoformat(), 1)

    assert verify([ok], session) == []
    problems = verify([wrong, missing], session)
    assert "simulator 600 Wh, server billed 500 Wh" in problems[0]
    assert "not found" in problems[1]
