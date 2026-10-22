"""範例資料：120 戶（15 層 × 8 戶）、每戶 1–2 張卡、8 支充電樁、過去 2 個月的充電紀錄與帳單。"""

import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from chargeshare.clock import TAIPEI
from chargeshare.config import load_tariff
from chargeshare.db.models import Card, Charger, Household, MeterSample, PowerSnapshot, Transaction
from chargeshare.domain.allocation import ChargerInput, PowerConfig, allocate
from chargeshare.domain.billing import price
from chargeshare.domain.metering import Reading, split_energy
from chargeshare.domain.tariff import Tariff
from chargeshare.security import hash_password
from chargeshare.services.billing import generate_monthly_bills, period_end

FLOORS, UNITS_PER_FLOOR = 15, 8
CHARGER_IDS = [f"CP{i:02d}" for i in range(1, 9)]


def unit_no(floor: int, unit: int) -> str:
    return f"{floor:02d}-{unit:02d}"


def card_id(floor: int, unit: int, index: int) -> str:
    return f"RFID-{floor:02d}{unit:02d}-{index}"


def demo_password(unit: str) -> str:
    """範例資料的住戶密碼（實際由管委會發放）。"""
    return f"pw-{unit}"


def seed_base(session: Session, hasher: Callable[[str], str] = hash_password) -> bool:
    """建立戶、卡片與充電樁；已有資料時不做事，回傳是否有建立。

    hasher 可替換成低成本版本，供測試快速建立資料。
    """
    if session.scalar(select(Household.id).limit(1)) is not None:
        return False
    for floor in range(1, FLOORS + 1):
        for unit in range(1, UNITS_PER_FLOOR + 1):
            number = unit_no(floor, unit)
            household = Household(unit_no=number, password_hash=hasher(demo_password(number)))
            cards = 2 if unit % 3 == 0 else 1
            household.cards = [Card(id_tag=card_id(floor, unit, i)) for i in range(1, cards + 1)]
            session.add(household)
    session.add_all(Charger(id=charger_id) for charger_id in CHARGER_IDS)
    session.flush()
    return True


# ── 過去 2 個月的充電紀錄 ────────────────────────────────────────────

HISTORY_DAYS = 60
# 示範「有超過 2 個月未繳帳單」：這兩戶有一張 7 月帳單未繳
OVERDUE_DEMO_UNITS = ("15-07", "15-08")
# 示範「卡片停用」
DISABLED_DEMO_CARD = card_id(14, 8, 1)


@dataclass
class _Session:
    household_id: int
    id_tag: str
    charger_id: str
    start: datetime
    need_wh: float
    meter_start: float
    delivered_wh: float = 0.0
    samples: list[tuple[datetime, int, int]] = field(default_factory=list)
    stop: datetime | None = None


def seed_all(
    session: Session,
    now: datetime,
    config_dir: Path,
    hasher: Callable[[str], str] = hash_password,
    rng_seed: int = 2026,
) -> bool:
    """基本資料 + 過去 2 個月充電紀錄 + 已結算的月結帳單；已有資料時不做事。"""
    if not seed_base(session, hasher):
        return False
    tariff = load_tariff(config_dir / "tariff.toml", *sorted(config_dir.glob("holidays_*.toml")))
    rng = random.Random(rng_seed)
    households = {h.unit_no: h for h in session.scalars(select(Household))}
    session.get(Card, DISABLED_DEMO_CARD).active = False

    end = now.astimezone(TAIPEI).replace(second=0, microsecond=0)
    start = (end - timedelta(days=HISTORY_DAYS)).replace(hour=0, minute=0)
    finished, snapshots = _simulate(households, start, end, rng)
    finished += _overdue_demo_sessions(households, start)
    _store(session, tariff, finished)
    session.add_all(snapshots)

    first_period = min(s.stop for s in finished).strftime("%Y-%m")
    period = first_period
    while period_end(period) <= now:
        for bill in generate_monthly_bills(session, period, now=period_end(period)):
            # 約 15% 帳單尚未繳費；示範逾期的兩戶一律未繳
            if bill.household.unit_no not in OVERDUE_DEMO_UNITS and rng.random() > 0.15:
                bill.paid_at = bill.issued_at + timedelta(days=rng.randint(3, 20))
                if bill.paid_at > now:
                    bill.paid_at = None
        period = period_end(period).strftime("%Y-%m")
    session.flush()
    return True


def _arrivals(day: datetime, rng: random.Random) -> list[datetime]:
    """一天的到達時間：晚間下班尖峰最多。"""
    times = []
    for _ in range(rng.randint(8, 14)):
        roll = rng.random()
        if roll < 0.6:
            minute = rng.randint(18 * 60, 22 * 60)
        elif roll < 0.85:
            minute = rng.randint(7 * 60, 17 * 60)
        else:
            minute = rng.randint(22 * 60, 24 * 60 - 1)
        times.append(day + timedelta(minutes=minute))
    return sorted(times)


