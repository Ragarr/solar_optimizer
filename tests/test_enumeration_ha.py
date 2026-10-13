"""The enumeration optimiser + supervisor inside Home Assistant, with simulated time (_set_now):
dry run, invariants I1-I6 on the commands really sent, load shedding, manual changes and their
notification, restore after a restart"""

from datetime import datetime, timedelta
from unittest.mock import patch

from homeassistant.core import Context, State
from pytest_homeassistant_custom_component.common import mock_restore_cache

from custom_components.solar_optimizer.const import get_tz
from .commons import *  # pylint: disable=wildcard-import, unused-wildcard-import

AMPS = ["6A", "8A", "10A", "13A", "16A", "20A", "24A", "32A"]
CAR_SWITCH, CAR_AMPS, CAR_POWER = "switch.car", "select.car_amps", "sensor.car_power"
ACS = ["climate.ac1", "climate.ac2"]
OWNER = "owner-user"
OLD_CONTROLLER = "old-controller-user"

CENTRAL = {
    CONF_NAME: "Configuration",
    CONF_REFRESH_PERIOD_SEC: 30,
    CONF_DEVICE_TYPE: CONF_DEVICE_CENTRAL,
    CONF_POWER_CONSUMPTION_ENTITY_ID: "sensor.net",
    CONF_POWER_PRODUCTION_ENTITY_ID: "sensor.pv",
    CONF_SELL_COST_ENTITY_ID: "input_number.sell",
    CONF_BUY_COST_ENTITY_ID: "input_number.buy",
    CONF_SELL_TAX_PERCENT_ENTITY_ID: "input_number.tax",
    CONF_SMOOTH_PRODUCTION: True,
    CONF_RAZ_TIME: "05:00",
    CONF_ALGORITHM: ALGORITHM_ENUMERATION,
    CONF_CONTRACTED_POWER_ENTITY_ID: "input_number.contracted",
    CONF_CONTRACTED_POWER_MARGIN: 10,
    CONF_OFFPEAK_ENTITY_ID: "binary_sensor.offpeak",
    CONF_NOTIFY_SERVICE: "notify.phone",
    CONF_GROUP_COUNT: 2,
    CONF_WARMUP_SEC: 120,
    CONF_FOREIGN_USER_IDS: OLD_CONTROLLER,
}

CAR = {
    CONF_NAME: "Coche",
    CONF_DEVICE_TYPE: CONF_POWERED_DEVICE,
    CONF_ENTITY_ID: CAR_SWITCH,
    CONF_POWER_MAX: "7424",
    CONF_POWER_MIN: 1392,
    CONF_POWER_STEP: 232,
    CONF_POWER_ENTITY_ID: CAR_AMPS,
    CONF_CHANGE_POWER_SERVICE: "select/select_option/option",
    CONF_CONVERT_POWER_DIVIDE_FACTOR: 232,
    CONF_CHECK_USABLE_TEMPLATE: "{{ True }}",
    CONF_DURATION_MIN: 10,
    CONF_DURATION_STOP_MIN: 10,
    CONF_DURATION_POWER_MIN: 0.5,
    CONF_ACTION_MODE: CONF_ACTION_MODE_ACTION,
    CONF_ACTIVATION_SERVICE: "switch/turn_on",
    CONF_DEACTIVATION_SERVICE: "switch/turn_off",
    CONF_BATTERY_SOC_THRESHOLD: "0",
    CONF_MAX_ON_TIME_PER_DAY_MIN: "1440",
    CONF_MIN_ON_TIME_PER_DAY_MIN: "0",
    CONF_MANUAL_DETECTION: True,
    CONF_ECHO_WINDOW_SEC: 60,
    CONF_GROUP: 1,
    CONF_VALUE: 0.13,
    CONF_MEASURED_POWER_ENTITY_ID: CAR_POWER,
    CONF_CONFIRM_ON_MIN: 1,
    CONF_CONFIRM_OFF_MIN: 5,
    CONF_MAX_STARTS_PER_HOUR: 3,
    CONF_SHED_REACTION_SEC: 10,
    CONF_VERIFY_AFTER_SEC: 45,
    CONF_NOT_ABSORBING_W: 300,
}


