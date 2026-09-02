from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.const import Platform

from . import VolvoCoordinator, VolvoEntity, metaMap
from .volvooncall_cn import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Configure sensors from a config entry created in the integrations UI."""
    coordinator: VolvoCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    entities = []
    for idx, _ in enumerate(coordinator.data):
        entities.append(VolvoSensor(coordinator, idx, "distance_to_empty"))
        entities.append(VolvoSensor(coordinator, idx, "odo_meter"))
        entities.append(VolvoSensor(coordinator, idx, "fuel_amount"))
        entities.append(VolvoSensor(coordinator, idx, "fuel_average_consumption_liters_per_100_km"))
        entities.append(VolvoSensor(coordinator, idx, "fuel_consumption_at"))
        entities.append(VolvoRefuelSensor(coordinator, idx, "fuel_consumption_measured"))
        entities.append(VolvoSensor(coordinator, idx, "trip_meter_manual"))
        entities.append(VolvoSensor(coordinator, idx, "trip_meter_auto"))
        entities.append(VolvoSensor(coordinator, idx, "trip_since_charge"))
        entities.append(VolvoSensor(coordinator, idx, "avg_speed_manual"))
        entities.append(VolvoSensor(coordinator, idx, "avg_speed_auto"))
        entities.append(VolvoSensor(coordinator, idx, "avg_speed_since_charge"))
        entities.append(VolvoSensor(coordinator, idx, "next_maintenance_km"))
        entities.append(VolvoSensor(coordinator, idx, "distance_to_maintenance"))
        # Trailing-30-day distance + monthly history (from odometer statistics).
        entities.append(VolvoStatSensor(coordinator, idx, "distance_last_30d", "distance_monthly"))
        entities.append(VolvoSensor(coordinator, idx, "service_warning_msg"))
        entities.append(VolvoConnectionStatusSensor(coordinator, idx, "connection_status"))
        # entities.append(VolvoSensor(coordinator, idx, "fuel_amount_level"))

        # Battery / charging sensors only for PHEV/BEV models (data present).
        if getattr(coordinator.data[idx], "has_battery", False):
            entities.append(VolvoSensor(coordinator, idx, "battery_charge_level"))
            entities.append(VolvoSensor(coordinator, idx, "electric_range"))
            entities.append(VolvoSensor(coordinator, idx, "energy_consumption"))
            entities.append(VolvoChargingStatusSensor(coordinator, idx, "charging_status"))
            entities.append(VolvoSensor(coordinator, idx, "charging_power"))
            entities.append(VolvoSensor(coordinator, idx, "estimated_charging_time"))

        # Home wallbox (家充桩) sensors — only for electric cars. The wallbox is
        # bound to the ACCOUNT, so get_home_pile returns it for every VIN; without
        # the has_battery guard a fuel car on the same account would show the EV's
        # charging data.
        if (
            getattr(coordinator.data[idx], "has_battery", False)
            and getattr(coordinator.data[idx], "has_home_pile", False)
        ):
            entities.append(VolvoHomePileSensor(coordinator, idx, "home_pile_connector_status"))
            entities.append(VolvoSensor(coordinator, idx, "home_pile_last_energy"))
            entities.append(VolvoSensor(coordinator, idx, "home_pile_appointment"))
            entities.append(VolvoSensor(coordinator, idx, "charging_voltage"))
            entities.append(VolvoSensor(coordinator, idx, "charging_current"))
            entities.append(VolvoSensor(coordinator, idx, "charging_session_energy"))
            # Trailing-30-day charged energy + monthly history (from charge records).
            entities.append(VolvoStatSensor(coordinator, idx, "energy_last_30d", "energy_monthly"))

    async_add_entities(entities)


class VolvoSensor(VolvoEntity, SensorEntity):
    """An entity using CoordinatorEntity.

    The CoordinatorEntity class provides:
      should_poll
      async_update
      async_added_to_hass
      available
    """

    def __init__(self, coordinator, idx, metaMapKey):
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(coordinator, idx, metaMapKey, Platform.SENSOR)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._attr_native_value = self.coordinator.data[self.idx].get(self.metaMapKey)
        self._attr_native_unit_of_measurement = metaMap[self.metaMapKey]["unit"]
        # Set state_class if defined in metaMap
        if "state_class" in metaMap[self.metaMapKey]:
            self._attr_state_class = metaMap[self.metaMapKey]["state_class"]
        # Set entity_category if defined in metaMap
        if "entity_category" in metaMap[self.metaMapKey]:
            self._attr_entity_category = metaMap[self.metaMapKey]["entity_category"]
        self.async_write_ha_state()


class VolvoChargingStatusSensor(VolvoSensor):
    """Charging status text sensor.

    Exposes raw battery fields plus the last-charge session and home-pile
    identity as attributes, which the charging card reads for its statistics
    section (last_charge_order / charge_pile_name / charge_pile_address)."""

    @property
    def extra_state_attributes(self):
        vehicle = self.coordinator.data[self.idx]
        attrs = dict(vehicle.get("battery_raw") or {})
        order = vehicle.get("last_charge_order")
        if order:
            attrs["last_charge_order"] = order
        pile_name = vehicle.get("home_pile_name")
        if pile_name:
            attrs["charge_pile_name"] = pile_name
        pile_address = vehicle.get("charge_pile_address")
        if pile_address:
            attrs["charge_pile_address"] = pile_address
        return attrs


class VolvoHomePileSensor(VolvoSensor):
    """Home wallbox connector status; exposes pile + last-session details as attributes."""

    @property
    def extra_state_attributes(self):
        return self.coordinator.data[self.idx].get("home_pile_raw") or {}


class VolvoStatSensor(VolvoEntity, SensorEntity):
    """A trailing-30-day stat whose per-month history is exposed as an attribute
    (``monthly``) for the charging card's inline histograms."""

    def __init__(self, coordinator, idx, metaMapKey, monthly_key):
        super().__init__(coordinator, idx, metaMapKey, Platform.SENSOR)
        self._monthly_key = monthly_key

    @callback
    def _handle_coordinator_update(self) -> None:
        vehicle = self.coordinator.data[self.idx]
        self._attr_native_value = vehicle.get(self.metaMapKey)
        self._attr_native_unit_of_measurement = metaMap[self.metaMapKey]["unit"]
        if "state_class" in metaMap[self.metaMapKey]:
            self._attr_state_class = metaMap[self.metaMapKey]["state_class"]
        self._attr_extra_state_attributes = {
            "monthly": vehicle.get(self._monthly_key) or []
        }
        self.async_write_ha_state()


