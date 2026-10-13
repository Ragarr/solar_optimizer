""" Alls constants for the Solar Optimizer integration. """

import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from homeassistant.helpers import selector
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.components.binary_sensor import DOMAIN as BINARY_SENSOR_DOMAIN
from homeassistant.components.input_number import DOMAIN as INPUT_NUMBER_DOMAIN
from homeassistant.components.input_boolean import DOMAIN as INPUT_BOOLEAN_DOMAIN
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.components.humidifier import DOMAIN as HUMIDIFIER_DOMAIN
from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.components.number import DOMAIN as NUMBER_DOMAIN
from homeassistant.components.fan import DOMAIN as FAN_DOMAIN
from homeassistant.components.light import DOMAIN as LIGHT_DOMAIN
from homeassistant.components.select import DOMAIN as SELECT_DOMAIN
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.water_heater import DOMAIN as WATER_HEATER_DOMAIN


from .const import *  # pylint: disable=wildcard-import, unused-wildcard-import

types_schema_devices = vol.Schema(
    {
        vol.Required(
            CONF_DEVICE_TYPE, default=CONF_DEVICE
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=CONF_DEVICE_TYPES,
                translation_key="device_type",
                mode="list",
            )
        )
    }
)

central_config_schema = vol.Schema(
    {
        vol.Required(CONF_REFRESH_PERIOD_SEC, default=DEFAULT_ENUMERATION_REFRESH_PERIOD_SEC): int,
        vol.Required(CONF_POWER_CONSUMPTION_ENTITY_ID): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=[SENSOR_DOMAIN, INPUT_NUMBER_DOMAIN])
        ),
        vol.Required(CONF_POWER_PRODUCTION_ENTITY_ID): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=[SENSOR_DOMAIN, INPUT_NUMBER_DOMAIN])
        ),
        vol.Optional(CONF_SUBSCRIBE_TO_EVENTS, default=False): cv.boolean,
        vol.Required(CONF_SELL_COST_ENTITY_ID): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=[SENSOR_DOMAIN, INPUT_NUMBER_DOMAIN])
        ),
        vol.Required(CONF_BUY_COST_ENTITY_ID): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=[SENSOR_DOMAIN, INPUT_NUMBER_DOMAIN])
        ),
        vol.Required(CONF_SELL_TAX_PERCENT_ENTITY_ID): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=[SENSOR_DOMAIN, INPUT_NUMBER_DOMAIN])
        ),
        vol.Optional(CONF_SMOOTH_PRODUCTION, default=True): cv.boolean,
        vol.Optional(CONF_SMOOTHING_WINDOW_SEC, default=DEFAULT_SMOOTHING_WINDOW_SEC): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=3600, step=1, unit_of_measurement="s", mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Optional(CONF_BATTERY_SOC_ENTITY_ID): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=[SENSOR_DOMAIN, INPUT_NUMBER_DOMAIN])
        ),
        vol.Optional(CONF_BATTERY_CHARGE_POWER_ENTITY_ID): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=[SENSOR_DOMAIN, INPUT_NUMBER_DOMAIN])
        ),
        vol.Optional(CONF_RAZ_TIME, default=DEFAULT_RAZ_TIME): str,
        vol.Optional(CONF_ALGORITHM, default=DEFAULT_ALGORITHM): selector.SelectSelector(
            selector.SelectSelectorConfig(options=ALGORITHMS, translation_key="algorithm", mode="dropdown")
        ),
        vol.Optional(CONF_CONTRACTED_POWER_ENTITY_ID): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=[SENSOR_DOMAIN, INPUT_NUMBER_DOMAIN, NUMBER_DOMAIN])
        ),
        vol.Optional(CONF_CONTRACTED_POWER_MARGIN, default=DEFAULT_CONTRACTED_POWER_MARGIN): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=50, step=1, unit_of_measurement="%", mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Optional(CONF_OFFPEAK_ENTITY_ID): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=[BINARY_SENSOR_DOMAIN, INPUT_BOOLEAN_DOMAIN])
        ),
        vol.Optional(CONF_NOTIFY_SERVICE): str,
        vol.Optional(CONF_GROUP_COUNT, default=DEFAULT_GROUP_COUNT): selector.NumberSelector(
            selector.NumberSelectorConfig(min=1, max=5, step=1, mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Optional(CONF_GROUP_IMPORT_CAPS): str,
        vol.Optional(CONF_START_SEPARATION_SEC, default=DEFAULT_START_SEPARATION_SEC): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=3600, step=1, unit_of_measurement="s", mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Optional(CONF_WARMUP_SEC, default=DEFAULT_WARMUP_SEC): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=3600, step=1, unit_of_measurement="s", mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Optional(CONF_DISCARD_AFTER_COMMAND_SEC, default=DEFAULT_DISCARD_AFTER_COMMAND_SEC): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=600, step=1, unit_of_measurement="s", mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Optional(CONF_FOREIGN_USER_IDS): str,
    }
)

