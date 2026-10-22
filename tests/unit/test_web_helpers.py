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


def test_secret_key_is_generated_once_and_kept_private(tmp_path):
    import stat

    from chargeshare.security import load_or_create_secret_key

    path = tmp_path / "data" / "secret_key"
    first = load_or_create_secret_key(path)
    assert len(first) >= 43  # 32 bytes 以上的亂數
    assert load_or_create_secret_key(path) == first  # 重啟後沿用，既有 session 不失效
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_or_create_secret_key(tmp_path / "other") != first


def test_default_admin_password_requires_dev_mode(tmp_path):
    from chargeshare.settings import InsecureConfiguration, Settings

    db = f"sqlite:///{tmp_path / 'x.db'}"
    with pytest.raises(InsecureConfiguration, match="--dev"):
        Settings(database_url=db, admin_password="admin").ensure_secure()
    Settings(database_url=db, admin_password="admin", dev_mode=True).ensure_secure()
    Settings(database_url=db, admin_password="a-strong-one").ensure_secure()


def test_secret_key_file_lives_next_to_database(tmp_path):
    from chargeshare.settings import Settings

    settings = Settings(database_url=f"sqlite:///{tmp_path / 'db' / 'x.db'}")
    assert settings.secret_key_path == tmp_path / "db" / "secret_key"
