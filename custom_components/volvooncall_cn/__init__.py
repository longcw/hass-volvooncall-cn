from datetime import timedelta
import logging
import asyncio
import re
from pathlib import Path

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory

from homeassistant.components.sensor import SensorEntity
from homeassistant.components.http import StaticPathConfig
from homeassistant.exceptions import ConfigEntryAuthFailed, ServiceValidationError
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.const import CONF_USERNAME, CONF_PASSWORD, CONF_SCAN_INTERVAL

from .store import VolvoStore, CHARGE_LIMIT_DISABLED
from homeassistant.util import dt as dt_util
from .volvooncall_base import DEFAULT_SCAN_INTERVAL
from .volvooncall_cn import VehicleAPI
from .volvooncall_cn import Vehicle
from .volvooncall_cn import DOMAIN

PLATFORMS = {
    "sensor": "sensor",
    "binary_sensor": "binary_sensor",
    "device_tracker": "device_tracker",
    "lock": "lock",
    "button": "button",
    "number": "number",
    "switch": "switch",
    "time": "time",
}

_LOGGER = logging.getLogger(__name__)

# Bundled Lovelace cards, served by the integration.
FRONTEND_PATH = Path(__file__).parent / "frontend"
FRONTEND_URL_PATH = f"/{DOMAIN}/frontend"
CARD_RESOURCE_PATH = f"{FRONTEND_URL_PATH}/volvo-car-card.js"
CHARGING_CARD_RESOURCE_PATH = f"{FRONTEND_URL_PATH}/volvo-charging-card.js"
# (filename, served resource path) for every bundled card.
BUNDLED_CARDS = (
    ("volvo-car-card.js", CARD_RESOURCE_PATH),
    ("volvo-charging-card.js", CHARGING_CARD_RESOURCE_PATH),
)

# Daily charge-timer policy. The battery must sit at least the deadband below
# the charge limit before the timer books a session — a couple of percent is not
# worth one — and the timer only fires this long after its time, so a Home
# Assistant that was down all night does not start charging at breakfast.
CHARGE_TIMER_DEADBAND = 5
CHARGE_TIMER_GRACE = timedelta(minutes=60)

# Services for the refuel log (see services.yaml).
SERVICE_LOG_REFUEL = "log_refuel"
SERVICE_UPDATE_REFUEL = "update_refuel"
SERVICE_DELETE_REFUEL = "delete_refuel"
ATTR_VIN = "vin"
ATTR_LITERS = "liters"
ATTR_ODOMETER = "odometer"
ATTR_RECORD_ID = "record_id"
ATTR_AT = "at"


def _read_card_version(filename: str) -> str:
    """Read CARD_VERSION from a bundled card JS, so the cache-busting resource
    URL has a single source of truth (the .js file) instead of a second constant
    that silently drifts out of sync on every card change."""
    try:
        text = (FRONTEND_PATH / filename).read_text(encoding="utf-8")
        match = re.search(r'CARD_VERSION\s*=\s*"([^"]+)"', text)
        if match:
            return match.group(1)
    except Exception:  # pragma: no cover - defensive, never block setup
        pass
    return "0"


async def _async_register_card_resource(
    hass: HomeAssistant, resource_path: str, card_url: str
) -> None:
    """Auto-register a bundled card as a storage-mode Lovelace resource.

    Lovelace internals are imported lazily and every failure degrades to a log
    hint ("add the resource manually") so a Home Assistant version mismatch can
    never break integration setup.
    """
    try:
        from homeassistant.components.lovelace import LOVELACE_DATA
        from homeassistant.components.lovelace.const import MODE_STORAGE
    except ImportError:
        _LOGGER.info(
            "Lovelace internals unavailable; add %s as a module resource manually",
            card_url,
        )
        return

    lovelace = hass.data.get(LOVELACE_DATA)
    if lovelace is None:
        _LOGGER.info(
            "Lovelace not loaded; add %s as a module resource manually",
            card_url,
        )
        return

    if getattr(lovelace, "resource_mode", None) != MODE_STORAGE:
        _LOGGER.info(
            "Lovelace resources use YAML mode; add %s as a module resource",
            card_url,
        )
        return

    resources = lovelace.resources
    await resources.async_get_info()
    for item in resources.async_items() or []:
        url = item.get("url", "")
        if url.split("?", 1)[0] != resource_path:
            continue
        # Already registered; bump the cache-busting version if it changed.
        if url != card_url:
            await resources.async_update_item(
                item["id"],
                {"res_type": "module", "url": card_url},
            )
        return

    await resources.async_create_item(
        {"res_type": "module", "url": card_url}
    )


