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
