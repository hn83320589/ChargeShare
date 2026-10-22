from datetime import date

from chargeshare.domain.auth_rules import AuthStatus, CardInfo, add_months, decide_authorization
from tests.unit.conftest import tw

NOW = tw(2026, 11, 2, 18, 0)


def test_active_card_without_unpaid_bills_is_accepted():
    card = CardInfo(id_tag="CARD-0101-1", household_id=1, active=True)
    result = decide_authorization(card, unpaid_bill_dates=[], now=NOW)
    assert result.status == AuthStatus.ACCEPTED


def test_unknown_card_is_invalid():
    result = decide_authorization(None, unpaid_bill_dates=[], now=NOW)
    assert result.status == AuthStatus.INVALID


def test_disabled_card_is_blocked():
    # 驗收 2
    card = CardInfo(id_tag="CARD-0101-1", household_id=1, active=False)
    result = decide_authorization(card, unpaid_bill_dates=[], now=NOW)
    assert result.status == AuthStatus.BLOCKED
    assert "停用" in result.reason


def test_bill_unpaid_more_than_two_months_is_blocked():
    # 驗收 2：9/1 開立的帳單，11/2 已超過 2 個月
    card = CardInfo(id_tag="CARD-0101-1", household_id=1, active=True)
    result = decide_authorization(card, unpaid_bill_dates=[date(2026, 9, 1)], now=NOW)
    assert result.status == AuthStatus.BLOCKED
    assert "未繳" in result.reason


def test_bill_unpaid_exactly_two_months_is_still_accepted():
    card = CardInfo(id_tag="CARD-0101-1", household_id=1, active=True)
    result = decide_authorization(
        card, unpaid_bill_dates=[date(2026, 9, 1), date(2026, 10, 1)], now=tw(2026, 11, 1, 23, 59)
    )
    assert result.status == AuthStatus.ACCEPTED


def test_add_months_clamps_to_month_end():
    assert add_months(date(2026, 12, 31), 2) == date(2027, 2, 28)
    assert add_months(date(2026, 11, 30), 2) == date(2027, 1, 30)