def _async_resolve_store(hass: HomeAssistant, vin: str):
    """(coordinator, idx, store) for a VIN, across every configured account."""
    target = str(vin or "").strip().lower()
    for coordinator in (hass.data.get(DOMAIN) or {}).values():
        for idx, vehicle in enumerate(coordinator.data or []):
            if str(getattr(vehicle, "vin", "")).lower() != target:
                continue
            if idx >= len(coordinator.store_datas):
                break
            return coordinator, idx, coordinator.store_datas[idx]
    raise ServiceValidationError(f"No Volvo vehicle found for VIN {vin}")


async def _async_register_refuel_services(hass: HomeAssistant) -> None:
    """Services backing the car card's 加油记录 dialog.

    The log lives in the per-VIN store, so these only touch local state: after
    mutating it we push the new value to the entities instead of polling the
    car."""
    if hass.services.has_service(DOMAIN, SERVICE_LOG_REFUEL):
        return

    def _resolve_when(value):
        """A local-aware timestamp; a bare `2026-07-20 08:00` means local time."""
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
        return dt_util.as_local(value)

    async def _handle_log_refuel(call: ServiceCall) -> None:
        coordinator, idx, store = _async_resolve_store(hass, call.data[ATTR_VIN])
        odometer = call.data.get(ATTR_ODOMETER)
        if odometer is None:
            odometer = getattr(coordinator.data[idx], "odo_meter", None)
        record = await store.add_refuel(
            call.data[ATTR_LITERS],
            odometer,
            _resolve_when(call.data.get(ATTR_AT)) or dt_util.now(),
        )
        if record is None:
            raise ServiceValidationError("Refuel litres must be greater than 0")
        coordinator.async_update_listeners()

    async def _handle_update_refuel(call: ServiceCall) -> None:
        coordinator, _idx, store = _async_resolve_store(hass, call.data[ATTR_VIN])
        updated = await store.update_refuel(
            call.data[ATTR_RECORD_ID],
            liters=call.data.get(ATTR_LITERS),
            odometer=call.data.get(ATTR_ODOMETER),
            when=_resolve_when(call.data.get(ATTR_AT)),
        )
        if not updated:
            raise ServiceValidationError(
                f"No refuel record {call.data[ATTR_RECORD_ID]} to update"
            )
        coordinator.async_update_listeners()

    async def _handle_delete_refuel(call: ServiceCall) -> None:
        coordinator, _idx, store = _async_resolve_store(hass, call.data[ATTR_VIN])
        if not await store.delete_refuel(call.data[ATTR_RECORD_ID]):
            raise ServiceValidationError(
                f"No refuel record {call.data[ATTR_RECORD_ID]} to delete"
            )
        coordinator.async_update_listeners()

    hass.services.async_register(
        DOMAIN,
        SERVICE_LOG_REFUEL,
        _handle_log_refuel,
        schema=vol.Schema({
            vol.Required(ATTR_VIN): cv.string,
            vol.Required(ATTR_LITERS): vol.Coerce(float),
            vol.Optional(ATTR_ODOMETER): vol.Coerce(float),
            vol.Optional(ATTR_AT): cv.datetime,
        }),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_UPDATE_REFUEL,
        _handle_update_refuel,
        schema=vol.Schema({
            vol.Required(ATTR_VIN): cv.string,
            vol.Required(ATTR_RECORD_ID): cv.string,
            vol.Optional(ATTR_LITERS): vol.Coerce(float),
            vol.Optional(ATTR_ODOMETER): vol.Coerce(float),
            vol.Optional(ATTR_AT): cv.datetime,
        }),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_DELETE_REFUEL,
        _handle_delete_refuel,
        schema=vol.Schema({
            vol.Required(ATTR_VIN): cv.string,
            vol.Required(ATTR_RECORD_ID): cv.string,
        }),
    )


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Serve and register the bundled Volvo cards (runs once at integration load)."""
    try:
        await hass.http.async_register_static_paths(
            [StaticPathConfig(FRONTEND_URL_PATH, str(FRONTEND_PATH), True)]
        )
        for filename, resource_path in BUNDLED_CARDS:
            version = await hass.async_add_executor_job(_read_card_version, filename)
            card_url = f"{resource_path}?v={version}"
            await _async_register_card_resource(hass, resource_path, card_url)
    except Exception as err:  # pragma: no cover - never block integration setup
        _LOGGER.warning("Volvo card frontend setup failed: %s", err)
    await _async_register_refuel_services(hass)
    return True


async def async_update_options(hass: HomeAssistant, config_entry: ConfigEntry):
    # entry = {**config_entry.data, **config_entry.options}
    config_data = {**config_entry.data, **config_entry.options}
    entry_id = config_entry.entry_id

    username = config_data.get(CONF_USERNAME)
    password = config_data.get(CONF_PASSWORD)
    interval = config_data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    _LOGGER.info("new interval: %s", interval)
    session = async_get_clientsession(hass)
    volvo_api = VehicleAPI(session=session, username=username, password=password)
    hass.data.setdefault(DOMAIN, {})
    if config_entry.entry_id in hass.data[DOMAIN]:
        coordinator = hass.data[DOMAIN][entry_id]
        coordinator.volvo_api = volvo_api
        coordinator.update_interval = timedelta(seconds=interval)


async def async_setup_entry(hass, entry):
    """Config entry example."""
    session = async_get_clientsession(hass)

    username = entry.data.get(CONF_USERNAME)
    password = entry.data.get(CONF_PASSWORD)
    interval = entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    volvo_api = VehicleAPI(session=session, username=username, password=password)
    hass.data.setdefault(DOMAIN, {})
    coordinator = hass.data[DOMAIN][entry.entry_id] = VolvoCoordinator(hass, volvo_api, interval)

    # Fetch initial data so we have data when entities subscribe
    #
    # If the refresh fails, async_config_entry_first_refresh will
    # raise ConfigEntryNotReady and setup will try again later
    #
    # If you do not want to retry setup on failure, use
    # coordinator.async_refresh() instead
    #
    if not entry.update_listeners:
        entry.add_update_listener(async_update_options)
    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


class VolvoCoordinator(DataUpdateCoordinator):
    """My custom coordinator."""

    def __init__(self, hass, volvo_api, scan_interval):
        """Initialize my coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            # Name of the data. For logging purposes.
            name="Volvo On Call CN sensor",
            # Polling interval. Will only be polled if there are subscribers.
            update_interval=timedelta(seconds=scan_interval),
        )
        self.volvo_api = volvo_api
        self.store_datas = []
        # Persistent per-VIN store, reused across polls (rebuilt list each poll
        # so store_datas stays aligned to the vehicle order without leaking).
        self._stores_by_vin = {}
        # Connection health tracking
        self._consecutive_failures = 0
        self._last_failure_reason = None


    async def _seed_odometer_from_stats(self, statistic_id):
        """Ascending [(date, km)] from recent clean daily odometer statistics.

        Only recent daily statistics are used (they're reliable); the monthly
        long-term aggregation is skipped because total_increasing accounting can
        corrupt it. Used once per VIN to backfill the store so the trailing-30d
        figure and current-month bar aren't blank on day one."""
        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.statistics import (
                statistics_during_period,
            )
        except Exception:
            return []
        start = dt_util.utcnow() - timedelta(days=45)

        def _query():
            return statistics_during_period(
                self.hass, start, None, {statistic_id}, "day", None, {"state"}
            )

        try:
            raw = await get_instance(self.hass).async_add_executor_job(_query)
        except Exception as err:
            _LOGGER.debug("Odometer seed query failed for %s: %s", statistic_id, err)
            return []

        points = []
        prev = None
        for row in (raw or {}).get(statistic_id) or []:
            try:
                km = float(row.get("state"))
            except (TypeError, ValueError):
                continue
            if km <= 0 or (prev is not None and km < prev):
                continue  # skip zero / backward glitches
            start_ts = row.get("start")
            if isinstance(start_ts, (int, float)):
                day = dt_util.as_local(dt_util.utc_from_timestamp(start_ts)).date()
            elif start_ts is not None:
                day = dt_util.as_local(start_ts).date()
            else:
                continue
            points.append((day, km))
            prev = km
        return points

    async def async_force_refresh(self):
        """Force an immediate data refresh, bypassing the debouncer.

        Used right after a control command (charge limit, plug-and-charge,
        home charge start/stop) so the UI reflects the new state without
        waiting for the next scheduled poll."""
        await self.async_refresh()

    async def _retry_with_backoff(self, func, max_retries=2, initial_delay=1.0):
        """Retry a function with exponential backoff."""
        delay = initial_delay
        last_error = None
        
        for attempt in range(max_retries + 1):
            try:
                return await func()
            except Exception as err:
                last_error = err
                if attempt < max_retries:
                    _LOGGER.warning(
                        f"Attempt {attempt + 1}/{max_retries + 1} failed: {err}. "
                        f"Retrying in {delay}s..."
                    )
                    await asyncio.sleep(delay)
                    delay *= 2  # Exponential backoff
                else:
                    _LOGGER.error(f"All {max_retries + 1} attempts failed: {err}")
                    raise last_error

    async def _apply_charge_timer(self, vehicle, store_data, now):
        """Start the home charge once a day at the user's time, when it is worth it.

        The timer only ever starts a charge: a session started or stopped by hand
        (or by plug-and-charge) is never touched. The battery has to sit a
        deadband below the charge limit, so a car left plugged in overnight that
        is already at the ceiling stays as it is and the timer re-arms for
        tomorrow.
        """
        if not store_data.get("charge_timer_enabled"):
            return
        today = now.date().isoformat()
        if store_data.get("charge_timer_last_run") == today:
            return
        start = store_data.get_charge_timer_start()
        trigger = dt_util.start_of_local_day(now).replace(
            hour=start.hour, minute=start.minute
        )
        if now < trigger or now - trigger > CHARGE_TIMER_GRACE:
            return

        # The pile is bound to the account, so a plugged pile can be plugged into
        # another car; the car's own connector state is what proves this cable is
        # in this car. Either way an unplugged car leaves the day unspent, so one
        # plugged in a few minutes late still charges tonight.
        if not vehicle.home_pile_plugged:
            return
        if not (vehicle.charger_connected or store_data.get("charge_timer_any_car")):
            return
        if vehicle.home_pile_charging:
            # Already charging: nothing to start, and the day is spent so that
            # stopping it by hand a minute later is not overridden.
            await store_data.update(charge_timer_last_run=today)
            return

        limit = store_data.get_charge_limit()
        level = vehicle.battery_charge_level
        if level is None:
            return  # battery level unknown: decide on a later poll, not blind
        if float(level) > limit - CHARGE_TIMER_DEADBAND:
            _LOGGER.debug(
                "Charge timer for %s: %.0f%% is within %s%% of the %s%% limit, "
                "not charging",
                vehicle.vin, float(level), CHARGE_TIMER_DEADBAND, limit,
            )
            await store_data.update(charge_timer_last_run=today)
            return

        _LOGGER.info(
            "Charge timer %s reached for %s (%.0f%%, limit %s%%); starting home charge",
            start.strftime("%H:%M"), vehicle.vin, float(level), limit,
        )
        await vehicle.home_pile_charge_start()
        await store_data.update(charge_timer_last_run=today)

    async def _async_update_data(self):
        """Fetch data from API endpoint with retry and caching support."""
        try:
            async with asyncio.timeout(30):
                # Retry login and token update
                await self._retry_with_backoff(self.volvo_api.login, max_retries=2)
                await self._retry_with_backoff(self.volvo_api.update_token, max_retries=2)
                
                vinVehicleMaps = await self.volvo_api.get_vehicles_vins()
                vehicles = []
                store_datas = []

                # Iterate VINs in a stable, deterministic order. Entities bind to
                # a vehicle by list index (coordinator.data[idx]); the API does not
                # guarantee a stable vehicle order between polls, so without this
                # sort a reordered response swaps every value between vehicles
                # (e.g. the XC90's data showing up on the XC60 and vice versa).
                for vin, vehicleInfos in sorted(vinVehicleMaps.items()):
                    modelYear = int(vehicleInfos.get("modelYear", 2020))
                    isAaos = modelYear >= 2022
                    vehicle = Vehicle(vin, self.volvo_api, isAaos)
                    
                    # Try to update, but don't fail completely
                    try:
                        await vehicle.update()
                        vehicle._consecutive_failures = 0
                        # Note: _last_successful_update is updated by _save_to_cache() in each parse method
                    except Exception as err:
                        vehicle._consecutive_failures += 1
                        _LOGGER.error(
                            f"Failed to update vehicle {vin} (failure #{vehicle._consecutive_failures}): {err}"
                        )
                        # Don't raise - continue with cached data
                    
                    vehicles.append(vehicle)

                    # Reuse a single persistent store per VIN across polls.
                    store_data = self._stores_by_vin.get(vin)
                    if store_data is None:
                        store_data = VolvoStore(self.hass, vin)
                        await store_data.load_create_data()
                        self._stores_by_vin[vin] = store_data

                    # Trailing-30-day + monthly driving distance from validated
                    # odometer snapshots kept in the store (forward-only).
                    try:
                        odo = getattr(vehicle, "odo_meter", None)
                        now_local = dt_util.now()
                        # One-time backfill from clean recent daily statistics.
                        if not (store_data.data or {}).get("odometer_seeded"):
                            seed = await self._seed_odometer_from_stats(
                                f"sensor.{vin.lower()}_odometer"
                            )
                            await store_data.seed_odometer(seed)
                        await store_data.record_odometer(odo, now_local)
                        dstats = store_data.get_distance_stats(odo, now_local)
                        vehicle.distance_last_30d = dstats.get("last_30d")
                        vehicle.distance_monthly = dstats.get("monthly") or []
                    except Exception as err:
                        _LOGGER.debug("Distance stats failed for %s: %s", vin, err)

                    # Refuel log: a jump in the reported tank level is a fill-up.
                    # Recording it here (not when the user gets around to logging
                    # it) pins the odometer to the moment of the fill, which is
                    # what makes the tank-to-tank L/100km figure meaningful.
                    try:
                        logged = await store_data.record_fuel_amount(
                            getattr(vehicle, "fuel_amount", None),
                            getattr(vehicle, "odo_meter", None),
                            dt_util.now(),
                        )
                        if logged:
                            _LOGGER.info("Refuel detected for %s", vin)
                    except Exception as err:
                        _LOGGER.warning("Refuel detection failed for %s: %s", vin, err)

                    # Enforce the persisted charge limit: once the battery
                    # reaches the ceiling, stop the active home-charge session.
                    # A limit of 100 (CHARGE_LIMIT_DISABLED) means "no limit".
                    try:
                        if getattr(vehicle, "has_home_pile", False):
                            limit = store_data.get_charge_limit()
                            level = vehicle.battery_charge_level
                            if (
                                limit is not None
                                and limit < CHARGE_LIMIT_DISABLED
                                and getattr(vehicle, "home_pile_charging", False)
                                and level is not None
                                and float(level) >= limit
                            ):
                                _LOGGER.info(
                                    "Charge limit %s%% reached for %s (%.0f%%); "
                                    "stopping home charge",
                                    limit, vin, float(level),
                                )
                                await vehicle.home_pile_charge_stop()
                    except Exception as err:
                        _LOGGER.warning(
                            "Charge-limit auto-stop failed for %s: %s", vin, err
                        )

                    # Daily timed home charge.
                    try:
                        if getattr(vehicle, "has_home_pile", False):
                            await self._apply_charge_timer(
                                vehicle, store_data, dt_util.now()
                            )
                    except Exception as err:
                        _LOGGER.warning("Charge timer failed for %s: %s", vin, err)

                    store_datas.append(store_data)

                # Track successful update
                self._consecutive_failures = 0
                self.store_datas = store_datas
                return vehicles
                
        except Exception as err:
            # Track failure but still return vehicles with cache
            self._consecutive_failures += 1
            self._last_failure_reason = str(err)
            _LOGGER.error(
                f"Coordinator update failed (failure #{self._consecutive_failures}): {err}"
            )
            
            # If we have existing data (vehicles from previous update), return it
            if self.data:
                _LOGGER.warning("Returning cached vehicle data due to update failure")
                return self.data
            
            # Only raise if we have no data at all (first load)
            raise UpdateFailed(f"Error communicating with API: {err}")

