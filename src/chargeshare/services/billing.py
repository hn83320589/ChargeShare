"""月結帳單：每月 1 日以戶為單位，結算所有已結束且尚未入帳的充電。"""

import logging
from collections import defaultdict
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from chargeshare.clock import TAIPEI
from chargeshare.db.models import Bill, BillingRun, Transaction
from chargeshare.domain.billing import bill_total

logger = logging.getLogger(__name__)


class BillingAlreadyRun(Exception):
    pass


def period_of_previous_month(now: datetime) -> str:
    local = now.astimezone(TAIPEI)
    year, month = (local.year, local.month - 1) if local.month > 1 else (local.year - 1, 12)
    return f"{year:04d}-{month:02d}"


def period_end(period: str) -> datetime:
    year, month = (int(part) for part in period.split("-"))
    year, month = (year, month + 1) if month < 12 else (year + 1, 1)
    return datetime(year, month, 1, tzinfo=TAIPEI)


def generate_monthly_bills(session: Session, period: str, now: datetime) -> list[Bill]:
    if session.get(BillingRun, period) is not None:
        raise BillingAlreadyRun(f"bills for {period} were already generated")
    if now < period_end(period):
        raise ValueError(f"period {period} has not ended yet at {now.isoformat()}")

    unbilled = session.scalars(
        select(Transaction)
        .where(
            Transaction.bill_id.is_(None),
            Transaction.stop_ts.is_not(None),
            Transaction.stop_ts < period_end(period),
        )
        .order_by(Transaction.start_ts)
    )
    by_household: dict[int, list[Transaction]] = defaultdict(list)
    for tx in unbilled:
        if tx.amount is None:
            raise ValueError(f"transaction {tx.id} has stopped but was never priced")
        by_household[tx.household_id].append(tx)

    bills = []
    for household_id, txs in sorted(by_household.items()):
        bill = Bill(
            household_id=household_id,
            period=period,
            issued_at=now,
            peak_wh=sum(tx.peak_wh for tx in txs),
            offpeak_wh=sum(tx.offpeak_wh for tx in txs),
            total_amount=bill_total(tx.amount for tx in txs),
            transactions=txs,
        )
        session.add(bill)
        bills.append(bill)
    session.add(BillingRun(period=period, ran_at=now))
    session.flush()
    logger.info("generated %d bills for %s", len(bills), period)
    return bills


def ensure_monthly_bills(session: Session, now: datetime) -> list[Bill]:
    """排程呼叫：上個月尚未結算就結算，已結算則不做事。"""
    period = period_of_previous_month(now)
    if session.get(BillingRun, period) is not None:
        return []
    return generate_monthly_bills(session, period, now)


def set_bill_paid(session: Session, bill_id: int, paid: bool, now: datetime) -> Bill:
    bill = session.get(Bill, bill_id)
    if bill is None:
        raise LookupError(f"bill {bill_id} not found")
    if paid and bill.paid_at is None:
        bill.paid_at = now
    elif not paid:
        bill.paid_at = None
    session.flush()
    return bill
