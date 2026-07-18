import logging
import datetime
import grpc
import grpc.aio
import asyncio
from datetime import datetime as dt, timedelta, timezone
from typing import Dict, Any, Optional
import copy
from .volvooncall_base import VehicleBaseAPI, gcj02towgs84
from .proto.exterior_pb2_grpc import ExteriorServiceStub
from .proto.exterior_pb2 import GetExteriorReq, GetExteriorResp, ExteriorStatus
from .proto.exterior_pb2 import LockStatus, OpenStatus
from .proto.health_pb2_grpc import HealthServiceStub
from .proto.health_pb2 import GetHealthReq, GetHealthResp, HealthStatus
from .proto.fuel_pb2_grpc import FuelServiceStub
from .proto.fuel_pb2 import GetFuelReq, GetFuelResp
from .proto.invocation_pb2_grpc import InvocationServiceStub
from .proto.invocation_pb2 import invocationHead, invocationStatus, invocationControlType, invocationCommResp
from .proto.invocation_pb2 import windowControlReq
from .proto.invocation_pb2 import EngineStartReq
from .proto.invocation_pb2 import HonkFlashReq, HonkFlashType
from .proto.invocation_pb2 import LockReq, LockType
from .proto.invocation_pb2 import UnlockReq, UnlockType
from .proto.invocation_pb2 import TailgateControlReq
from .proto.invocation_pb2 import SunroofControlReq
from .proto.invocation_pb2 import UpdateStatusReq
from .proto.invocation_pb2 import ClimatizationStartReq, ClimatizationStopReq
from .proto.odometer_pb2_grpc import OdometerServiceStub
from .proto.odometer_pb2 import GetOdometerReq, GetOdometerResp
from .proto.availability_pb2_grpc import AvailabilityServiceStub
from .proto.availability_pb2 import GetAvailabilityReq, GetAvailabilityResp, AvailabilityStatus, AvailabilityReason
from .proto.dtlinternet_pb2_grpc import DtlInternetServiceStub
from .proto.dtlinternet_pb2 import StreamLastKnownLocationsReq, StreamLastKnownLocationsResp
from .proto.engineremotestart_pb2_grpc import EngineRemoteStartServiceStub
from .proto.engineremotestart_pb2 import GetEngineRemoteStartReq, GetEngineRemoteStartResp, EngineRunningStatus
from .proto.parkingclimatization_pb2_grpc import ParkingClimatizationServiceStub
from .proto.parkingclimatization_pb2 import GetParkingClimatizationReq, GetParkingClimatizationResp, ParkingClimatizationStatus
from .proto.car_preferences_pb2_grpc import CarPreferencesStub
from .proto.car_preferences_pb2 import GetPreferencesReq, GetPreferencesResp
from .proto.car_preferences_pb2 import UpdatePreferencesReq, UpdatePreferencesResp, Preference
from .proto.battery_pb2_grpc import BatteryServiceStub
from .proto.battery_pb2 import GetLatestBatteryReq, GetLatestBatteryResp, BatteryStatus


_LOGGER = logging.getLogger(__name__)

GRPC_DIGITALVOLVO_HOST = "cepmobtoken.prod.c3.volvocars.com.cn:443"
GRPC_LBS_VOLVO_HOST = "cepmobtoken.lbs.prod.c3.volvocars.com.cn:443"
USER_AGENT = "vca-android/5.53.1 grpc-java-okhttp/1.68.0"
MAX_RETRIES = 1
TIMEOUT = datetime.timedelta(seconds=10)
DOMAIN = "volvooncall_cn"


def isWindowOpen(status) -> bool:
    return status == OpenStatus.OPEN_STATUS_OPEN or status == OpenStatus.OPEN_STATUS_AJAR


