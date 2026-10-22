"""PowerManager 測試用：假的充電樁連線，以及最壞情況總功率的計算。"""

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from chargeshare.clock import FakeClock
from chargeshare.ocpp.connections import ChargerOffline

SETTLE = timedelta(seconds=10)


class FakeLink:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.online: set[str] = set()
        self.failing: set[str] = set()
        self.sends: list[tuple[datetime, str, int]] = []

    def is_online(self, charger_id: str) -> bool:
        return charger_id in self.online

    async def call(self, charger_id: str, action: str, payload: dict[str, Any], timeout_s=30):
        if charger_id not in self.online or charger_id in self.failing:
            raise ChargerOffline(charger_id)
        assert action == "SetChargingProfile"
        self.sends.append((self.clock.now(), charger_id, payload["limitW"]))
        return {"status": "Accepted"}

    def current(self) -> dict[str, int]:
        latest: dict[str, int] = {}
        for _, charger_id, limit in self.sends:
            latest[charger_id] = limit
        return latest

    def sends_since(self, since: datetime) -> list[tuple[datetime, str, int]]:
        return [s for s in self.sends if s[0] >= since]


def worst_case_peak(sends: list[tuple[datetime, str, int]]) -> int:
    """樁收到新上限後 10 秒內才生效：這段期間舊值與新值都可能在用，取較大者。"""
    history: dict[str, list[tuple[datetime, int]]] = defaultdict(list)
    for ts, charger_id, limit in sorted(sends):
        history[charger_id].append((ts, limit))
    checkpoints = {ts for ts, _, _ in sends} | {ts + SETTLE for ts, _, _ in sends}

    def upper(charger_id: str, at: datetime) -> int:
        value = 0
        for ts, limit in history[charger_id]:
            if ts > at:
                break
            previous = value
            value = limit if at >= ts + SETTLE else max(previous, limit)
        return value

    return max((sum(upper(c, t) for c in history) for t in checkpoints), default=0)
