"""模擬一支（韌體不太穩定的）交流充電樁。

- 所有要送給伺服器的訊息先進本機佇列，連線時依序送出、收到回應才移除；
  斷線或重開機後以「原 messageId」補送，佇列與電表皆保存於本機。
- 收到 SetChargingProfile 後，於 apply_delay_s 秒後生效（< 10 秒）；未收到前為 0 kW。
- 電量以事件驅動精確積分：每次功率改變前先累加到電表。
"""

import asyncio
import contextlib
import json
import logging
import random
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from chargeshare.clock import Clock
from chargeshare.simulator.ev import Battery
from chargeshare.simulator.recorder import FleetMonitor, Recorder, SessionRecord

logger = logging.getLogger(__name__)


class FaultKind(StrEnum):
    DISCONNECT = "disconnect"  # 網路斷線，樁持續依最後上限供電
    REBOOT = "reboot"  # 重新開機：期間不供電，開機後上限歸零直到伺服器重送


@dataclass(frozen=True)
class Fault:
    kind: FaultKind
    at_s: float  # 模擬開始後幾秒發生
    duration_s: float
    reset_meter: bool = False  # 重開機後電表從 0 起算（不穩定韌體）


@dataclass(frozen=True)
class SessionPlan:
    id_tag: str
    plug_in_at_s: float
    battery_wh: float
    initial_soc: float


@dataclass
class _Outgoing:
    message_id: str
    action: str
    payload: dict[str, Any]
    reply: asyncio.Future[dict[str, Any]] | None = None


class _State(StrEnum):
    IDLE = "idle"
    STARTING = "starting"
    CHARGING = "charging"
    DONE = "done"


@dataclass
class SimConfig:
    apply_delay_s: tuple[float, float] = (2.0, 8.0)
    sample_interval_s: float = 5.0  # 電池降載與雜訊的更新頻率
    meter_interval_s: float = 60.0
    heartbeat_s: float = 60.0
    reconnect_s: float = 3.0
    response_timeout_s: float = 30.0
    noise: float = 0.02


