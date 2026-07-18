import logging
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.components.button import ButtonEntity
from homeassistant.const import Platform

from . import VolvoCoordinator, VolvoEntity
from .volvooncall_cn import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up button."""
    coordinator: VolvoCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    buttons = []
    for idx, ent in enumerate(coordinator.data):
        buttons.append(VolvoFlashButton(coordinator, idx, "flash_button"))
        buttons.append(VolvoHonkFlashButton(coordinator, idx, "honk_flash_button"))
        buttons.append(VolvoHonkButton(coordinator, idx, "honk_button"))
        # Daily charging-membership check-in; needs a bound home pile (member id).
        if ent.get("has_home_pile"):
            buttons.append(VolvoSignInButton(coordinator, idx, "app_sign_in_button"))

    async_add_entities(buttons)


class VolvoFlashButton(VolvoEntity, ButtonEntity):
    """Representation of a Volvo Cars button."""

    def __init__(self, coordinator, idx, metaMapKey):
        super().__init__(coordinator, idx, metaMapKey, Platform.BUTTON)

    async def async_press(self) -> None:
        await self.coordinator.data[self.idx].flash()


class VolvoHonkFlashButton(VolvoEntity, ButtonEntity):
    """Representation of a Volvo Cars button."""

    def __init__(self, coordinator, idx, metaMapKey):
        super().__init__(coordinator, idx, metaMapKey, Platform.BUTTON)

    async def async_press(self) -> None:
        await self.coordinator.data[self.idx].honk_and_flash()


class VolvoHonkButton(VolvoEntity, ButtonEntity):
    """Representation of a Volvo Cars button."""

    def __init__(self, coordinator, idx, metaMapKey):
        super().__init__(coordinator, idx, metaMapKey, Platform.BUTTON)

    async def async_press(self) -> None:
        await self.coordinator.data[self.idx].honk()


class VolvoSignInButton(VolvoEntity, ButtonEntity):
    """Daily Volvo app member check-in (签到) for the charging membership."""

    def __init__(self, coordinator, idx, metaMapKey):
        super().__init__(coordinator, idx, metaMapKey, Platform.BUTTON)

    @property
    def available(self) -> bool:
        return bool(self.coordinator.data[self.idx].get("home_pile_member_id"))

    async def async_press(self) -> None:
        await self.coordinator.data[self.idx].sign_in()