def _simulate(
    households: dict[str, Household], start: datetime, end: datetime, rng: random.Random
) -> tuple[list[_Session], list[PowerSnapshot]]:
    """以每分鐘為步長，用實際的分配演算法模擬 8 支樁。"""
    power = PowerConfig()
    eligible = [h for unit, h in households.items() if unit not in OVERDUE_DEMO_UNITS]
    days = (end.date() - start.date()).days + 1  # 含今天
    arrivals = [
        t for day in range(days) for t in _arrivals(start + timedelta(days=day), rng) if t < end
    ]
    meters = {cid: rng.uniform(100_000, 900_000) for cid in CHARGER_IDS}
    active: dict[str, _Session] = {}
    finished: list[_Session] = []
    snapshots: list[PowerSnapshot] = []
    step = timedelta(minutes=1)
    t = start
    while t < end:
        while arrivals and arrivals[0] <= t:
            arrivals.pop(0)
            free = [cid for cid in CHARGER_IDS if cid not in active]
            if not free:
                continue  # 樁都被佔用，住戶改天再來
            household = rng.choice(eligible)
            charger_id = rng.choice(free)
            active[charger_id] = _Session(
                household.id,
                household.cards[0].id_tag,
                charger_id,
                t,
                rng.uniform(6_000, 25_000),
                meters[charger_id],
            )
        inputs = [
            ChargerInput(cid, True, True, s.start, _demand_cap(s), 0) for cid, s in active.items()
        ]
        limits = allocate(inputs, power).limits
        t_next = t + step
        measured = 0
        for charger_id, s in list(active.items()):
            power_w = min(limits[charger_id], _acceptance(s))
            measured += power_w
            s.delivered_wh += power_w / 60
            meters[charger_id] += power_w / 60
            s.samples.append((t_next, int(meters[charger_id]), round(power_w)))
            if s.delivered_wh >= s.need_wh or t_next >= end:
                s.stop = t_next
                finished.append(active.pop(charger_id))
        if end - t <= timedelta(hours=1):
            snapshots.append(
                PowerSnapshot(
                    ts=t_next,
                    measured_w=round(measured),
                    limit_w=sum(limits.values()),
                    reserved_w=0,
                )
            )
        t = t_next
    return finished, snapshots


def _acceptance(s: _Session) -> float:
    remaining = s.need_wh - s.delivered_wh
    return 7_000 if remaining > 3_000 else max(700.0, 7_000 * remaining / 3_000)


def _demand_cap(s: _Session) -> int:
    accept = _acceptance(s)
    return 7_000 if accept >= 7_000 else int(accept) + 500


def _overdue_demo_sessions(households: dict[str, Household], start: datetime) -> list[_Session]:
    july = (start.replace(day=1) - timedelta(days=1)).replace(day=10, hour=20, minute=0)
    sessions = []
    for index, unit in enumerate(OVERDUE_DEMO_UNITS):
        household = households[unit]
        begin = july + timedelta(days=index)
        s = _Session(
            household.id, household.cards[0].id_tag, "CP08", begin, 14_000, 5_000.0 * index
        )
        for minute in range(1, 121):
            s.samples.append(
                (begin + timedelta(minutes=minute), int(s.meter_start + 7_000 * minute / 60), 7_000)
            )
        s.stop = begin + timedelta(minutes=120)
        sessions.append(s)
    return sessions


def _store(session: Session, tariff: Tariff, sessions: list[_Session]) -> None:
    for s in sessions:
        meter_stop = s.samples[-1][1]
        readings = [Reading(s.start, int(s.meter_start))]
        readings += [Reading(ts, energy) for ts, energy, _ in s.samples[:-1]]
        readings.append(Reading(s.stop, meter_stop))
        charge = price(split_energy(readings, tariff), tariff)
        tx = Transaction(
            charger_id=s.charger_id,
            household_id=s.household_id,
            id_tag=s.id_tag,
            start_ts=s.start,
            meter_start_wh=int(s.meter_start),
            stop_ts=s.stop,
            meter_stop_wh=meter_stop,
            stop_reason="EVDisconnected",
            peak_wh=charge.peak_wh,
            offpeak_wh=charge.offpeak_wh,
            amount=charge.amount,
        )
        session.add(tx)
        session.flush()
        session.execute(
            insert(MeterSample),
            [
                {"transaction_id": tx.id, "ts": ts, "energy_wh": energy, "power_w": power_w}
                for ts, energy, power_w in s.samples[:-1]
            ],
        )
