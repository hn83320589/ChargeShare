"""測試用的充電樁端：送出 CALL、等待對應的 CALLRESULT，並自動回覆伺服器的 CALL。"""

import json
import uuid
from typing import Any


class FakeChargePoint:
    def __init__(self, ws) -> None:
        self.ws = ws
        self.server_calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, action: str, payload: dict[str, Any], message_id: str | None = None):
        message_id = message_id or uuid.uuid4().hex
        self.ws.send_text(json.dumps([2, message_id, action, payload]))
        while True:
            frame = json.loads(self.ws.receive_text())
            if frame[0] == 2:
                self.server_calls.append((frame[2], frame[3]))
                self.ws.send_text(json.dumps([3, frame[1], {"status": "Accepted"}]))
                continue
            if frame[1] != message_id:
                raise AssertionError(f"unexpected frame {frame}")
            if frame[0] == 4:
                return {"error": frame[2], "description": frame[3]}
            return frame[2]