managed_device_schema = vol.Schema(
    {
        vol.Required(CONF_NAME): str,
        vol.Required(CONF_ENTITY_ID): selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain=[INPUT_BOOLEAN_DOMAIN, SWITCH_DOMAIN, HUMIDIFIER_DOMAIN, CLIMATE_DOMAIN, FAN_DOMAIN, LIGHT_DOMAIN, SELECT_DOMAIN, BUTTON_DOMAIN, WATER_HEATER_DOMAIN]
            )
        ),
        vol.Required(CONF_POWER_MAX): str,
        vol.Optional(CONF_CHECK_USABLE_TEMPLATE, default="{{ True }}"): str,
        vol.Optional(CONF_CHECK_ACTIVE_TEMPLATE): str,
        vol.Optional(CONF_DURATION_MIN, default="60"): selector.NumberSelector(selector.NumberSelectorConfig(min=0.0, max=1440, step=0.1, mode=selector.NumberSelectorMode.BOX)),
        vol.Optional(CONF_DURATION_STOP_MIN): selector.NumberSelector(selector.NumberSelectorConfig(min=0.0, max=1440, step=0.1, mode=selector.NumberSelectorMode.BOX)),
        vol.Optional(CONF_ACTION_MODE, default=CONF_ACTION_MODE_ACTION): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=CONF_ACTION_MODES,
                translation_key="action_mode",
                mode="dropdown",
            )
        ),
        vol.Required(CONF_ACTIVATION_SERVICE, default="switch/turn_on"): str,
        vol.Optional(CONF_DEACTIVATION_SERVICE, default="switch/turn_off"): str,
        vol.Optional(CONF_BATTERY_SOC_THRESHOLD, default=0): str,
        vol.Optional(CONF_MAX_ON_TIME_PER_DAY_MIN): str,
        vol.Optional(CONF_MIN_ON_TIME_PER_DAY_MIN): str,
        vol.Optional(CONF_MAX_ON_TIME_OFFPEAK_MIN): str,
        vol.Optional(CONF_OFFPEAK_TIME): str,
        vol.Optional(CONF_MANUAL_DETECTION, default=False): cv.boolean,
        vol.Optional(CONF_MANUAL_MAX_HOURS, default=DEFAULT_MANUAL_MAX_HOURS): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0.1, max=48, step=0.1, unit_of_measurement="h", mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Optional(CONF_ECHO_WINDOW_SEC, default=DEFAULT_ECHO_WINDOW_SEC): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=600, step=1, unit_of_measurement="s", mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Optional(CONF_MANUAL_RELEASE_TEMPLATE): str,
        vol.Optional(CONF_GROUP, default=1): selector.NumberSelector(
            selector.NumberSelectorConfig(min=1, max=5, step=1, mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Optional(CONF_VALUE, default=DEFAULT_VALUE): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=1, step=0.001, unit_of_measurement="€/kWh", mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Optional(CONF_MEASURED_POWER_ENTITY_ID): selector.EntitySelector(selector.EntitySelectorConfig(domain=[SENSOR_DOMAIN])),
        vol.Optional(CONF_CONFIRM_ON_MIN, default=DEFAULT_CONFIRM_ON_MIN): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=120, step=0.1, unit_of_measurement="min", mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Optional(CONF_CONFIRM_OFF_MIN, default=DEFAULT_CONFIRM_OFF_MIN): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=120, step=0.1, unit_of_measurement="min", mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Optional(CONF_MAX_STARTS_PER_HOUR, default=DEFAULT_MAX_STARTS_PER_HOUR): selector.NumberSelector(
            selector.NumberSelectorConfig(min=1, max=60, step=1, mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Optional(CONF_SHED_REACTION_SEC, default=DEFAULT_SHED_REACTION_SEC): selector.NumberSelector(
            selector.NumberSelectorConfig(min=1, max=600, step=1, unit_of_measurement="s", mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Optional(CONF_VERIFY_AFTER_SEC, default=DEFAULT_VERIFY_AFTER_SEC): selector.NumberSelector(
            selector.NumberSelectorConfig(min=5, max=600, step=1, unit_of_measurement="s", mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Optional(CONF_NOT_ABSORBING_W): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=10000, step=10, unit_of_measurement="W", mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Optional(CONF_MANUAL_IGNORE_TEMPLATE): str,
    }
)

power_managed_device_schema = vol.Schema(
    {
        vol.Required(CONF_NAME): str,
        vol.Required(CONF_ENTITY_ID): selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain=[
                    INPUT_BOOLEAN_DOMAIN,
                    SWITCH_DOMAIN,
                    HUMIDIFIER_DOMAIN,
                    CLIMATE_DOMAIN,
                    FAN_DOMAIN,
                    LIGHT_DOMAIN,
                ]
            )
        ),
        vol.Optional(CONF_POWER_ENTITY_ID): selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain=[
                    INPUT_NUMBER_DOMAIN,
                    NUMBER_DOMAIN,
                    FAN_DOMAIN,
                    LIGHT_DOMAIN,
                    SELECT_DOMAIN,
                ]
            )
        ),
        vol.Optional(CONF_POWER_MIN, default=220): vol.Coerce(float),
        vol.Required(CONF_POWER_MAX): str,
        vol.Optional(CONF_POWER_STEP, default=220): vol.All(vol.Coerce(float), vol.Range(min=0, min_included=False)),
        vol.Optional(CONF_CHECK_USABLE_TEMPLATE, default="{{ True }}"): str,
        vol.Optional(CONF_CHECK_ACTIVE_TEMPLATE): str,
        vol.Optional(CONF_DURATION_MIN, default="60"): selector.NumberSelector(selector.NumberSelectorConfig(min=0.0, max=1440, step=0.1, mode=selector.NumberSelectorMode.BOX)),
        vol.Optional(CONF_DURATION_STOP_MIN): selector.NumberSelector(selector.NumberSelectorConfig(min=0.0, max=1440, step=0.1, mode=selector.NumberSelectorMode.BOX)),
        vol.Optional(CONF_DURATION_POWER_MIN): selector.NumberSelector(selector.NumberSelectorConfig(min=0.0, max=1440, step=0.1, mode=selector.NumberSelectorMode.BOX)),
        vol.Optional(CONF_ACTION_MODE, default=CONF_ACTION_MODE_ACTION): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=CONF_ACTION_MODES,
                translation_key="action_mode",
                mode="dropdown",
            )
        ),
        vol.Required(CONF_ACTIVATION_SERVICE, default="switch/turn_on"): str,
        vol.Required(CONF_DEACTIVATION_SERVICE, default="switch/turn_off"): str,
        vol.Optional(CONF_CHANGE_POWER_SERVICE, default="number/set_value"): str,
        vol.Optional(CONF_CONVERT_POWER_DIVIDE_FACTOR, default=220): selector.NumberSelector(
            selector.NumberSelectorConfig(min=1.0, max=9999, step=0.1, mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Optional(CONF_BATTERY_SOC_THRESHOLD, default=0): str,
        vol.Optional(CONF_MAX_ON_TIME_PER_DAY_MIN): str,
        vol.Optional(CONF_MIN_ON_TIME_PER_DAY_MIN): str,
        vol.Optional(CONF_MAX_ON_TIME_OFFPEAK_MIN): str,
        vol.Optional(CONF_OFFPEAK_TIME): str,
        vol.Optional(CONF_OFFPEAK_POWER): str,
        vol.Optional(CONF_MANUAL_DETECTION, default=False): cv.boolean,
        vol.Optional(CONF_MANUAL_MAX_HOURS, default=DEFAULT_MANUAL_MAX_HOURS): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0.1, max=48, step=0.1, unit_of_measurement="h", mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Optional(CONF_ECHO_WINDOW_SEC, default=DEFAULT_ECHO_WINDOW_SEC): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=600, step=1, unit_of_measurement="s", mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Optional(CONF_MANUAL_RELEASE_TEMPLATE): str,
        vol.Optional(CONF_GROUP, default=1): selector.NumberSelector(
            selector.NumberSelectorConfig(min=1, max=5, step=1, mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Optional(CONF_VALUE, default=DEFAULT_VALUE): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=1, step=0.001, unit_of_measurement="€/kWh", mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Optional(CONF_MEASURED_POWER_ENTITY_ID): selector.EntitySelector(selector.EntitySelectorConfig(domain=[SENSOR_DOMAIN])),
        vol.Optional(CONF_CONFIRM_ON_MIN, default=DEFAULT_CONFIRM_ON_MIN): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=120, step=0.1, unit_of_measurement="min", mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Optional(CONF_CONFIRM_OFF_MIN, default=DEFAULT_CONFIRM_OFF_MIN): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=120, step=0.1, unit_of_measurement="min", mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Optional(CONF_MAX_STARTS_PER_HOUR, default=DEFAULT_MAX_STARTS_PER_HOUR): selector.NumberSelector(
            selector.NumberSelectorConfig(min=1, max=60, step=1, mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Optional(CONF_SHED_REACTION_SEC, default=DEFAULT_SHED_REACTION_SEC): selector.NumberSelector(
            selector.NumberSelectorConfig(min=1, max=600, step=1, unit_of_measurement="s", mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Optional(CONF_VERIFY_AFTER_SEC, default=DEFAULT_VERIFY_AFTER_SEC): selector.NumberSelector(
            selector.NumberSelectorConfig(min=5, max=600, step=1, unit_of_measurement="s", mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Optional(CONF_NOT_ABSORBING_W): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=10000, step=10, unit_of_measurement="W", mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Optional(CONF_MANUAL_IGNORE_TEMPLATE): str,
    }
)