class VehicleAPI(VehicleBaseAPI):
    def __init__(self, session, username, password):
        super(VehicleAPI, self).__init__(session, username, password)
        self.channel = None
        self.lbs_channel = None
        self._channel_lock = asyncio.Lock()
        self._lbs_channel_lock = asyncio.Lock()

    def _metadata_callback(self, context, callback):
        token = self._vocapi_access_token.strip()
        metadata = [('authorization', f'Bearer {token}')]
        callback(metadata, None)

    async def gen_channel(self, target):
        callCreds = grpc.metadata_call_credentials(self._metadata_callback)
        sslCreds = grpc.ssl_channel_credentials()
        creds = grpc.composite_channel_credentials(sslCreds, callCreds)
        channel_options: tuple = (
            ("grpc.primary_user_agent", USER_AGENT),
            ('grpc.accept_encoding', 'gzip'),
            ('grpc.keepalive_time_ms', 60000),
            ('grpc.keepalive_timeout_ms', 10000),
            ('grpc.keepalive_permit_without_calls', 1),
        )
        channel = grpc.aio.secure_channel(target, creds, options=channel_options)
        return channel

    async def get_channel(self):
        if self.channel:
            return

        async with self._channel_lock:
            if not self.channel:
                self.channel = await self.gen_channel(GRPC_DIGITALVOLVO_HOST)

    async def get_lbs_channel(self):
        if self.lbs_channel:
            return

        async with self._lbs_channel_lock:
            if not self.lbs_channel:
                self.lbs_channel = await self.gen_channel(GRPC_LBS_VOLVO_HOST)

    def raise_invocation_fail(self, status):
        if status in [invocationStatus.SUCCESS, invocationStatus.SENT, invocationStatus.DELIVERED]:
            return
        if status == invocationStatus.CAR_OFFLINE:
            raise Exception("车辆离线或无网络")
        elif status in [invocationStatus.DELIVERY_TIMEOUT, invocationStatus.RESPONSE_TIMEOUT]:
            raise Exception("请求超时")
        elif status == invocationStatus.UNKNOWN_CAR_ERROR:
            raise Exception("车辆未知错误")
        elif status == invocationStatus.NOT_ALLOWED_PRIVACY_ENABLED:
            raise Exception("车辆隐私协议未同意")
        elif status == invocationStatus.NOT_ALLOWED_WRONG_USAGE_MODE:
            raise Exception("请求模式错误")
        elif status == invocationStatus.NOT_ALLOWED_CONFLICTING_INVOCATION:
            raise Exception("请求操作存在冲突")
        else:
            raise Exception("未知错误")

    async def get_fuel_status(self, vin) -> GetFuelResp:
        stub = FuelServiceStub(self.channel)
        req = GetFuelReq(vin=vin)
        metadata: list = [("vin", vin)]
        res = GetFuelResp()
        async for resp in stub.GetFuel(req, metadata=metadata, timeout=TIMEOUT.seconds):
            res = resp
            break
        return res

    async def get_exterior(self, vin) -> GetExteriorResp:
        stub = ExteriorServiceStub(self.channel)
        req = GetExteriorReq(vin=vin)
        metadata: list = [("vin", vin)]
        res = GetExteriorResp()
        async for resp in stub.GetExterior(req, metadata=metadata, timeout=TIMEOUT.seconds):
            res = resp
            break
        return res

    async def get_health(self, vin) -> GetHealthResp:
        stub = HealthServiceStub(self.channel)
        req = GetHealthReq(vin=vin)
        metadata: list = [("vin", vin)]
        res = GetHealthResp()
        async for resp in stub.GetHealth(req, metadata=metadata, timeout=TIMEOUT.seconds):
            res = resp
            _LOGGER.debug("get_health resp")
            _LOGGER.debug(res)
            break
        return res

    async def get_odometer(self, vin) -> GetOdometerResp:
        stub = OdometerServiceStub(self.channel)
        req = GetOdometerReq(vin=vin)
        metadata: list = [("vin", vin)]
        res = GetOdometerResp()
        async for resp in stub.GetOdometer(req, metadata=metadata, timeout=TIMEOUT.seconds):
            res = resp
            break
        return res

    async def get_availability(self, vin) -> GetAvailabilityResp:
        stub = AvailabilityServiceStub(self.channel)
        req = GetAvailabilityReq(vin=vin)
        metadata: list = [("vin", vin)]
        res = GetAvailabilityResp()
        async for resp in stub.GetAvailability(req, metadata=metadata, timeout=TIMEOUT.seconds):
            res = resp
            break
        return res

    async def window_control(self, vin, opentype):
        stub = InvocationServiceStub(self.channel)
        req_header = invocationHead(vin=vin)
        req = windowControlReq(head=req_header, openType=opentype)
        metadata: list = [("vin", vin)]
        res: invocationCommResp = invocationCommResp()
        async for resp in stub.WindowControl(req, metadata=metadata, timeout=TIMEOUT.seconds):
            res = resp
            _LOGGER.debug(res)
            self.raise_invocation_fail(res.data.status)
            break
        return

    async def get_location(self, vin) -> StreamLastKnownLocationsResp:
        await self.get_lbs_channel()
        stub = DtlInternetServiceStub(self.lbs_channel)
        req = StreamLastKnownLocationsReq(vin=vin)
        metadata: list = [("vin", vin)]
        res: StreamLastKnownLocationsResp = StreamLastKnownLocationsResp()
        async for resp in stub.StreamLastKnownLocations(req, metadata=metadata, timeout=TIMEOUT.seconds):
            res = resp
            break
        return res

    async def engine_control(self, vin, isStart: bool, duration: int):
        stub = InvocationServiceStub(self.channel)
        req_header = invocationHead(vin=vin)
        req = EngineStartReq()
        if isStart:
            req = EngineStartReq(head=req_header, isStart=isStart, startDurationMin=duration)
        else:
            req = EngineStartReq(head=req_header, isStart=isStart)
        metadata: list = [("vin", vin)]
        res: invocationCommResp = invocationCommResp()
        async for resp in stub.EngineStart(req, metadata=metadata, timeout=TIMEOUT.seconds):
            res = resp
            _LOGGER.debug(res)
            self.raise_invocation_fail(res.data.status)
            break
        return

    async def climatization_control(self, vin, start: bool):
        stub = InvocationServiceStub(self.channel)
        req_header = invocationHead(vin=vin)
        metadata: list = [("vin", vin)]
        res: invocationCommResp = invocationCommResp()
        if start:
            req = ClimatizationStartReq(head=req_header)
            call = stub.ClimatizationStart
        else:
            req = ClimatizationStopReq(head=req_header)
            call = stub.ClimatizationStop
        async for resp in call(req, metadata=metadata, timeout=TIMEOUT.seconds):
            res = resp
            _LOGGER.debug(res)
            self.raise_invocation_fail(res.data.status)
            break
        return

    async def honk_flash_control(self, vin, honk_flash_type: HonkFlashType):
        stub = InvocationServiceStub(self.channel)
        req_header = invocationHead(vin=vin)
        req = HonkFlashReq(head=req_header, honkFlashType=honk_flash_type)
        metadata: list = [("vin", vin)]
        res: invocationCommResp = invocationCommResp()
        async for resp in stub.HonkFlash(req, metadata=metadata, timeout=TIMEOUT.seconds):
            res = resp
            _LOGGER.debug(res)
            self.raise_invocation_fail(res.data.status)
            break
        return

    async def door_lock(self, vin):
        stub = InvocationServiceStub(self.channel)
        req_header = invocationHead(vin=vin)
        req = LockReq(head=req_header, lockType=LockType.LOCK_REDUCED_GUARD)
        metadata: list = [("vin", vin)]
        res: invocationCommResp = invocationCommResp()
        async for resp in stub.Lock(req, metadata=metadata, timeout=TIMEOUT.seconds):
            res = resp
            _LOGGER.debug(res)
            self.raise_invocation_fail(res.data.status)
            break
        return

    async def door_unlock(self, vin, unlockType):
        stub = InvocationServiceStub(self.channel)
        req_header = invocationHead(vin=vin)
        req = UnlockReq(head=req_header)
        if unlockType != UnlockType.UNLOCK_UNSPECIFIED:
            req = UnlockReq(head=req_header, unlockType=unlockType)
        metadata: list = [("vin", vin)]
        res: invocationCommResp = invocationCommResp()
        async for resp in stub.Unlock(req, metadata=metadata, timeout=TIMEOUT.seconds):
            res = resp
            _LOGGER.debug(res)
            self.raise_invocation_fail(res.data.status)
            break
        return

    async def get_engine_status(self, vin):
        stub = EngineRemoteStartServiceStub(self.channel)
        req = GetEngineRemoteStartReq(vin=vin)
        metadata: list = [("vin", vin)]
        res: GetEngineRemoteStartResp = GetEngineRemoteStartResp()
        async for resp in stub.GetEngineRemoteStart(req, metadata=metadata, timeout=TIMEOUT.seconds):
            res = resp
            _LOGGER.debug(res)
            break
        return res

    async def get_parking_climatization(self, vin):
        stub = ParkingClimatizationServiceStub(self.channel)
        req = GetParkingClimatizationReq(vin=vin)
        metadata: list = [("vin", vin)]
        res: GetParkingClimatizationResp = GetParkingClimatizationResp()
        async for resp in stub.GetParkingClimatization(req, metadata=metadata, timeout=TIMEOUT.seconds):
            res = resp
            _LOGGER.debug(res)
            break
        return res

    async def sunroof_contorl(self, vin: str, controlType: invocationControlType):
        stub = InvocationServiceStub(self.channel)
        req_header = invocationHead(vin=vin)
        req = SunroofControlReq(head=req_header, type=controlType)
        metadata: list = [("vin", vin)]
        res: invocationCommResp = invocationCommResp()
        async for resp in stub.SunroofControl(req, metadata=metadata, timeout=TIMEOUT.seconds):
            res = resp
            _LOGGER.debug(res)
            self.raise_invocation_fail(res.data.status)
            break
        return

    async def tailgate_contorl(self, vin: str, controlType: invocationControlType):
        stub = InvocationServiceStub(self.channel)
        req_header = invocationHead(vin=vin)
        req = TailgateControlReq(head=req_header, type=controlType)
        metadata: list = [("vin", vin)]
        res: invocationCommResp = invocationCommResp()
        async for resp in stub.TailgateControl(req, metadata=metadata, timeout=TIMEOUT.seconds):
            res = resp
            _LOGGER.debug(res)
            self.raise_invocation_fail(res.data.status)
            break
        return

    async def update_status(self, vin: str):
        stub = InvocationServiceStub(self.channel)
        req_header = invocationHead(vin=vin)
        req = UpdateStatusReq(head=req_header)
        metadata: list = [("vin", vin)]
        res: invocationCommResp = invocationCommResp()
        async for resp in stub.UpdateStatus(req, metadata=metadata, timeout=TIMEOUT.seconds):
            res = resp
            _LOGGER.debug("update_status resp")
            _LOGGER.debug(res)
            self.raise_invocation_fail(res.data.status)
            break
        return

    async def get_car_preferences(self, vin: str):
        stub = CarPreferencesStub(self.channel)
        req = GetPreferencesReq(vin=vin)
        metadata: list = [("vin", vin)]
        res: GetPreferencesResp = GetPreferencesResp()
        async for resp in stub.GetPreferences(req, metadata=metadata, timeout=TIMEOUT.seconds):
            res = resp
            _LOGGER.debug(res)
            break
        return res

    async def update_car_preference(self, vin: str, nickname: str):
        stub = CarPreferencesStub(self.channel)
        preference = Preference(nickName=nickname)
        req = UpdatePreferencesReq(vin=vin, preference=preference)
        metadata: list = [("vin", vin)]
        res: UpdatePreferencesResp = UpdatePreferencesResp()
        async for resp in stub.UpdatePreferences(req, metadata=metadata, timeout=TIMEOUT.seconds):
            res = resp
            _LOGGER.debug(res)
            break
        return res

    async def get_latest_battery(self, vin) -> GetLatestBatteryResp:
        stub = BatteryServiceStub(self.channel)
        req = GetLatestBatteryReq(vin=vin)
        metadata: list = [("vin", vin)]
        res = GetLatestBatteryResp()
        async for resp in stub.GetLatestBattery(req, metadata=metadata, timeout=TIMEOUT.seconds):
            res = resp
            break
        return res


