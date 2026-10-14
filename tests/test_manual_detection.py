"""Detection of manual changes by context, manual mode and its end (no command sent)"""

from datetime import datetime, timedelta
from unittest.mock import patch

from homeassistant.core import Context, State
from pytest_homeassistant_custom_component.common import mock_restore_cache

from custom_components.solar_optimizer.const import get_tz
from .commons import *  # pylint: disable=wildcard-import, unused-wildcard-import

ENTITY = "input_boolean.fake_device_a"
AMPS = "input_number.fake_amps"

DEVICE_DATA = {
    CONF_NAME: "Equipement A",
    CONF_DEVICE_TYPE: CONF_POWERED_DEVICE,
    CONF_ENTITY_ID: ENTITY,
    CONF_POWER_MAX: 3200,
    CONF_POWER_MIN: 600,
    CONF_POWER_STEP: 100,
    CONF_POWER_ENTITY_ID: AMPS,
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
    CONF_MANUAL_DETECTION: True,
    CONF_MANUAL_MAX_HOURS: 12,
    CONF_ECHO_WINDOW_SEC: 60,
}

USER = Context(user_id="a-user")


async def _setup(hass, data=None, restore=None):
    if restore is not None:
        mock_restore_cache(hass, [State("switch.solar_optimizer_equipement_a", "off", restore)])
    hass.states.async_set(ENTITY, "off")
    hass.states.async_set(AMPS, "10")
    entry = MockConfigEntry(domain=DOMAIN, title="Equipement A", unique_id="eqtAUniqueId", data=data or DEVICE_DATA)
    device = await create_managed_device(hass, entry, "equipement_a")
    await hass.async_block_till_done()
    return device


def _at(hass, hour, minute=0):
    return datetime(2026, 10, 13, hour, minute, tzinfo=get_tz(hass))


async def _set(hass, entity_id, state, context=None):
    hass.states.async_set(entity_id, state, context=context)
    await hass.async_block_till_done()


async def _command(device, action):
    """Send a command through the device (the service itself is not available in tests)"""
    with patch("homeassistant.core.ServiceRegistry.async_call"):
        if action == "on":
            await device.activate()
        elif action == "off":
            await device.deactivate()
        else:
            await device.change_requested_power(action)
    return device._own_contexts[-1][0]


async def test_user_change_puts_device_in_manual(hass: HomeAssistant, init_solar_optimizer_central_config):
    """A change with a user_id is manual; the end is now + 12 h but never after the daily reset"""
    device = await _setup(hass)
    device._set_now(_at(hass, 10))
    await _set(hass, ENTITY, "on", USER)
    assert device.is_manual
    assert device.manual_end_time == _at(hass, 22)
    # the minimal on duration counts from the manual change
    assert device.next_date_available == _at(hass, 10, 15)

    switch = search_entity(hass, "switch.solar_optimizer_equipement_a", "switch")
    assert switch.get_attr_extra_state_attributes["is_manual"] is True

    device.end_manual("test")
    device._set_now(_at(hass, 20))
    await _set(hass, ENTITY, "off", USER)
    assert device.manual_end_time == _at(hass, 5) + timedelta(days=1)  # raz_time 05:00 comes first


async def test_own_and_derived_contexts_are_echoes(hass: HomeAssistant, init_solar_optimizer_central_config):
    """Changes caused by our commands (own context, or derived from it) are never manual, even
    after the echo window"""
    device = await _setup(hass)
    device._set_now(_at(hass, 10))
    ctx_id = await _command(device, "on")
    device._set_now(_at(hass, 10, 5))
    await _set(hass, ENTITY, "on", Context(id=ctx_id))
    assert not device.is_manual
    ctx_id = await _command(device, 2000)
    device._set_now(_at(hass, 10, 15))
    await _set(hass, AMPS, "20", Context(parent_id=ctx_id))
    assert not device.is_manual


async def test_echo_window_and_value_comparison(hass: HomeAssistant, init_solar_optimizer_central_config):
    """Without user nor origin: within 60 s of a command it is an echo; later it is manual only if
    the value differs from the last one commanded"""
    device = await _setup(hass)
    device._set_now(_at(hass, 10))
    await _command(device, "on")
    device._set_now(_at(hass, 10) + timedelta(seconds=50))
    await _set(hass, ENTITY, "on", Context())
    assert not device.is_manual
    # The device flickers back to off for a moment within the window: echo too
    await _set(hass, ENTITY, "off", Context())
    assert not device.is_manual
    # Later, a change to the commanded value is not manual
    device._set_now(_at(hass, 10, 5))
    await _set(hass, ENTITY, "on", Context())
    assert not device.is_manual
    # ... but a change away from it is
    device._set_now(_at(hass, 10, 6))
    await _set(hass, ENTITY, "off", Context())
    assert device.is_manual


