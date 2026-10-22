"""可注入的時鐘。系統中所有「現在幾點」與「等待」都必須透過 Clock 取得。"""

import asyncio
import heapq
import itertools
import time
from datetime import datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

TAIPEI = ZoneInfo("Asia/Taipei")


def _require_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError(f"datetime must be timezone-aware, got naive {dt!r}")
    return dt.astimezone(TAIPEI)


class Clock(Protocol):
    def now(self) -> datetime: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(TAIPEI)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class ScaledClock:
    """以 start 為起點、speed 倍速前進的時鐘，供模擬器與伺服器加速執行。"""

    def __init__(self, start: datetime, speed: float) -> None:
        if speed <= 0:
            raise ValueError(f"speed must be positive, got {speed}")
        self._start = _require_aware(start)
        self._speed = speed
        self._origin = time.monotonic()

    def now(self) -> datetime:
        elapsed = (time.monotonic() - self._origin) * self._speed
        return self._start + timedelta(seconds=elapsed)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds / self._speed)


class FakeClock:
    """測試用時鐘：時間只在呼叫 advance() 時前進。"""

    def __init__(self, start: datetime) -> None:
        self._now = _require_aware(start)
        self._sleepers: list[tuple[datetime, int, asyncio.Future[None]]] = []
        self._seq = itertools.count()

    def now(self) -> datetime:
        return self._now

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        deadline = self._now + timedelta(seconds=seconds)
        heapq.heappush(self._sleepers, (deadline, next(self._seq), future))
        await future

    async def advance(self, seconds: float) -> None:
        """逐一喚醒截止時間落在範圍內的 sleeper，讓它們依時間順序執行。"""
        target = self._now + timedelta(seconds=seconds)
        await _let_tasks_run()
        while self._sleepers and self._sleepers[0][0] <= target:
            deadline, _, future = heapq.heappop(self._sleepers)
            self._now = deadline
            if not future.done():
                future.set_result(None)
            await _let_tasks_run()
        self._now = target
        await _let_tasks_run()


async def _let_tasks_run() -> None:
    # 讓出數次 event loop，使被喚醒的 task 能執行到下一個 await
    for _ in range(5):
        await asyncio.sleep(0)
