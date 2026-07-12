from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class GetLatestBatteryReq(_message.Message):
    __slots__ = ("vin",)
    VIN_FIELD_NUMBER: _ClassVar[int]
    vin: str
    def __init__(self, vin: _Optional[str] = ...) -> None: ...

class Timestamp(_message.Message):
    __slots__ = ("seconds", "nanos")
    SECONDS_FIELD_NUMBER: _ClassVar[int]
    NANOS_FIELD_NUMBER: _ClassVar[int]
    seconds: int
    nanos: int
    def __init__(self, seconds: _Optional[int] = ..., nanos: _Optional[int] = ...) -> None: ...

class BatteryStatus(_message.Message):
    __slots__ = ("updateTime", "batteryChargeLevel", "batteryVoltage", "electricRange", "field5", "chargerConnectionStatus", "field7", "field8", "chargingStatus", "field26", "field28")
    UPDATETIME_FIELD_NUMBER: _ClassVar[int]
    BATTERYCHARGELEVEL_FIELD_NUMBER: _ClassVar[int]
    BATTERYVOLTAGE_FIELD_NUMBER: _ClassVar[int]
    ELECTRICRANGE_FIELD_NUMBER: _ClassVar[int]
    FIELD5_FIELD_NUMBER: _ClassVar[int]
    CHARGERCONNECTIONSTATUS_FIELD_NUMBER: _ClassVar[int]
    FIELD7_FIELD_NUMBER: _ClassVar[int]
    FIELD8_FIELD_NUMBER: _ClassVar[int]
    CHARGINGSTATUS_FIELD_NUMBER: _ClassVar[int]
    FIELD26_FIELD_NUMBER: _ClassVar[int]
    FIELD28_FIELD_NUMBER: _ClassVar[int]
    updateTime: Timestamp
    batteryChargeLevel: float
    batteryVoltage: float
    electricRange: int
    field5: int
    chargerConnectionStatus: int
    field7: int
    field8: int
    chargingStatus: int
    field26: int
    field28: int
    def __init__(self, updateTime: _Optional[_Union[Timestamp, _Mapping]] = ..., batteryChargeLevel: _Optional[float] = ..., batteryVoltage: _Optional[float] = ..., electricRange: _Optional[int] = ..., field5: _Optional[int] = ..., chargerConnectionStatus: _Optional[int] = ..., field7: _Optional[int] = ..., field8: _Optional[int] = ..., chargingStatus: _Optional[int] = ..., field26: _Optional[int] = ..., field28: _Optional[int] = ...) -> None: ...

class GetLatestBatteryResp(_message.Message):
    __slots__ = ("vin", "data")
    VIN_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    vin: str
    data: BatteryStatus
    def __init__(self, vin: _Optional[str] = ..., data: _Optional[_Union[BatteryStatus, _Mapping]] = ...) -> None: ...