metaMap = {
    "car_lock": {
        "name": "Lock",
        "device_class": None,
        "icon": "",
        "unit": "",
        "entity_id": "lock",
    },
    "window_lock": {
        "name": "Winodw Lock",
        "device_class": None,
        "icon": "",
        "unit": "",
        "entity_id": "window_lock",
    },
    # "remote_door_unlock": {
    #    "name": "Remote Door Unlock",
    #    "device_class": "lock",
    #    "icon": "",
    #    "unit": "",
    # },
    "distance_to_empty": {
        "name": "Distance to empty",
        "device_class": None,
        "icon": "mdi:ruler",
        "unit": "km",
        "entity_id": "distance_to_empty",
        "state_class": "measurement",
    },
    "tail_gate_open": {
        "name": "Tail gate",
        "device_class": "door",
        "icon": "mdi:car-back",
        "unit": "",
        "entity_id": "tail_gate",
    },
    "rear_right_door_open": {
        "name": "Rear right door",
        "device_class": "door",
        "icon": "",
        "unit": "",
        "entity_id": "rear_right_door",
    },
    "rear_left_door_open": {
        "name": "Rear left door",
        "device_class": "door",
        "icon": "",
        "unit": "",
        "entity_id": "rear_left_door",
    },
    "front_right_door_open": {
        "name": "Front right door",
        "device_class": "door",
        "icon": "",
        "unit": "",
        "entity_id": "front_right_door",
    },
    "front_left_door_open": {
        "name": "Front left door",
        "device_class": "door",
        "icon": "",
        "unit": "",
        "entity_id": "front_left_door",
    },
    "hood_open": {
        "name": "Hood",
        "device_class": "door",
        "icon": "",
        "unit": "",
        "entity_id": "hood",
    },
    "sunroof_open": {
        "name": "Sunroof",
        "device_class": "window",
        "icon": "mdi:home-roof",
        "unit": "",
        "entity_id": "sunroof",
    },
    "engine_running": {
        "name": "Engine",
        "device_class": "power",
        "icon": "",
        "unit": "",
        "entity_id": "engine",
    },
    "odo_meter": {
        "name": "Odometer",
        "device_class": None,
        "icon": "mdi:speedometer",
        "unit": "km",
        "entity_id": "odometer",
        "state_class": "total_increasing",
    },
    "front_left_window_open": {
        "name": "Front left window",
        "device_class": "window",
        "icon": "",
        "unit": "",
        "entity_id": "front_left_window",
    },
    "front_right_window_open": {
        "name": "Front right window",
        "device_class": "window",
        "icon": "",
        "unit": "",
        "entity_id": "front_right_window",
    },
    "rear_left_window_open": {
        "name": "Rear left window",
        "device_class": "window",
        "icon": "",
        "unit": "",
        "entity_id": "rear_left_window",
    },
    "rear_right_window_open": {
        "name": "Rear right window",
        "device_class": "window",
        "icon": "",
        "unit": "",
        "entity_id": "rear_right_window",
    },
    "fuel_amount": {
        "name": "Fuel amount",
        "device_class": "volume_storage",
        "icon": "mdi:gas-station",
        "unit": "L",
        "entity_id": "fuel_amount",
        "state_class": "measurement",
    },
    "fuel_average_consumption_liters_per_100_km": {
        "name": "Fuel average consumption liters per 100 km",
        "device_class": None,
        "icon": "mdi:gas-station",
        "unit": "L/100km",
        "entity_id": "fuel_average_consumption_liters_per_100_km",
        "state_class": "measurement",
    },
    # TODO
    # "fuel_amount_level": {
    #    "name": "Fuel amount level",
    #    "device_class": None,
    #    "icon": "mdi:gas-station",
    #    "unit": "%",
    # },
    "position": {
        "name": "Position",
        "device_class": None,
        "icon": "",
        "unit": "",
        "entity_id": "position",
    },
    "position_wgs84": {
        "name": "Position WGS84",
        "device_class": None,
        "icon": "",
        "unit": "",
        "entity_id": "position_wgs84",
    },
    "flash_button": {
        "name": "Flash",
        "device_class": None,
        "icon": "mdi:car-light-high",
        "unit": "",
        "entity_id": "flash",
    },
    "honk_flash_button": {
        "name": "Honk And Flash",
        "device_class": None,
        "icon": "mdi:alarm-light",
        "unit": "",
        "entity_id": "honk_and_flash",
    },
    "engine_duration_number": {
        "name": "Engine Duration",
        "device_class": None,
        "icon": "mdi:clock-time-eight-outline",
        "unit": "Minute",
        "entity_id": "engine_duration",
    },
    "charge_limit_number": {
        "name": "Charge Limit",
        "device_class": None,
        "icon": "mdi:battery-charging-90",
        "unit": "%",
        "entity_id": "charge_limit",
    },
    "charge_timer_start_time": {
        "name": "Charge Timer Start",
        "device_class": None,
        "icon": "mdi:clock-time-eight-outline",
        "unit": "",
        "entity_id": "charge_timer_start",
    },
    "engine_switch": {
        "name": "Engine Remote control",
        "device_class": None,
        "icon": "mdi:engine-outline",
        "unit": "",
        "entity_id": "engine_remote_control",
    },
    "honk_button": {
        "name": "Honk",
        "device_class": None,
        "icon": "mdi:bugle",
        "unit": "",
        "entity_id": "honk",
    },
    "app_sign_in_button": {
        "name": "App Sign In",
        "device_class": None,
        "icon": "mdi:calendar-check",
        "unit": "",
        "entity_id": "app_sign_in",
    },
    "tail_gate_switch": {
        "name": "Tailgate control",
        "device_class": None,
        "icon": "mdi:car-back",
        "unit": "",
        "entity_id": "tailgate_control",
    },
    "sunroof_switch": {
        "name": "Sunroof control",
        "device_class": None,
        "icon": "mdi:home-roof",
        "unit": "",
        "entity_id": "sunroof_control",
    },
    "climatization_switch": {
        "name": "A/C Preconditioning",
        "device_class": None,
        "icon": "mdi:air-conditioner",
        "unit": "",
        "entity_id": "climatization",
    },
    "charging_switch": {
        "name": "Charging",
        "device_class": None,
        "icon": "mdi:ev-station",
        "unit": "",
        "entity_id": "charging",
    },
    "plug_and_charge_switch": {
        "name": "Plug and Charge",
        "device_class": None,
        "icon": "mdi:ev-plug-type2",
        "unit": "",
        "entity_id": "plug_and_charge",
    },
    "charge_timer_switch": {
        "name": "Charge Timer",
        "device_class": None,
        "icon": "mdi:clock-check-outline",
        "unit": "",
        "entity_id": "charge_timer",
    },
    "charge_timer_any_car_switch": {
        "name": "Charge Timer Any Car",
        "device_class": None,
        "icon": "mdi:car-multiple",
        "unit": "",
        "entity_id": "charge_timer_any_car",
    },
    "service_warning_msg": {
        "name": "Service Warning Message",
        "device_class": None,
        "icon": "mdi:car-wrench",
        "unit": None,
        "entity_id": "service_warning_msg",
    },
    "service_warning": {
        "name": "Service Warning",
        "device_class": "problem",
        "icon": "mdi:car-wrench",
        "unit": None,
        "entity_id": "service_warning",
    },
    "brake_fluid_level_warning": {
        "name": "Brake Fluid Level Warning",
        "device_class": "problem",
        "icon": "mdi:car-brake-fluid-level",
        "unit": None,
        "entity_id": "brake_fluid_level_warning",
    },
    "engine_coolant_level_warning": {
        "name": "Engine Coolant Level Warning",
        "device_class": "problem",
        "icon": "mdi:car-coolant-level",
        "unit": None,
        "entity_id": "engine_coolant_level_warning",
    },
    "oil_level_warning": {
        "name": "Oil Level Warning",
        "device_class": "problem",
        "icon": "mdi:oil-level",
        "unit": None,
        "entity_id": "oil_level_warning",
    },
    "washer_fluid_level_warning": {
        "name": "Washer Fluid Level Warning",
        "device_class": "problem",
        "icon": "mdi:wiper-wash",
        "unit": None,
        "entity_id": "washer_fluid_level_warning",
    },
    "front_left_tyre_pressure_warning": {
        "name": "Front Left Tyre Pressure Warning",
        "device_class": "problem",
        "icon": "mdi:car-tire-alert",
        "unit": None,
        "entity_id": "front_left_tyre_pressure_warning",
    },
    "front_right_tyre_pressure_warning": {
        "name": "Front Right Tyre Pressure Warning",
        "device_class": "problem",
        "icon": "mdi:car-tire-alert",
        "unit": None,
        "entity_id": "front_right_tyre_pressure_warning",
    },
    "rear_left_tyre_pressure_warning": {
        "name": "Rear Left Tyre Pressure Warning",
        "device_class": "problem",
        "icon": "mdi:car-tire-alert",
        "unit": None,
        "entity_id": "rear_left_tyre_pressure_warning",
    },
    "rear_right_tyre_pressure_warning": {
        "name": "Rear Right Tyre Pressure Warning",
        "device_class": "problem",
        "icon": "mdi:car-tire-alert",
        "unit": None,
        "entity_id": "rear_right_tyre_pressure_warning",
    },
    "connection_status": {
        "name": "Connection Status",
        "device_class": None,
        "icon": "mdi:connection",
        "unit": None,
        "entity_id": "connection_status",
        "entity_category": EntityCategory.DIAGNOSTIC,
    },
    "battery_charge_level": {
        "name": "Battery Level",
        "device_class": "battery",
        "icon": "",
        "unit": "%",
        "entity_id": "battery_charge_level",
        "state_class": "measurement",
    },
    "electric_range": {
        "name": "Electric Range",
        "device_class": "distance",
        "icon": "mdi:map-marker-distance",
        "unit": "km",
        "entity_id": "electric_range",
        "state_class": "measurement",
    },
    "energy_consumption": {
        "name": "Average Energy Consumption",
        "device_class": None,
        "icon": "mdi:lightning-bolt",
        "unit": "kWh/100km",
        "entity_id": "energy_consumption",
        "state_class": "measurement",
    },
    "charging_status": {
        "name": "Charging Status",
        "device_class": None,
        "icon": "mdi:ev-station",
        "unit": None,
        "entity_id": "charging_status",
    },
    "charging_power": {
        "name": "Charging Power",
        "device_class": "power",
        "icon": "mdi:flash",
        "unit": "kW",
        "entity_id": "charging_power",
        "state_class": "measurement",
    },
    "estimated_charging_time": {
        "name": "Estimated Charging Time",
        "device_class": "duration",
        "icon": "mdi:timer-outline",
        "unit": "min",
        "entity_id": "estimated_charging_time",
        "state_class": "measurement",
    },
    "charging_voltage": {
        "name": "Charging Voltage",
        "device_class": "voltage",
        "icon": "mdi:sine-wave",
        "unit": "V",
        "entity_id": "charging_voltage",
        "state_class": "measurement",
    },
    "charging_current": {
        "name": "Charging Current",
        "device_class": "current",
        "icon": "mdi:current-ac",
        "unit": "A",
        "entity_id": "charging_current",
        "state_class": "measurement",
    },
    "charging_session_energy": {
        "name": "Charging Session Energy",
        "device_class": "energy",
        "icon": "mdi:lightning-bolt",
        "unit": "kWh",
        "entity_id": "charging_session_energy",
        "state_class": "total_increasing",
    },
    "charger_connected": {
        "name": "Charger Connected",
        "device_class": "plug",
        "icon": "mdi:ev-plug-type2",
        "unit": "",
        "entity_id": "charger_connected",
    },
    "home_pile_connector_status": {
        "name": "Home Charger",
        "device_class": None,
        "icon": "mdi:ev-station",
        "unit": None,
        "entity_id": "home_charger_status",
    },
    "home_pile_last_energy": {
        "name": "Last Charge Energy",
        "device_class": None,
        "icon": "mdi:lightning-bolt",
        "unit": "kWh",
        "entity_id": "last_charge_energy",
        "state_class": "measurement",
    },
    "home_pile_appointment": {
        "name": "Charge Schedule",
        "device_class": None,
        "icon": "mdi:clock-outline",
        "unit": None,
        "entity_id": "charge_schedule",
    },
    "home_pile_plugged": {
        "name": "Home Charger Plugged",
        "device_class": "plug",
        "icon": "mdi:ev-plug-type2",
        "unit": "",
        "entity_id": "home_charger_plugged",
    },
    "trip_meter_manual": {
        "name": "Trip Meter TM", "device_class": "distance", "icon": "mdi:map-marker-path",
        "unit": "km", "entity_id": "trip_meter_tm", "state_class": "measurement",
    },
    "trip_meter_auto": {
        "name": "Trip Meter AT", "device_class": "distance", "icon": "mdi:map-marker-path",
        "unit": "km", "entity_id": "trip_meter_at", "state_class": "measurement",
    },
    "trip_since_charge": {
        "name": "Trip Since Charge", "device_class": "distance", "icon": "mdi:map-marker-distance",
        "unit": "km", "entity_id": "trip_since_charge", "state_class": "measurement",
    },
    "avg_speed_manual": {
        "name": "Avg Speed TM", "device_class": "speed", "icon": "mdi:speedometer-medium",
        "unit": "km/h", "entity_id": "avg_speed_tm", "state_class": "measurement",
    },
    "avg_speed_auto": {
        "name": "Avg Speed AT", "device_class": "speed", "icon": "mdi:speedometer-medium",
        "unit": "km/h", "entity_id": "avg_speed_at", "state_class": "measurement",
    },
    "avg_speed_since_charge": {
        "name": "Avg Speed Since Charge", "device_class": "speed", "icon": "mdi:speedometer-slow",
        "unit": "km/h", "entity_id": "avg_speed_since_charge", "state_class": "measurement",
    },
    "fuel_consumption_at": {
        "name": "Fuel Consumption AT", "device_class": None, "icon": "mdi:gas-station",
        "unit": "L/100km", "entity_id": "fuel_consumption_at", "state_class": "measurement",
    },
    "next_maintenance_km": {
        "name": "Next Maintenance", "device_class": "distance", "icon": "mdi:wrench-clock",
        "unit": "km", "entity_id": "next_maintenance",
    },
    "distance_to_maintenance": {
        "name": "Distance to Maintenance", "device_class": "distance", "icon": "mdi:wrench-clock",
        "unit": "km", "entity_id": "distance_to_maintenance", "state_class": "measurement",
    },
    "distance_last_30d": {
        "name": "Distance (30 days)", "device_class": "distance", "icon": "mdi:map-marker-distance",
        "unit": "km", "entity_id": "distance_last_30d", "state_class": "measurement",
    },
    "energy_last_30d": {
        "name": "Charged Energy (30 days)", "device_class": None, "icon": "mdi:lightning-bolt",
        "unit": "kWh", "entity_id": "energy_last_30d",
    },
    # Tank-to-tank consumption from the refuel log — what the pump actually
    # charged for, divided by the distance driven on that tank.
    "fuel_consumption_measured": {
        "name": "Fuel Consumption (measured)", "device_class": None, "icon": "mdi:gas-station-outline",
        "unit": "L/100km", "entity_id": "fuel_consumption_measured", "state_class": "measurement",
    },
}


