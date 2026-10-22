from datetime import datetime, timedelta

import pytest

from chargeshare.clock import TAIPEI, FakeClock
from chargeshare.db.models import Charger, Household, Transaction
from chargeshare.domain.allocation import PowerConfig
from chargeshare.services.events import (
    ChargerConnected,
    ChargerDisconnected,
    MeterSampled,
    SessionStarted,
    SessionStopped,
)
from chargeshare.services.power_manager import PowerManager
from tests.integration.power_helpers import FakeLink, worst_case_peak

T0 = datetime(2026, 10, 22, 18, 0, tzinfo=TAIPEI)
IDS = [f"CP0{i}" for i in range(1, 9)]


@pytest.fixture
def clock():
    return FakeClock(T0)


@pytest.fixture
def link(clock):
    return FakeLink(clock)


def make_manager(engine, clock, link, power=None) -> PowerManager:
    from chargeshare.db.session import make_session_factory

    manager = PowerManager(make_session_factory(engine), clock, link, power or PowerConfig())
    manager.load()
    return manager


@pytest.fixture
def manager(engine, session, clock, link):
    session.add_all(Charger(id=cid) for cid in IDS)
    session.commit()
    return make_manager(engine, clock, link)


async def settle(clock: FakeClock, manager: PowerManager, rounds: int = 4) -> None:
    for _ in range(rounds):
        await clock.advance(11)
    assert manager.idle


async def start_sessions(manager, link, ids):
    for index, cid in enumerate(ids, start=1):
        link.online.add(cid)
        manager.publish(ChargerConnected(cid))
        manager.publish(SessionStarted(cid, index, T0 + timedelta(seconds=index)))


async def report(manager, clock, cid, power_w, tx_id=None, at=None):
    tx_id = tx_id or IDS.index(cid) + 1
    manager.publish(MeterSampled(cid, tx_id, at or clock.now(), power_w))


async def test_eight_sessions_get_equal_share(manager, link, clock):
    await start_sessions(manager, link, IDS)
    await settle(clock, manager)
    assert link.current() == {cid: 3_750 for cid in IDS}
    assert worst_case_peak(link.sends) <= 30_000


async def test_tapering_cars_release_power_with_decrease_first(manager, link, clock):
    # 驗收 4（服務層）：先降後升，過渡期間總和也不超過 30 kW
    await start_sessions(manager, link, IDS)
    await settle(clock, manager)
    mark = clock.now()
    for _ in range(2):
        await clock.advance(60)
        for cid in IDS:
            await report(manager, clock, cid, 800 if cid in ("CP01", "CP02") else 3_700)
    await settle(clock, manager)

    new_sends = link.sends_since(mark)
    decreases = [s for s in new_sends if s[1] in ("CP01", "CP02")]
    increases = [s for s in new_sends if s[1] not in ("CP01", "CP02")]
    assert {s[2] for s in decreases} == {1_500}
    assert {s[2] for s in increases} == {4_500}
    assert min(s[0] for s in increases) >= max(s[0] for s in decreases) + timedelta(seconds=10)
    assert worst_case_peak(link.sends) <= 30_000


async def test_small_power_fluctuation_sends_nothing(manager, link, clock):
    await start_sessions(manager, link, IDS)
    await settle(clock, manager)
    mark = clock.now()
    for minute in range(10):
        await clock.advance(60)
        for cid in IDS:
            await report(manager, clock, cid, 3_700 - (minute % 3) * 30)
    await settle(clock, manager)
    assert link.sends_since(mark) == []


async def test_stale_replayed_samples_do_not_affect_demand(manager, link, clock):
    await start_sessions(manager, link, IDS)
    await settle(clock, manager)
    await clock.advance(600)
    mark = clock.now()
    for minute in (1, 2, 3):  # 斷線期間的舊讀數補傳進來
        await report(manager, clock, "CP01", 200, at=T0 + timedelta(minutes=minute))
    await settle(clock, manager)
    assert link.sends_since(mark) == []


async def test_disconnected_charger_keeps_reservation_after_five_minutes(manager, link, clock):
    # 驗收 9
    await start_sessions(manager, link, IDS)
    await settle(clock, manager)
    mark = clock.now()
    link.online.discard("CP01")
    manager.publish(ChargerDisconnected("CP01"))
    for _ in range(6):
        await clock.advance(60)
        for cid in IDS[1:]:
            await report(manager, clock, cid, 3_700)
    await settle(clock, manager)

    assert link.sends_since(mark) == []
    status = manager.status()["CP01"]
    assert status.reserved_w == 3_750
    assert manager.total_held_w() == 30_000


