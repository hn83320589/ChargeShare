from chargeshare.domain.dispatch import plan_dispatch


def test_decreases_and_increases_are_separated():
    plan = plan_dispatch(
        current={"CP01": 3_750, "CP02": 3_750, "CP03": 3_750},
        target={"CP01": 1_500, "CP02": 4_500, "CP03": 3_750},
        deadband_w=300,
    )
    assert plan.decreases == {"CP01": 1_500}
    assert plan.increases == {"CP02": 4_500}


def test_small_increase_is_skipped_but_small_decrease_is_sent():
    plan = plan_dispatch(
        current={"CP01": 4_500, "CP02": 4_500},
        target={"CP01": 4_700, "CP02": 4_400},
        deadband_w=300,
    )
    assert plan.increases == {}
    assert plan.decreases == {"CP02": 4_400}


def test_charger_missing_from_target_is_lowered_to_zero():
    plan = plan_dispatch(current={"CP01": 3_750}, target={}, deadband_w=300)
    assert plan.decreases == {"CP01": 0}


def test_new_charger_starting_from_zero_is_an_increase():
    plan = plan_dispatch(current={}, target={"CP01": 1_400}, deadband_w=300)
    assert plan.increases == {"CP01": 1_400}
    assert plan.is_empty is False
    assert plan_dispatch(current={"CP01": 1_400}, target={"CP01": 1_400}, deadband_w=300).is_empty
