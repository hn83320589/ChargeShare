from datetime import datetime

from chargeshare.clock import TAIPEI
from chargeshare.db.models import Bill, Card, Household
from chargeshare.domain.auth_rules import AuthStatus
from chargeshare.services.authorization import authorize_id_tag

NOW = datetime(2026, 11, 5, 19, 0, tzinfo=TAIPEI)


def _household(session, unit_no: str, *cards: tuple[str, bool]) -> Household:
    household = Household(unit_no=unit_no, password_hash="x")
    household.cards = [Card(id_tag=tag, active=active) for tag, active in cards]
    session.add(household)
    session.flush()
    return household


def _bill(household: Household, period: str, issued: datetime, paid: bool) -> Bill:
    return Bill(
        household=household,
        period=period,
        issued_at=issued,
        peak_wh=0,
        offpeak_wh=0,
        total_amount=100,
        paid_at=issued if paid else None,
    )


def test_authorize_rejects_disabled_card_but_accepts_sibling_card(session):
    _household(session, "A01-01", ("CARD-A", True), ("CARD-B", False))
    assert authorize_id_tag(session, "CARD-A", NOW).status == AuthStatus.ACCEPTED
    assert authorize_id_tag(session, "CARD-B", NOW).status == AuthStatus.BLOCKED
    assert authorize_id_tag(session, "NO-SUCH", NOW).status == AuthStatus.INVALID


def test_authorize_rejects_household_with_overdue_bill(session):
    overdue = _household(session, "A01-02", ("CARD-C", True))
    session.add(_bill(overdue, "2026-08", datetime(2026, 9, 1, tzinfo=TAIPEI), paid=False))
    paid_up = _household(session, "A01-03", ("CARD-D", True))
    session.add(_bill(paid_up, "2026-08", datetime(2026, 9, 1, tzinfo=TAIPEI), paid=True))
    session.add(_bill(paid_up, "2026-09", datetime(2026, 10, 1, tzinfo=TAIPEI), paid=False))
    session.flush()

    assert authorize_id_tag(session, "CARD-C", NOW).status == AuthStatus.BLOCKED
    assert authorize_id_tag(session, "CARD-D", NOW).status == AuthStatus.ACCEPTED
