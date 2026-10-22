"""組裝 FastAPI 應用程式。時鐘與設定皆由外部注入。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI
from sqlalchemy import Engine, update
from sqlalchemy.orm import Session, sessionmaker

from chargeshare.clock import Clock
from chargeshare.config import load_tariff
from chargeshare.db.migrate import upgrade_to_head
from chargeshare.db.models import Charger
from chargeshare.db.session import create_db_engine, make_session_factory
from chargeshare.domain.tariff import Tariff
from chargeshare.ocpp import endpoint
from chargeshare.ocpp.connections import ConnectionManager
from chargeshare.ocpp.router import ChargePointRouter
from chargeshare.services.events import EventSink, NullEventSink
from chargeshare.settings import Settings


@dataclass
class AppContext:
    settings: Settings
    clock: Clock
    engine: Engine
    session_factory: sessionmaker[Session]
    tariff: Tariff
    connections: ConnectionManager
    events: EventSink
    router: ChargePointRouter


def build_context(settings: Settings, clock: Clock) -> AppContext:
    engine = create_db_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    tariff = load_tariff(
        settings.config_dir / "tariff.toml",
        *sorted(settings.config_dir.glob("holidays_*.toml")),
    )
    events = NullEventSink()
    return AppContext(
        settings=settings,
        clock=clock,
        engine=engine,
        session_factory=session_factory,
        tariff=tariff,
        connections=ConnectionManager(clock),
        events=events,
        router=ChargePointRouter(
            session_factory, clock, tariff, events, settings.heartbeat_interval_s
        ),
    )


def create_app(settings: Settings, clock: Clock) -> FastAPI:
    ctx = build_context(settings, clock)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        upgrade_to_head(ctx.engine)
        with ctx.session_factory.begin() as session:
            # 伺服器重啟時所有連線都已中斷
            session.execute(update(Charger).values(connected=False))
        yield
        ctx.engine.dispose()

    app = FastAPI(title="ChargeShare", lifespan=lifespan)
    app.state.ctx = ctx
    app.include_router(endpoint.router)
    return app
