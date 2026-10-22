"""功率管理：接收充電樁事件、重新分配功率，並以「先降後升」兩階段下達上限。

每支樁的「持有上限」= max(已確認的上限, 已送出未確認的上限)。
樁可能正在使用其中任何一個值，所以預算一律以持有上限計算。
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from chargeshare.clock import Clock
from chargeshare.db.models import Charger, PowerSnapshot, Transaction
from chargeshare.domain.allocation import Allocation, ChargerInput, PowerConfig, allocate
from chargeshare.domain.demand import DemandConfig, DemandMode, DemandState, update_demand
from chargeshare.domain.dispatch import plan_dispatch
from chargeshare.services.events import (
    ChargerBooted,
    ChargerConnected,
    ChargerDisconnected,
    Event,
    MeterSampled,
    SessionStarted,
    SessionStopped,
)

logger = logging.getLogger(__name__)


class ChargerLink(Protocol):
    def is_online(self, charger_id: str) -> bool: ...

    async def call(
        self, charger_id: str, action: str, payload: dict[str, Any], timeout_s: float = 30
    ) -> dict[str, Any]: ...


@dataclass
class _ChargerState:
    assigned_w: int = 0  # 樁已確認的上限
    pending_w: int | None = None  # 已送出、尚未確認的上限
    session_started_at: datetime | None = None
    demand: DemandState | None = None
    power_w: int = 0
    power_ts: datetime | None = None

    @property
    def held_w(self) -> int:
        return max(self.assigned_w, self.pending_w or 0)

    @property
    def has_session(self) -> bool:
        return self.session_started_at is not None


@dataclass(frozen=True)
class ChargerPowerStatus:
    charger_id: str
    online: bool
    has_session: bool
    queued: bool
    limit_w: int
    reserved_w: int
    power_w: int
    demand_mode: DemandMode


class PowerManager:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        clock: Clock,
        link: ChargerLink,
        power: PowerConfig,
        demand: DemandConfig | None = None,
        deadband_w: int = 300,
        settle_s: float = 10,
        stale_after_s: float = 120,
        call_timeout_s: float = 30,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._link = link
        self._power = power
        self._demand = demand or DemandConfig(max_w=power.max_w)
        self._deadband_w = deadband_w
        self._settle_s = settle_s
        self._stale_after = timedelta(seconds=stale_after_s)
        self._call_timeout_s = call_timeout_s
        self._chargers: dict[str, _ChargerState] = {}
        self._force_send: set[str] = set()
        self._allocation = Allocation({}, ())
        self._dirty = False
        self._worker: asyncio.Task[None] | None = None
        self._retry: asyncio.Task[None] | None = None

    # ── 狀態載入與查詢 ──────────────────────────────────────────────

    def load(self) -> None:
        """伺服器啟動時，從資料庫還原各樁上限與進行中的交易。"""
        with self._session_factory() as session:
            for charger in session.scalars(select(Charger)):
                self._chargers[charger.id] = _ChargerState(
                    assigned_w=charger.assigned_limit_w, pending_w=charger.pending_limit_w
                )
            for tx in session.scalars(select(Transaction).where(Transaction.stop_ts.is_(None))):
                self._state(tx.charger_id).session_started_at = tx.start_ts
        self._allocation = allocate(self._inputs(), self._power)

    @property
    def idle(self) -> bool:
        return self._worker is None or self._worker.done()

    def status(self) -> dict[str, ChargerPowerStatus]:
        fresh_after = self._clock.now() - self._stale_after
        result = {}
        for charger_id, state in sorted(self._chargers.items()):
            online = self._link.is_online(charger_id)
            fresh = state.power_ts is not None and state.power_ts >= fresh_after
            result[charger_id] = ChargerPowerStatus(
                charger_id=charger_id,
                online=online,
                has_session=state.has_session,
                queued=charger_id in self._allocation.queued,
                limit_w=state.assigned_w,
                reserved_w=self._allocation.reserved.get(charger_id, 0),
                power_w=state.power_w if online and state.has_session and fresh else 0,
                demand_mode=(state.demand or DemandState.initial(self._demand)).mode,
            )
        return result

    def total_held_w(self) -> int:
        return sum(state.held_w for state in self._chargers.values())

    def release_reservation(self, charger_id: str) -> None:
        """管委會確認離線樁已停止供電後，手動釋放其保留額度。"""
        if self._link.is_online(charger_id):
            raise ValueError(f"{charger_id} is online; only offline reservations can be released")
        state = self._state(charger_id)
        logger.warning("release reservation of %s (%s W)", charger_id, state.held_w)
        state.assigned_w, state.pending_w = 0, None
        self._persist(charger_id, state)
        self._kick()

    def record_snapshot(self) -> None:
        """記錄儀表板總功率曲線的一個取樣點。"""
        statuses = self.status().values()
        with self._session_factory.begin() as session:
            session.add(
                PowerSnapshot(
                    ts=self._clock.now(),
                    measured_w=sum(s.power_w for s in statuses),
                    limit_w=sum(s.limit_w for s in statuses if s.online),
                    reserved_w=sum(s.reserved_w for s in statuses),
                )
            )

    # ── 事件 ────────────────────────────────────────────────────────

    def publish(self, event: Event) -> None:
        match event:
            case ChargerConnected(charger_id) | ChargerBooted(charger_id):
                # 樁可能重開機而遺失上限，重新連線後一律重送
                self._force_send.add(charger_id)
                self._kick()
            case ChargerDisconnected():
                self._kick()
            case SessionStarted(charger_id, _, started_at):
                state = self._state(charger_id)
                state.session_started_at = started_at
                state.demand = DemandState.initial(self._demand)
                self._kick()
            case SessionStopped(charger_id, _):
                state = self._state(charger_id)
                state.session_started_at = None
                state.demand = None
                self._kick()
            case MeterSampled(charger_id, _, ts, power_w):
                self._on_sample(charger_id, ts, power_w)

    def _on_sample(self, charger_id: str, ts: datetime, power_w: int) -> None:
        if ts < self._clock.now() - self._stale_after:
            return  # 補傳的舊資料只用於計費
        state = self._state(charger_id)
        state.power_w, state.power_ts = power_w, ts
        if not state.has_session:
            return
        previous = state.demand or DemandState.initial(self._demand)
        state.demand = update_demand(previous, power_w, state.assigned_w, self._demand)
        if (state.demand.mode, state.demand.cap_w) != (previous.mode, previous.cap_w):
            logger.info("%s demand %s → %s", charger_id, previous, state.demand)
            self._kick()

    # ── 重新分配與下達 ──────────────────────────────────────────────

    def _kick(self) -> None:
        self._dirty = True
        if self.idle:
            self._worker = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        while self._dirty:
            self._dirty = False
            try:
                await self._rebalance()
            except Exception:
                logger.exception("rebalance failed")
                self._retry_later()

    async def _rebalance(self) -> None:
        self._allocation = allocate(self._inputs(), self._power)
        online = [cid for cid in self._chargers if self._link.is_online(cid)]
        current = {cid: self._chargers[cid].assigned_w for cid in online}
        target = {cid: self._allocation.limits.get(cid, 0) for cid in online}
        plan = plan_dispatch(current, target, self._deadband_w)
        decreases, increases = dict(plan.decreases), dict(plan.increases)
        for cid in self._force_send & set(online):
            if cid not in decreases and cid not in increases:
                bucket = decreases if target[cid] <= current[cid] else increases
                bucket[cid] = target[cid]
        self._force_send -= set(online)

        if decreases:
            results = await asyncio.gather(*(self._send(c, w) for c, w in decreases.items()))
            if any(results):
                await self._clock.sleep(self._settle_s)  # 等調降生效
        if increases:
            await self._send_increases(increases)

    async def _send_increases(self, increases: dict[str, int]) -> None:
        held_total = self.total_held_w()
        approved: dict[str, int] = {}
        for charger_id, limit in sorted(increases.items()):
            held = self._chargers[charger_id].held_w
            if held_total - held + limit <= self._power.capacity_w:
                approved[charger_id] = limit
                held_total += limit - held
        if len(approved) < len(increases):
            logger.warning(
                "defer increases %s: decreases not confirmed", increases.keys() - approved
            )
            self._retry_later()
        await asyncio.gather(*(self._send(c, w) for c, w in approved.items()))

    async def _send(self, charger_id: str, limit_w: int) -> bool:
        state = self._chargers[charger_id]
        state.pending_w = limit_w
        self._persist(charger_id, state)
        payload = {"connectorId": 1, "limitW": limit_w}
        try:
            response = await self._link.call(
                charger_id, "SetChargingProfile", payload, self._call_timeout_s
            )
        except Exception as exc:
            logger.warning("SetChargingProfile %s → %s W failed: %r", charger_id, limit_w, exc)
            self._retry_later()
            return False
        if response.get("status") != "Accepted":
            logger.warning("%s rejected limit %s W: %s", charger_id, limit_w, response)
            self._retry_later()
            return False
        state.assigned_w, state.pending_w = limit_w, None
        self._persist(charger_id, state)
        return True

    def _retry_later(self) -> None:
        if self._retry is not None and not self._retry.done():
            return

        async def retry() -> None:
            await self._clock.sleep(self._settle_s)
            self._kick()

        self._retry = asyncio.get_running_loop().create_task(retry())

    # ── 內部工具 ────────────────────────────────────────────────────

    def _state(self, charger_id: str) -> _ChargerState:
        return self._chargers.setdefault(charger_id, _ChargerState())

    def _inputs(self) -> list[ChargerInput]:
        return [
            ChargerInput(
                charger_id=cid,
                online=self._link.is_online(cid),
                has_session=state.has_session,
                arrived_at=state.session_started_at or datetime.max.replace(tzinfo=None),
                demand_cap_w=(state.demand or DemandState.initial(self._demand)).cap_w,
                held_limit_w=state.held_w,
            )
            for cid, state in self._chargers.items()
        ]

    def _persist(self, charger_id: str, state: _ChargerState) -> None:
        with self._session_factory.begin() as session:
            charger = session.get(Charger, charger_id)
            if charger is None:
                raise LookupError(f"charger {charger_id} not found in database")
            charger.assigned_limit_w = state.assigned_w
            charger.pending_limit_w = state.pending_w