async def test_back_from_unavailable(hass: HomeAssistant, init_solar_optimizer_central_config):
    """Coming back from unavailable with a value different from the commanded one is manual"""
    device = await _setup(hass)
    device._set_now(_at(hass, 10))
    await _command(device, "on")
    device._set_now(_at(hass, 11))
    await _set(hass, ENTITY, "unavailable", Context())
    await _set(hass, ENTITY, "on", Context())
    assert not device.is_manual
    await _set(hass, ENTITY, "unavailable", Context())
    await _set(hass, ENTITY, "off", Context())
    assert device.is_manual


async def test_power_entity_user_change(hass: HomeAssistant, init_solar_optimizer_central_config):
    """The power entity (amps) is watched too; its change resets the power-change delay"""
    device = await _setup(hass)
    device._set_now(_at(hass, 10))
    await _set(hass, AMPS, "16", USER)
    assert device.is_manual
    assert device.next_date_available_power == _at(hass, 10, 1)


async def test_power_entity_without_user(hass: HomeAssistant, init_solar_optimizer_central_config):
    """A power change without user equal to the commanded power is not manual; a different one is"""
    device = await _setup(hass)
    device._set_now(_at(hass, 10))
    await _command(device, 1300)
    device._set_now(_at(hass, 10, 5))
    await _set(hass, AMPS, "13", Context())
    assert not device.is_manual
    await _set(hass, AMPS, "8", Context())
    assert device.is_manual


async def test_attribute_only_change_is_ignored(hass: HomeAssistant, init_solar_optimizer_central_config):
    """A change of attributes only (same state) is not a manual change"""
    device = await _setup(hass)
    device._set_now(_at(hass, 10))
    hass.states.async_set(ENTITY, "off", {"friendly_name": "renamed"}, context=USER)
    await hass.async_block_till_done()
    assert not device.is_manual


async def test_disabled_detection_or_excluded_device(hass: HomeAssistant, init_solar_optimizer_central_config):
    """Without detection, or when the device is not enabled for SO, nothing becomes manual"""
    device = await _setup(hass, {**DEVICE_DATA, CONF_MANUAL_DETECTION: False})
    await _set(hass, ENTITY, "on", USER)
    assert not device.is_manual
    assert search_entity(hass, "button.solar_optimizer_end_manual_equipement_a", "button") is None


async def test_excluded_device_is_not_marked(hass: HomeAssistant, init_solar_optimizer_central_config):
    """A device excluded from SO (Enable off) is not put in manual mode"""
    device = await _setup(hass)
    device.set_enable(False)
    await _set(hass, ENTITY, "on", USER)
    assert not device.is_manual


async def test_end_of_manual_sends_no_command(hass: HomeAssistant, init_solar_optimizer_central_config):
    """The button, the timeout and the release template end the manual mode without commands"""
    device = await _setup(hass, {**DEVICE_DATA, CONF_MANUAL_RELEASE_TEMPLATE: "{{ is_state('input_boolean.unplugged', 'on') }}"})
    hass.states.async_set("input_boolean.unplugged", "off")
    device._set_now(_at(hass, 10))
    await _set(hass, ENTITY, "on", USER)
    assert device.is_manual

    with patch("homeassistant.core.ServiceRegistry.async_call") as mock_call:
        button = search_entity(hass, "button.solar_optimizer_end_manual_equipement_a", "button")
        await button.async_press()
        assert not device.is_manual

        await _set(hass, ENTITY, "off", USER)
        assert device.is_manual
        device._set_now(_at(hass, 22, 1))
        assert device.check_manual_expiry() is True
        assert not device.is_manual

        await _set(hass, ENTITY, "on", USER)
        assert not device.check_manual_expiry()
        hass.states.async_set("input_boolean.unplugged", "on")
        assert device.check_manual_expiry() is True
        assert not device.is_manual

        await hass.services.async_call(DOMAIN, SERVICE_END_MANUAL, {"device_id": "equipement_a"}, blocking=True)
        # only the service call of the test itself: nothing sent to the device
        assert all(c.args[0] == DOMAIN for c in mock_call.call_args_list)


async def test_manual_device_is_left_out_of_the_algorithm(hass: HomeAssistant, init_solar_optimizer_central_config):
    """A device in manual mode is not part of the optimisation"""
    device = await _setup(hass)
    coordinator: SolarOptimizerCoordinator = SolarOptimizerCoordinator.get_coordinator()
    solution, _, _ = coordinator._algo.recuit_simule([device], -2000, 3000, 1, 1, 0, 0, 0)
    assert [e["name"] for e in solution] == ["Equipement A"]
    await _set(hass, ENTITY, "on", USER)
    solution, _, _ = coordinator._algo.recuit_simule([device], -2000, 3000, 1, 1, 0, 0, 0)
    assert solution == []


