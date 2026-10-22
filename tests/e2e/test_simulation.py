"""端對端：伺服器與 8 支模擬樁在同一個 event loop 內，共用加速時鐘。"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime

from sqlalchemy import func, select

from chargeshare.app import create_app
from chargeshare.clock import TAIPEI, ScaledClock
from chargeshare.db.migrate import upgrade_to_head
from chargeshare.db.models import MeterSample, Transaction
from chargeshare.db.session import create_db_engine, make_session_factory
from chargeshare.runner import serving
from chargeshare.seed import seed_base
from chargeshare.settings import Settings
from chargeshare.simulator.fleet import FleetResult, run_fleet
from chargeshare.simulator.recorder import verify
from chargeshare.simulator.scenarios import FleetOptions

# 週五 23:40 開始，跨午夜進入週六離峰
SIM_START = datetime(2026, 10, 23, 23, 40, tzinfo=TAIPEI)
SPEED = 120


@asynccontextmanager
async def live_server(tmp_path) -> AsyncIterator[tuple]:
    url = f"sqlite:///{tmp_path / 'e2e.db'}"
    engine = create_db_engine(url)
    upgrade_to_head(engine)
    with make_session_factory(engine).begin() as session:
        seed_base(session, hasher=lambda password: "unused")
    engine.dispose()
    clock = ScaledClock(SIM_START, speed=SPEED)
    app = create_app(Settings(database_url=url, dev_mode=True), clock)
    async with serving(app) as port:
        yield app, clock, f"ws://127.0.0.1:{port}/ocpp"


async def simulate(
    tmp_path, scenario: str, minutes: float
) -> tuple[FleetResult, list[str], object]:
    async with live_server(tmp_path) as (app, clock, url):
        result = await run_fleet(FleetOptions(scenario=scenario), url, clock, minutes)
        with app.state.ctx.session_factory() as session:
            problems = verify(result.recorder.sessions, session)
        return result, problems, app


def server_errors(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]


async def test_reboot_mid_charge_keeps_one_session_with_exact_energy(tmp_path, caplog):
    # 驗收 10
    result, problems, app = await simulate(tmp_path, "reboot", minutes=12)
    assert problems == []
    [record] = [r for r in result.recorder.sessions if r.charger_id == "CP04"]
    assert record.reboots == 1
    assert record.energy_wh > 0
    with app.state.ctx.session_factory() as session:
        cp04 = session.scalars(select(Transaction).where(Transaction.charger_id == "CP04")).all()
    assert len(cp04) == 1
    assert cp04[0].peak_wh + cp04[0].offpeak_wh == record.energy_wh
    assert server_errors(caplog) == []


async def test_ten_minute_disconnect_is_replayed_and_billed(tmp_path, caplog):
    result, problems, app = await simulate(tmp_path, "disconnect", minutes=20)
    assert problems == []
    [record] = [r for r in result.recorder.sessions if r.charger_id == "CP03"]
    assert record.disconnects == 1
    with app.state.ctx.session_factory() as session:
        samples = session.scalar(
            select(func.count())
            .select_from(MeterSample)
            .where(MeterSample.transaction_id == record.transaction_id)
        )
    assert samples >= 17  # 斷線 10 分鐘的每分鐘讀數都補進來了
    assert server_errors(caplog) == []


async def test_fleet_of_eight_for_thirty_minutes(tmp_path, caplog):
    # 驗收 13：8 支樁 30 分鐘（含快充滿、斷線、重開機），每筆度數一致、沒有伺服器錯誤
    result, problems, _ = await simulate(tmp_path, "all", minutes=30)
    assert len(result.recorder.sessions) == 8
    assert problems == []
    assert result.monitor.peak_w <= 30_000  # 驗收 12：實際總功率從未超過上限
    assert server_errors(caplog) == []


async def test_meter_reset_on_reboot_is_billed_and_flagged(tmp_path, caplog):
    # 審查回饋：不穩定韌體重開機後電表從 0 起算，交易仍須正常結束並計費
    result, problems, app = await simulate(tmp_path, "meter-reset", minutes=12)
    assert problems == []
    [record] = [r for r in result.recorder.sessions if r.charger_id == "CP04"]
    assert record.meter_resets == 1
    assert record.meter_stop_wh < record.meter_start_wh  # 電表確實歸零過
    with app.state.ctx.session_factory() as session:
        tx = session.get(Transaction, record.transaction_id)
        assert tx.stop_ts is not None
        assert tx.peak_wh + tx.offpeak_wh == record.energy_wh > 0
        assert tx.meter_resets == 1
        assert tx.needs_review
    assert server_errors(caplog) == []
