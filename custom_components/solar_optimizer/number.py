""" Number entities of the enumeration optimiser: value of 1 kWh per device and tariff period,
share of the surplus (alpha) per group """

import logging

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.components.number import NumberMode, RestoreNumber, DOMAIN as NUMBER_DOMAIN
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.device_registry import DeviceInfo, DeviceEntryType

from .const import *  # pylint: disable=wildcard-import, unused-wildcard-import
from .coordinator import SolarOptimizerCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Values per device; alphas per group on the central configuration (enumeration only)"""
    coordinator: SolarOptimizerCoordinator = SolarOptimizerCoordinator.get_coordinator()
    device_type = entry.data.get(CONF_DEVICE_TYPE)
    if device_type is None:
        return
    if device_type == CONF_DEVICE_CENTRAL:
        if entry.data.get(CONF_ALGORITHM) != ALGORITHM_ENUMERATION:
            return
        count = int(entry.data.get(CONF_GROUP_COUNT) or DEFAULT_GROUP_COUNT)
        async_add_entities([GroupAlphaNumber(coordinator, group) for group in range(1, count + 1)])
        return
    device = coordinator.get_device_by_unique_id(name_to_unique_id(entry.data[CONF_NAME]))
    if device is None:
        return
    async_add_entities([DeviceValueNumber(device, "peak"), DeviceValueNumber(device, "offpeak")])


class DeviceValueNumber(RestoreNumber):
    """Value of 1 kWh delivered to the device (€/kWh) in a tariff period. It must stay between
    the selling price and the cheapest buying price to make sense."""

    _attr_has_entity_name = True
    _attr_native_min_value = 0.0
    _attr_native_max_value = 1.0
    _attr_native_step = 0.001
    _attr_native_unit_of_measurement = "€/kWh"
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:cash"

    def __init__(self, device, period: str):
        idx = name_to_unique_id(device.name)
        self._device = device
        self._period = period
        self.entity_id = f"{NUMBER_DOMAIN}.solar_optimizer_value_{period}_{idx}"
        self._attr_unique_id = f"solar_optimizer_value_{period}_{idx}"
        self._attr_translation_key = f"value_{period}"
        self._attr_name = "Value peak" if period == "peak" else "Value off-peak"
        self._attr_native_value = device.default_value

    @property
    def device_info(self) -> DeviceInfo | None:
        return DeviceInfo(
            entry_type=DeviceEntryType.SERVICE,
            identifiers={(DOMAIN, self._device.name)},
            name="Solar Optimizer-" + self._device.name,
            manufacturer=DEVICE_MANUFACTURER,
            model=DEVICE_MODEL,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            self._attr_native_value = last.native_value
        self._device.set_value_entity(self._period, self)

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()


class GroupAlphaNumber(RestoreNumber):
    """Share of the surplus given first to a group (the shares are normalised to sum 1)"""

    _attr_has_entity_name = True
    _attr_native_min_value = 0.0
    _attr_native_max_value = 1.0
    _attr_native_step = 0.05
    _attr_mode = NumberMode.SLIDER
    _attr_icon = "mdi:chart-pie"

    def __init__(self, coordinator, group: int):
        self._coordinator = coordinator
        self._group = group
        self.entity_id = f"{NUMBER_DOMAIN}.solar_optimizer_alpha_group_{group}"
        self._attr_unique_id = f"solar_optimizer_alpha_group_{group}"
        self._attr_translation_key = "alpha"
        self._attr_translation_placeholders = {"group": str(group)}
        self._attr_name = f"Alpha group {group}"
        self._attr_native_value = 1.0 if group == 1 else 0.0

    @property
    def device_info(self) -> DeviceInfo | None:
        return DeviceInfo(
            entry_type=DeviceEntryType.SERVICE,
            identifiers={(DOMAIN, CONF_DEVICE_CENTRAL)},
            name="Solar Optimizer",
            manufacturer=DEVICE_MANUFACTURER,
            model=INTEGRATION_MODEL,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            self._attr_native_value = last.native_value
        self._coordinator.runtime.alpha_entities[self._group] = self

    async def async_will_remove_from_hass(self) -> None:
        if self._coordinator.runtime.alpha_entities.get(self._group) is self:
            del self._coordinator.runtime.alpha_entities[self._group]

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()