def ac_config(i):
    return {
        CONF_NAME: f"Aire {i}",
        CONF_DEVICE_TYPE: CONF_DEVICE,
        CONF_ENTITY_ID: ACS[i - 1],
        CONF_POWER_MAX: "800",
        CONF_CHECK_USABLE_TEMPLATE: "{{ True }}",
        CONF_CHECK_ACTIVE_TEMPLATE: "{{ not is_state('" + ACS[i - 1] + "', 'off') }}",
        CONF_DURATION_MIN: 15,
        CONF_DURATION_STOP_MIN: 10,
        CONF_ACTION_MODE: CONF_ACTION_MODE_ACTION,
        CONF_ACTIVATION_SERVICE: "climate/set_hvac_mode/hvac_mode:heat",
        CONF_DEACTIVATION_SERVICE: "climate/set_hvac_mode/hvac_mode:off",
        CONF_BATTERY_SOC_THRESHOLD: "0",
        CONF_MAX_ON_TIME_PER_DAY_MIN: "1440",
        CONF_MIN_ON_TIME_PER_DAY_MIN: "0",
        CONF_MANUAL_DETECTION: True,
        CONF_ECHO_WINDOW_SEC: 90,
        CONF_GROUP: 2,
        CONF_VALUE: 0.09 if i == 1 else 0.08,
        CONF_CONFIRM_ON_MIN: 2,
        CONF_CONFIRM_OFF_MIN: 5,
        CONF_MAX_STARTS_PER_HOUR: 2,
        CONF_SHED_REACTION_SEC: 30,
        CONF_VERIFY_AFTER_SEC: 60,
    }


