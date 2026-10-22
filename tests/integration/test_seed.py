from sqlalchemy import func, select

from chargeshare.db.models import (
    Bill,
    BillingRun,
    Card,
    Charger,
    Household,
    PowerSnapshot,
    Transaction,
)
from chargeshare.domain.auth_rules import AuthStatus
from chargeshare.seed import DISABLED_DEMO_CARD, OVERDUE_DEMO_UNITS, card_id, seed_all
from chargeshare.services.authorization import authorize_id_tag
from chargeshare.settings import Settings
from tests.conftest import tw

NOW = tw(2026, 10, 22, 21, 30)


def test_seed_creates_two_months_of_consistent_history(session):
    assert seed_all(session, NOW, Settings().config_dir, hasher=lambda p: "x")
    count = lambda model: session.scalar(select(func.count()).select_from(model))  # noqa: E731

    assert count(Household) == 120
    assert count(Charger) == 8
    assert count(Card) > 120  # 部分住戶有 2 張卡
    assert count(Transaction) > 300
    first = session.scalar(select(func.min(Transaction.start_ts)))
    assert (NOW - first).days >= 60
    unpriced = session.scalar(select(func.count()).where(Transaction.amount.is_(None)))
    assert unpriced == 0
    assert set(session.scalars(select(BillingRun.period))) == {"2026-07", "2026-08", "2026-09"}

    # 範例資料以實際分配演算法產生，最近 1 小時曲線不超過 30 kW
    snapshots = session.scalars(select(PowerSnapshot)).all()
    assert len(snapshots) == 60
    assert max(s.limit_w for s in snapshots) <= 30_000
    assert all(s.measured_w <= s.limit_w for s in snapshots)
    # 晚間 20:30–21:30 是充電尖峰，曲線不應該是空的
    assert max(s.measured_w for s in snapshots) > 0
    today = session.scalar(
        select(func.count()).where(Transaction.start_ts >= tw(2026, 10, 22, 0, 0))
    )
    assert today > 0

    # 重複執行不會重建
    assert not seed_all(session, NOW, Settings().config_dir, hasher=lambda p: "x")


def test_seed_includes_authorization_demos(session):
    seed_all(session, NOW, Settings().config_dir, hasher=lambda p: "x")
    later = tw(2026, 10, 22, 22, 0)
    blocked_unit = OVERDUE_DEMO_UNITS[0]
    floor, unit = (int(x) for x in blocked_unit.split("-"))
    assert authorize_id_tag(session, card_id(floor, unit, 1), later).status == AuthStatus.BLOCKED
    assert authorize_id_tag(session, DISABLED_DEMO_CARD, later).status == AuthStatus.BLOCKED
    assert authorize_id_tag(session, card_id(1, 1, 1), later).status == AuthStatus.ACCEPTED
    unpaid = session.scalars(select(Bill).where(Bill.paid_at.is_(None))).all()
    assert {b.household.unit_no for b in unpaid if b.period == "2026-07"} == set(OVERDUE_DEMO_UNITS)


def test_store_handles_session_with_single_reading(session, tariff):
    # 在「現在」前 1 分鐘才開始的充電只有結束讀數，沒有中間讀數可批次寫入
    from chargeshare.seed import _Session, _store, seed_base

    seed_base(session, hasher=lambda p: "x")
    household = session.scalars(select(Household)).first()
    start = tw(2026, 10, 22, 21, 29)
    one_minute = _Session(household.id, "RFID-0101-1", "CP01", start, 5_000, 1_000.0)
    one_minute.samples.append((tw(2026, 10, 22, 21, 30), 1_116, 7_000))
    one_minute.stop = tw(2026, 10, 22, 21, 30)
    _store(session, tariff, [one_minute])
    tx = session.scalars(select(Transaction)).one()
    assert tx.peak_wh + tx.offpeak_wh == 116
