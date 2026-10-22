import pytest

from chargeshare.security import hash_password, verify_password
from chargeshare.web.views import ChargerView, display_status


def test_password_hash_round_trip():
    hashed = hash_password("s3cret")
    assert hashed != hash_password("s3cret")  # 每次加鹽不同
    assert verify_password("s3cret", hashed)
    assert not verify_password("wrong", hashed)


@pytest.mark.parametrize(
    ("online", "ocpp_status", "has_session", "queued", "expected"),
    [
        (False, "Charging", True, False, "斷線"),
        (True, "Faulted", False, False, "故障"),
        (True, "Charging", True, True, "排隊中"),
        (True, "SuspendedEVSE", True, False, "充電中"),
        (True, "Available", False, False, "可用"),
        (True, "Preparing", False, False, "可用"),
    ],
)
def test_display_status(online, ocpp_status, has_session, queued, expected):
    assert display_status(online, ocpp_status, has_session, queued) == expected


def test_charger_view_formats_kw():
    view = ChargerView("CP01", "充電中", limit_w=3750, power_w=3612, reserved_w=0)
    assert (view.limit_kw, view.power_kw) == ("3.75", "3.61")


def test_settings_report_insecure_defaults():
    from chargeshare.settings import Settings

    assert Settings(
        admin_password="admin", secret_key="dev-only-change-me"
    ).insecure_defaults() == [
        "CHARGESHARE_ADMIN_PASSWORD",
        "CHARGESHARE_SECRET_KEY",
    ]
    assert Settings(admin_password="x9!", secret_key="k" * 32).insecure_defaults() == []
