"""簡化版 OCPP 1.6J 的 payload 定義（格式說明見 README）。"""

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class OcppModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


class BootNotificationRequest(OcppModel):
    charge_point_vendor: str
    charge_point_model: str
    firmware_version: str | None = None


class HeartbeatRequest(OcppModel):
    pass


class StatusNotificationRequest(OcppModel):
    connector_id: int = 1
    # Available / Preparing / Charging / SuspendedEV / SuspendedEVSE / Finishing / Faulted
    status: str
    error_code: str = "NoError"
    timestamp: AwareDatetime | None = None


class AuthorizeRequest(OcppModel):
    id_tag: str = Field(max_length=32)


class StartTransactionRequest(OcppModel):
    connector_id: int = 1
    id_tag: str = Field(max_length=32)
    meter_start: int = Field(ge=0)  # Wh
    timestamp: AwareDatetime


class MeterValuesRequest(OcppModel):
    connector_id: int = 1
    transaction_id: int
    timestamp: AwareDatetime
    energy_wh: int = Field(ge=0)  # 電表累計值
    power_w: int = Field(ge=0)  # 當下功率


class StopTransactionRequest(OcppModel):
    transaction_id: int
    meter_stop: int = Field(ge=0)
    timestamp: AwareDatetime
    reason: str | None = None