class VolvoEntity(CoordinatorEntity):
    def __init__(self, coordinator, idx, metaMapKey, platform):
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(coordinator, context=idx)
        self.idx = idx
        self.metaMapKey = metaMapKey
        # Lowercase the VIN: HA registers entity IDs lowercased anyway, so this
        # produces the same IDs (no rename) while satisfying HA's entity-ID
        # validation (an uppercase VIN triggers an "invalid entity ID" warning).
        vin = self.coordinator.data[self.idx].vin.lower()
        self.entity_id = f"{platform}.{vin}_{metaMap[self.metaMapKey]['entity_id']}"

    @property
    def icon(self):
        return metaMap[self.metaMapKey]["icon"]

    @property
    def device_class(self):
        return metaMap[self.metaMapKey]["device_class"]

    @property
    def device_info(self) -> DeviceInfo:
        """Return a inique set of attributes for each vehicle."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.data[self.idx].vin)},
            name="Volvo " + self.coordinator.data[self.idx].series_name,
            model=self.coordinator.data[self.idx].series_name + " " + self.coordinator.data[self.idx].model_name,
            manufacturer="Volvo",
        )

    @property
    def unique_id(self) -> str:
        """Return a unique ID."""
        return f"{self.coordinator.data[self.idx].vin}-{self.metaMapKey}"

    @property
    def translation_key(self) -> str:
        return self.metaMapKey

    @property
    def has_entity_name(self) -> bool:
        return True

    @property
    def translation_placeholders(self):
        return {"nickname": (self.coordinator.data[self.idx].nickname)}
