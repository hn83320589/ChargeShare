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


async def test_call_retrieves_failed_future_when_send_fails(caplog):
    # 與模擬器同類的競態：送出時連線關閉，mark_closed 已把等待中的 future 設為例外
    import asyncio
    import gc
    import logging

    import pytest

    clock = FakeClock(datetime(2026, 10, 22, 18, 0, tzinfo=TAIPEI))
    manager = ConnectionManager(clock)

    class ClosingSocket(FakeSocket):
        async def send_text(self, data: str) -> None:
            connection.mark_closed()
            raise ConnectionResetError("socket closed")

    connection = manager.register("CP01", ClosingSocket())
    caplog.set_level(logging.ERROR, logger="asyncio")
    with pytest.raises(ConnectionResetError):
        await connection.call("SetChargingProfile", {"limitW": 1400}, timeout_s=30)
    await asyncio.sleep(0)
    gc.collect()
    assert "never retrieved" not in caplog.text
