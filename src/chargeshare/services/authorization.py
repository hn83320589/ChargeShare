from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from chargeshare.db.models import Bill, Card
from chargeshare.domain.auth_rules import AuthResult, CardInfo, decide_authorization


def authorize_id_tag(session: Session, id_tag: str, now: datetime) -> AuthResult:
    card = session.get(Card, id_tag)
    if card is None:
        return decide_authorization(None, [], now)
    unpaid = session.scalars(
        select(Bill.issued_at).where(Bill.household_id == card.household_id, Bill.paid_at.is_(None))
    )
    return decide_authorization(
        CardInfo(card.id_tag, card.household_id, card.active),
        [issued.date() for issued in unpaid],
        now,
    )
