from chargeshare.simulator.ev import Battery


def test_battery_tapers_after_eighty_percent():
    battery = Battery(capacity_wh=10_000, soc=0.5)
    assert battery.acceptance_w() == 7_000
    battery.soc = 0.9
    assert battery.acceptance_w() == 3_500
    battery.soc = 0.98
    assert battery.acceptance_w() < 1_000
    battery.charge(1_000)
    assert battery.full
    assert battery.acceptance_w() == 0


async def test_sender_retrieves_failed_future_when_connection_drops_mid_send(caplog):
    # CI 偶發：送出時連線中斷，連線迴圈把等待中的 future 設為例外，
    # 送出端卻走 send() 的例外分支，future 沒被讀取 → asyncio 記錄 ERROR
    import asyncio
    import gc
    import logging
    from datetime import datetime

    from websockets.exceptions import ConnectionClosed

    from chargeshare.clock import TAIPEI, FakeClock
    from chargeshare.simulator.charge_point import SimChargePoint
    from chargeshare.simulator.recorder import FleetMonitor, Recorder

    clock = FakeClock(datetime(2026, 10, 22, 18, 0, tzinfo=TAIPEI))
    cp = SimChargePoint("CP01", "ws://unused", clock, Recorder(), FleetMonitor(), None)

    class DroppingSocket:
        async def send(self, text: str) -> None:
            for future in cp._waiting.values():
                future.set_exception(ConnectionError("disconnected"))
            raise ConnectionClosed(None, None)

    cp._ws = DroppingSocket()
    cp._connected.set()
    cp._queue("Heartbeat", {})
    caplog.set_level(logging.ERROR, logger="asyncio")
    sender = asyncio.create_task(cp._sender_loop())
    for _ in range(5):
        await asyncio.sleep(0)
    sender.cancel()
    await asyncio.gather(sender, return_exceptions=True)
    del sender
    gc.collect()
    assert "never retrieved" not in caplog.text
    assert cp._waiting == {}