async def test_manual_end_time_is_restored(hass: HomeAssistant, init_solar_optimizer_central_config):
    """The manual mode survives a restart (only if not expired)"""
    end = (datetime.now(get_tz(hass)) + timedelta(hours=3)).replace(microsecond=0)
    device = await _setup(hass, restore={"manual_end_time": end.isoformat()})
    assert device.is_manual
    assert device.manual_end_time == end


async def test_manual_not_ended_at_reset_when_disabled(hass: HomeAssistant, init_solar_optimizer_central_config):
    """With manual_end_at_reset off, a device switched on by hand at night stays manual after the
    daily reset (only the maximal duration applies)"""
    device = await _setup(hass, {**DEVICE_DATA, CONF_MANUAL_END_AT_RESET: False})
    device._set_now(_at(hass, 2))
    await _set(hass, ENTITY, "on", USER)
    assert device.manual_end_time == _at(hass, 14)  # 2:00 + 12 h, not 5:00
    device._set_now(_at(hass, 5, 1))
    assert device.check_manual_expiry() is False and device.is_manual


async def test_manual_ends_when_switched_off_for_a_minute(hass: HomeAssistant, init_solar_optimizer_central_config):
    """manual_end_when_off: a device switched on by hand goes back to automatic once it has been
    off for 60 s (a short flicker does not count); switching it off restarts nothing"""
    device = await _setup(hass, {**DEVICE_DATA, CONF_MANUAL_END_WHEN_OFF: True, CONF_MANUAL_MAX_HOURS: 16, CONF_MANUAL_END_AT_RESET: False})
    device._set_now(_at(hass, 2))
    await _set(hass, ENTITY, "on", USER)
    assert device.is_manual and device.manual_started_on
    assert device.manual_end_time == _at(hass, 18)
    # flicker: off 20 s then on again
    device._set_now(_at(hass, 3))
    await _set(hass, ENTITY, "off", Context())
    assert device.check_manual_expiry() is False
    device._set_now(_at(hass, 3) + timedelta(seconds=20))
    await _set(hass, ENTITY, "on", Context())
    assert device.check_manual_expiry() is False
    device._set_now(_at(hass, 3, 2))
    assert device.check_manual_expiry() is False and device.is_manual
    # switched off by hand for good
    device._set_now(_at(hass, 4))
    await _set(hass, ENTITY, "off", USER)
    assert device.is_manual  # not yet: 60 s off needed
    device._set_now(_at(hass, 4) + timedelta(seconds=30))
    device.check_manual_expiry()
    device._set_now(_at(hass, 4) + timedelta(seconds=95))
    assert device.check_manual_expiry() is True and not device.is_manual


async def test_manual_switched_off_by_hand_stays_manual(hass: HomeAssistant, init_solar_optimizer_central_config):
    """A device switched OFF by hand stays manual (until the button or the maximal time)"""
    device = await _setup(hass, {**DEVICE_DATA, CONF_MANUAL_END_WHEN_OFF: True})
    device._set_now(_at(hass, 9))
    await _set(hass, ENTITY, "on", Context())  # e.g. started by SO before: back to auto first
    device.end_manual("test")
    device._set_now(_at(hass, 10))
    await _set(hass, ENTITY, "off", USER)
    assert device.is_manual and not device.manual_started_on
    device._set_now(_at(hass, 11))
    assert device.check_manual_expiry() is False and device.is_manual


async def test_level_power_map(hass: HomeAssistant, init_solar_optimizer_central_config):
    """level_power_map: the levels are the measured powers; the commands send the setting"""
    data = {**DEVICE_DATA, CONF_LEVEL_POWER_MAP: "6:690, 10:1050, 16:1580"}
    device = await _setup(hass, data)
    assert device.units_to_w(10) == 1050 and device.units_to_w(20) == 2000
    assert device.w_to_units(1050) == 10 and device.w_to_units(1580) == 16
    assert device.levels_w()[:2] == (0.0, 600.0)  # input_number: power_min..max by step, unmapped ones by factor
    hass.states.async_set(AMPS, "10")
    assert device.level_w() == 1050
    with patch("homeassistant.core.ServiceRegistry.async_call") as call:
        await device.change_requested_power(1580)
    assert call.call_args.kwargs["service_data"] == {"value": 16}
    # the echo of that command is not a manual change
    device._set_now(_at(hass, 12))
    await _set(hass, AMPS, "16", Context())
    assert not device.is_manual