class SimChargePoint:
    def __init__(
        self,
        charger_id: str,
        base_url: str,
        clock: Clock,
        recorder: Recorder,
        monitor: FleetMonitor,
        plan: SessionPlan | None,
        faults: list[Fault] | None = None,
        config: SimConfig | None = None,
        seed: int = 0,
        meter_wh: float = 0.0,
    ) -> None:
        self.charger_id = charger_id
        self._url = f"{base_url.rstrip('/')}/{charger_id}"
        self._clock = clock
        self._recorder = recorder
        self._monitor = monitor
        self._plan = plan
        self._faults = sorted(faults or [], key=lambda f: f.at_s)
        self._cfg = config or SimConfig()
        self._rng = random.Random(seed)

        self._outbox: deque[_Outgoing] = deque()
        self._outbox_changed = asyncio.Event()
        self._ws: ClientConnection | None = None
        self._connected = asyncio.Event()
        self._waiting: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._offline_until = None
        self._rebooting = False

        self._meter_wh = meter_wh
        self._power_w = 0.0
        self._last_integrated = clock.now()
        self._limit_w = 0.0
        self._profile_seq = 0
        self._applied_seq = 0
        self._next_apply_at = clock.now()
        self._state = _State.IDLE
        self._battery = Battery(plan.battery_wh, plan.initial_soc) if plan else None
        self._record: SessionRecord | None = None
        self._transaction_id: int | None = None
        self._started = clock.now()
        self._stopping = False

    # ── 對外 ───────────────────────────────────────────────────────

    @property
    def meter_wh(self) -> int:
        return int(self._meter_wh)

    async def run(self, duration_s: float) -> None:
        self._started = self._clock.now()
        tasks = [
            asyncio.create_task(self._connection_loop()),
            asyncio.create_task(self._sender_loop()),
            asyncio.create_task(self._heartbeat_loop()),
            asyncio.create_task(self._fault_loop()),
        ]
        try:
            await self._physics_loop(duration_s)
            await self._finish()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self._ws is not None:
                await self._ws.close()

    # ── 物理：功率、電表、充電流程 ────────────────────────────────

    async def _physics_loop(self, duration_s: float) -> None:
        next_meter = self._cfg.meter_interval_s
        while self._elapsed() < duration_s:
            await self._clock.sleep(self._cfg.sample_interval_s)
            if (
                self._state == _State.IDLE
                and self._plan
                and self._elapsed() >= self._plan.plug_in_at_s
            ):
                self._state = _State.STARTING
                asyncio.create_task(self._start_session())
            if self._state == _State.CHARGING:
                self._update_power()
                if self._battery.full:
                    await self._stop_session("EVDisconnected")
                elif self._elapsed() >= next_meter and not self._rebooting:
                    self._queue_meter_values()
            if self._elapsed() >= next_meter:
                next_meter += self._cfg.meter_interval_s

    def _elapsed(self) -> float:
        return (self._clock.now() - self._started).total_seconds()

    def _integrate(self) -> None:
        now = self._clock.now()
        energy = self._power_w * (now - self._last_integrated).total_seconds() / 3600
        self._meter_wh += energy
        if self._battery is not None and self._state == _State.CHARGING:
            self._battery.charge(energy)
        self._last_integrated = now

    def _set_power(self, power_w: float) -> None:
        self._integrate()
        self._power_w = power_w
        self._monitor.report(self.charger_id, power_w, self._clock.now())

    def _update_power(self) -> None:
        if self._state != _State.CHARGING or self._rebooting:
            self._set_power(0.0)
            return
        self._integrate()
        wanted = min(self._limit_w, self._battery.acceptance_w())
        self._set_power(wanted * (1 - self._rng.uniform(0, self._cfg.noise)))

    async def _apply_limit_later(self, limit_w: float) -> None:
        # 真實的樁依收到順序套用設定：套用時間單調遞增，且舊設定不會蓋掉新設定
        self._profile_seq += 1
        seq = self._profile_seq
        now = self._clock.now()
        apply_at = max(
            now + timedelta(seconds=self._rng.uniform(*self._cfg.apply_delay_s)),
            self._next_apply_at,
        )
        self._next_apply_at = apply_at
        await self._clock.sleep((apply_at - now).total_seconds())
        if self._rebooting or seq <= self._applied_seq:
            return
        self._applied_seq = seq
        self._limit_w = limit_w
        self._update_power()

    async def _start_session(self) -> None:
        try:
            await self._begin_transaction()
        except RuntimeError:
            logger.exception("%s could not start a session", self.charger_id)
            self._state = _State.DONE

    async def _begin_transaction(self) -> None:
        plan = self._plan
        await self._connected.wait()
        self._queue("StatusNotification", {"connectorId": 1, "status": "Preparing"})
        auth = await self._request("Authorize", {"idTag": plan.id_tag})
        if auth["idTagInfo"]["status"] != "Accepted":
            logger.warning("%s card %s rejected: %s", self.charger_id, plan.id_tag, auth)
            self._state = _State.DONE
            return
        self._integrate()
        meter_start = self.meter_wh
        started = self._clock.now()
        result = await self._request(
            "StartTransaction",
            {
                "connectorId": 1,
                "idTag": plan.id_tag,
                "meterStart": meter_start,
                "timestamp": started.isoformat(),
            },
        )
        if result["idTagInfo"]["status"] != "Accepted":
            self._state = _State.DONE
            return
        self._transaction_id = result["transactionId"]
        self._record = self._recorder.started(
            SessionRecord(
                self.charger_id, self._transaction_id, plan.id_tag, started.isoformat(), meter_start
            )
        )
        self._state = _State.CHARGING
        self._queue("StatusNotification", {"connectorId": 1, "status": "Charging"})
        self._update_power()

    def _report_reading(self, energy_wh: int) -> None:
        """記錄送出的讀數：依「讀數下降即電表重置」的規則累加應計費電量。"""
        record = self._record
        if energy_wh >= record.last_reading_wh:
            record.reported_wh += energy_wh - record.last_reading_wh
        else:
            record.reported_wh += energy_wh
            record.meter_resets += 1
        record.last_reading_wh = energy_wh

    def _queue_meter_values(self) -> None:
        self._integrate()
        self._report_reading(self.meter_wh)
        self._queue(
            "MeterValues",
            {
                "connectorId": 1,
                "transactionId": self._transaction_id,
                "timestamp": self._clock.now().isoformat(),
                "energyWh": self.meter_wh,
                "powerW": round(self._power_w),
            },
        )

    async def _stop_session(self, reason: str) -> None:
        self._set_power(0.0)
        self._state = _State.DONE
        self._record.stop = self._clock.now().isoformat()
        self._record.meter_stop_wh = self.meter_wh
        self._report_reading(self.meter_wh)
        self._queue(
            "StopTransaction",
            {
                "transactionId": self._transaction_id,
                "meterStop": self.meter_wh,
                "timestamp": self._record.stop,
                "reason": reason,
            },
        )
        self._queue("StatusNotification", {"connectorId": 1, "status": "Available"})

    async def _finish(self) -> None:
        """模擬時間到：結束進行中的充電，並等佇列送完。"""
        if self._state == _State.STARTING:
            await self._wait_until(lambda: self._state != _State.STARTING, "session start")
        if self._state == _State.CHARGING:
            await self._stop_session("Local")
        await self._wait_until(lambda: not self._outbox, "outbox drain")

    async def _wait_until(self, condition, what: str, limit_s: float = 1_800) -> None:
        waited = 0.0
        while not condition():
            if waited >= limit_s:
                raise TimeoutError(f"{self.charger_id}: gave up waiting for {what}")
            await self._clock.sleep(1)
            waited += 1

    # ── 故障情境 ───────────────────────────────────────────────────

    async def _fault_loop(self) -> None:
        for fault in self._faults:
            await self._clock.sleep(max(0.0, fault.at_s - self._elapsed()))
            logger.info("%s %s for %ss", self.charger_id, fault.kind, fault.duration_s)
            if self._record is not None:
                if fault.kind == FaultKind.REBOOT:
                    self._record.reboots += 1
                else:
                    self._record.disconnects += 1
            self._offline_until = self._clock.now().timestamp() + fault.duration_s
            if fault.kind == FaultKind.REBOOT:
                self._rebooting = True
                self._limit_w = 0.0  # 重開機遺失伺服器下達的上限
                self._applied_seq = self._profile_seq  # 尚未生效的設定一併遺失
                self._update_power()
                if fault.reset_meter:
                    self._meter_wh = 0.0
            if self._ws is not None:
                await self._ws.close()
            await self._clock.sleep(fault.duration_s)
            self._rebooting = False

    # ── 通訊 ───────────────────────────────────────────────────────

    def _queue(self, action: str, payload: dict[str, Any]) -> _Outgoing:
        item = _Outgoing(uuid.uuid4().hex, action, payload)
        self._outbox.append(item)
        self._outbox_changed.set()
        return item

    async def _request(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        item = self._queue(action, payload)
        item.reply = asyncio.get_running_loop().create_future()
        return await item.reply

    async def _connection_loop(self) -> None:
        while True:
            if self._offline_until and self._clock.now().timestamp() < self._offline_until:
                await self._clock.sleep(self._offline_until - self._clock.now().timestamp())
                continue
            try:
                async with connect(self._url, subprotocols=["ocpp1.6"]) as ws:
                    self._ws = ws
                    await self._boot(ws)
                    await self._receive(ws)
            except (OSError, WebSocketException) as exc:
                logger.info("%s connection lost: %r", self.charger_id, exc)
            finally:
                self._ws = None
                self._connected.clear()
                for future in self._waiting.values():
                    if not future.done():
                        future.set_exception(ConnectionError("disconnected"))
                self._waiting.clear()
            await self._clock.sleep(self._cfg.reconnect_s)

    async def _boot(self, ws: ClientConnection) -> None:
        message_id = uuid.uuid4().hex
        payload = {
            "chargePointVendor": "ChargeShare",
            "chargePointModel": "SIM-AC7",
            "firmwareVersion": "0.9-unstable",
        }
        await ws.send(json.dumps([2, message_id, "BootNotification", payload]))
        while True:
            frame = json.loads(await ws.recv())
            if frame[0] == 3 and frame[1] == message_id:
                break
            if frame[0] == 2:
                await self._answer(ws, frame)
        self._connected.set()

    async def _receive(self, ws: ClientConnection) -> None:
        try:
            async for text in ws:
                frame = json.loads(text)
                if frame[0] == 2:
                    await self._answer(ws, frame)
                elif (future := self._waiting.pop(frame[1], None)) and not future.done():
                    if frame[0] == 3:
                        future.set_result(frame[2])
                    else:
                        future.set_exception(RuntimeError(f"{frame[2]}: {frame[3]}"))
        except ConnectionClosed:
            pass

    async def _answer(self, ws: ClientConnection, frame: list) -> None:
        _, message_id, action, payload = frame
        if action == "SetChargingProfile":
            asyncio.create_task(self._apply_limit_later(float(payload["limitW"])))
            await ws.send(json.dumps([3, message_id, {"status": "Accepted"}]))
        else:
            await ws.send(json.dumps([4, message_id, "NotImplemented", action, {}]))

    async def _sender_loop(self) -> None:
        while True:
            if not self._outbox:
                self._outbox_changed.clear()
                await self._outbox_changed.wait()
                continue
            await self._connected.wait()
            item = self._outbox[0]
            ws = self._ws
            if ws is None:
                continue
            future = asyncio.get_running_loop().create_future()
            self._waiting[item.message_id] = future
            try:
                await ws.send(json.dumps([2, item.message_id, item.action, item.payload]))
                response = await self._await_response(future)
            except (ConnectionError, ConnectionClosed, TimeoutError) as exc:
                logger.info("%s will resend %s: %r", self.charger_id, item.action, exc)
                await self._clock.sleep(1)
                continue
            except RuntimeError:
                logger.exception(
                    "%s server rejected %s %s", self.charger_id, item.action, item.payload
                )
                response = None
            finally:
                _release(self._waiting, item.message_id, future)
            self._outbox.popleft()
            if item.reply is not None and not item.reply.done():
                if response is None:
                    item.reply.set_exception(RuntimeError(f"{item.action} rejected"))
                else:
                    item.reply.set_result(response)

    async def _await_response(self, future: asyncio.Future) -> dict[str, Any]:
        timer = asyncio.ensure_future(self._clock.sleep(self._cfg.response_timeout_s))
        try:
            await asyncio.wait({future, timer}, return_when=asyncio.FIRST_COMPLETED)
            if not future.done():
                raise TimeoutError("no response")
            return future.result()
        finally:
            timer.cancel()

    async def _heartbeat_loop(self) -> None:
        while True:
            await self._clock.sleep(self._cfg.heartbeat_s)
            ws = self._ws
            if ws is not None and self._connected.is_set():
                with contextlib.suppress(ConnectionClosed):
                    await ws.send(json.dumps([2, uuid.uuid4().hex, "Heartbeat", {}]))


def _release(waiting: dict, message_id: str, future: asyncio.Future) -> None:
    """送出端結束等待時移除自己的 future；若連線迴圈已設下例外而沒人讀取，於此讀取。"""
    waiting.pop(message_id, None)
    if future.done() and not future.cancelled():
        future.exception()
