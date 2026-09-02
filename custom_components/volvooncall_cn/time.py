import logging
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.components.time import TimeEntity
from homeassistant.const import Platform

from . import VolvoCoordinator, VolvoEntity
from .volvooncall_cn import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up time."""
    coordinator: VolvoCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    times = []
    for idx, ent in enumerate(coordinator.data):
        # Same guard as the charge-limit slider: an electric car with a bound
        # home wallbox is the only thing the timer can start a charge on.
        if ent.get("has_battery") and ent.get("has_home_pile"):
            times.append(VolvoChargeTimerStartTime(coordinator, idx, "charge_timer_start_time"))

    async_add_entities(times)


class VolvoChargeTimerStartTime(VolvoEntity, TimeEntity):
    """Daily time at which the timer decides whether to start a home charge.

    Local time, persisted in the store; the coordinator acts on it (see
    _apply_charge_timer in __init__.py)."""

    def __init__(self, coordinator, idx, metaMapKey):
        super().__init__(coordinator, idx, metaMapKey, Platform.TIME)

    @property
    def native_value(self):
        return self.coordinator.store_datas[self.idx].get_charge_timer_start()

    async def async_set_value(self, value) -> None:
        await self.coordinator.store_datas[self.idx].set_charge_timer_start(value)
        self.async_write_ha_state()
