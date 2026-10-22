"""目前在線的充電樁連線，以及伺服器主動送出的 CALL。"""

import asyncio
import uuid
from typing import Any, Protocol

from chargeshare.clock import Clock
from chargeshare.ocpp.protocol import Call, CallError, CallResult


class TextSocket(Protocol):
    async def send_text(self, data: str) -> None: ...

    async def close(self, code: int = 1000) -> None: ...


class ChargerOffline(Exception):
    pass


class CallTimeout(Exception):
    pass


class ChargerConnection:
    def __init__(self, charger_id: str, socket: TextSocket, clock: Clock) -> None:
        self.charger_id = charger_id
        self._socket = socket
        self._clock = clock
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.closed = False

    async def send(self, text: str) -> None:
        await self._socket.send_text(text)

    async def call(self, action: str, payload: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        if self.closed:
            raise ChargerOffline(self.charger_id)
        message_id = uuid.uuid4().hex
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        timer = asyncio.ensure_future(self._clock.sleep(timeout_s))
        try:
            await self.send(Call(message_id, action, payload).to_json())
            await asyncio.wait({future, timer}, return_when=asyncio.FIRST_COMPLETED)
            if not future.done():
                raise CallTimeout(f"{self.charger_id} did not answer {action} in {timeout_s}s")
            return future.result()
        finally:
            timer.cancel()
            self._pending.pop(message_id, None)

    def resolve(self, frame: CallResult | CallError) -> None:
        future = self._pending.get(frame.message_id)
        if future is None or future.done():
            return
        if isinstance(frame, CallResult):
            future.set_result(frame.payload)
        else:
            future.set_exception(RuntimeError(f"{frame.code}: {frame.description}"))

    def mark_closed(self) -> None:
        self.closed = True
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ChargerOffline(self.charger_id))


class ConnectionManager:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._connections: dict[str, ChargerConnection] = {}

    def register(self, charger_id: str, socket: TextSocket) -> ChargerConnection:
        previous = self._connections.get(charger_id)
        if previous is not None:
            previous.mark_closed()
        connection = ChargerConnection(charger_id, socket, self._clock)
        self._connections[charger_id] = connection
        return connection

    def unregister(self, connection: ChargerConnection) -> bool:
        """回傳 True 表示該樁已無任何連線（舊連線被新連線取代時回傳 False）。"""
        connection.mark_closed()
        if self._connections.get(connection.charger_id) is connection:
            del self._connections[connection.charger_id]
            return True
        return False

    def is_online(self, charger_id: str) -> bool:
        return charger_id in self._connections

    async def call(
        self, charger_id: str, action: str, payload: dict[str, Any], timeout_s: float = 30
    ) -> dict[str, Any]:
        connection = self._connections.get(charger_id)
        if connection is None:
            raise ChargerOffline(charger_id)
        return await connection.call(action, payload, timeout_s)
