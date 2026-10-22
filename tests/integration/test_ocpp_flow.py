from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from starlette.websockets import WebSocketDisconnect

from chargeshare.app import create_app
from chargeshare.clock import TAIPEI, FakeClock
from chargeshare.db.models import Card, Charger, Household, MeterSample, Transaction
from chargeshare.settings import Settings
from tests.integration.ocpp_client import FakeChargePoint

NOW = datetime(2026, 10, 23, 22, 0, tzinfo=TAIPEI)  # 週五


@pytest.fixture
def app(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'app.db'}")
    app = create_app(settings, FakeClock(NOW))
    return app


@pytest.fixture
def client(app):
    with TestClient(app) as client:
        ctx = app.state.ctx
        with ctx.session_factory.begin() as session:
            household = Household(unit_no="A01-01", password_hash="x")
            household.cards = [
                Card(id_tag="CARD-OK", active=True),
                Card(id_tag="CARD-OFF", active=False),
            ]
            session.add(household)
            session.add_all([Charger(id="CP01"), Charger(id="CP02")])
        yield client


def connect(client, charger_id="CP01"):
    return client.websocket_connect(f"/ocpp/{charger_id}", subprotocols=["ocpp1.6"])


def ts(minutes: int) -> str:
    return (NOW + timedelta(minutes=minutes)).isoformat()


def start(cp, meter_start=0, minutes=0, message_id=None):
    return cp.call(
        "StartTransaction",
        {"connectorId": 1, "idTag": "CARD-OK", "meterStart": meter_start, "timestamp": ts(minutes)},
        message_id,
    )


def meter(cp, tx_id, minutes, energy_wh, power_w=7000, message_id=None):
    payload = {
        "connectorId": 1,
        "transactionId": tx_id,
        "timestamp": ts(minutes),
        "energyWh": energy_wh,
        "powerW": power_w,
    }
    return cp.call("MeterValues", payload, message_id)


def stop(cp, tx_id, minutes, meter_stop, message_id=None):
    payload = {"transactionId": tx_id, "meterStop": meter_stop, "timestamp": ts(minutes)}
    return cp.call("StopTransaction", payload, message_id)


def load_tx(app, tx_id) -> Transaction:
    with app.state.ctx.session_factory() as session:
        return session.get(Transaction, tx_id)


def sample_count(app, tx_id) -> int:
    with app.state.ctx.session_factory() as session:
        return session.scalar(
            select(func.count()).select_from(MeterSample).where(MeterSample.transaction_id == tx_id)
        )


def test_boot_notification_is_accepted_and_marks_charger_connected(client, app):
    with connect(client) as ws:
        result = FakeChargePoint(ws).call(
            "BootNotification", {"chargePointVendor": "Acme", "chargePointModel": "AC7"}
        )
        assert result["status"] == "Accepted"
        assert result["interval"] == 60
        with app.state.ctx.session_factory() as session:
            assert session.get(Charger, "CP01").connected is True
    with app.state.ctx.session_factory() as session:
        assert session.get(Charger, "CP01").connected is False


def test_unknown_charger_is_rejected(client):
    with pytest.raises(WebSocketDisconnect), connect(client, "CP99") as ws:
        ws.receive_text()


def test_authorize_over_websocket(client):
    with connect(client) as ws:
        cp = FakeChargePoint(ws)
        assert cp.call("Authorize", {"idTag": "CARD-OK"})["idTagInfo"]["status"] == "Accepted"
        assert cp.call("Authorize", {"idTag": "CARD-OFF"})["idTagInfo"]["status"] == "Blocked"


def test_start_transaction_with_blocked_card_creates_nothing(client, app):
    with connect(client) as ws:
        payload = {"connectorId": 1, "idTag": "CARD-OFF", "meterStart": 0, "timestamp": ts(0)}
        result = FakeChargePoint(ws).call("StartTransaction", payload)
    assert result["idTagInfo"]["status"] == "Blocked"
    assert result["transactionId"] == 0


def test_full_session_is_priced_on_stop(client, app):
    # 週五 22:00–24:00 尖峰 14 度 × 4.8 + 管理費 7 = 74.2
    with connect(client) as ws:
        cp = FakeChargePoint(ws)
        tx_id = start(cp)["transactionId"]
        for minute in range(1, 120):
            meter(cp, tx_id, minute, round(7000 * minute / 60))
        stop(cp, tx_id, 120, 14000)
    tx = load_tx(app, tx_id)
    assert (tx.peak_wh, tx.offpeak_wh) == (14000, 0)
    assert str(tx.amount) == "74.2"


