from datetime import datetime

from chargeshare.clock import TAIPEI, FakeClock
from chargeshare.ocpp.connections import ConnectionManager


class FakeSocket:
    def __init__(self) -> None:
        self.closed_with: int | None = None
        self.sent: list[str] = []

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000) -> None:
        self.closed_with = code


async def test_watchdog_closes_silent_connections_only():
    clock = FakeClock(datetime(2026, 10, 22, 18, 0, tzinfo=TAIPEI))
    manager = ConnectionManager(clock)
    silent, chatty = FakeSocket(), FakeSocket()
    manager.register("CP01", silent)
    talking = manager.register("CP02", chatty)

    await clock.advance(100)
    talking.touch()
    await clock.advance(30)  # CP01 已 130 秒無訊息
    await manager.close_stale(timeout_s=120)

    assert silent.closed_with is not None
    assert chatty.closed_with is None


async def test_newer_connection_replaces_old_one():
    clock = FakeClock(datetime(2026, 10, 22, 18, 0, tzinfo=TAIPEI))
    manager = ConnectionManager(clock)
    old = manager.register("CP01", FakeSocket())
    new = manager.register("CP01", FakeSocket())
    assert manager.unregister(old) is False  # 舊連線關閉不代表樁離線
    assert manager.is_online("CP01")
    assert manager.unregister(new) is True
    assert not manager.is_online("CP01")
