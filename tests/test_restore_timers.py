"""The availability dates (minimal on/off durations) survive a restart of Home Assistant"""

from datetime import datetime, timedelta

from homeassistant.core import State
from pytest_homeassistant_custom_component.common import mock_restore_cache

from custom_components.solar_optimizer.const import get_tz
from .commons import *  # pylint: disable=wildcard-import, unused-wildcard-import

DEVICE_DATA = {
    CONF_NAME: "Equipement A",
    CONF_DEVICE_TYPE: CONF_POWERED_DEVICE,
    CONF_ENTITY_ID: "input_boolean.fake_device_a",
    CONF_POWER_MAX: 1000,
    CONF_POWER_MIN: 100,
    CONF_POWER_STEP: 100,
    CONF_POWER_ENTITY_ID: "input_number.fake_amps",
    CONF_CHANGE_POWER_SERVICE: "input_number/set_value",
    CONF_CONVERT_POWER_DIVIDE_FACTOR: 100,
    CONF_CHECK_USABLE_TEMPLATE: "{{ True }}",
    CONF_DURATION_MIN: 15,
    CONF_DURATION_STOP_MIN: 10,
    CONF_DURATION_POWER_MIN: 1,
    CONF_ACTION_MODE: CONF_ACTION_MODE_ACTION,
    CONF_ACTIVATION_SERVICE: "input_boolean/turn_on",
    CONF_DEACTIVATION_SERVICE: "input_boolean/turn_off",
    CONF_BATTERY_SOC_THRESHOLD: 0,
    CONF_MAX_ON_TIME_PER_DAY_MIN: 1440,
    CONF_MIN_ON_TIME_PER_DAY_MIN: 0,
}


async def _setup(hass, attributes):
    mock_restore_cache(hass, [State("switch.solar_optimizer_equipement_a", "on", attributes)])
    entry = MockConfigEntry(domain=DOMAIN, title="Equipement A", unique_id="eqtAUniqueId", data=DEVICE_DATA)
    device = await create_managed_device(hass, entry, "equipement_a")
    await hass.async_block_till_done()
    return device


async def test_restore_future_dates(hass: HomeAssistant, init_solar_optimizer_central_config):
    """Dates still in the future are restored: the device keeps waiting after the restart"""
    tz = get_tz(hass)
    now = datetime.now(tz)
    nda = (now + timedelta(minutes=12)).replace(microsecond=0)
    ndap = (now + timedelta(seconds=40)).replace(microsecond=0)
    device = await _setup(hass, {"next_date_available": nda.isoformat(), "next_date_available_power": ndap.isoformat()})

    assert device.next_date_available == nda
    assert device.next_date_available_power == ndap
    assert device.is_waiting is True

    switch = search_entity(hass, "switch.solar_optimizer_equipement_a", "switch")
    assert switch.get_attr_extra_state_attributes["next_date_available"] == nda.astimezone(tz).isoformat()


async def test_restore_past_or_invalid_dates_are_ignored(hass: HomeAssistant, init_solar_optimizer_central_config):
    """Past or unreadable dates leave the device available now, as before"""
    tz = get_tz(hass)
    past = datetime.now(tz) - timedelta(minutes=5)
    device = await _setup(hass, {"next_date_available": past.isoformat(), "next_date_available_power": "not a date"})

    now = datetime.now(tz)
    assert abs((device.next_date_available - now).total_seconds()) < 2
    assert abs((device.next_date_available_power - now).total_seconds()) < 2
    assert device.is_waiting is False
