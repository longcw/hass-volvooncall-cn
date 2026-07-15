from datetime import timedelta
import logging
import asyncio
import re
from datetime import datetime, timezone
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory

from homeassistant.components.sensor import SensorEntity
from homeassistant.components.http import StaticPathConfig
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.const import CONF_USERNAME, CONF_PASSWORD, CONF_SCAN_INTERVAL

from .store import VolvoStore
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
}

_LOGGER = logging.getLogger(__name__)

# Bundled Lovelace card (custom:volvo-car-card), served by the integration.
FRONTEND_PATH = Path(__file__).parent / "frontend"
FRONTEND_URL_PATH = f"/{DOMAIN}/frontend"
CARD_RESOURCE_PATH = f"{FRONTEND_URL_PATH}/volvo-car-card.js"


def _read_card_version() -> str:
    """Read CARD_VERSION from the bundled card JS, so the cache-busting resource
    URL has a single source of truth (the .js file) instead of a second constant
    that silently drifts out of sync on every card change."""
    try:
        text = (FRONTEND_PATH / "volvo-car-card.js").read_text(encoding="utf-8")
        match = re.search(r'CARD_VERSION\s*=\s*"([^"]+)"', text)
        if match:
            return match.group(1)
    except Exception:  # pragma: no cover - defensive, never block setup
        pass
    return "0"


async def _async_register_card_resource(hass: HomeAssistant, card_url: str) -> None:
    """Auto-register the bundled card as a storage-mode Lovelace resource.

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
        if url.split("?", 1)[0] != CARD_RESOURCE_PATH:
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


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Serve and register the bundled Volvo card (runs once at integration load)."""
    try:
        await hass.http.async_register_static_paths(
            [StaticPathConfig(FRONTEND_URL_PATH, str(FRONTEND_PATH), True)]
        )
        version = await hass.async_add_executor_job(_read_card_version)
        card_url = f"{CARD_RESOURCE_PATH}?v={version}"
        await _async_register_card_resource(hass, card_url)
    except Exception as err:  # pragma: no cover - never block integration setup
        _LOGGER.warning("Volvo card frontend setup failed: %s", err)
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

                    # Snapshot the electric range once per 100% charge session
                    # for long-term battery-health statistics.
                    if getattr(vehicle, "has_battery", False):
                        try:
                            await store_data.async_capture_full_charge_range(
                                battery_level=vehicle.battery_charge_level,
                                electric_range=vehicle.electric_range,
                                sampled_at=datetime.now(timezone.utc).isoformat(),
                                data_source="battery_grpc",
                            )
                        except Exception as err:
                            _LOGGER.warning(
                                "Full-charge range capture failed for %s: %s", vin, err
                            )

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
    "full_charge_electric_range": {
        "name": "Full Charge Electric Range",
        "device_class": "distance",
        "icon": "mdi:map-marker-distance",
        "unit": "km",
        "entity_id": "full_charge_electric_range",
        "state_class": "measurement",
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
}


class VolvoEntity(CoordinatorEntity):
    def __init__(self, coordinator, idx, metaMapKey, platform):
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(coordinator, context=idx)
        self.idx = idx
        self.metaMapKey = metaMapKey
        self.entity_id = f"{platform}.{self.coordinator.data[self.idx].vin}_{metaMap[self.metaMapKey]['entity_id']}"

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
