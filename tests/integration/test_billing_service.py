from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from chargeshare.db.models import Bill, Card, Charger, Household
from chargeshare.domain.auth_rules import AuthStatus
from chargeshare.services import transactions
from chargeshare.services.authorization import authorize_id_tag
from chargeshare.services.billing import (
    BillingAlreadyRun,
    ensure_monthly_bills,
    generate_monthly_bills,
    period_of_previous_month,
    set_bill_paid,
)
from tests.conftest import tw


@pytest.fixture
def households(session):
    a = Household(unit_no="A01-01", password_hash="x")
    a.cards = [Card(id_tag="CARD-A")]
    b = Household(unit_no="A01-02", password_hash="x")
    b.cards = [Card(id_tag="CARD-B")]
    session.add_all([a, b, Charger(id="CP01"), Charger(id="CP02")])
    session.flush()
    return a, b


def charge(session, tariff, id_tag, start: datetime, hours: float, power_w: int, stop=True):
    """以每分鐘一筆讀數建立一筆定功率充電。"""
    tx, _ = transactions.find_or_start_transaction(session, "CP01", id_tag, 0, start)
    minutes = int(hours * 60)
    for minute in range(1, minutes):
        transactions.record_meter_value(
            session,
            tariff,
            tx,
            start + timedelta(minutes=minute),
            round(power_w * minute / 60),
            power_w,
        )
    if stop:
        energy = round(power_w * hours)
        transactions.stop_transaction(
            session, tariff, tx, energy, start + timedelta(minutes=minutes), "Local"
        )
    return tx


def test_monthly_bill_matches_hand_calculation(session, tariff, households):
    # 驗收 11：
    #   第 1 筆 10/23（五）23:00 → 10/24（六）07:00，7 kW：尖峰 7 度、離峰 49 度 → 154.70
    #   第 2 筆 10/22（四）10:00 → 12:00，3.5 kW：尖峰 3.5 度、離峰 3.5 度 → 26.95
    #   合計 181.65 → 182 元
    a, _ = households
    charge(session, tariff, "CARD-A", tw(2026, 10, 23, 23, 0), 8, 7000)
    charge(session, tariff, "CARD-A", tw(2026, 10, 22, 10, 0), 2, 3500)

    bills = generate_monthly_bills(session, "2026-10", now=tw(2026, 11, 1, 0, 5))

    assert len(bills) == 1
    bill = bills[0]
    assert bill.household_id == a.id
    assert (bill.peak_wh, bill.offpeak_wh) == (10_500, 52_500)
    assert bill.total_amount == 182
    assert [tx.amount for tx in bill.transactions] == [Decimal("26.95"), Decimal("154.70")]


def test_only_finished_transactions_of_the_period_are_billed(session, tariff, households):
    charge(session, tariff, "CARD-A", tw(2026, 10, 31, 23, 0), 2, 7000)  # 結束於 11/1
    charge(session, tariff, "CARD-B", tw(2026, 10, 30, 20, 0), 2, 7000, stop=False)  # 進行中
    assert generate_monthly_bills(session, "2026-10", now=tw(2026, 11, 1, 2, 0)) == []


def test_billing_runs_once_per_period(session, tariff, households):
    charge(session, tariff, "CARD-A", tw(2026, 10, 22, 10, 0), 2, 3500)
    generate_monthly_bills(session, "2026-10", now=tw(2026, 11, 1, 0, 5))
    with pytest.raises(BillingAlreadyRun):
        generate_monthly_bills(session, "2026-10", now=tw(2026, 11, 1, 1, 5))
    assert len(session.scalars(select(Bill)).all()) == 1


def test_late_transaction_rolls_into_next_bill(session, tariff, households):
    _, b = households
    generate_monthly_bills(session, "2026-10", now=tw(2026, 11, 1, 0, 5))
    # 斷線樁在 11/1 之後才補送 10 月的 StopTransaction
    late = charge(session, tariff, "CARD-B", tw(2026, 10, 31, 20, 0), 1, 7000)
    bills = generate_monthly_bills(session, "2026-11", now=tw(2026, 12, 1, 0, 5))
    assert [bill.household_id for bill in bills] == [b.id]
    assert bills[0].transactions == [late]


def test_ensure_monthly_bills_generates_previous_month_once(session, tariff, households):
    charge(session, tariff, "CARD-A", tw(2026, 10, 22, 10, 0), 2, 3500)
    assert period_of_previous_month(tw(2026, 11, 1, 0, 5)) == "2026-10"
    assert period_of_previous_month(tw(2026, 1, 15, 9, 0)) == "2025-12"
    assert len(ensure_monthly_bills(session, now=tw(2026, 11, 1, 0, 5))) == 1
    assert ensure_monthly_bills(session, now=tw(2026, 11, 1, 1, 5)) == []


def test_marking_bill_paid_lifts_authorization_block(session, tariff, households):
    charge(session, tariff, "CARD-A", tw(2026, 8, 20, 10, 0), 2, 3500)
    [bill] = generate_monthly_bills(session, "2026-08", now=tw(2026, 9, 1, 0, 5))
    now = tw(2026, 11, 2, 18, 0)
    assert authorize_id_tag(session, "CARD-A", now).status == AuthStatus.BLOCKED

    set_bill_paid(session, bill.id, paid=True, now=now)
    assert bill.paid_at == now
    assert authorize_id_tag(session, "CARD-A", now).status == AuthStatus.ACCEPTED

    set_bill_paid(session, bill.id, paid=False, now=now)
    assert bill.paid_at is None
