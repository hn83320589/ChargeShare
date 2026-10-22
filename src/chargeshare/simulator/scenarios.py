"""模擬情境：決定每支樁何時接上哪台車、何時斷線或重開機。"""

from dataclasses import dataclass

from chargeshare.clock import Clock
from chargeshare.seed import CHARGER_IDS, card_id
from chargeshare.simulator.charge_point import (
    Fault,
    FaultKind,
    SessionPlan,
    SimChargePoint,
    SimConfig,
)
from chargeshare.simulator.recorder import FleetMonitor, Recorder

SCENARIOS = ("normal", "taper", "disconnect", "reboot", "all")

TAPER_CHARGERS = ("CP01", "CP02")  # 插電混合車小電池、接近充滿
DISCONNECT = Fault(FaultKind.DISCONNECT, at_s=5 * 60, duration_s=10 * 60)
REBOOT = Fault(FaultKind.REBOOT, at_s=5 * 60, duration_s=60)


@dataclass(frozen=True)
class FleetOptions:
    scenario: str = "normal"
    battery_kwh: float = 60.0
    initial_soc: float = 0.2
    seed: int = 1


def build_fleet(
    options: FleetOptions,
    base_url: str,
    clock: Clock,
    recorder: Recorder,
    monitor: FleetMonitor,
    config: SimConfig | None = None,
) -> list[SimChargePoint]:
    if options.scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {options.scenario!r}; choose from {SCENARIOS}")
    tapering = options.scenario in ("taper", "all")
    fleet = []
    for index, charger_id in enumerate(CHARGER_IDS):
        battery_kwh, soc = options.battery_kwh, options.initial_soc
        if tapering and charger_id in TAPER_CHARGERS:
            battery_kwh, soc = 10.0, 0.88
        plan = SessionPlan(
            id_tag=card_id(1, index + 1, 1),
            plug_in_at_s=index * 20,
            battery_wh=battery_kwh * 1000,
            initial_soc=soc,
        )
        faults = []
        if charger_id == "CP03" and options.scenario in ("disconnect", "all"):
            faults.append(DISCONNECT)
        if charger_id == "CP04" and options.scenario in ("reboot", "all"):
            faults.append(REBOOT)
        fleet.append(
            SimChargePoint(
                charger_id,
                base_url,
                clock,
                recorder,
                monitor,
                plan,
                faults,
                config,
                seed=options.seed * 100 + index,
                meter_wh=1_000_000 + index * 37_000,
            )
        )
    return fleet