def _read_unknown_varint(message, field_number):
    """Read a varint-typed field that is present on the wire but not declared
    in the .proto (so it lands in the message's unknown-field set).

    Used for battery fields we don't want to regenerate the generated protobuf
    modules for. Returns the int value, or None if absent/unreadable.
    """
    try:
        from google.protobuf.unknown_fields import UnknownFieldSet

        for field in UnknownFieldSet(message):
            # wire_type 0 == varint
            if field.field_number == field_number and field.wire_type == 0:
                return int(field.data)
    except Exception:  # pragma: no cover - defensive, never break parsing
        return None
    return None


def _derive_charging_status(
    *, has_battery, charge, charger_connected, home_pile_connector_status
):
    """Derive the display charging status from the available signals.

    The battery gRPC status alone cannot distinguish "plugged in but idle"
    from "actively charging" (chargingStatus #17 reads 2 in both cases), so
    treating plugged + <100% as "charging" is wrong. The home wallbox
    connectorStatus is the authoritative signal, and the Volvo app itself uses
    it for display:

        2 = 已插枪  (plugged in, not charging)
        3 = 充电中  (charging)

    ``home_pile_connector_status`` is that int when the car is engaged with its
    home pile, else None (no home pile, or pile not in use — e.g. public
    charging), in which case we fall back to the battery connection flag.

    Returns one of "disconnected" / "connected" / "charging" / "done", or None
    for non-PHEV/BEV cars.
    """
    if not has_battery:
        return None
    charge = charge or 0
    if home_pile_connector_status in (2, 3):
        plugged = True
        charging = home_pile_connector_status == 3
    else:
        plugged = charger_connected
        # No wallbox signal available: best-effort legacy heuristic.
        charging = plugged and charge < 100
    if not plugged:
        return "disconnected"
    if charging:
        return "charging"
    if charge >= 100:
        return "done"
    return "connected"