def test_resending_same_meter_message_three_times_is_counted_once(client, app):
    # 驗收 8
    with connect(client) as ws:
        cp = FakeChargePoint(ws)
        tx_id = start(cp)["transactionId"]
        meter(cp, tx_id, 1, 117)
        responses = [meter(cp, tx_id, 30, 3500, message_id="mv-30") for _ in range(3)]
        assert responses == [{}, {}, {}]
        for _ in range(3):
            stop(cp, tx_id, 60, 7000, message_id="stop-1")
    tx = load_tx(app, tx_id)
    assert sample_count(app, tx_id) == 2
    assert tx.peak_wh + tx.offpeak_wh == 7000


def test_same_reading_with_new_message_id_is_not_duplicated(client, app):
    # 樁重開機後可能用新的 messageId 重送同一筆讀數
    with connect(client) as ws:
        cp = FakeChargePoint(ws)
        tx_id = start(cp)["transactionId"]
        meter(cp, tx_id, 1, 117, message_id="a")
        meter(cp, tx_id, 1, 117, message_id="b")
    assert sample_count(app, tx_id) == 1


def test_start_transaction_resent_returns_same_transaction(client):
    with connect(client) as ws:
        cp = FakeChargePoint(ws)
        first = start(cp, message_id="s1")["transactionId"]
        assert start(cp, message_id="s1")["transactionId"] == first
        assert start(cp, message_id="s2")["transactionId"] == first


def test_buffered_messages_replayed_after_reconnect_are_billed(client, app):
    # 驗收 8：斷線期間的 MeterValues 與 StopTransaction 於重連後補送
    with connect(client) as ws:
        cp = FakeChargePoint(ws)
        tx_id = start(cp, minutes=0)["transactionId"]
        for minute in range(1, 11):
            meter(cp, tx_id, minute, round(7000 * minute / 60))
    with connect(client) as ws:  # 重新連線後依序補送
        cp = FakeChargePoint(ws)
        cp.call("BootNotification", {"chargePointVendor": "Acme", "chargePointModel": "AC7"})
        for minute in range(11, 120):
            meter(cp, tx_id, minute, round(7000 * minute / 60))
        stop(cp, tx_id, 120, 14000)
    tx = load_tx(app, tx_id)
    assert sample_count(app, tx_id) == 119
    assert tx.peak_wh == 14000


def test_late_sample_after_stop_reprices_transaction(client, app):
    with connect(client) as ws:
        cp = FakeChargePoint(ws)
        tx_id = start(cp, minutes=0)["transactionId"]
        stop(cp, tx_id, 180, 21000)  # 22:00–01:00，中間讀數晚到
        first = load_tx(app, tx_id)
        assert first.peak_wh == 14000  # 線性分攤：前 2/3 時間在尖峰
        meter(cp, tx_id, 120, 18000)  # 24:00 已累計 18 度
    tx = load_tx(app, tx_id)
    assert (tx.peak_wh, tx.offpeak_wh) == (18000, 3000)


def test_unknown_action_returns_not_implemented(client):
    with connect(client) as ws:
        result = FakeChargePoint(ws).call("DataTransfer", {})
    assert result["error"] == "NotImplemented"


def test_invalid_payload_returns_call_error(client):
    with connect(client) as ws:
        result = FakeChargePoint(ws).call("Authorize", {"wrong": 1})
    assert result["error"] in {"FormationViolation", "TypeConstraintViolation"}


def test_server_sends_charging_profile_after_start(client):
    with connect(client) as ws:
        cp = FakeChargePoint(ws)
        start(cp)
        cp.call("Heartbeat", {})
    assert ("SetChargingProfile", {"connectorId": 1, "limitW": 7000}) in cp.server_calls


def test_meter_reset_mid_charge_is_accumulated_and_flagged(client, app):
    # 樁重開機後電表從 0 開始：1,111,213 → 1,111,330 → 重置 → 4 → 56 → 364 → 停止 400
    # 分段累加：117 + 4 + 52 + 308 + 36 = 517 Wh
    with connect(client) as ws:
        cp = FakeChargePoint(ws)
        tx_id = start(cp, meter_start=1_111_213)["transactionId"]
        for minute, energy in [(1, 1_111_330), (2, 4), (3, 56), (5, 364)]:
            assert meter(cp, tx_id, minute, energy) == {}
        assert stop(cp, tx_id, 6, 400) == {}
    tx = load_tx(app, tx_id)
    assert tx.stop_ts is not None
    assert tx.peak_wh + tx.offpeak_wh == 517
    assert tx.amount is not None
    assert tx.meter_resets == 1
    assert tx.needs_review


def test_meter_reset_right_before_stop_is_accepted(client, app):
    # 重置後還沒送出任何讀數就結束：100 + 30 = 130 Wh
    with connect(client) as ws:
        cp = FakeChargePoint(ws)
        tx_id = start(cp, meter_start=5_000)["transactionId"]
        meter(cp, tx_id, 1, 5_100)
        assert stop(cp, tx_id, 2, 30) == {}
    tx = load_tx(app, tx_id)
    assert tx.peak_wh + tx.offpeak_wh == 130
    assert tx.meter_resets == 1
