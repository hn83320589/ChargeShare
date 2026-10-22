"""充電樁 WebSocket 端點：ws://host/ocpp/{charger_id}"""

import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from chargeshare.db.models import Charger
from chargeshare.ocpp.protocol import Call, CallError, CallResult, OcppError, parse_frame
from chargeshare.services.events import ChargerConnected, ChargerDisconnected

logger = logging.getLogger(__name__)

SUBPROTOCOL = "ocpp1.6"
POLICY_VIOLATION = 1008

router = APIRouter()


@router.websocket("/ocpp/{charger_id}")
async def ocpp_socket(websocket: WebSocket, charger_id: str) -> None:
    ctx = websocket.app.state.ctx
    with ctx.session_factory() as session:
        known = session.get(Charger, charger_id) is not None
    if not known:
        logger.warning("reject unknown charger %s", charger_id)
        await websocket.close(code=POLICY_VIOLATION)
        return

    offered = websocket.scope.get("subprotocols", [])
    await websocket.accept(subprotocol=SUBPROTOCOL if SUBPROTOCOL in offered else None)
    connection = ctx.connections.register(charger_id, websocket)
    _set_connected(ctx, charger_id, True)
    ctx.events.publish(ChargerConnected(charger_id))
    try:
        while True:
            text = await websocket.receive_text()
            reply = _handle_text(ctx, charger_id, connection, text)
            if reply is not None:
                await connection.send(reply)
    except WebSocketDisconnect:
        logger.info("%s disconnected", charger_id)
    finally:
        if ctx.connections.unregister(connection):
            _set_connected(ctx, charger_id, False)
            ctx.events.publish(ChargerDisconnected(charger_id))


def _handle_text(ctx, charger_id: str, connection, text: str) -> str | None:
    try:
        frame = parse_frame(text)
    except OcppError as exc:
        logger.warning("%s sent malformed frame: %s", charger_id, exc)
        return CallError(exc.message_id or "-1", exc.code, exc.description, {}).to_json()

    if not isinstance(frame, Call):
        connection.resolve(frame)
        return None
    try:
        return CallResult(frame.message_id, ctx.router.handle_call(charger_id, frame)).to_json()
    except OcppError as exc:
        logger.warning("%s %s rejected: %s", charger_id, frame.action, exc)
        return CallError(frame.message_id, exc.code, exc.description, {}).to_json()
    except Exception:
        logger.exception("%s %s failed", charger_id, frame.action)
        return CallError(frame.message_id, "InternalError", "server error", {}).to_json()


def _set_connected(ctx, charger_id: str, connected: bool) -> None:
    with ctx.session_factory.begin() as session:
        charger = session.get(Charger, charger_id)
        charger.connected = connected
        now = ctx.clock.now()
        charger.last_seen_at = now
        if not connected:
            charger.disconnected_at = now