def _parse_record_dt(value):
    """Parse a charge-record timestamp ('2026-07-16 00:05:00') to naive datetime."""
    if not value:
        return None
    text = str(value).strip().replace("T", " ")
    for fmt in (
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d",
    ):
        try:
            return dt.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _parse_duration_minutes(value):
    """Parse a charge-session duration into whole minutes.

    The records API returns a pre-formatted Chinese string ('6时25分9秒');
    getPileList returns plain minutes. Returns an int, or None if unparseable.
    The charging card formats minutes for display, so it must receive a number."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(float(text))  # already a plain number of minutes
    except (TypeError, ValueError):
        pass
    total = 0
    found = False
    num = ""
    for ch in text:
        if ch.isdigit():
            num += ch
            continue
        if num and ch in ("时", "分", "秒"):
            if ch == "时":
                total += int(num) * 60
            elif ch == "分":
                total += int(num)
            else:  # 秒 -> round up to whole minutes
                total += round(int(num) / 60)
            found = True
        num = ""
    return total if found else None


class Vehicle(object):
    def __init__(self, vin, api, isAaos):
        self.vin = vin
        self._api = api
        self.isAaos = isAaos

        self.series_name = ""
        self.model_name = ""
        self.car_locked = False
        self.distance_to_empty = 0  # 续航公里
        self.tail_gate_open = False
        self.rear_right_door_open = False
        self.rear_left_door_open = False
        self.front_right_door_open = False
        self.front_left_door_open = False
        self.hood_open = False
        self.sunroof_open = False
        self.engine_running = False
        self.engine_remote_running = False
        self.climatization_status = ParkingClimatizationStatus.PARKING_CLIMATIZATION_STATUS_UNSPECIFIED
        self.climatization_on = False
        self.odo_meter = 0
        self.front_left_window_open = False
        self.front_right_window_open = False
        self.rear_left_window_open = False
        self.rear_right_window_open = False
        self.front_left_window_open_ajar = False
        self.front_right_window_open_ajar = False
        self.rear_left_window_open_ajar = False
        self.rear_right_window_open_ajar = False
        self.fuel_amount = 0
        self.fuel_average_consumption_liters_per_100_km = 0
        self.tank_lid_open = False
        self.availability_status = AvailabilityStatus.Available
        self.unavailable_reason = AvailabilityReason.Unspecified1
        self.engine_remote_start_time = 0
        self.engine_remote_end_time = 0
        # self.fuel_amount_level = 0
        self.position = {
            "longitude": 0.0,
            "latitude": 0.0
        }
        self.position_wgs84 = {
            "longitude": 0.0,
            "latitude": 0.0
        }
        self.service_warning_msg = "1"
        self.service_warning = False
        self.brake_fluid_level_warning = False
        self.engine_coolant_level_warning = False
        self.oil_level_warning = False
        self.washer_fluid_level_warning = False
        self.front_left_tyre_pressure_warning = False
        self.front_right_tyre_pressure_warning = False
        self.rear_left_tyre_pressure_warning = False
        self.rear_right_tyre_pressure_warning = False
        self.nickname = ""

        # Battery / charging (PHEV/BEV only; non-PHEV returns empty data)
        self.has_battery = False
        self.battery_charge_level = None
        self.electric_range = None
        self.energy_consumption = None
        self.charging_status = None
        self.charger_connected = False
        self.charging_power = None
        self.estimated_charging_time = None
        self.battery_raw = {}

        # Home wallbox (家充桩) — REST; only present if a Volvo-brand pile is bound
        self.series_code = ""
        self.has_home_pile = False
        self.home_pile_name = None
        self.home_pile_connector_status = None
        self.home_pile_plugged = False
        self.home_pile_charging = False
        # Live charge session metrics (from brandHomePile/status while charging).
        self.home_pile_power = None
        self.home_pile_eta = None
        # Live charging electrical metrics (brandHomePile/status: voltageA /
        # currentA / totalPower). Only populated while a session is active.
        self.charging_voltage = None
        self.charging_current = None
        self.charging_session_energy = None
        self.home_pile_appointment = None
        self.home_pile_last_energy = None
        self.home_pile_last_session = None
        self.home_pile_raw = {}
        # Identifiers needed to issue start/stop charging commands (home pile).
        self.home_pile_connector_id = None
        self.home_pile_phone = None
        self.home_pile_member_id = None
        self.home_pile_trade_no = None
        self.home_pile_equipment_id = None
        # Plug-and-charge (即插即充) enabled flag; None = unknown (see parse note).
        self.plug_and_charge_enabled = None
        # Last completed charge session + pile address, surfaced as attributes on
        # the charging_status sensor for the charging card's statistics section.
        self.last_charge_order = None
        self.charge_pile_address = None
        # Trailing-30-day + monthly stats: distance (from odometer statistics,
        # filled by the coordinator) and charged energy (from charge records).
        self.distance_last_30d = None
        self.distance_monthly = []
        self.energy_last_30d = None
        self.energy_monthly = []

        # Trip computer: TM = manual trip meter, AT = automatic trip meter
        self.trip_meter_manual = None
        self.trip_meter_auto = None
        self.trip_since_charge = None
        self.avg_speed_manual = None
        self.avg_speed_auto = None
        self.avg_speed_since_charge = None
        self.fuel_consumption_at = None
        # Maintenance (from listBindCar)
        self.next_maintenance_km = None
        self.distance_to_maintenance = None

        # Caching infrastructure for resilience
        self._cache: Dict[str, Any] = {}  # Stores last known good values
        self._cache_timestamp: Dict[str, dt] = {}  # Timestamps for each data source
        self._last_successful_update = dt.now(timezone.utc)
        self._consecutive_failures = 0
        self._data_source_status: Dict[str, bool] = {
            "exterior": True,
            "fuel": True,
            "odometer": True,
            "health": True,
            "location": True,
            "availability": True,
            "engine_status": True,
            "parking_climatization": True,
            "preference": True,
            "battery": True,
            "home_pile": True,
        }


    def _save_to_cache(self, source: str, data_dict: Dict[str, Any]):
        """Save successful data to cache."""
        self._cache[source] = copy.deepcopy(data_dict)
        self._cache_timestamp[source] = dt.now(timezone.utc)
        self._last_successful_update = dt.now(timezone.utc)
        self._data_source_status[source] = True
        self._consecutive_failures = 0
        _LOGGER.debug(f"Cached {source} data for VIN {self.vin}")
    
    def _restore_from_cache(self, source: str) -> bool:
        """Restore data from cache if available and not too old."""
        if source not in self._cache:
            return False
        
        # Check if cache is not too old (1 hour default)
        cache_age = dt.now(timezone.utc) - self._cache_timestamp.get(source, dt.min.replace(tzinfo=timezone.utc))
        if cache_age > timedelta(hours=1):
            _LOGGER.warning(f"Cache for {source} is too old ({cache_age}), not restoring")
            return False
        
        # Restore cached values
        cached_data = self._cache[source]
        for key, value in cached_data.items():
            if hasattr(self, key):
                setattr(self, key, value)
        
        _LOGGER.info(f"Restored {source} from cache (age: {cache_age}) for VIN {self.vin}")
        return True
    
    def get_cache_info(self) -> Dict[str, Any]:
        """Get cache status for diagnostics."""
        return {
            "last_update": self._last_successful_update,
            "consecutive_failures": self._consecutive_failures,
            "data_sources": self._data_source_status.copy(),
            "cached_sources": list(self._cache.keys()),
        }
    
    @property
    def connection_status(self) -> str:
        """Return connection status for diagnostic sensor."""
        if self._consecutive_failures == 0:
            return "Connected"
        elif self._consecutive_failures < 3:
            failed_sources = [k for k, v in self._data_source_status.items() if not v]
            return f"Degraded ({len(failed_sources)} sources failed)"
        else:
            return f"Disconnected ({self._consecutive_failures} failures)"
    
    @property
    def last_update_time(self) -> dt:
        """Return last successful update time for diagnostic sensor."""
        return self._last_successful_update

    async def _parse_exterior(self):
        try:
            exterior_resp: GetExteriorResp = await self._api.get_exterior(self.vin)
            exterior_status: ExteriorStatus = exterior_resp.data
            _LOGGER.debug(exterior_status)
            
            # Build data dict before setting attributes
            data = {
                "car_locked": exterior_status.central_lock == LockStatus.LOCK_STATUS_LOCKED,
                "front_left_door_open": isWindowOpen(exterior_status.front_left_door),
                "front_right_door_open": isWindowOpen(exterior_status.front_right_door),
                "rear_left_door_open": isWindowOpen(exterior_status.rear_left_door),
                "rear_right_door_open": isWindowOpen(exterior_status.rear_right_door),
                "sunroof_open": isWindowOpen(exterior_status.sunroof),
                "tail_gate_open": isWindowOpen(exterior_status.tailgate),
                "hood_open": isWindowOpen(exterior_status.hood),
                "tank_lid_open": isWindowOpen(exterior_status.tank_lid),
            }
            
            # Handle window sensors
            window_sensors = ["front_left_window", "front_right_window", "rear_left_window", "rear_right_window"]
            for window_sensor in window_sensors:
                status = getattr(exterior_status, window_sensor)
                openkey = window_sensor + "_open"
                ajarkey = window_sensor + "_open_ajar"
                if status == OpenStatus.OPEN_STATUS_OPEN:
                    data[openkey] = True
                    data[ajarkey] = False
                elif status == OpenStatus.OPEN_STATUS_AJAR:
                    data[openkey] = True
                    data[ajarkey] = True
                else:
                    data[openkey] = False
                    data[ajarkey] = False
            
            # Set attributes from data dict
            for key, value in data.items():
                setattr(self, key, value)
            
            # Cache successful data
            self._save_to_cache("exterior", data)
            
        except Exception as err:
            _LOGGER.exception(f"Failed to parse exterior for VIN {self.vin}: {err}")
            self._data_source_status["exterior"] = False
            # Try to restore from cache
            if not self._restore_from_cache("exterior"):
                _LOGGER.warning(f"No cache available for exterior data on VIN {self.vin}")
            return

    async def _parse_health(self):
        try:
            health_resp: GetHealthResp = await self._api.get_health(self.vin)
            health_status: HealthStatus = health_resp.data

            # Build data dict
            data = {
                "service_warning_msg": health_status.service_warning,
                "service_warning": health_status.service_warning > 1,
                "brake_fluid_level_warning": health_status.brake_fluid_level_warning > 1,
                "engine_coolant_level_warning": health_status.engine_coolant_level_warning > 1,
                "oil_level_warning": health_status.oil_level_warning > 1,
                "washer_fluid_level_warning": health_status.washer_fluid_level_warning > 1,
                "front_left_tyre_pressure_warning": health_status.front_left_tyre_pressure_warning > 1,
                "front_right_tyre_pressure_warning": health_status.front_right_tyre_pressure_warning > 1,
                "rear_left_tyre_pressure_warning": health_status.rear_left_tyre_pressure_warning > 1,
                "rear_right_tyre_pressure_warning": health_status.rear_right_tyre_pressure_warning > 1,
            }
            
            # Set attributes
            for key, value in data.items():
                setattr(self, key, value)
            
            # Cache successful data
            self._save_to_cache("health", data)

        except Exception as err:
            _LOGGER.exception(f"Failed to parse health for VIN {self.vin}: {err}")
            self._data_source_status["health"] = False
            if not self._restore_from_cache("health"):
                _LOGGER.warning(f"No cache available for health data on VIN {self.vin}")
            return

    async def _parse_fuel(self):
        try:
            fuel_resp: GetFuelResp = await self._api.get_fuel_status(self.vin)
            fuel_data = fuel_resp.data
            _LOGGER.debug(fuel_data)
            
            # Build data dict
            data = {
                "fuel_amount": round(fuel_data.fuelAmount, 2),
                "distance_to_empty": fuel_data.distanceToEmptyKm,
                "fuel_average_consumption_liters_per_100_km": fuel_data.TMFuelAvgConsum,
                "fuel_consumption_at": round(fuel_data.ATFuleAvgConsum, 1),
            }
            
            # Set attributes
            for key, value in data.items():
                setattr(self, key, value)
            
            # Cache successful data
            self._save_to_cache("fuel", data)
            
        except Exception as err:
            _LOGGER.exception(f"Failed to parse fuel for VIN {self.vin}: {err}")
            self._data_source_status["fuel"] = False
            if not self._restore_from_cache("fuel"):
                _LOGGER.warning(f"No cache available for fuel data on VIN {self.vin}")
            return

    async def _parse_odometer(self):
        try:
            odometer_resp: GetOdometerResp = await self._api.get_odometer(self.vin)
            odometer_data = odometer_resp.data
            _LOGGER.debug(odometer_data)
            
            # Build data dict (TM = manual trip, AT = automatic trip)
            odo_km = odometer_data.odometerMeters / 1000
            data = {
                "odo_meter": odo_km,
                "trip_meter_manual": round(odometer_data.tripMeterManualKm, 1),
                "trip_meter_auto": round(odometer_data.tripMeterAutomaticKm, 1),
                "trip_since_charge": odometer_data.tripMeterSinceChargeKm,
                "avg_speed_manual": odometer_data.averageSpeedKmPerHour,
                "avg_speed_auto": odometer_data.averageSpeedKmPerHourAutomatic,
                "avg_speed_since_charge": odometer_data.averageSpeedKmPerHourSinceCharge,
            }
            if self.next_maintenance_km:
                data["distance_to_maintenance"] = round(self.next_maintenance_km - odo_km, 1)

            # Set attributes
            for key, value in data.items():
                setattr(self, key, value)

            # Cache successful data
            self._save_to_cache("odometer", data)
            
        except Exception as err:
            _LOGGER.exception(f"Failed to parse odometer for VIN {self.vin}: {err}")
            self._data_source_status["odometer"] = False
            if not self._restore_from_cache("odometer"):
                _LOGGER.warning(f"No cache available for odometer data on VIN {self.vin}")
            return

    async def _parse_availability(self):
        try:
            availability_resp: GetAvailabilityResp = await self._api.get_availability(self.vin)
            availability_data = availability_resp.data
            _LOGGER.debug(availability_data)
            
            # Build data dict
            data = {
                "availability_status": availability_data.availableStatus,
                "unavailable_reason": availability_data.unavailableReason,
                "engine_running": (availability_data.availableStatus == AvailabilityStatus.Unavailable 
                                 and availability_data.unavailableReason == AvailabilityReason.CarInUse),
            }
            
            # Set attributes
            for key, value in data.items():
                setattr(self, key, value)
            
            # Cache successful data
            self._save_to_cache("availability", data)
            
        except Exception as err:
            _LOGGER.exception(f"Failed to parse availability for VIN {self.vin}: {err}")
            self._data_source_status["availability"] = False
            if not self._restore_from_cache("availability"):
                _LOGGER.warning(f"No cache available for availability data on VIN {self.vin}")
            return

    async def _parse_location(self):
        try:
            location_resp: StreamLastKnownLocationsResp = await self._api.get_location(self.vin)
            
            # Build data dict
            data = {
                "position": {
                    "latitude": location_resp.latitude,
                    "longitude": location_resp.longitude,
                },
            }
            
            # Calculate WGS84 coordinates
            wgs84_coords = gcj02towgs84(location_resp.longitude, location_resp.latitude)
            data["position_wgs84"] = {
                "longitude": wgs84_coords[0],
                "latitude": wgs84_coords[1],
            }
            
            # Set attributes
            for key, value in data.items():
                setattr(self, key, value)
            
            # Cache successful data
            self._save_to_cache("location", data)
            
        except Exception as err:
            _LOGGER.exception(f"Failed to parse location for VIN {self.vin}: {err}")
            self._data_source_status["location"] = False
            if not self._restore_from_cache("location"):
                _LOGGER.warning(f"No cache available for location data on VIN {self.vin}")
            return

    async def _parse_engine_status(self):
        try:
            if not self.isAaos:
                return
            
            engine_status_resp: GetEngineRemoteStartResp = await self._api.get_engine_status(self.vin)
            engine_status = engine_status_resp.data
            _LOGGER.debug(engine_status)
            
            # Build data dict
            data = {
                "engine_remote_running": engine_status.engineRunningStatus in (
                    EngineRunningStatus.Starting,
                    EngineRunningStatus.Running,
                ),
                "engine_remote_start_time": engine_status.engineStartTime.seconds,
                "engine_remote_end_time": engine_status.engineEndTime.seconds,
            }
            
            # Set attributes
            for key, value in data.items():
                setattr(self, key, value)
            
            # Cache successful data
            self._save_to_cache("engine_status", data)
            
        except Exception as err:
            _LOGGER.exception(f"Failed to parse engine status for VIN {self.vin}: {err}")
            self._data_source_status["engine_status"] = False
            if not self._restore_from_cache("engine_status"):
                _LOGGER.warning(f"No cache available for engine status data on VIN {self.vin}")
            return

    async def _parse_parking_climatization(self):
        try:
            if not self.isAaos:
                return

            resp: GetParkingClimatizationResp = await self._api.get_parking_climatization(self.vin)
            _LOGGER.debug(resp)
            status = resp.data.status

            # No stateful "on" flag comes back from the car; derive it from the
            # status enum. STARTING is treated as on so the control lights up
            # immediately after a start request (verified: 1=ON, 2=OFF, 3=STARTING).
            data = {
                "climatization_status": status,
                "climatization_on": status in (
                    ParkingClimatizationStatus.PARKING_CLIMATIZATION_STATUS_ON,
                    ParkingClimatizationStatus.PARKING_CLIMATIZATION_STATUS_STARTING,
                ),
            }

            for key, value in data.items():
                setattr(self, key, value)

            self._save_to_cache("parking_climatization", data)

        except Exception as err:
            _LOGGER.exception(f"Failed to parse parking climatization for VIN {self.vin}: {err}")
            self._data_source_status["parking_climatization"] = False
            if not self._restore_from_cache("parking_climatization"):
                _LOGGER.warning(f"No cache available for parking climatization data on VIN {self.vin}")
            return

    async def _parse_car_preference(self):
        try:
            preference_resp: GetPreferencesResp = await self._api.get_car_preferences(self.vin)
            _LOGGER.debug(preference_resp)
            
            # Build data dict
            data = {
                "nickname": preference_resp.preference.nickName,
            }
            
            # Set attributes
            for key, value in data.items():
                setattr(self, key, value)
            
            # Cache successful data
            self._save_to_cache("preference", data)
            
        except Exception as err:
            _LOGGER.exception(f"Failed to parse car preference for VIN {self.vin}: {err}")
            self._data_source_status["preference"] = False
            if not self._restore_from_cache("preference"):
                _LOGGER.warning(f"No cache available for preference data on VIN {self.vin}")
            return

    async def _parse_battery(self):
        try:
            battery_resp: GetLatestBatteryResp = await self._api.get_latest_battery(self.vin)
            # Non-PHEV models (e.g. mild hybrids) return an empty data payload.
            if not battery_resp.HasField("data"):
                self.has_battery = False
                return
            b: BatteryStatus = battery_resp.data
            _LOGGER.debug(b)

            # chargerConnectionStatus #6: 1=plugged, 2=unplugged. This only
            # tells us the plug is connected, NOT whether the car is actively
            # charging — the raw chargingStatus enum (#17) reads 2 both when
            # plugged+full and plugged+idle, so it can't distinguish charging
            # either. The actual charging state is reconciled after the update
            # using the home wallbox connectorStatus (see
            # _reconcile_charging_status); #17 is kept in battery_raw only.
            charge = round(b.batteryChargeLevel)
            plugged = b.chargerConnectionStatus == 1

            # estimatedChargingTimeToFullMinutes (field #5) is already parsed
            # into the generated message. chargingPowerWatts (field #10) is NOT
            # declared in battery.proto, so read it from the unknown-field set
            # to avoid regenerating the protobuf modules (dev/HA protobuf
            # runtime versions differ). Both are best-effort until validated
            # during an actual charge session (observed 0 when full/unplugged).
            estimated_charging_time = b.field5 or 0
            charging_power_watts = _read_unknown_varint(b, 10) or 0
            charging_power = round(charging_power_watts / 1000, 1)

            data = {
                "has_battery": True,
                "battery_charge_level": charge,
                "electric_range": b.electricRange,
                # Battery field #3 (named batteryVoltage in the generated proto)
                # is actually the average energy consumption in kWh/100km,
                # confirmed by the vehicle owner (not the 12V system voltage).
                "energy_consumption": round(b.batteryVoltage, 1),
                "charger_connected": plugged,
                "charging_power": charging_power,
                "estimated_charging_time": estimated_charging_time,
                # Not-yet-identified fields, exposed as diagnostics to label later.
                "battery_raw": {
                    "charging_status_raw": b.chargingStatus,
                    "connection_raw": b.chargerConnectionStatus,
                    "field5": b.field5,
                    "field7": b.field7,
                    "field8": b.field8,
                    "field26": b.field26,
                    "field28": b.field28,
                },
            }

            for key, value in data.items():
                setattr(self, key, value)

            self._save_to_cache("battery", data)

        except Exception as err:
            _LOGGER.exception(f"Failed to parse battery for VIN {self.vin}: {err}")
            self._data_source_status["battery"] = False
            if not self._restore_from_cache("battery"):
                _LOGGER.warning(f"No cache available for battery data on VIN {self.vin}")
            return

    async def _parse_home_pile(self):
        try:
            if not self.series_code:
                self.has_home_pile = False
                return
            pile = await self._api.get_home_pile(self.vin, self.series_code)
            if not pile:
                self.has_home_pile = False
                return
            records = await self._api.get_home_pile_records(pile.get("connectorId"))
            latest = records[0] if records else {}

            # Aggregate charge-session energy: trailing-30-day total + per-month
            # history (for the charging card). Records are past sessions with
            # chargeUsePower (kWh) and startTime.
            energy_30d = 0.0
            energy_by_month = {}
            cutoff = dt.now() - timedelta(days=30)
            for rec in records or []:
                try:
                    session_kwh = float(rec.get("chargeUsePower") or 0)
                except (TypeError, ValueError):
                    continue
                if session_kwh <= 0:
                    continue
                rec_dt = _parse_record_dt(rec.get("startTime"))
                if rec_dt is None:
                    continue
                month_key = rec_dt.strftime("%Y-%m")
                energy_by_month[month_key] = (
                    energy_by_month.get(month_key, 0.0) + session_kwh
                )
                if rec_dt >= cutoff:
                    energy_30d += session_kwh
            energy_monthly = [
                {"month": k, "kwh": round(v, 2)}
                for k, v in sorted(energy_by_month.items())
            ][-12:]
            try:
                last_energy = round(float(latest.get("chargeUsePower") or 0), 2)
            except (TypeError, ValueError):
                last_energy = None
            # Last completed session, shaped for the charging card statistics.
            last_charge_order = None
            if latest:
                last_charge_order = {
                    "energy_kwh": last_energy,
                    "duration": _parse_duration_minutes(latest.get("chargeUseTime")),
                    "start_time": latest.get("startTime"),
                    "end_time": latest.get("endTime"),
                    "order_no": latest.get("orderNo") or latest.get("tradeNo"),
                    "station_name": latest.get("stationName"),
                }
            appt = f'{pile.get("appointmentStartTime", "")}-{pile.get("appointmentEndTime", "")}'.strip("-")
            # connectorStatus: 2 = 已插枪 (plugged, idle), 3 = 充电中 (charging).
            connector_status = pile.get("connectorStatus")

            # Plug-and-charge (即插即充) state. The pile list does not expose a
            # reliable boolean (openEnabled is the share/open flag, not this), so
            # read plugAndChargeEnabled when present and otherwise leave it
            # unknown (None) — the switch is optimistic. TODO: confirm the real
            # field against a live toggle during validation.
            pnc = pile.get("plugAndChargeEnabled")
            plug_and_charge_enabled = bool(pnc) if pnc is not None else None

            # While charging, the wallbox live status carries the instantaneous
            # power (kW) and ETA — the battery gRPC does not (its power field
            # reads 0). getPileList's chargeUsePower is session *energy* (kWh),
            # not power, so it can't be used here.
            pile_power = None
            pile_eta = None
            charging_voltage = None
            charging_current = None
            charging_session_energy = None
            if connector_status == 3 and pile.get("tradeNo"):
                status = await self._api.get_home_pile_status(pile["tradeNo"], self.vin)
                if status:
                    try:
                        pile_power = round(float(status.get("power") or 0), 1)
                    except (TypeError, ValueError):
                        pile_power = None
                    try:
                        pile_eta = int(float(status.get("estimatedChargingTime") or 0))
                    except (TypeError, ValueError):
                        pile_eta = None
                    # Live A/V per phase A and this session's accumulated energy
                    # (kWh). Field names confirmed against a live charging dump.
                    try:
                        charging_voltage = round(float(status.get("voltageA")), 1)
                    except (TypeError, ValueError):
                        charging_voltage = None
                    try:
                        charging_current = round(float(status.get("currentA")), 2)
                    except (TypeError, ValueError):
                        charging_current = None
                    try:
                        charging_session_energy = round(float(status.get("totalPower")), 2)
                    except (TypeError, ValueError):
                        charging_session_energy = None

            data = {
                "has_home_pile": True,
                "home_pile_name": pile.get("equipmentName"),
                "home_pile_connector_status": pile.get("connectorStatusName"),
                "home_pile_plugged": connector_status in (2, 3),
                "home_pile_charging": connector_status == 3,
                "home_pile_power": pile_power,
                "home_pile_eta": pile_eta,
                "charging_voltage": charging_voltage,
                "charging_current": charging_current,
                "charging_session_energy": charging_session_energy,
                "home_pile_appointment": appt or None,
                "home_pile_last_energy": last_energy,
                "home_pile_last_session": latest.get("chargeUseTime"),
                # Identifiers for the start/stop charging commands. tradeNo is
                # the active session id (== startChargeSeq) while charging.
                "home_pile_connector_id": pile.get("connectorId"),
                "home_pile_phone": pile.get("phone"),
                "home_pile_member_id": pile.get("memberId"),
                "home_pile_trade_no": pile.get("tradeNo"),
                "home_pile_equipment_id": pile.get("equipmentId"),
                "plug_and_charge_enabled": plug_and_charge_enabled,
                "last_charge_order": last_charge_order,
                "charge_pile_address": pile.get("address"),
                "energy_last_30d": round(energy_30d, 2) if records else None,
                "energy_monthly": energy_monthly,
                "home_pile_raw": {
                    "connector_id": pile.get("connectorId"),
                    "connector_status": connector_status,
                    "address": pile.get("address"),
                    "plug_and_charge": pile.get("openEnabled"),
                    "last_start": latest.get("startTime"),
                    "last_end": latest.get("endTime"),
                    "last_stop_reason": latest.get("stopFailReason"),
                    "station_name": latest.get("stationName"),
                },
            }
            for key, value in data.items():
                setattr(self, key, value)
            self._save_to_cache("home_pile", data)
        except Exception as err:
            _LOGGER.exception(f"Failed to parse home pile for VIN {self.vin}: {err}")
            self._data_source_status["home_pile"] = False
            if not self._restore_from_cache("home_pile"):
                _LOGGER.warning(f"No cache available for home pile data on VIN {self.vin}")
            return

    async def update(self):
        if not self.series_name:
            vehicles = await self._api.get_vehicles()
            for vehicle in vehicles:
                if vehicle["vinCode"] == self.vin:
                    self.series_name = vehicle["seriesName"]
                    self.model_name = vehicle["modelName"]
                    self.series_code = vehicle.get("seriesCode", "")
                    try:
                        self.next_maintenance_km = float(vehicle["maintenanceKM"]) if vehicle.get("maintenanceKM") else None
                    except (TypeError, ValueError):
                        self.next_maintenance_km = None

        tasks = []
        await self._api.get_channel()
        async with asyncio.TaskGroup() as tg:
            funcs = [self._parse_exterior, self._parse_odometer,
                     self._parse_fuel, self._parse_availability,
                     self._parse_location, self._parse_engine_status,
                     self._parse_health, self._parse_car_preference,
                     self._parse_battery, self._parse_home_pile,
                     self._parse_parking_climatization]
            for runf in funcs:
                task = tg.create_task(runf())
                tasks.append(task)
        for task in tasks:
            _LOGGER.debug(task.result())

        # Battery and home-pile parse concurrently above; reconcile the final
        # charging status once both have completed.
        self._reconcile_charging_status()

    def _reconcile_charging_status(self):
        """Set charging_status from the reconciled battery + home-pile signals."""
        if not self.has_battery:
            return
        home_status = (
            self.home_pile_raw.get("connector_status") if self.has_home_pile else None
        )
        self.charging_status = _derive_charging_status(
            has_battery=self.has_battery,
            charge=self.battery_charge_level,
            charger_connected=self.charger_connected,
            home_pile_connector_status=home_status,
        )

        # Charging power (kW) and ETA (min): the wallbox live status is the
        # source of truth. The battery gRPC power field reads 0, so only trust
        # it as a fallback when no wallbox reading is available. When not
        # charging there is no power flowing and no ETA.
        if self.charging_status == "charging":
            if self.home_pile_power is not None:
                self.charging_power = self.home_pile_power
            if self.home_pile_eta is not None:
                self.estimated_charging_time = self.home_pile_eta
        else:
            self.charging_power = 0.0
            self.estimated_charging_time = 0

    async def home_pile_charge_start(self):
        """Start charging on the bound home wallbox."""
        await self._api.start_home_pile_charging(
            self.home_pile_connector_id,
            self.vin,
            self.home_pile_phone,
            self.home_pile_member_id,
        )

    async def home_pile_charge_stop(self):
        """Stop the active charging session on the bound home wallbox."""
        await self._api.stop_home_pile_charging(
            self.home_pile_trade_no,
            self.home_pile_connector_id,
        )

    async def set_plug_and_charge(self, enabled: bool):
        """Toggle plug-and-charge (即插即充) on the bound home wallbox."""
        await self._api.set_plug_and_charge(self.home_pile_equipment_id, enabled)

    async def sign_in(self):
        """Perform the app's daily member check-in (签到)."""
        return await self._api.sign_in(self.home_pile_member_id)

    async def lock_window(self):
        await self._api.window_control(self.vin, invocationControlType.CLOSE)

    async def unlock_window(self):
        await self._api.window_control(self.vin, invocationControlType.OPEN)

    async def lock_vehicle(self):
        await self._api.door_lock(self.vin)

    async def unlock_vehicle(self):
        await self._api.door_unlock(self.vin, UnlockType.UNLOCK_UNSPECIFIED)

    async def unlock_vehicle_trunk_only(self):
        await self._api.door_unlock(self.vin, UnlockType.TRUNK_ONLY)

    async def flash(self):
        await self._api.honk_flash_control(self.vin, HonkFlashType.FLASH)

    async def honk_and_flash(self):
        await self._api.honk_flash_control(self.vin, HonkFlashType.HONK_AND_FLASH)

    async def honk(self):
        await self._api.honk_flash_control(self.vin, HonkFlashType.HONK)

    async def engine_start(self, duration):
        await self._api.engine_control(self.vin, True, duration)

    async def engine_stop(self):
        await self._api.engine_control(self.vin, False, 0)

    async def climatization_start(self):
        await self._api.climatization_control(self.vin, True)

    async def climatization_stop(self):
        await self._api.climatization_control(self.vin, False)

    def get(self, key):
        if not hasattr(self, key):
            raise Exception(f"{key} not found")
        return getattr(self, key)

    async def tail_gate_control_open(self):
        await self._api.tailgate_contorl(self.vin, invocationControlType.OPEN)

    async def tail_gate_control_close(self):
        await self._api.tailgate_contorl(self.vin, invocationControlType.CLOSE)

    async def sunroof_control_open(self):
        await self._api.sunroof_contorl(self.vin, invocationControlType.OPEN)

    async def sunroof_control_close(self):
        await self._api.sunroof_contorl(self.vin, invocationControlType.CLOSE)