class World:
    """Fake devices: the service calls change the states (with the caller's context) after the
    test advances the time; the net consumption follows the devices"""

    def __init__(self, hass):
        self.hass = hass
        self.calls = []  # (time, domain, service, data, target)
        self.base, self.pv = 0.4, 0.0
        self.now = None
        self.respond = True

    def set_now(self, now):
        self.now = now
        coordinator = SolarOptimizerCoordinator.get_coordinator()
        coordinator._set_now(now)
        for device in coordinator.devices:
            device._set_now(now)

    async def service(self, domain, service, service_data=None, target=None, context=None, **kwargs):
        if domain == DOMAIN:
            return
        entity = (target or {}).get("entity_id")
        self.calls.append((self.now, domain, service, dict(service_data or {}), entity))
        if domain == "notify" or not self.respond:
            return
        if domain == "switch":
            self.hass.states.async_set(entity, "on" if service == "turn_on" else "off", context=context)
        elif domain == "select":
            state = self.hass.states.get(entity)
            self.hass.states.async_set(entity, service_data["option"], state.attributes, context=context)
        elif domain == "climate":
            self.hass.states.async_set(entity, service_data["hvac_mode"], context=context)
        self.update_power()

    def car_w(self):
        if self.hass.states.get(CAR_SWITCH).state != "on":
            return 0.0
        return float(self.hass.states.get(CAR_AMPS).state.rstrip("A")) * 232

    def update_power(self):
        acs = sum(800 for e in ACS if self.hass.states.get(e).state != "off")
        car = self.car_w()
        self.hass.states.async_set(CAR_POWER, car, {"unit_of_measurement": "W", "device_class": "power"})
        # a little noise: a constant reading with sun would be a frozen sensor
        noise = (self.now.minute * 7 + self.now.second // 10) % 5 if self.now else 0
        net = self.base * 1000 + acs + car - self.pv * 1000 + noise
        self.hass.states.async_set("sensor.net", round(net, 1), {"unit_of_measurement": "W", "device_class": "power"})
        self.hass.states.async_set("sensor.pv", round(self.pv * 1000 + (self.now.second if self.now else 0) * 0.01, 2), {"unit_of_measurement": "W", "device_class": "power"})

    def device_calls(self, entity=None):
        return [c for c in self.calls if c[1] != "notify" and (entity is None or c[4] == entity)]


async def setup(hass, restore=None, dry_run=None, car_on=False):
    for entity, value in (
        ("input_number.sell", 0.06),
        ("input_number.buy", 0.26371),
        ("input_number.tax", 0),
        ("input_number.contracted", 4.6),
        ("binary_sensor.offpeak", "off"),
        (CAR_SWITCH, "on" if car_on else "off"),
        (ACS[0], "off"),
        (ACS[1], "off"),
    ):
        hass.states.async_set(entity, value)
    hass.states.async_set("input_number.contracted", 4.6, {"unit_of_measurement": "kW"})
    hass.states.async_set(CAR_AMPS, "13A", {"options": AMPS})
    cache = list(restore or [])
    if dry_run is not None:
        cache.append(State("switch.solar_optimizer_dry_run", "on" if dry_run else "off"))
    if cache:
        mock_restore_cache(hass, cache)
    world = World(hass)
    t0 = datetime(2026, 10, 13, 14, 0, tzinfo=get_tz(hass))
    with patch("homeassistant.core.ServiceRegistry.async_call", side_effect=world.service):
        await create_managed_device(hass, MockConfigEntry(domain=DOMAIN, title="Central", unique_id="central", data=CENTRAL), "central")
        await create_managed_device(hass, MockConfigEntry(domain=DOMAIN, title="Coche", unique_id="coche", data=CAR), "coche")
        for i in (1, 2):
            await create_managed_device(hass, MockConfigEntry(domain=DOMAIN, title=f"Aire {i}", unique_id=f"aire{i}", data=ac_config(i)), f"aire_{i}")
        await hass.async_block_till_done()
    world.set_now(t0)
    world.update_power()
    return world, t0


async def run(hass, world, t_from, seconds, step=10):
    """Advance the time: grid sensor every 10 s (fast path), cycle every 30 s"""
    coordinator = SolarOptimizerCoordinator.get_coordinator()
    data = None
    with patch("homeassistant.core.ServiceRegistry.async_call", side_effect=world.service):
        for s in range(0, seconds, step):
            world.set_now(t_from + timedelta(seconds=s))
            world.update_power()
            await hass.async_block_till_done()
            if s % 30 == 0:
                data = await coordinator._async_update_data()
                coordinator.async_set_updated_data(data)
                await hass.async_block_till_done()
    return data


def check_invariants(world, t_on_min=600, t_off_min=600):
    """I1-I4, I6 on the calls really sent (real transitions = the calls, devices respond at once)"""
    starts, last_on, last_off, last_amps = [], {}, {}, None
    for t, domain, service, data, entity in world.device_calls():
        is_on = (domain == "switch" and service == "turn_on") or (domain == "climate" and data.get("hvac_mode") != "off")
        is_off = (domain == "switch" and service == "turn_off") or (domain == "climate" and data.get("hvac_mode") == "off")
        t_on = t_on_min if entity == CAR_SWITCH else 900
        if is_on:
            assert entity not in last_off or (t - last_off[entity]).total_seconds() >= t_off_min, ("I1", entity, t)
            assert sum(1 for x in starts if x[1] == entity and (t - x[0]).total_seconds() < 3600) < (3 if entity == CAR_SWITCH else 2), ("I3", entity, t)
            assert not starts or (t - starts[-1][0]).total_seconds() >= 60, ("I6", entity, t)
            starts.append((t, entity))
            last_on[entity] = t
        elif is_off:
            assert entity not in last_on or (t - last_on[entity]).total_seconds() >= t_on, ("I2", entity, t)
            last_off[entity] = t
        elif domain == "select":
            assert last_amps is None or (t - last_amps).total_seconds() >= 30, ("I4", t)
            last_amps = t


async def test_dry_run_by_default_sends_nothing(hass: HomeAssistant):
    """A new installation is in dry run: plan and commands published, nothing sent"""
    world, t0 = await setup(hass)
    world.pv = 4.0
    data = await run(hass, world, t0, 600)
    assert world.device_calls() == []
    plan = hass.states.get("sensor.solar_optimizer_plan")
    assert "Coche" in plan.state
    assert plan.attributes["dry_run"] is True
    assert any(c.startswith("Coche: on") for c in data["commands"])
    assert hass.states.get("switch.solar_optimizer_dry_run").state == "on"
    assert float(hass.states.get("sensor.solar_optimizer_surplus").state) > 3000


async def test_acts_with_the_guards_and_never_violates(hass: HomeAssistant):
    """Out of dry run: warm-up, confirmations, amps changes, clouds; the calls sent respect
    I1-I6 and the counter stays at 0"""
    world, t0 = await setup(hass, dry_run=False)
    world.pv = 4.0
    await run(hass, world, t0, 100)
    assert world.device_calls() == []  # warm-up (2 min)
    await run(hass, world, t0 + timedelta(seconds=100), 400)
    assert world.device_calls(CAR_SWITCH)[0][2] == "turn_on"
    assert hass.states.get(CAR_AMPS).state == "16A"  # 3.712 kW for 3.6 kW of surplus at peak
    # Clouds every 3 min for 2 hours
    t = t0 + timedelta(seconds=500)
    for k in range(40):
        world.pv = 4.5 if k % 2 == 0 else 0.6
        await run(hass, world, t, 180)
        t += timedelta(seconds=180)
    check_invariants(world)
    assert hass.states.get("sensor.solar_optimizer_violations").state == "0"
    status = hass.states.get("sensor.solar_optimizer_status_coche")
    assert status.state == "auto" and status.attributes["group"] == 1


async def test_load_shedding_fast_path(hass: HomeAssistant):
    """Import over the contracted power: the car goes to 6 A at once, then stops"""
    world, t0 = await setup(hass, dry_run=False)
    world.pv = 5.0
    await run(hass, world, t0, 600)
    assert hass.states.get(CAR_SWITCH).state == "on"
    world.pv, world.base = 0.0, 3.0  # clouds + oven: 3 kW + car > 4.14 kW
    calls_before = len(world.device_calls())
    await run(hass, world, t0 + timedelta(seconds=600), 30)
    new = world.device_calls()[calls_before:]
    assert new[0][1:4] == ("select", "select_option", {"option": "6A"})
    assert any(c[1:3] == ("switch", "turn_off") for c in new)
    assert hass.states.get("sensor.solar_optimizer_violations").state == "0"


async def test_manual_change_notified_and_back_to_auto(hass: HomeAssistant):
    """A change by the owner: manual mode, notification with the "back to automatic" action;
    the action ends it without sending any command. A change of the old controller is not manual."""
    world, t0 = await setup(hass, dry_run=False)
    world.pv = 0.0
    await run(hass, world, t0, 300)
    car = SolarOptimizerCoordinator.get_coordinator().get_device_by_name("Coche")

    with patch("homeassistant.core.ServiceRegistry.async_call", side_effect=world.service):
        hass.states.async_set(CAR_AMPS, "20A", {"options": AMPS}, context=Context(user_id=OLD_CONTROLLER))
        await hass.async_block_till_done()
        assert not car.is_manual
        hass.states.async_set(CAR_SWITCH, "on", context=Context(user_id=OWNER))
        await hass.async_block_till_done()
    assert car.is_manual
    notes = [c for c in world.calls if c[1] == "notify"]
    assert "Coche en manual hasta las" in notes[-1][3]["message"]
    action = notes[-1][3]["data"]["actions"][0]["action"]

    calls_before = len(world.device_calls())
    await run(hass, world, t0 + timedelta(seconds=300), 600)
    assert world.device_calls()[calls_before:] == []  # manual: not touched (no sun, it would stop it)
    assert hass.states.get("sensor.solar_optimizer_status_coche").state == "manual"

    hass.bus.async_fire("mobile_app_notification_action", {"action": action})
    await hass.async_block_till_done()
    assert not car.is_manual
    assert world.device_calls()[calls_before:] == []  # ending the manual mode sends nothing


async def test_supervisor_state_restored_after_restart(hass: HomeAssistant):
    """The timers survive a restart: a car started 2 min before the restart cannot be stopped
    before its 10 min; the violation counter is kept"""
    tz = get_tz(hass)
    t0 = datetime(2026, 10, 13, 14, 0, tzinfo=tz)
    started = (t0 - timedelta(minutes=2)).timestamp()
    restore = [
        State("switch.solar_optimizer_coche", "on", {"supervisor": {"on": True, "level": 3.016, "t_on": started, "starts": [started]}}),
        State("sensor.solar_optimizer_violations", "3", {"supervisor": {"last_start": started}}),
    ]
    world, _ = await setup(hass, restore=restore, dry_run=False, car_on=True)
    world.pv = 0.0  # no sun: the plan wants the car off
    await run(hass, world, t0, 450)
    assert world.device_calls(CAR_SWITCH) == []  # T_on (10 min from the real start) not reached
    # Meanwhile the plan cannot include 0: least import, 6 A
    assert hass.states.get(CAR_AMPS).state == "6A"
    await run(hass, world, t0 + timedelta(seconds=450), 450)
    stops = world.device_calls(CAR_SWITCH)
    assert stops and stops[0][2] == "turn_off"
    # stopping allowed 10 min after the real start, then the 5 min "off" confirmation
    assert (stops[0][0].timestamp() - started) >= 600 + 300
    assert hass.states.get("sensor.solar_optimizer_violations").state == "3"
