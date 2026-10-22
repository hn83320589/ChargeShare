import asyncio
from datetime import datetime, timedelta

import pytest

from chargeshare.clock import TAIPEI, FakeClock, ScaledClock, SystemClock

START = datetime(2026, 10, 23, 19, 0, tzinfo=TAIPEI)


def test_system_clock_returns_taipei_aware_time():
    now = SystemClock().now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(hours=8)


def test_fake_clock_rejects_naive_start():
    with pytest.raises(ValueError):
        FakeClock(datetime(2026, 10, 23, 19, 0))


async def test_fake_clock_advance_moves_now():
    clock = FakeClock(START)
    await clock.advance(90)
    assert clock.now() == START + timedelta(seconds=90)


async def test_fake_clock_wakes_sleepers_in_deadline_order():
    clock = FakeClock(START)
    woke: list[tuple[str, datetime]] = []

    async def sleeper(name: str, seconds: float) -> None:
        await clock.sleep(seconds)
        woke.append((name, clock.now()))

    tasks = [
        asyncio.create_task(sleeper("late", 20)),
        asyncio.create_task(sleeper("early", 10)),
    ]
    await asyncio.sleep(0)
    await clock.advance(15)
    assert woke == [("early", START + timedelta(seconds=10))]

    await clock.advance(10)
    assert woke[-1] == ("late", START + timedelta(seconds=20))
    await asyncio.gather(*tasks)


async def test_fake_clock_wakes_chained_sleeps_within_one_advance():
    clock = FakeClock(START)
    stamps: list[datetime] = []

    async def two_phase() -> None:
        await clock.sleep(10)
        stamps.append(clock.now())
        await clock.sleep(10)
        stamps.append(clock.now())

    task = asyncio.create_task(two_phase())
    await asyncio.sleep(0)
    await clock.advance(30)
    await task
    assert stamps == [START + timedelta(seconds=10), START + timedelta(seconds=20)]
    assert clock.now() == START + timedelta(seconds=30)


async def test_scaled_clock_runs_faster_than_real_time():
    clock = ScaledClock(START, speed=600)
    await clock.sleep(60)  # 0.1 real seconds
    elapsed = clock.now() - START
    assert timedelta(seconds=60) <= elapsed < timedelta(seconds=120)
