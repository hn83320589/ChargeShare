import random
from datetime import timedelta

from chargeshare.domain.allocation import ChargerInput, PowerConfig, allocate
from chargeshare.domain.demand import DemandConfig, DemandMode, DemandState, update_demand
from chargeshare.domain.dispatch import plan_dispatch
from tests.conftest import tw

CFG = DemandConfig()
HUNGRY = DemandState.initial(CFG)


def test_single_low_sample_does_not_saturate():
    state = update_demand(HUNGRY, power_w=800, limit_w=3_750, config=CFG)
    assert state.mode == DemandMode.HUNGRY
    assert state.cap_w == 7_000


def test_two_consecutive_low_samples_saturate_with_headroom():
    state = update_demand(HUNGRY, power_w=800, limit_w=3_750, config=CFG)
    state = update_demand(state, power_w=820, limit_w=3_750, config=CFG)
    assert state.mode == DemandMode.SATURATED
    assert state.cap_w == 1_500  # 820 + 500 → 以 500 W 級距向上取


def test_high_sample_resets_low_streak():
    state = update_demand(HUNGRY, power_w=800, limit_w=3_750, config=CFG)
    state = update_demand(state, power_w=3_700, limit_w=3_750, config=CFG)
    state = update_demand(state, power_w=800, limit_w=3_750, config=CFG)
    assert state.mode == DemandMode.HUNGRY


def test_noise_around_cap_edge_does_not_change_saturated_cap():
    state = DemandState(DemandMode.SATURATED, cap_w=1_500, low_streak=0)
    for power in (1_000, 980, 1_020, 990, 1_010, 1_000):
        state = update_demand(state, power_w=power, limit_w=1_500, config=CFG)
        assert state == DemandState(DemandMode.SATURATED, cap_w=1_500, low_streak=0)


def test_saturated_cap_steps_down_when_car_tapers_further():
    state = DemandState(DemandMode.SATURATED, cap_w=3_000, low_streak=0)
    state = update_demand(state, power_w=1_200, limit_w=3_000, config=CFG)
    assert state.cap_w == 2_000


def test_saturated_charger_recovers_when_it_uses_its_cap():
    state = DemandState(DemandMode.SATURATED, cap_w=1_500, low_streak=0)
    state = update_demand(state, power_w=1_450, limit_w=1_500, config=CFG)
    assert state == HUNGRY


def test_recovery_uses_limit_when_limit_is_below_cap():
    # 上限被分配壓在 cap 之下時，車用滿上限就代表它還要更多
    state = DemandState(DemandMode.SATURATED, cap_w=3_000, low_streak=0)
    state = update_demand(state, power_w=1_380, limit_w=1_400, config=CFG)
    assert state.mode == DemandMode.HUNGRY


def test_queued_charger_sample_is_ignored():
    assert update_demand(HUNGRY, power_w=0, limit_w=0, config=CFG) == HUNGRY


def test_no_repeated_dispatch_under_noise_after_two_cars_taper():
    # 驗收 4 的防振盪：2 台降到 1 kW 以下，其餘功率小幅波動，30 分鐘內只調整一次
    rng = random.Random(4)
    power_config = PowerConfig()
    t0 = tw(2026, 10, 22, 18, 0)
    ids = [f"CP0{i}" for i in range(1, 9)]
    states = {cid: DemandState.initial(CFG) for cid in ids}
    limits = {cid: 3_750 for cid in ids}
    dispatches = 0
    for _minute in range(30):
        for cid in ids:
            base = 900 if cid in ("CP01", "CP02") else limits[cid]
            power = round(base * rng.uniform(0.98, 1.0))
            states[cid] = update_demand(states[cid], power, limits[cid], CFG)
        inputs = [
            ChargerInput(cid, True, True, t0 + timedelta(minutes=i), states[cid].cap_w, limits[cid])
            for i, cid in enumerate(ids)
        ]
        plan = plan_dispatch(limits, allocate(inputs, power_config).limits, deadband_w=300)
        if not plan.is_empty:
            dispatches += 1
            limits.update(plan.decreases)
            limits.update(plan.increases)
        assert sum(limits.values()) <= power_config.capacity_w
    assert dispatches == 1
    assert limits["CP01"] == limits["CP02"] == 1_500
    assert {limits[cid] for cid in ids[2:]} == {4_500}