class VolvoRefuelSensor(VolvoEntity, SensorEntity):
    """Measured fuel consumption for the last tank, from the refuel log.

    State is the newest tank-to-tank L/100km figure; the log itself rides along
    as the ``records`` attribute, which is what the car card's 加油记录 dialog
    lists and edits."""

    def __init__(self, coordinator, idx, metaMapKey):
        super().__init__(coordinator, idx, metaMapKey, Platform.SENSOR)

    @callback
    def _handle_coordinator_update(self) -> None:
        store_data = self.coordinator.store_datas[self.idx]
        stats = store_data.get_refuel_stats()
        records = stats["records"]
        newest = records[0] if records else {}
        self._attr_native_value = stats["last"]
        self._attr_native_unit_of_measurement = metaMap[self.metaMapKey]["unit"]
        self._attr_state_class = metaMap[self.metaMapKey]["state_class"]
        self._attr_extra_state_attributes = {
            "average": stats["average"],
            "record_count": stats["count"],
            "last_refuel_at": newest.get("at"),
            "last_refuel_liters": newest.get("liters"),
            # Newest first, trimmed: the state machine keeps attributes in
            # memory and writes them to the recorder on every state change.
            "records": records[:12],
        }
        self.async_write_ha_state()


class VolvoConnectionStatusSensor(VolvoEntity, SensorEntity):
    """Sensor for connection status with last update time as attribute."""

    def __init__(self, coordinator, idx, metaMapKey):
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(coordinator, idx, metaMapKey, Platform.SENSOR)
        # Set entity_category to diagnostic
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        vehicle = self.coordinator.data[self.idx]
        self._attr_native_value = vehicle.connection_status
        # Add last_update_time as an attribute
        self._attr_extra_state_attributes = {
            "last_update_time": vehicle.last_update_time.isoformat() if vehicle.last_update_time else None,
            "consecutive_failures": vehicle._consecutive_failures,
            "cache_info": vehicle.get_cache_info(),
        }
        self.async_write_ha_state()
