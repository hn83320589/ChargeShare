"""通訊層發布給功率管理等元件的事件。發布端不等待處理結果。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class ChargerConnected:
    charger_id: str


@dataclass(frozen=True)
class ChargerDisconnected:
    charger_id: str


@dataclass(frozen=True)
class ChargerBooted:
    charger_id: str


@dataclass(frozen=True)
class StatusChanged:
    charger_id: str
    status: str


@dataclass(frozen=True)
class SessionStarted:
    charger_id: str
    transaction_id: int


@dataclass(frozen=True)
class SessionStopped:
    charger_id: str
    transaction_id: int


@dataclass(frozen=True)
class MeterSampled:
    charger_id: str
    transaction_id: int
    ts: datetime
    power_w: int


Event = (
    ChargerConnected
    | ChargerDisconnected
    | ChargerBooted
    | StatusChanged
    | SessionStarted
    | SessionStopped
    | MeterSampled
)


class EventSink(Protocol):
    def publish(self, event: Event) -> None: ...


class NullEventSink:
    def publish(self, event: Event) -> None:
        pass
