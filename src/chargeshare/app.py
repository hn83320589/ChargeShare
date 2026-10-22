"""組裝 FastAPI 應用程式。時鐘與設定皆由外部注入。"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import Engine, update
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.sessions import SessionMiddleware

from chargeshare.clock import Clock
from chargeshare.config import load_tariff
from chargeshare.db.migrate import upgrade_to_head
from chargeshare.db.models import Charger
from chargeshare.db.session import create_db_engine, make_session_factory
from chargeshare.domain.tariff import Tariff
from chargeshare.ocpp import endpoint
from chargeshare.ocpp.connections import ConnectionManager
from chargeshare.ocpp.router import ChargePointRouter
from chargeshare.services.billing import ensure_monthly_bills
from chargeshare.services.power_manager import PowerManager
from chargeshare.settings import Settings
from chargeshare.web import routes as web_routes

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "web" / "static"


@dataclass
class AppContext:
    settings: Settings
    clock: Clock
    engine: Engine
    session_factory: sessionmaker[Session]
    tariff: Tariff
    connections: ConnectionManager
    power: PowerManager
    router: ChargePointRouter


def build_context(settings: Settings, clock: Clock) -> AppContext:
    engine = create_db_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    tariff = load_tariff(
        settings.config_dir / "tariff.toml",
        *sorted(settings.config_dir.glob("holidays_*.toml")),
    )
    connections = ConnectionManager(clock)
    power = PowerManager(session_factory, clock, connections, settings.power)
    return AppContext(
        settings=settings,
        clock=clock,
        engine=engine,
        session_factory=session_factory,
        tariff=tariff,
        connections=connections,
        power=power,
        router=ChargePointRouter(
            session_factory, clock, tariff, power, settings.heartbeat_interval_s
        ),
    )


async def _every(clock: Clock, interval_s: float, action, name: str) -> None:
    while True:
        await clock.sleep(interval_s)
        try:
            await action()
        except Exception:
            logger.exception("background task %s failed", name)


def create_app(settings: Settings, clock: Clock) -> FastAPI:
    ctx = build_context(settings, clock)
    if insecure := settings.insecure_defaults():
        logger.warning("using insecure development defaults for %s", ", ".join(insecure))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        upgrade_to_head(ctx.engine)
        with ctx.session_factory.begin() as session:
            # 伺服器重啟時所有連線都已中斷
            session.execute(update(Charger).values(connected=False))
        ctx.power.load()
        heartbeat_timeout = settings.heartbeat_interval_s * 2

        async def snapshot() -> None:
            ctx.power.record_snapshot()

        async def monthly_bills() -> None:
            with ctx.session_factory.begin() as session:
                ensure_monthly_bills(session, clock.now())

        await monthly_bills()

        tasks = [
            asyncio.create_task(_every(clock, settings.snapshot_interval_s, snapshot, "snapshot")),
            asyncio.create_task(_every(clock, 3600, monthly_bills, "monthly_bills")),
            asyncio.create_task(
                _every(
                    clock,
                    settings.heartbeat_interval_s,
                    lambda: ctx.connections.close_stale(heartbeat_timeout),
                    "watchdog",
                )
            ),
        ]
        yield
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        ctx.engine.dispose()

    app = FastAPI(title="ChargeShare", lifespan=lifespan)
    app.state.ctx = ctx
    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, same_site="lax")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(endpoint.router)
    app.include_router(web_routes.router)
    return app
