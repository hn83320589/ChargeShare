"""把資料庫與功率管理的狀態整理成畫面用的資料。"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from chargeshare.db.models import Charger, PowerSnapshot
from chargeshare.services.power_manager import PowerManager

OFFLINE, FAULTED, QUEUED, CHARGING, AVAILABLE = "斷線", "故障", "排隊中", "充電中", "可用"


def display_status(online: bool, ocpp_status: str, has_session: bool, queued: bool) -> str:
    if not online:
        return OFFLINE
    if ocpp_status == "Faulted":
        return FAULTED
    if has_session:
        return QUEUED if queued else CHARGING
    return AVAILABLE


@dataclass(frozen=True)
class ChargerView:
    charger_id: str
    status: str
    limit_w: int
    power_w: int
    reserved_w: int

    @property
    def limit_kw(self) -> str:
        return kw(self.limit_w)

    @property
    def power_kw(self) -> str:
        return kw(self.power_w)

    @property
    def reserved_kw(self) -> str:
        return kw(self.reserved_w)


def charger_views(session: Session, power: PowerManager) -> list[ChargerView]:
    statuses = power.status()
    views = []
    for charger in session.scalars(select(Charger).order_by(Charger.id)):
        st = statuses.get(charger.id)
        if st is None:
            views.append(ChargerView(charger.id, OFFLINE, 0, 0, 0))
            continue
        views.append(
            ChargerView(
                charger.id,
                display_status(st.online, charger.ocpp_status, st.has_session, st.queued),
                limit_w=st.limit_w if st.online else 0,
                power_w=st.power_w,
                reserved_w=st.reserved_w,
            )
        )
    return views


def power_points(session: Session, power: PowerManager, now: datetime) -> list[dict]:
    """最近 1 小時的總功率取樣，最後加上一個即時點。"""
    snapshots = session.scalars(
        select(PowerSnapshot)
        .where(PowerSnapshot.ts >= now - timedelta(hours=1), PowerSnapshot.ts < now)
        .order_by(PowerSnapshot.ts)
    )
    points = [_point(s.ts, s.measured_w, s.limit_w, s.reserved_w) for s in snapshots]
    statuses = power.status().values()
    points.append(
        _point(
            now,
            sum(s.power_w for s in statuses),
            sum(s.limit_w for s in statuses if s.online),
            sum(s.reserved_w for s in statuses),
        )
    )
    return points


def _point(ts: datetime, measured_w: int, limit_w: int, reserved_w: int) -> dict:
    return {
        "ts": ts.isoformat(),
        "measured_w": measured_w,
        "limit_w": limit_w,
        "reserved_w": reserved_w,
    }


def kw(watts: int) -> str:
    return f"{watts / 1000:.2f}"


def kwh(wh: int | None) -> str:
    return "—" if wh is None else f"{wh / 1000:.3f}"


def money(amount: Decimal | None) -> str:
    return "—" if amount is None else f"{amount:,.2f}"


def local_time(ts: datetime | None) -> str:
    return "—" if ts is None else ts.strftime("%Y-%m-%d %H:%M")
