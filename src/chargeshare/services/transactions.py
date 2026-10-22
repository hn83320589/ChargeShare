"""充電交易的開始、讀數、結束與計價。所有操作皆為冪等。"""

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from chargeshare.db.models import Card, MeterSample, Transaction
from chargeshare.domain.billing import price
from chargeshare.domain.metering import Reading, count_meter_resets, split_energy
from chargeshare.domain.tariff import Tariff

logger = logging.getLogger(__name__)


def find_or_start_transaction(
    session: Session, charger_id: str, id_tag: str, meter_start: int, start_ts: datetime
) -> tuple[Transaction, bool]:
    """回傳 (交易, 是否新建立)。同一支樁同一開始時間視為同一筆交易。"""
    existing = session.scalar(
        select(Transaction).where(
            Transaction.charger_id == charger_id, Transaction.start_ts == start_ts
        )
    )
    if existing is not None:
        return existing, False

    card = session.get(Card, id_tag)
    if card is None:
        raise ValueError(f"card {id_tag!r} must be authorized before starting a transaction")
    tx = Transaction(
        charger_id=charger_id,
        household_id=card.household_id,
        id_tag=id_tag,
        start_ts=start_ts,
        meter_start_wh=meter_start,
    )
    session.add(tx)
    session.flush()
    return tx, True


def record_meter_value(
    session: Session, tariff: Tariff, tx: Transaction, ts: datetime, energy_wh: int, power_w: int
) -> MeterSample | None:
    """寫入一筆讀數；重複或超出交易區間的讀數回傳 None。"""
    if ts <= tx.start_ts or (tx.stop_ts is not None and ts >= tx.stop_ts):
        logger.info("ignore reading at %s outside transaction %s", ts.isoformat(), tx.id)
        return None
    duplicate = session.scalar(
        select(MeterSample.id).where(MeterSample.transaction_id == tx.id, MeterSample.ts == ts)
    )
    if duplicate is not None:
        return None
    previous = _reading_before(session, tx, ts)
    if energy_wh < previous:
        tx.meter_resets += 1
        logger.warning(
            "transaction %s: meter reset on %s (%s Wh → %s Wh at %s), needs review",
            tx.id,
            tx.charger_id,
            previous,
            energy_wh,
            ts.isoformat(),
        )
    sample = MeterSample(transaction=tx, ts=ts, energy_wh=energy_wh, power_w=power_w)
    session.add(sample)
    session.flush()
    if not tx.is_active:
        # 結束後才補到的讀數：重新分攤時段
        _apply_charge(tx, tariff)
    return sample


def stop_transaction(
    session: Session,
    tariff: Tariff,
    tx: Transaction,
    meter_stop: int,
    stop_ts: datetime,
    reason: str | None,
) -> bool:
    """結束交易並計價；已結束的交易不再變更，回傳是否為本次結束。

    meterStop 小於先前讀數時視為電表重置，照樣收下並分段累加計費，
    不可回錯誤：CALLERROR 會讓樁無限重送，交易永遠無法結束。
    """
    if not tx.is_active:
        return False
    if stop_ts < tx.start_ts:
        raise ValueError(
            f"transaction {tx.id}: stop time {stop_ts.isoformat()} "
            f"precedes start {tx.start_ts.isoformat()}"
        )
    tx.stop_ts = stop_ts
    tx.meter_stop_wh = meter_stop
    tx.stop_reason = reason
    session.flush()
    _apply_charge(tx, tariff)
    return True


def _apply_charge(tx: Transaction, tariff: Tariff) -> None:
    readings = [Reading(tx.start_ts, tx.meter_start_wh)]
    readings += [Reading(s.ts, s.energy_wh) for s in tx.samples if tx.start_ts < s.ts < tx.stop_ts]
    readings.append(Reading(tx.stop_ts, tx.meter_stop_wh))
    resets = count_meter_resets(readings)
    if resets > tx.meter_resets:
        logger.warning("transaction %s: %s meter reset(s), needs review", tx.id, resets)
    tx.meter_resets = resets
    charge = price(split_energy(readings, tariff), tariff)
    tx.peak_wh = charge.peak_wh
    tx.offpeak_wh = charge.offpeak_wh
    tx.amount = charge.amount


def _reading_before(session: Session, tx: Transaction, ts: datetime) -> int:
    """ts 之前最近一筆讀數；沒有讀數時為 meterStart。"""
    energy = session.scalar(
        select(MeterSample.energy_wh)
        .where(MeterSample.transaction_id == tx.id, MeterSample.ts < ts)
        .order_by(MeterSample.ts.desc())
        .limit(1)
    )
    return tx.meter_start_wh if energy is None else energy


def confirm_meter_reset(session: Session, transaction_id: int, now: datetime) -> Transaction:
    tx = session.get(Transaction, transaction_id)
    if tx is None:
        raise LookupError(f"transaction {transaction_id} not found")
    if tx.meter_resets == 0:
        raise ValueError(f"transaction {transaction_id} has no meter reset to confirm")
    tx.reset_reviewed_at = now
    session.flush()
    return tx