async def test_freed_power_respects_reservation_and_reconnect_resends(manager, link, clock):
    await start_sessions(manager, link, IDS)
    await settle(clock, manager)
    link.online.discard("CP01")
    manager.publish(ChargerDisconnected("CP01"))
    manager.publish(SessionStopped("CP02", 2))
    await settle(clock, manager)
    # 釋出的 3.75 kW 由其他 6 支平分，CP01 的保留額度不動
    current = link.current()
    assert current["CP02"] == 0
    assert {current[cid] for cid in IDS[2:]} == {4_350}
    assert manager.total_held_w() <= 30_000

    mark = clock.now()
    link.online.add("CP01")
    manager.publish(ChargerConnected("CP01"))
    await settle(clock, manager)
    # 重新連線後 CP01 重新加入分配（7 支平分），並一定會收到上限
    assert link.current() == {"CP02": 0} | {cid: 4_250 for cid in IDS if cid != "CP02"}
    assert "CP01" in {s[1] for s in link.sends_since(mark)}
    assert worst_case_peak(link.sends) <= 30_000


async def test_queued_session_is_promoted_when_another_stops(engine, session, clock, link):
    session.add_all(Charger(id=cid) for cid in IDS[:3])
    session.commit()
    manager = make_manager(engine, clock, link, PowerConfig(capacity_w=4_000))
    await start_sessions(manager, link, IDS[:3])  # 只容得下 2 支 1.4 kW
    await settle(clock, manager)
    assert manager.status()["CP03"].queued
    assert link.current().get("CP03", 0) == 0

    manager.publish(SessionStopped("CP01", 1))
    await settle(clock, manager)
    assert not manager.status()["CP03"].queued
    assert link.current()["CP03"] == 2_000
    assert worst_case_peak(link.sends) <= 4_000


async def test_failed_decrease_blocks_increase(manager, link, clock):
    await start_sessions(manager, link, IDS)
    await settle(clock, manager)
    mark = clock.now()
    link.failing.add("CP01")  # CP01 調降失敗，可能仍在用 3.75 kW
    for _ in range(2):
        await clock.advance(60)
        for cid in IDS:
            await report(manager, clock, cid, 800 if cid in ("CP01", "CP02") else 3_700)
    await settle(clock, manager)
    # CP01 仍以 3.75 kW 計入；調升只到預算用完為止
    assert manager.total_held_w() <= 30_000
    assert worst_case_peak(link.sends) <= 30_000
    raised = [cid for _, cid, limit in link.sends_since(mark) if limit > 3_750]
    assert 0 < len(set(raised)) < 6


async def test_manual_release_frees_reserved_power(manager, link, clock):
    await start_sessions(manager, link, IDS)
    await settle(clock, manager)
    link.online.discard("CP01")
    manager.publish(ChargerDisconnected("CP01"))
    await settle(clock, manager)
    manager.release_reservation("CP01")
    await settle(clock, manager)
    assert manager.status()["CP01"].reserved_w == 0
    assert {link.current()[cid] for cid in IDS[1:]} == {4_250}  # 30 kW / 7 → 4.25 kW


async def test_load_restores_reservation_after_server_restart(engine, session, clock, link):
    household = Household(unit_no="A01-01", password_hash="x")
    session.add_all([Charger(id="CP01", assigned_limit_w=3_750), Charger(id="CP02")])
    session.add(
        Transaction(
            charger_id="CP01", household=household, id_tag="C", start_ts=T0, meter_start_wh=0
        )
    )
    session.commit()
    manager = make_manager(engine, clock, link)
    assert manager.status()["CP01"].reserved_w == 3_750
    assert manager.total_held_w() == 3_750


async def test_snapshot_records_measured_limit_and_reserved(manager, link, clock, session):
    from sqlalchemy import select

    from chargeshare.db.models import PowerSnapshot

    await start_sessions(manager, link, IDS)
    await settle(clock, manager)
    for cid in IDS:
        await report(manager, clock, cid, 3_600)
    link.online.discard("CP08")
    manager.publish(ChargerDisconnected("CP08"))
    await settle(clock, manager)

    manager.record_snapshot()
    snapshot = session.scalars(select(PowerSnapshot)).one()
    assert snapshot.ts == clock.now()
    assert snapshot.limit_w == 7 * 3_750
    assert snapshot.reserved_w == 3_750
    # 量測值仍在 2 分鐘有效期內；離線樁不計入量測
    assert snapshot.measured_w == 7 * 3_600
