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
        entities.append(VolvoSensor(coordinator, idx, "trip_meter_manual"))
        entities.append(VolvoSensor(coordinator, idx, "trip_meter_auto"))
        entities.append(VolvoSensor(coordinator, idx, "trip_since_charge"))
        entities.append(VolvoSensor(coordinator, idx, "avg_speed_manual"))
        entities.append(VolvoSensor(coordinator, idx, "avg_speed_auto"))
        entities.append(VolvoSensor(coordinator, idx, "avg_speed_since_charge"))
        entities.append(VolvoSensor(coordinator, idx, "next_maintenance_km"))
        entities.append(VolvoSensor(coordinator, idx, "distance_to_maintenance"))
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
            entities.append(VolvoFullChargeRangeSensor(coordinator, idx, "full_charge_electric_range"))

        # Home wallbox (家充桩) sensors only if a Volvo-brand pile is bound.
        if getattr(coordinator.data[idx], "has_home_pile", False):
            entities.append(VolvoHomePileSensor(coordinator, idx, "home_pile_connector_status"))
            entities.append(VolvoSensor(coordinator, idx, "home_pile_last_energy"))
            entities.append(VolvoSensor(coordinator, idx, "home_pile_appointment"))

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
    """Charging status text sensor; exposes not-yet-identified raw fields as attributes."""

    @property
    def extra_state_attributes(self):
        return self.coordinator.data[self.idx].get("battery_raw") or {}


class VolvoHomePileSensor(VolvoSensor):
    """Home wallbox connector status; exposes pile + last-session details as attributes."""

    @property
    def extra_state_attributes(self):
        return self.coordinator.data[self.idx].get("home_pile_raw") or {}


class VolvoFullChargeRangeSensor(VolvoEntity, SensorEntity):
    """Publish the electric range captured at the start of each 100% charge session."""

    def __init__(self, coordinator, idx, metaMapKey):
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(coordinator, idx, metaMapKey, Platform.SENSOR)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Publish the most recent persisted full-charge range sample."""
        store_data = self.coordinator.store_datas[self.idx]
        self._attr_native_value = store_data.get("full_charge_electric_range")
        self._attr_native_unit_of_measurement = metaMap[self.metaMapKey]["unit"]
        self._attr_state_class = metaMap[self.metaMapKey]["state_class"]
        self._attr_extra_state_attributes = {
            "sampled_at": store_data.get("full_charge_sampled_at"),
            "sample_count": store_data.get("full_charge_sample_count") or 0,
            "data_source": store_data.get("full_charge_data_source"),
            "trigger_battery_level": 100,
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
