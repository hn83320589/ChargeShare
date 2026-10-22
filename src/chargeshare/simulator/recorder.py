"""記錄模擬器實際送出的電量，並與伺服器資料庫比對。"""

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from chargeshare.db.models import Transaction


@dataclass
class SessionRecord:
    charger_id: str
    transaction_id: int
    id_tag: str
    start: str
    meter_start_wh: int
    stop: str | None = None
    meter_stop_wh: int | None = None
    reboots: int = 0
    disconnects: int = 0
    meter_resets: int = 0
    # 依送出的讀數累加（讀數下降視為電表重置），即伺服器應計費的電量
    reported_wh: int = 0
    last_reading_wh: int = -1

    def __post_init__(self) -> None:
        if self.last_reading_wh < 0:
            self.last_reading_wh = self.meter_start_wh

    @property
    def energy_wh(self) -> int:
        if self.meter_stop_wh is None:
            raise ValueError(f"session {self.transaction_id} on {self.charger_id} never stopped")
        return self.reported_wh


class FleetMonitor:
    """所有模擬樁的瞬間實際功率加總，記錄峰值。"""

    def __init__(self) -> None:
        self._power: dict[str, float] = {}
        self.peak_w = 0.0
        self.peak_at: datetime | None = None

    def report(self, charger_id: str, power_w: float, at: datetime) -> None:
        self._power[charger_id] = power_w
        total = sum(self._power.values())
        if total > self.peak_w:
            self.peak_w, self.peak_at = total, at


@dataclass
class Recorder:
    sessions: list[SessionRecord] = field(default_factory=list)

    def started(self, record: SessionRecord) -> SessionRecord:
        self.sessions.append(record)
        return record

    def write(self, path: Path, monitor: FleetMonitor) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            for record in self.sessions:
                f.write(
                    json.dumps({"type": "session", **asdict(record)}, ensure_ascii=False) + "\n"
                )
            summary = {
                "type": "summary",
                "peak_total_w": round(monitor.peak_w, 1),
                "peak_at": monitor.peak_at.isoformat() if monitor.peak_at else None,
            }
            f.write(json.dumps(summary) + "\n")


def load_sessions(path: Path) -> tuple[list[SessionRecord], dict]:
    sessions, summary = [], {}
    for line in path.read_text(encoding="utf-8").splitlines():
        data = json.loads(line)
        kind = data.pop("type")
        if kind == "session":
            sessions.append(SessionRecord(**data))
        else:
            summary = data
    return sessions, summary


def verify(sessions: list[SessionRecord], session: Session) -> list[str]:
    """回傳不一致的描述；空串列代表每筆度數都與伺服器一致。"""
    problems = []
    for record in sessions:
        tx = session.get(Transaction, record.transaction_id)
        label = f"{record.charger_id} tx {record.transaction_id}"
        if tx is None:
            problems.append(f"{label}: not found on server")
            continue
        if record.meter_stop_wh is None:
            problems.append(f"{label}: simulator never stopped the session")
            continue
        if tx.stop_ts is None:
            problems.append(f"{label}: server has no StopTransaction")
            continue
        billed = tx.peak_wh + tx.offpeak_wh
        if billed != record.energy_wh:
            problems.append(f"{label}: simulator {record.energy_wh} Wh, server billed {billed} Wh")
    return problems
