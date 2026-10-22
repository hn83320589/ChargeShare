"""chargeshare 指令列工具。

chargeshare serve                 啟動伺服器（首次啟動自動建立範例資料）
chargeshare simulate              伺服器 + 8 支模擬樁在同一個行程、共用加速時鐘，結束後自動比對
chargeshare sim --url ws://…      只跑模擬器，連到另一個行程的伺服器
chargeshare verify --log … --db … 以模擬器紀錄比對伺服器資料庫
"""

import argparse
import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

import uvicorn

from chargeshare.app import create_app
from chargeshare.clock import TAIPEI, Clock, ScaledClock, SystemClock
from chargeshare.db.migrate import upgrade_to_head
from chargeshare.db.session import create_db_engine, make_session_factory
from chargeshare.runner import serving
from chargeshare.seed import seed_all
from chargeshare.settings import Settings
from chargeshare.simulator.fleet import FleetResult, run_fleet
from chargeshare.simulator.recorder import load_sessions, verify
from chargeshare.simulator.scenarios import SCENARIOS, FleetOptions

logger = logging.getLogger("chargeshare")
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    return args.handler(args)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chargeshare", description="社區充電樁共享管理平台")
    sub = parser.add_subparsers(required=True)

    serve = sub.add_parser("serve", help="啟動伺服器")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--db", default=None, help="SQLite 檔案路徑（預設 data/chargeshare.db）")
    serve.add_argument("--no-seed", action="store_true", help="不建立範例資料")
    _add_clock_args(serve)
    serve.set_defaults(handler=_serve)

    simulate = sub.add_parser("simulate", help="伺服器與模擬器同行程執行，結束後比對度數")
    simulate.add_argument("--port", type=int, default=8000)
    simulate.add_argument("--db", default=None)
    simulate.add_argument("--no-seed", action="store_true")
    _add_fleet_args(simulate)
    _add_clock_args(simulate, default_speed=60)
    simulate.set_defaults(handler=_simulate)

    sim = sub.add_parser("sim", help="只執行模擬器")
    sim.add_argument("--url", default="ws://127.0.0.1:8000/ocpp")
    _add_fleet_args(sim)
    _add_clock_args(sim)
    sim.set_defaults(handler=_sim)

    check = sub.add_parser("verify", help="比對模擬器紀錄與伺服器資料庫")
    check.add_argument("--log", type=Path, required=True)
    check.add_argument("--db", required=True)
    check.set_defaults(handler=_verify)
    return parser


def _add_clock_args(parser: argparse.ArgumentParser, default_speed: float = 1) -> None:
    parser.add_argument("--speed", type=float, default=default_speed, help="時鐘倍速")
    parser.add_argument(
        "--start", type=datetime.fromisoformat, default=None, help="模擬起始時間（ISO 8601）"
    )


def _add_fleet_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--scenario", choices=SCENARIOS, default="all")
    parser.add_argument("--minutes", type=float, default=30)
    parser.add_argument("--battery-kwh", type=float, default=60)
    parser.add_argument("--initial-soc", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--log", type=Path, default=Path("sim-logs/latest.jsonl"))


def _clock(args) -> Clock:
    if args.speed == 1 and args.start is None:
        return SystemClock()
    start = args.start or SystemClock().now()
    if start.tzinfo is None:
        start = start.replace(tzinfo=TAIPEI)
    return ScaledClock(start, args.speed)


def _settings(args) -> Settings:
    if args.db:
        return Settings(database_url=f"sqlite:///{Path(args.db).resolve()}")
    settings = Settings()
    Path(settings.database_url.removeprefix("sqlite:///")).parent.mkdir(exist_ok=True)
    return settings


def _prepare_database(settings: Settings, clock: Clock, seed: bool) -> None:
    engine = create_db_engine(settings.database_url)
    upgrade_to_head(engine)
    if seed:
        with make_session_factory(engine).begin() as session:
            if seed_all(session, clock.now(), settings.config_dir):
                logger.info("範例資料已建立：120 戶、8 支樁、過去 2 個月的充電紀錄")
    engine.dispose()


def _serve(args) -> int:
    clock, settings = _clock(args), _settings(args)
    _prepare_database(settings, clock, seed=not args.no_seed)
    logger.info(
        "儀表板 http://%s:%s/admin  住戶 http://%s:%s/login",
        args.host,
        args.port,
        args.host,
        args.port,
    )
    uvicorn.run(create_app(settings, clock), host=args.host, port=args.port, log_level="info")
    return 0


def _simulate(args) -> int:
    clock, settings = _clock(args), _settings(args)
    _prepare_database(settings, clock, seed=not args.no_seed)

    async def run() -> int:
        app = create_app(settings, clock)
        async with serving(app, port=args.port) as port:
            logger.info("模擬中，儀表板 http://127.0.0.1:%s/admin", port)
            result = await run_fleet(
                _fleet_options(args), f"ws://127.0.0.1:{port}/ocpp", clock, args.minutes
            )
            with app.state.ctx.session_factory() as session:
                return _report(result, verify(result.recorder.sessions, session), args.log)

    return asyncio.run(run())


def _sim(args) -> int:
    async def run() -> FleetResult:
        return await run_fleet(_fleet_options(args), args.url, _clock(args), args.minutes)

    result = asyncio.run(run())
    result.recorder.write(args.log, result.monitor)
    logger.info("模擬紀錄已寫入 %s，可用 chargeshare verify 比對", args.log)
    return 0


def _verify(args) -> int:
    sessions, summary = load_sessions(args.log)
    engine = create_db_engine(f"sqlite:///{Path(args.db).resolve()}")
    with make_session_factory(engine)() as session:
        problems = verify(sessions, session)
    engine.dispose()
    return _print_problems(len(sessions), problems, summary.get("peak_total_w"))


def _fleet_options(args) -> FleetOptions:
    return FleetOptions(args.scenario, args.battery_kwh, args.initial_soc, args.seed)


def _report(result: FleetResult, problems: list[str], log: Path) -> int:
    result.recorder.write(log, result.monitor)
    for record in result.recorder.sessions:
        logger.info(
            "%s tx %s：%.3f 度（重開機 %d 次、斷線 %d 次）",
            record.charger_id,
            record.transaction_id,
            record.energy_wh / 1000,
            record.reboots,
            record.disconnects,
        )
    return _print_problems(len(result.recorder.sessions), problems, result.monitor.peak_w)


def _print_problems(count: int, problems: list[str], peak_w: float | None) -> int:
    if peak_w is not None:
        logger.info("模擬期間實際總功率峰值：%.2f kW", peak_w / 1000)
    if problems:
        for problem in problems:
            logger.error("不一致：%s", problem)
        return 1
    logger.info("比對完成：%d 筆充電的度數與伺服器完全一致", count)
    return 0


if __name__ == "__main__":
    sys.exit(main())
