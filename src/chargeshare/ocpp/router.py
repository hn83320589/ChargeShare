"""處理充電樁送來的 CALL：訊息層去重、payload 驗證、分派到各 handler。"""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session, sessionmaker

from chargeshare.clock import Clock
from chargeshare.db.models import Charger, ProcessedMessage, Transaction
from chargeshare.domain.auth_rules import AuthStatus
from chargeshare.domain.tariff import Tariff
from chargeshare.ocpp.messages import (
    AuthorizeRequest,
    BootNotificationRequest,
    HeartbeatRequest,
    MeterValuesRequest,
    StartTransactionRequest,
    StatusNotificationRequest,
    StopTransactionRequest,
)
from chargeshare.ocpp.protocol import Call, OcppError
from chargeshare.services import transactions
from chargeshare.services.authorization import authorize_id_tag
from chargeshare.services.events import (
    ChargerBooted,
    Event,
    EventSink,
    MeterSampled,
    SessionStarted,
    SessionStopped,
    StatusChanged,
)

logger = logging.getLogger(__name__)

# 沒有副作用、不需要去重的訊息
STATELESS_ACTIONS = {"Heartbeat"}


@dataclass
class HandlerContext:
    session: Session
    charger: Charger
    events: list[Event]


Handler = Callable[[HandlerContext, Any], dict[str, Any]]


class ChargePointRouter:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        clock: Clock,
        tariff: Tariff,
        events: EventSink,
        heartbeat_interval_s: int,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._tariff = tariff
        self._events = events
        self._heartbeat_interval_s = heartbeat_interval_s
        self._handlers: dict[str, tuple[type[BaseModel], Handler]] = {
            "BootNotification": (BootNotificationRequest, self._boot_notification),
            "Heartbeat": (HeartbeatRequest, self._heartbeat),
            "StatusNotification": (StatusNotificationRequest, self._status_notification),
            "Authorize": (AuthorizeRequest, self._authorize),
            "StartTransaction": (StartTransactionRequest, self._start_transaction),
            "MeterValues": (MeterValuesRequest, self._meter_values),
            "StopTransaction": (StopTransactionRequest, self._stop_transaction),
        }

    def handle_call(self, charger_id: str, call: Call) -> dict[str, Any]:
        if call.action not in self._handlers:
            raise OcppError("NotImplemented", f"action {call.action} is not supported")
        model, handler = self._handlers[call.action]
        try:
            request = model.model_validate(call.payload)
        except ValidationError as exc:
            raise OcppError("FormationViolation", str(exc)) from exc

        dedupe = call.action not in STATELESS_ACTIONS
        with self._session_factory.begin() as session:
            charger = session.get(Charger, charger_id)
            if charger is None:
                raise OcppError("SecurityError", f"unknown charger {charger_id}")
            charger.last_seen_at = self._clock.now()
            if dedupe:
                cached = session.get(ProcessedMessage, (charger_id, call.message_id))
                if cached is not None:
                    logger.info("%s resent %s %s", charger_id, call.action, call.message_id)
                    return json.loads(cached.response_json)
            ctx = HandlerContext(session, charger, [])
            response = handler(ctx, request)
            if dedupe:
                session.add(
                    ProcessedMessage(
                        charger_id=charger_id,
                        message_id=call.message_id,
                        action=call.action,
                        response_json=json.dumps(response),
                        received_at=self._clock.now(),
                    )
                )
        # 交易 commit 後才發布事件，避免訂閱者讀到未寫入的資料
        for event in ctx.events:
            self._events.publish(event)
        return response

    def _boot_notification(self, ctx: HandlerContext, req: BootNotificationRequest):
        logger.info(
            "%s booted (%s %s)", ctx.charger.id, req.charge_point_vendor, req.firmware_version
        )
        ctx.events.append(ChargerBooted(ctx.charger.id))
        return {
            "status": "Accepted",
            "currentTime": self._clock.now().isoformat(),
            "interval": self._heartbeat_interval_s,
        }

    def _heartbeat(self, ctx: HandlerContext, req: HeartbeatRequest):
        return {"currentTime": self._clock.now().isoformat()}

    def _status_notification(self, ctx: HandlerContext, req: StatusNotificationRequest):
        if ctx.charger.ocpp_status != req.status:
            ctx.charger.ocpp_status = req.status
            ctx.events.append(StatusChanged(ctx.charger.id, req.status))
        return {}

    def _authorize(self, ctx: HandlerContext, req: AuthorizeRequest):
        result = authorize_id_tag(ctx.session, req.id_tag, self._clock.now())
        return {"idTagInfo": {"status": result.status.value}}

    def _start_transaction(self, ctx: HandlerContext, req: StartTransactionRequest):
        result = authorize_id_tag(ctx.session, req.id_tag, self._clock.now())
        if result.status != AuthStatus.ACCEPTED:
            logger.info("%s start rejected for %s: %s", ctx.charger.id, req.id_tag, result.reason)
            return {"transactionId": 0, "idTagInfo": {"status": result.status.value}}
        tx, created = transactions.find_or_start_transaction(
            ctx.session, ctx.charger.id, req.id_tag, req.meter_start, req.timestamp
        )
        if created:
            ctx.events.append(SessionStarted(ctx.charger.id, tx.id))
        return {"transactionId": tx.id, "idTagInfo": {"status": "Accepted"}}

    def _meter_values(self, ctx: HandlerContext, req: MeterValuesRequest):
        tx = self._own_transaction(ctx, req.transaction_id)
        sample = transactions.record_meter_value(
            ctx.session, self._tariff, tx, req.timestamp, req.energy_wh, req.power_w
        )
        if sample is not None and tx.is_active:
            ctx.events.append(MeterSampled(ctx.charger.id, tx.id, req.timestamp, req.power_w))
        return {}

    def _stop_transaction(self, ctx: HandlerContext, req: StopTransactionRequest):
        tx = self._own_transaction(ctx, req.transaction_id)
        if transactions.stop_transaction(
            ctx.session, self._tariff, tx, req.meter_stop, req.timestamp, req.reason
        ):
            ctx.events.append(SessionStopped(ctx.charger.id, tx.id))
        return {}

    def _own_transaction(self, ctx: HandlerContext, transaction_id: int) -> Transaction:
        tx = ctx.session.get(Transaction, transaction_id)
        if tx is None or tx.charger_id != ctx.charger.id:
            raise OcppError(
                "PropertyConstraintViolation",
                f"transaction {transaction_id} does not belong to {ctx.charger.id}",
            )
        return tx
