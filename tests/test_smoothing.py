""" Tests of the time-window smoothing of the net consumption """

from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant

from custom_components.solar_optimizer.smoothing import TimeWindowAverage
from .commons import *  # pylint: disable=wildcard-import, unused-wildcard-import


def test_time_window_average_holds_each_sample():
    """Each sample counts for the time it was the current value"""
    f = TimeWindowAverage(120)
    assert f.mean(0) is None
    f.add(0, 1000)
    assert f.mean(0) == 1000
    f.add(60, -1000)
    # 60 s at 1000, 60 s at -1000
    assert f.mean(120) == 0
    # 30 s later the oldest 30 s of the 1000 W step have left the window
    assert f.mean(150) == (1000 * 30 - 1000 * 90) / 120


def test_time_window_average_drops_old_samples_and_zero_window():
    """Samples older than the window do not count; a window of 0 gives the last value"""
    f = TimeWindowAverage(60)
    for t, v in ((0, 5000), (10, 0), (100, 200)):
        f.add(t, v)
    assert f.mean(110) == (0 * 50 + 200 * 10) / 60
    g = TimeWindowAverage(0)
    g.add(0, 10)
    g.add(1, 30)
    assert g.mean(5) == 30


def test_time_window_average_short_spike_is_damped():
    """A 10 s spike of 3 kW moves the 2-min mean by only 250 W"""
    f = TimeWindowAverage(120)
    for t in range(0, 300, 10):
        f.add(t, 3000 if t == 200 else 0)
    assert f.mean(290) == 3000 * 10 / 120


async def test_coordinator_smooths_net_consumption_not_production(hass: HomeAssistant):
    """The objective depends on the net consumption: that is the smoothed signal. The production is
    used as measured."""
    entry_central = MockConfigEntry(
        domain=DOMAIN,
        title="Central",
        unique_id="centralUniqueId",
        data={
            CONF_NAME: "Configuration",
            CONF_REFRESH_PERIOD_SEC: 60,
            CONF_DEVICE_TYPE: CONF_DEVICE_CENTRAL,
            CONF_POWER_CONSUMPTION_ENTITY_ID: "sensor.fake_power_consumption",
            CONF_POWER_PRODUCTION_ENTITY_ID: "sensor.fake_power_production",
            CONF_SELL_COST_ENTITY_ID: "input_number.fake_sell_cost",
            CONF_BUY_COST_ENTITY_ID: "input_number.fake_buy_cost",
            CONF_SELL_TAX_PERCENT_ENTITY_ID: "input_number.fake_sell_tax_percent",
            CONF_SMOOTH_PRODUCTION: True,
            CONF_SMOOTHING_WINDOW_SEC: 120,
            CONF_RAZ_TIME: "05:00",
        },
    )
    await create_managed_device(hass, entry_central, "centralUniqueId")
    coordinator: SolarOptimizerCoordinator = SolarOptimizerCoordinator.get_coordinator()

    for entity_id, value in (
        ("input_number.fake_sell_cost", 1),
        ("input_number.fake_buy_cost", 1),
        ("input_number.fake_sell_tax_percent", 0),
    ):
        hass.states.async_set(entity_id, value)

    t0 = datetime(2026, 10, 13, 12, 0, 0)
    readings = ((0, -1000, 2000), (60, 1000, 500))
    data = None
    for dt, consumption, production in readings:
        coordinator._set_now(t0 + timedelta(seconds=dt))
        hass.states.async_set("sensor.fake_power_consumption", consumption, {"device_class": "power", "unit_of_measurement": "W"})
        hass.states.async_set("sensor.fake_power_production", production, {"device_class": "power", "unit_of_measurement": "W"})
        data = await coordinator._async_update_data()

    coordinator._set_now(t0 + timedelta(seconds=60))
    assert data["power_consumption_brut"] == 1000
    # 60 s at -1000 W and the new value just arrived
    assert data["power_consumption"] == -1000
    assert data["power_production"] == 500
    assert data["power_production_brut"] == 500

    coordinator._set_now(t0 + timedelta(seconds=120))
    data = await coordinator._async_update_data()
    assert data["power_consumption"] == 0  # 60 s at -1000 W and 60 s at +1000 W
