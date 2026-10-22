import json

import pytest

from chargeshare.ocpp.protocol import Call, CallError, CallResult, OcppError, parse_frame


def test_parse_call_and_result_frames():
    call = parse_frame('[2, "m1", "Heartbeat", {}]')
    assert call == Call("m1", "Heartbeat", {})
    assert parse_frame('[3, "m1", {"currentTime": "x"}]') == CallResult("m1", {"currentTime": "x"})
    error = parse_frame('[4, "m1", "InternalError", "boom", {}]')
    assert error == CallError("m1", "InternalError", "boom", {})


@pytest.mark.parametrize("text", ["not json", "{}", '[9, "m", {}]', '[2, "m", "Heartbeat"]'])
def test_malformed_frames_raise_ocpp_error(text):
    with pytest.raises(OcppError) as exc:
        parse_frame(text)
    assert exc.value.code in {"FormationViolation", "ProtocolError"}


def test_frames_serialize_to_ocpp_j_arrays():
    assert json.loads(Call("m2", "SetChargingProfile", {"limitW": 3750}).to_json()) == [
        2,
        "m2",
        "SetChargingProfile",
        {"limitW": 3750},
    ]
    assert json.loads(CallResult("m2", {}).to_json()) == [3, "m2", {}]
    assert json.loads(CallError("m2", "NotImplemented", "x", {}).to_json()) == [
        4,
        "m2",
        "NotImplemented",
        "x",
        {},
    ]
