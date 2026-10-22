"""同時執行整個模擬車隊並輸出紀錄。"""

import asyncio
from dataclasses import dataclass

from chargeshare.clock import Clock
from chargeshare.simulator.charge_point import SimConfig
from chargeshare.simulator.recorder import FleetMonitor, Recorder
from chargeshare.simulator.scenarios import FleetOptions, build_fleet


@dataclass
class FleetResult:
    recorder: Recorder
    monitor: FleetMonitor


async def run_fleet(
    options: FleetOptions,
    base_url: str,
    clock: Clock,
    minutes: float,
    config: SimConfig | None = None,
) -> FleetResult:
    recorder, monitor = Recorder(), FleetMonitor()
    fleet = build_fleet(options, base_url, clock, recorder, monitor, config)
    await asyncio.gather(*(charge_point.run(minutes * 60) for charge_point in fleet))
    return FleetResult(recorder, monitor)
