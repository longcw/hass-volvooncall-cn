"""Tests for the battery charging-power / charging-time field extraction.

charging_power comes from battery wire field #10 (chargingPowerWatts), which is
NOT declared in battery.proto, so it is read from the protobuf unknown-field set
rather than regenerating the generated modules. charging_time comes from the
already-declared field #5.
"""

from custom_components.volvooncall_cn.volvooncall_cn import _read_unknown_varint
from custom_components.volvooncall_cn.proto.battery_pb2 import BatteryStatus


def _with_field10(message: BatteryStatus, watts: int) -> BatteryStatus:
    """Append an undeclared varint field #10 to the wire and re-parse."""
    tag = bytes([(10 << 3) | 0])  # field 10, wire type 0 (varint)
    value = b""
    v = watts
    while True:
        byte = v & 0x7F
        v >>= 7
        value += bytes([byte | (0x80 if v else 0)])
        if not v:
            break
    return BatteryStatus.FromString(message.SerializeToString() + tag + value)


def test_reads_charging_power_from_unknown_field():
    msg = _with_field10(BatteryStatus(batteryChargeLevel=60.0, electricRange=40), 3300)
    watts = _read_unknown_varint(msg, 10)
    assert watts == 3300
    assert round(watts / 1000, 1) == 3.3


def test_missing_charging_power_is_none():
    msg = BatteryStatus(batteryChargeLevel=100.0, electricRange=54)
    assert _read_unknown_varint(msg, 10) is None


def test_declared_charging_time_field5_survives_roundtrip():
    msg = _with_field10(BatteryStatus(field5=95), 1500)
    assert msg.field5 == 95
    assert _read_unknown_varint(msg, 10) == 1500
