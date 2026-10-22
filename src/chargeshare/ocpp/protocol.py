"""OCPP-J frame 的解析與序列化。"""

import json
from dataclasses import dataclass
from typing import Any

CALL, CALLRESULT, CALLERROR = 2, 3, 4


class OcppError(Exception):
    """會以 CALLERROR 回覆給對方的錯誤。"""

    def __init__(self, code: str, description: str, message_id: str | None = None) -> None:
        super().__init__(f"{code}: {description}")
        self.code = code
        self.description = description
        self.message_id = message_id


@dataclass(frozen=True)
class Call:
    message_id: str
    action: str
    payload: dict[str, Any]

    def to_json(self) -> str:
        return json.dumps([CALL, self.message_id, self.action, self.payload])


@dataclass(frozen=True)
class CallResult:
    message_id: str
    payload: dict[str, Any]

    def to_json(self) -> str:
        return json.dumps([CALLRESULT, self.message_id, self.payload])


@dataclass(frozen=True)
class CallError:
    message_id: str
    code: str
    description: str
    details: dict[str, Any]

    def to_json(self) -> str:
        return json.dumps([CALLERROR, self.message_id, self.code, self.description, self.details])


Frame = Call | CallResult | CallError


def parse_frame(text: str) -> Frame:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise OcppError("FormationViolation", f"invalid JSON: {exc}") from exc
    if not isinstance(data, list) or len(data) < 3 or not isinstance(data[1], str):
        raise OcppError("FormationViolation", f"not an OCPP-J frame: {text[:100]}")
    kind, message_id = data[0], data[1]
    match kind, len(data):
        case 2, 4 if isinstance(data[2], str) and isinstance(data[3], dict):
            return Call(message_id, data[2], data[3])
        case 3, 3 if isinstance(data[2], dict):
            return CallResult(message_id, data[2])
        case 4, 5:
            return CallError(message_id, str(data[2]), str(data[3]), data[4] or {})
        case 2 | 3 | 4, _:
            raise OcppError("FormationViolation", f"malformed frame: {text[:100]}", message_id)
    raise OcppError("ProtocolError", f"unknown message type {kind!r}", message_id)
