""" Button entities: back to automatic after a manual change """

import logging

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.components.button import ButtonEntity, DOMAIN as BUTTON_DOMAIN
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.device_registry import DeviceInfo, DeviceEntryType

from .const import *  # pylint: disable=wildcard-import, unused-wildcard-import
from .coordinator import SolarOptimizerCoordinator
from .managed_device import ManagedDevice

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """One "back to automatic" button per managed device with manual detection"""
    if entry.data.get(CONF_DEVICE_TYPE) in (None, CONF_DEVICE_CENTRAL):
        return
    coordinator: SolarOptimizerCoordinator = SolarOptimizerCoordinator.get_coordinator()
    device = coordinator.get_device_by_unique_id(name_to_unique_id(entry.data[CONF_NAME]))
    if device is None or not device.manual_detection:
        return
    async_add_entities([ManagedDeviceEndManualButton(device)])


class ManagedDeviceEndManualButton(ButtonEntity):
    """Ends the manual mode of a device. No command is sent to the device."""

    def __init__(self, device: ManagedDevice):
        idx = name_to_unique_id(device.name)
        self._device = device
        self._attr_has_entity_name = True
        self.entity_id = f"{BUTTON_DOMAIN}.solar_optimizer_end_manual_{idx}"
        self._attr_name = "Back to automatic"
        self._attr_translation_key = "end_manual"
        self._attr_unique_id = "solar_optimizer_end_manual_" + idx
        self._attr_icon = "mdi:robot"

    @property
    def device_info(self) -> DeviceInfo | None:
        return DeviceInfo(
            entry_type=DeviceEntryType.SERVICE,
            identifiers={(DOMAIN, self._device.name)},
            name="Solar Optimizer-" + self._device.name,
            manufacturer=DEVICE_MANUFACTURER,
            model=DEVICE_MODEL,
        )

    async def async_press(self) -> None:
        self._device.end_manual("button")
