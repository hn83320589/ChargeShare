import random
from datetime import timedelta

from chargeshare.domain.allocation import ChargerInput, PowerConfig, allocate
from tests.conftest import tw

CONFIG = PowerConfig(capacity_w=30_000, min_w=1_400, max_w=7_000, step_w=50)
T0 = tw(2026, 10, 22, 18, 0)


def hungry(charger_id: str, minute: int = 0, **overrides) -> ChargerInput:
    fields = dict(
        charger_id=charger_id,
        online=True,
        has_session=True,
        arrived_at=T0 + timedelta(minutes=minute),
        demand_cap_w=7_000,
        held_limit_w=0,
    )
    fields.update(overrides)
    return ChargerInput(**fields)


def fleet(n: int) -> list[ChargerInput]:
    return [hungry(f"CP{i:02d}", minute=i) for i in range(1, n + 1)]


def test_eight_hungry_chargers_share_equally():
    # 驗收 3
    result = allocate(fleet(8), CONFIG)
    assert set(result.limits.values()) == {3_750}
    assert result.total_w == 30_000
    assert result.queued == ()


def test_few_chargers_are_capped_at_max():
    result = allocate(fleet(4), CONFIG)
    assert set(result.limits.values()) == {7_000}
    assert result.total_w == 28_000


def test_saturated_chargers_release_power_to_others():
    # 驗收 4：2 支快充滿（需求上限 1.5 kW），其餘 6 支平分剩下的 27 kW
    chargers = fleet(8)
    chargers[0] = hungry("CP01", minute=1, demand_cap_w=1_500)
    chargers[1] = hungry("CP02", minute=2, demand_cap_w=1_500)
    result = allocate(chargers, CONFIG)
    assert result.limits["CP01"] == result.limits["CP02"] == 1_500
    assert {result.limits[f"CP0{i}"] for i in range(3, 9)} == {4_500}
    assert result.total_w == 30_000


def test_demand_cap_below_minimum_is_raised_to_minimum():
    chargers = [hungry("CP01", demand_cap_w=500), hungry("CP02", minute=1)]
    result = allocate(chargers, CONFIG)
    assert result.limits == {"CP01": 1_400, "CP02": 7_000}


def test_twenty_two_chargers_only_twenty_one_served():
    # 驗收 5：21 × 1.4 = 29.4 kW，第 22 支排隊
    result = allocate(fleet(22), CONFIG)
    served = {cid: w for cid, w in result.limits.items() if w > 0}
    assert len(served) == 21
    assert set(served.values()) == {1_400}
    assert result.total_w == 29_400
    assert result.queued == ("CP22",)


def test_queued_chargers_are_promoted_in_arrival_order():
    chargers = fleet(23)  # CP22、CP23 排隊
    assert allocate(chargers, CONFIG).queued == ("CP22", "CP23")

    remaining = [c for c in chargers if c.charger_id != "CP05"]  # 有人結束充電
    result = allocate(remaining, CONFIG)
    assert result.limits["CP22"] == 1_400
    assert result.queued == ("CP23",)


def test_queue_order_follows_arrival_time_not_id():
    chargers = fleet(21) + [hungry("CP99", minute=30), hungry("CP00", minute=40)]
    assert allocate(chargers, CONFIG).queued == ("CP99", "CP00")


def test_offline_charger_keeps_its_reserved_power():
    # 驗收 9：斷線樁的額度保留，其他樁上限不因此提高
    chargers = [hungry(f"CP0{i}", minute=i, held_limit_w=3_750) for i in range(1, 9)]
    chargers[0] = hungry("CP01", minute=1, online=False, held_limit_w=3_750)
    result = allocate(chargers, CONFIG)
    assert result.reserved == {"CP01": 3_750}
    assert "CP01" not in result.limits
    assert set(result.limits.values()) == {3_750}
    assert result.total_w == 30_000


def test_offline_charger_without_session_reserves_nothing():
    chargers = fleet(4) + [hungry("CP05", online=False, has_session=False, held_limit_w=0)]
    result = allocate(chargers, CONFIG)
    assert result.reserved == {}
    assert set(result.limits.values()) == {7_000}


def test_idle_online_chargers_get_no_allocation():
    chargers = fleet(2) + [hungry("CP03", has_session=False)]
    result = allocate(chargers, CONFIG)
    assert set(result.limits) == {"CP01", "CP02"}


def test_invariants_hold_for_random_inputs():
    rng = random.Random(20261022)
    for _ in range(2_000):
        chargers = [
            hungry(
                f"CP{i:02d}",
                minute=rng.randint(0, 120),
                online=rng.random() > 0.15,
                has_session=rng.random() > 0.2,
                demand_cap_w=rng.choice([7_000, rng.randint(0, 7_000)]),
                held_limit_w=rng.choice([0, 1_400, 3_750, 7_000]),
            )
            for i in range(rng.randint(0, 30))
        ]
        result = allocate(chargers, CONFIG)
        served = [w for w in result.limits.values() if w > 0]
        budget = max(CONFIG.capacity_w - sum(result.reserved.values()), 0)
        assert sum(served) <= budget
        assert all(CONFIG.min_w <= w <= CONFIG.max_w for w in served)
        assert all(w % CONFIG.step_w == 0 for w in served)
        if result.queued:
            assert len(served) == budget // CONFIG.min_w
