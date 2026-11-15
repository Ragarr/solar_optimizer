"""The value of 1 kWh (v_i) lives in two places: the device configuration («Configurar») and the
panel numbers (peak / off-peak). A change made in either must be the one used next."""

from homeassistant.config_entries import ConfigEntryState

from .commons import *  # pylint: disable=wildcard-import, unused-wildcard-import
from .test_enumeration_ha import setup

PEAK, OFFPEAK = "number.solar_optimizer_value_peak_aire_1", "number.solar_optimizer_value_offpeak_aire_1"


def _entry(hass):
    return next(e for e in hass.config_entries.async_entries(DOMAIN) if e.title == "Aire 1")


async def _set_number(hass, entity_id, value):
    await hass.services.async_call("number", "set_value", {"entity_id": entity_id, "value": value}, blocking=True)


async def _open_options(hass, entry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "form" and result["step_id"] == "device"
    shown = {str(k): (k.description or {}).get("suggested_value") for k in result["data_schema"].schema}
    return result, shown


async def _save_options(hass, entry, result, shown, **changes):
    user_input = {k: v for k, v in shown.items() if v is not None}
    user_input.update({k: entry.data[k] for k in shown if shown[k] is None and k in entry.data})
    user_input.update(changes)
    result = await hass.config_entries.options.async_configure(result["flow_id"], user_input=user_input)
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


def _value(hass, entity_id):
    return float(hass.states.get(entity_id).state)


async def test_config_change_reaches_the_panel_and_the_optimizer(hass: HomeAssistant):
    """Changing the value in «Configurar» is used after the reload (it used to be lost: the
    panel numbers restored their previous value)"""
    await setup(hass)
    entry = _entry(hass)
    assert _value(hass, PEAK) == 0.09
    result, shown = await _open_options(hass, entry)
    await _save_options(hass, entry, result, shown, **{CONF_VALUE: 0.12})
    assert _value(hass, PEAK) == 0.12
    assert _value(hass, OFFPEAK) == 0.12
    device = SolarOptimizerCoordinator.get_coordinator().get_device_by_unique_id("aire_1")
    assert device.value(False) == 0.12 and device.value(True) == 0.12
    assert entry.data[CONF_VALUE] == 0.12


async def test_form_shows_the_value_in_use(hass: HomeAssistant):
    """«Configurar» shows the value of the panel (the one in use), not a stale one"""
    await setup(hass)
    await _set_number(hass, PEAK, 0.077)
    _, shown = await _open_options(hass, _entry(hass))
    assert shown[CONF_VALUE] == 0.077


async def test_other_changes_keep_the_panel_values(hass: HomeAssistant):
    """Saving «Configurar» without touching the value keeps the panel values, also an off-peak
    value different from the peak one"""
    await setup(hass)
    entry = _entry(hass)
    await _set_number(hass, PEAK, 0.077)
    await _set_number(hass, OFFPEAK, 0.05)
    result, shown = await _open_options(hass, entry)
    await _save_options(hass, entry, result, shown, **{CONF_DURATION_MIN: 20})
    assert entry.data[CONF_DURATION_MIN] == 20
    assert _value(hass, PEAK) == 0.077
    assert _value(hass, OFFPEAK) == 0.05
