from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from chargeshare.app import create_app
from chargeshare.clock import TAIPEI, FakeClock
from chargeshare.db.models import Bill, Card, Charger, Household, PowerSnapshot, Transaction
from chargeshare.security import hash_password
from chargeshare.settings import Settings

NOW = datetime(2026, 11, 5, 20, 0, tzinfo=TAIPEI)


@pytest.fixture
def app(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'web.db'}", admin_password="board-pass"
    )
    return create_app(settings, FakeClock(NOW))


@pytest.fixture
def client(app):
    with TestClient(app) as client:
        with app.state.ctx.session_factory.begin() as session:
            mine = Household(unit_no="A01-01", password_hash=hash_password("pw1"))
            other = Household(unit_no="A01-02", password_hash=hash_password("pw2"))
            mine.cards = [Card(id_tag="CARD-MINE")]
            other.cards = [Card(id_tag="CARD-OTHER")]
            session.add_all([mine, other, Charger(id="CP01", ocpp_status="Available")])
            session.flush()
            for household, tag, amount in (
                (mine, "CARD-MINE", "26.95"),
                (other, "CARD-OTHER", "9.5"),
            ):
                tx = Transaction(
                    charger_id="CP01",
                    household=household,
                    id_tag=tag,
                    start_ts=datetime(2026, 10, 22, 10, 0, tzinfo=TAIPEI)
                    + timedelta(hours=household.id),
                    meter_start_wh=0,
                    stop_ts=datetime(2026, 10, 22, 12, 0, tzinfo=TAIPEI)
                    + timedelta(hours=household.id),
                    meter_stop_wh=7000,
                    peak_wh=3500,
                    offpeak_wh=3500,
                    amount=Decimal(amount),
                )
                session.add(
                    Bill(
                        household=household,
                        period="2026-10",
                        issued_at=NOW - timedelta(days=4),
                        peak_wh=3500,
                        offpeak_wh=3500,
                        total_amount=round(float(amount)),
                        transactions=[tx],
                    )
                )
            session.add_all(
                PowerSnapshot(
                    ts=NOW - timedelta(minutes=m),
                    measured_w=20_000 + m,
                    limit_w=28_000,
                    reserved_w=0,
                )
                for m in (90, 30, 1)
            )
        yield client


def login_resident(client, unit_no="A01-01", password="pw1"):
    return client.post(
        "/login", data={"unit_no": unit_no, "password": password}, follow_redirects=False
    )


def login_admin(client):
    return client.post(
        "/admin/login", data={"username": "admin", "password": "board-pass"}, follow_redirects=False
    )


def bill_id(app, unit_no):
    with app.state.ctx.session_factory() as session:
        from sqlalchemy import select

        return session.scalar(select(Bill.id).join(Household).where(Household.unit_no == unit_no))


def test_resident_login_rejects_wrong_password(client):
    response = login_resident(client, password="nope")
    assert response.status_code == 401
    assert "戶號或密碼錯誤" in response.text


def test_resident_sees_only_own_records(client):
    assert login_resident(client).status_code == 303
    page = client.get("/me")
    assert page.status_code == 200
    assert "A01-01" in page.text
    assert "CARD-MINE" in page.text
    assert "CARD-OTHER" not in page.text
    assert "27" in page.text  # 帳單總額


def test_resident_cannot_open_other_households_bill(client, app):
    login_resident(client)
    assert client.get(f"/me/bills/{bill_id(app, 'A01-01')}").status_code == 200
    assert client.get(f"/me/bills/{bill_id(app, 'A01-02')}").status_code == 404


def test_admin_pages_require_admin_login(client):
    login_resident(client)  # 住戶身分不能進管委會頁面
    response = client.get("/admin", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


def test_admin_dashboard_lists_chargers(client):
    assert login_admin(client).status_code == 303
    assert "CP01" in client.get("/admin").text
    partial = client.get("/admin/chargers")
    assert "CP01" in partial.text
    assert "斷線" in partial.text  # 沒有連線


def test_admin_marks_bill_paid(client, app):
    login_admin(client)
    target = bill_id(app, "A01-02")
    response = client.post(f"/admin/bills/{target}/paid", data={"paid": "true"})
    assert response.status_code == 200
    assert "已繳" in response.text
    with app.state.ctx.session_factory() as session:
        assert session.get(Bill, target).paid_at == NOW


def test_power_api_returns_last_hour_with_capacity(client):
    login_admin(client)
    data = client.get("/api/power").json()
    assert data["capacity_w"] == 30_000
    measured = [point["measured_w"] for point in data["points"]]
    assert measured[:2] == [20_030, 20_001]  # 90 分鐘前的點不在範圍內
    assert data["points"][-1]["ts"] == NOW.isoformat()  # 最後一點為即時值
