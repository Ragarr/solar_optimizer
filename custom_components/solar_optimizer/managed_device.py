""" A ManagedDevice represent a device than can be managed by the optimisatiion algorithm"""
import logging
import math
import re
from collections import deque
from datetime import datetime, timedelta, time

from homeassistant.core import Context, State

from homeassistant.core import HomeAssistant
from homeassistant.helpers.template import Template
from homeassistant.components.select import SelectEntity
from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN

from homeassistant.components.light import DOMAIN as LIGHT_DOMAIN
from homeassistant.components.fan import DOMAIN as FAN_DOMAIN
from homeassistant.components.select import DOMAIN as SELECT_DOMAIN

from .supervisor_core import DeviceParams

from .const import (
    get_tz,
    name_to_unique_id,
    get_template_or_value,
    get_safe_float,
    convert_to_template_or_value,
    CONF_ACTION_MODE_ACTION,
    CONF_ACTION_MODE_EVENT,
    CONF_ACTION_MODES,
    CONF_MAX_ON_TIME_OFFPEAK_MIN,
    CONF_OFFPEAK_POWER,
    CONF_MANUAL_DETECTION,
    CONF_MANUAL_MAX_HOURS,
    CONF_ECHO_WINDOW_SEC,
    CONF_MANUAL_RELEASE_TEMPLATE,
    CONF_MANUAL_END_AT_RESET,
    DEFAULT_MANUAL_MAX_HOURS,
    DEFAULT_ECHO_WINDOW_SEC,
    EVENT_TYPE_SOLAR_OPTIMIZER_MANUAL_CHANGE,
    CONF_GROUP,
    CONF_VALUE,
    CONF_MEASURED_POWER_ENTITY_ID,
    CONF_CONFIRM_ON_MIN,
    CONF_CONFIRM_OFF_MIN,
    CONF_MAX_STARTS_PER_HOUR,
    CONF_SHED_REACTION_SEC,
    CONF_VERIFY_AFTER_SEC,
    CONF_NOT_ABSORBING_W,
    CONF_MANUAL_IGNORE_TEMPLATE,
    CONF_LEVEL_POWER_MAP,
    CONF_MANUAL_END_WHEN_OFF,
    DEFAULT_VALUE,
    DEFAULT_CONFIRM_ON_MIN,
    DEFAULT_CONFIRM_OFF_MIN,
    DEFAULT_MAX_STARTS_PER_HOUR,
    DEFAULT_SHED_REACTION_SEC,
    DEFAULT_VERIFY_AFTER_SEC,
    ConfigurationError,
    EVENT_TYPE_SOLAR_OPTIMIZER_CHANGE_POWER,
    EVENT_TYPE_SOLAR_OPTIMIZER_STATE_CHANGE,
    EVENT_TYPE_SOLAR_OPTIMIZER_ENABLE_STATE_CHANGE,
)

ACTION_ACTIVATE = "Activate"
ACTION_DEACTIVATE = "Deactivate"
ACTION_CHANGE_POWER = "ChangePower"

# Entity domains that require attribute to change power
POWERED_ENTITY_DOMAINS_NEED_ATTR = (
    LIGHT_DOMAIN,
    FAN_DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

async def do_service_action(
    hass: HomeAssistant,
    entity_id,
    action_type,
    service_name,
    current_power,
    requested_power,
    convert_power_divide_factor,
    context: Context | None = None,
):
    """Activate an entity via a service call. The call is made with `context` (when given) so
    that the resulting state changes can be recognised as caused by Solar Optimizer."""

    if service_name is None or len(service_name) == 0:
        _LOGGER.info(
            "No service name defined for entity %s. Cannot call service",
            entity_id,
        )
        return

    _LOGGER.info("Calling service %s for entity %s", service_name, entity_id)
    parties = service_name.split("/")
    if len(parties) < 2:
        raise ConfigurationError(
            f"Incorrect service declaration for entity {entity_id}. Service {service_name} should be formatted with: 'domain/action[/option:value]'"
        )

    service_data = {}
    domain = parties[0]
    action = parties[1]
    parameter = parties[2] if len(parties) == 3 else None

    attributes = "value" # default data key for most entities

    if action_type == ACTION_CHANGE_POWER:
        value = round(requested_power / convert_power_divide_factor)
        if parameter:
            attributes = parameter
        if domain in (SELECT_DOMAIN, "input_select"):
            # Options like "6A", "8A"...: the option whose number is the value
            value = _select_option_for(hass, entity_id, value)
        service_data = {attributes: value}
    else:
        if parameter:
            args = parameter.split(":")
            if len(args) >= 2:
                service_data = {args[0]: args[1]}

    target = {
        "entity_id": entity_id,
    }

    try:
        extra = {"context": context} if context is not None else {}
        await hass.services.async_call(
            domain, action, service_data=service_data, target=target, **extra
        )
    except Exception as err:  # pylint: disable=broad-except
        _LOGGER.exception(err)

    # Also send an event to inform
    do_event_action(
        hass,
        entity_id,
        action_type,
        current_power,
        requested_power,
        EVENT_TYPE_SOLAR_OPTIMIZER_CHANGE_POWER if action_type == ACTION_CHANGE_POWER else EVENT_TYPE_SOLAR_OPTIMIZER_STATE_CHANGE,
    )


def _leading_number(value) -> float | None:
    """Number at the start of a state or option ("13A" -> 13.0), None if there is none"""
    match = re.match(r"\s*(-?\d+(?:[.,]\d+)?)", str(value)) if value is not None else None
    return float(match.group(1).replace(",", ".")) if match else None


def _select_option_for(hass: HomeAssistant, entity_id: str, value):
    """The option of a select whose leading number equals value (the value itself if none)"""
    state = hass.states.get(entity_id)
    for option in (state.attributes.get("options") or []) if state else []:
        number = _leading_number(option)
        if number is not None and math.isclose(number, float(value)):
            return option
    return str(value)


def do_event_action(
    hass: HomeAssistant,
    entity_id,
    action_type,
    current_power,
    requested_power,
    event_type: str,
):
    """Activate an entity via an event"""
    _LOGGER.info(
        "Sending event %s with action %s for entity %s with requested_power %s and current_power %s",
        event_type,
        action_type,
        entity_id,
        requested_power,
        current_power,
    )

    hass.bus.fire(
        event_type=event_type,
        event_data={
            "action_type": action_type,
            "requested_power": requested_power,
            "current_power": current_power,
            "entity_id": entity_id,
        },
    )


def _parse_level_map(text) -> dict[float, float]:
    """"6:1387, 8:1732" -> {6.0: 1387.0, 8.0: 1732.0}"""
    out = {}
    for part in str(text or "").replace(";", ",").split(","):
        if ":" in part:
            key, value = part.split(":", 1)
            try:
                out[float(key.strip().rstrip("Aa"))] = float(value.strip())
            except ValueError:
                _LOGGER.warning("Invalid level_power_map entry %r", part)
    return out


def _or(value, default):
    return default if value in (None, "") else value


class ManagedDevice:
    """A Managed device representation"""

    def __init__(self, hass: HomeAssistant, device_config, coordinator):
        """Initialize a manageable device"""
        self._now = None  # For testing purpose only
        self._current_tz = get_tz(hass)

        self._hass = hass
        self._coordinator = coordinator
        self._name = device_config.get("name")
        self._unique_id = name_to_unique_id(self._name)
        self._entity_id = device_config.get("entity_id")
        self._power_entity_id = device_config.get("power_entity_id")
        self._power_max = convert_to_template_or_value(hass, device_config.get("power_max"))
        self._offpeak_power = convert_to_template_or_value(hass, device_config.get(CONF_OFFPEAK_POWER))

        self._power_min = (
            int(device_config.get("power_min"))
            if device_config.get("power_min") is not None
            else -1
        )
        self._power_step = (
            int(device_config.get("power_step"))
            if device_config.get("power_step") is not None
            else 0
        )
        self._can_change_power = self._power_min >= 0
        self._convert_power_divide_factor = int(
            device_config.get("convert_power_divide_factor") or 1
        )

        self._current_power = self._requested_power = 0
        duration_min = float(device_config.get("duration_min"))
        self._duration_sec = round(duration_min * 60)
        self._duration_power_sec = round(
            float(device_config.get("duration_power_min") or duration_min) * 60
        )

        self._duration_stop_sec = round(
            float(device_config.get("duration_stop_min") or duration_min) * 60
        )

        if device_config.get("check_usable_template"):
            self._check_usable_template = Template(
                device_config.get("check_usable_template"), hass
            )
        else:
            # If no template for usability, the device is supposed to be always usable
            self._check_usable_template = Template("{{ True }}", hass)
        if device_config.get("check_active_template"):
            self._check_active_template = Template(
                device_config.get("check_active_template"), hass
            )
        else:
            template_string = (
                "{{ is_state('" + self._entity_id + "', '" + STATE_ON + "') }}"
            )
            self._check_active_template = Template(template_string, hass)
        self._next_date_available_power = self._next_date_available = self.now
        self._action_mode = device_config.get("action_mode")
        self._activation_service = device_config.get("activation_service")
        self._deactivation_service = device_config.get("deactivation_service")
        self._change_power_service = device_config.get("change_power_service")

        self._battery_soc = None
        self._battery_soc_threshold = convert_to_template_or_value(hass, device_config.get("battery_soc_threshold") or 0)

        self._max_on_time_per_day_min = convert_to_template_or_value(hass, device_config.get("max_on_time_per_day_min") or 60 * 24)
        self._max_on_time_offpeak_min = convert_to_template_or_value(hass, device_config.get(CONF_MAX_ON_TIME_OFFPEAK_MIN))
        self._on_time_sec = 0

        self._min_on_time_per_day_min = convert_to_template_or_value(hass, device_config.get("min_on_time_per_day_min") or 0)

        offpeak_time = device_config.get("offpeak_time", None)
        self._offpeak_time = None

        if offpeak_time:
            self._offpeak_time = datetime.strptime(
                device_config.get("offpeak_time"), "%H:%M"
            ).time()

        if self.is_active:
            self._requested_power = self._current_power = self.power_max if self._can_change_power else self._power_min

        self._enable = True

        self._forced_end_time: datetime | None = None

        # Detection of manual changes: every command is sent with its own Context, remembered for a
        # while, so that the state changes it causes are recognised as echoes of our own commands.
        self._manual_detection: bool = bool(device_config.get(CONF_MANUAL_DETECTION, False))
        self._manual_max_hours = float(device_config.get(CONF_MANUAL_MAX_HOURS) or DEFAULT_MANUAL_MAX_HOURS)
        # The manual mode also ends at the daily reset (raz_time) unless disabled: e.g. an air
        # conditioner switched on by hand at night must not be switched off at the reset
        self._manual_end_at_reset: bool = device_config.get(CONF_MANUAL_END_AT_RESET, True) is not False
        echo = device_config.get(CONF_ECHO_WINDOW_SEC)
        self._echo_window_sec = float(DEFAULT_ECHO_WINDOW_SEC if echo is None else echo)
        release = device_config.get(CONF_MANUAL_RELEASE_TEMPLATE)
        self._manual_release_template = Template(release, hass) if release else None
        self._own_contexts: deque[tuple[str, datetime]] = deque(maxlen=50)
        self._last_state_command: bool | None = None  # last on/off commanded
        self._last_power_command: float | None = None  # last power commanded (W)
        self._last_command_time: datetime | None = None
        self._last_foreign_time: datetime | None = None  # last change made by another controller
        self._manual_end_time: datetime | None = None
        ignore = device_config.get(CONF_MANUAL_IGNORE_TEMPLATE)
        self._manual_ignore_template = Template(ignore, hass) if ignore else None

        # Optimiser (enumeration) and supervisor
        self._group = int(device_config.get(CONF_GROUP) or 1)
        self._default_value = float(device_config.get(CONF_VALUE) if device_config.get(CONF_VALUE) is not None else DEFAULT_VALUE)
        self._measured_power_entity_id = device_config.get(CONF_MEASURED_POWER_ENTITY_ID)
        self._confirm_on_sec = float(_or(device_config.get(CONF_CONFIRM_ON_MIN), DEFAULT_CONFIRM_ON_MIN)) * 60
        self._confirm_off_sec = float(_or(device_config.get(CONF_CONFIRM_OFF_MIN), DEFAULT_CONFIRM_OFF_MIN)) * 60
        self._max_starts_per_hour = int(_or(device_config.get(CONF_MAX_STARTS_PER_HOUR), DEFAULT_MAX_STARTS_PER_HOUR))
        self._shed_reaction_sec = float(_or(device_config.get(CONF_SHED_REACTION_SEC), DEFAULT_SHED_REACTION_SEC))
        self._verify_after_sec = float(_or(device_config.get(CONF_VERIFY_AFTER_SEC), DEFAULT_VERIFY_AFTER_SEC))
        not_absorbing = device_config.get(CONF_NOT_ABSORBING_W)
        self._not_absorbing_w = float(not_absorbing) if not_absorbing not in (None, "", 0) else None
        self._value_entities: dict[str, object] = {}  # "peak" / "offpeak" -> number entity
        # Measured power of each setting of the power entity ("6:1387, 8:1732": 6 A draws 1387 W);
        # settings not listed use the divide factor
        self._level_power_map: dict[float, float] = _parse_level_map(device_config.get(CONF_LEVEL_POWER_MAP))
        # The manual mode of a device switched on by hand ends when it is switched off again
        self._manual_end_when_off: bool = bool(device_config.get(CONF_MANUAL_END_WHEN_OFF, False))
        self._manual_started_on: bool = False
        self._manual_off_since: datetime | None = None

        # Some checks
        # min_on_time_per_day_sec requires an offpeak_time
        if self.min_on_time_per_day_sec > 0 and self._offpeak_time is None:
            msg = f"configuration of device ${self.name} is incorrect. min_on_time_per_day_sec requires offpeak_time value"
            _LOGGER.error("%s - %s", self, msg)
            raise ConfigurationError(msg)

        if self.min_on_time_per_day_sec > self.max_on_time_per_day_sec:
            msg = f"configuration of device ${self.name} is incorrect. min_on_time_per_day_sec should < max_on_time_per_day_sec"
            _LOGGER.error("%s - %s", self, msg)
            raise ConfigurationError(msg)

        try:
            self.offpeak_power
            self.max_on_time_offpeak_sec
        except ConfigurationError as err:
            _LOGGER.error("%s - invalid off-peak configuration: %s", self, err)
            raise

        self._priority_entity = None

    async def _apply_action(self, action_type: str, requested_power=None):
        """Apply an action to a managed device.
        This method is a generical method for activate, deactivate, change_requested_power
        """
        _LOGGER.debug(
            "Applying action %s for entity %s. requested_power=%s",
            action_type,
            self._entity_id,
            requested_power,
        )

        if requested_power is None:
            requested_power = self._requested_power

        # Dry run (enumeration algorithm): nothing reaches the device, whatever the origin of the
        # command (supervisor, "Active" switch, start_device / stop_device services)
        runtime = getattr(self._coordinator, "runtime", None)
        if runtime is not None and getattr(self._coordinator, "algorithm", None) == "enumeration" and runtime.dry_run:
            _LOGGER.info("%s - dry run: %s (%s W) not sent", self._name, action_type, requested_power)
            return

        if self._action_mode == CONF_ACTION_MODE_ACTION:
            method = None
            entity_id = self._entity_id
            if action_type == ACTION_ACTIVATE:
                method = self._activation_service
                self.reset_next_date_available(action_type)
                if self._can_change_power:
                    self.reset_next_date_available_power()
            elif action_type == ACTION_DEACTIVATE:
                method = self._deactivation_service
                self.reset_next_date_available(action_type)
            elif action_type == ACTION_CHANGE_POWER:
                assert (
                    self._can_change_power
                ), f"Equipment {self._name} cannot change its power. We should not be there."
                method = self._change_power_service
                entity_id = self._power_entity_id
                self.reset_next_date_available_power()

            context = self._new_command_context(action_type, requested_power)
            service_power = requested_power
            if action_type == ACTION_CHANGE_POWER and self._level_power_map and requested_power is not None:
                # the service takes the setting (e.g. 13 A), not the measured power
                service_power = self.w_to_units(float(requested_power)) * self._convert_power_divide_factor
            await do_service_action(
                self._hass,
                entity_id,
                action_type,
                method,
                self._current_power,
                service_power,
                self._convert_power_divide_factor,
                context,
            )
        elif self._action_mode == CONF_ACTION_MODE_EVENT:
            do_event_action(
                self._hass,
                self._entity_id,
                action_type,
                self._current_power,
                self._requested_power,
                EVENT_TYPE_SOLAR_OPTIMIZER_CHANGE_POWER,
            )
        else:
            raise ConfigurationError(
                f"Incorrect action_mode declaration for entity '{self._entity_id}'. Action_mode '{self._action_mode}' is not supported. Use one of {CONF_ACTION_MODES}"
            )

        self._current_power = self._requested_power

    async def activate(self, requested_power=None):
        """Use this method to activate this ManagedDevice"""
        return await self._apply_action(ACTION_ACTIVATE, requested_power)

    async def deactivate(self):
        """Use this method to deactivate this ManagedDevice"""
        if self._forced_end_time is not None:
            _LOGGER.info("%s - deactivate: clearing active forced_end_time timer", self.name)
            self._forced_end_time = None
            # Notifie le switch HA pour qu'il mette à jour l'attribut forced_end_time
            self.publish_enable_state_change()
        return await self._apply_action(ACTION_DEACTIVATE, 0)

    async def start_forced(self, duration_hours: float | None = None) -> None:
        """Force the activation of this device for an optional duration (in hours).
        Sets enable=False and activates the device. The forced_end_time is set if duration is given."""
        self.set_enable(False)
        if duration_hours is not None:
            self._forced_end_time = self.now + timedelta(hours=float(duration_hours))
            _LOGGER.info("%s - start_forced with duration=%.1fh, end_time=%s", self.name, duration_hours, self._forced_end_time)
        else:
            self._forced_end_time = None
            _LOGGER.info("%s - start_forced without duration limit", self.name)
        await self.activate()

    async def stop_forced(self) -> None:
        """Stop the forced activation of this device. Clears the timer and deactivates."""
        _LOGGER.info("%s - stop_forced", self.name)
        self._forced_end_time = None
        await self.deactivate()
        # Notifie le switch HA pour qu'il mette à jour ses attributs (forced_end_time → null)
        self.publish_enable_state_change()

    async def expire_forced_activation(self) -> bool:
        """Called periodically to check if the forced timer has expired.
        If so, gives the device back to SO management without sending any command: the device
        stays as it is and the algorithm decides in the next cycles (with the usual minimal
        durations). Stopping it here would switch off a device the algorithm may want on, and
        would bypass the anti short-cycle protections.
        Returns True if the timer expired."""
        if self._forced_end_time is None or self.now < self._forced_end_time:
            return False
        _LOGGER.info("%s - Forced activation timer expired, re-enabling SO management (no command sent)", self.name)
        self._forced_end_time = None
        self.set_enable(True)
        return True

    async def change_requested_power(self, requested_power, current_power=None):
        """Use this method to change the requested power of this ManagedDevice"""
        return await self._apply_action(ACTION_CHANGE_POWER, requested_power)

    def reset_next_date_available(self, action_type):
        """Incremente the next availability date to now + _duration_sec"""
        if action_type == ACTION_ACTIVATE:
            self._next_date_available = self.now + timedelta(seconds=self._duration_sec)
        else:
            self._next_date_available = self.now + timedelta(
                seconds=self._duration_stop_sec
            )

        _LOGGER.debug(
            "Next availability date for %s is %s", self._name, self._next_date_available
        )

    def reset_next_date_available_power(self):
        """Incremente the next availability date for power change to now + _duration_power_sec"""
        self._next_date_available_power = self.now + timedelta(
            seconds=self._duration_power_sec
        )
        _LOGGER.debug(
            "Next availability date for power change for %s is %s",
            self._name,
            self._next_date_available_power,
        )
        """Lors d'un changement de puissance, si _next_date_available ne respect pas
        le temps min de puissance, on le remet à jour pour assusrer un temps min à la
        nouvelle puissance avant de pouvoir autoriser une coupure"""
        if self._next_date_available <= self._next_date_available_power:
            self._next_date_available = self._next_date_available_power
            _LOGGER.info(
                "Mise à jour de Next availability date suite au changmement de puissance %s is %s",
                self._name,
                self._next_date_available,
            )

    def restore_next_dates_available(self, next_date: datetime | None, next_date_power: datetime | None) -> None:
        """Restore the availability dates persisted before a restart. Dates in the past are
        ignored (the device is available now, as without persistence)."""
        now = self.now
        if next_date is not None and next_date > now:
            self._next_date_available = next_date
            _LOGGER.info("%s - Restored next_date_available=%s", self.name, next_date)
        if next_date_power is not None and next_date_power > now:
            self._next_date_available_power = next_date_power
            _LOGGER.info("%s - Restored next_date_available_power=%s", self.name, next_date_power)

    # ------------------------------------------------------------------ manual changes

    def _new_command_context(self, action_type: str, requested_power) -> Context | None:
        """A new Context for a command, remembered to recognise its echoes (None when manual
        changes are not detected: the command is sent as before)"""
        if not self._manual_detection:
            return None
        context = Context()
        now = self.now
        self._own_contexts.append((context.id, now))
        if action_type == ACTION_CHANGE_POWER:
            self._last_power_command = requested_power
        else:
            self._last_state_command = action_type == ACTION_ACTIVATE
        self._last_command_time = now
        return context

    def is_own_context(self, context: Context | None) -> bool:
        """True if the context is one of our commands, or derived from one of them"""
        if context is None:
            return False
        ids = {cid for cid, _ in self._own_contexts}
        return context.id in ids or (context.parent_id is not None and context.parent_id in ids)

    def units_to_w(self, units: float) -> float:
        """Power (W) drawn at a setting of the power entity (e.g. 13 A)"""
        for key, watts in self._level_power_map.items():
            if math.isclose(key, units):
                return watts
        return units * self._convert_power_divide_factor

    def w_to_units(self, watts: float) -> float:
        """Setting of the power entity for a power (W): the inverse of units_to_w"""
        if self._level_power_map:
            best = min(self._level_power_map.items(), key=lambda kv: abs(kv[1] - watts))
            nominal = round(watts / self._convert_power_divide_factor)
            if abs(best[1] - watts) <= abs(self.units_to_w(nominal) - watts):
                return best[0]
            return nominal
        return round(watts / self._convert_power_divide_factor)

    def _power_from_state(self, state: State) -> float | None:
        """Requested power (W) shown by the power entity state (numbers like "13" or "13A")"""
        value = state.state
        if self._power_entity_id and self._power_entity_id.startswith(POWERED_ENTITY_DOMAINS_NEED_ATTR):
            parameter = (self._change_power_service or "").split("/")
            value = state.attributes.get(parameter[2]) if len(parameter) > 2 else None
        number = _leading_number(value)
        return None if number is None else self.units_to_w(number)

    def classify_underlying_change(self, entity_id: str, new_state: State | None, old_state: State | None) -> str | None:
        """Classify a state change of the device entity or of its power entity:
        - None: not relevant (no new value, unavailable, attribute-only change, detection disabled)
        - "echo": caused by a Solar Optimizer command (own context, derived from it, or within the
          echo window after one of our commands)
        - "manual": made by a user (context with user_id) or by something else with a value
          different from the last one we commanded (also when coming back from unavailable)
        - "same": a change without user that matches what we commanded
        """
        if not self._manual_detection or new_state is None or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        from_unavailable = old_state is None or old_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN)
        if not from_unavailable and old_state.state == new_state.state:
            if entity_id != self._power_entity_id or self._power_from_state(old_state) == self._power_from_state(new_state):
                return None
        context = new_state.context
        if self.is_own_context(context):
            return "echo"
        if self._last_command_time is not None and (self.now - self._last_command_time).total_seconds() <= self._echo_window_sec:
            return "echo"
        if context is not None and context.user_id is not None:
            if context.user_id in self._coordinator_foreign_users():
                # Another controller; its echoes (e.g. the state reported later by a cloud
                # device, without user) are also its changes during the echo window
                self._last_foreign_time = self.now
                return "foreign"
            return "manual"
        if self._last_foreign_time is not None and (self.now - self._last_foreign_time).total_seconds() <= self._echo_window_sec:
            return "foreign"
        if self._manual_ignore_template is not None:
            try:
                if self._manual_ignore_template.async_render(context={}) is True:
                    return "device"
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.warning("%s - manual ignore template failed: %s", self.name, err)
        # No user: compare with what we commanded last for this entity (or with the previous value)
        if entity_id == self._power_entity_id and entity_id != self._entity_id:
            value = self._power_from_state(new_state)
            if value is None:
                return None
            if self._last_power_command is not None:
                expected = self._last_power_command
            elif from_unavailable:
                return "same"  # nothing commanded yet: the value after an outage is the starting point
            else:
                expected = self._power_from_state(old_state)
            if expected is None:
                return "manual"
            return "same" if math.isclose(value, float(expected), abs_tol=1e-6) else "manual"
        active = bool(self.is_active)
        if self._last_state_command is not None:
            return "same" if active == self._last_state_command else "manual"
        if from_unavailable:
            # Nothing commanded yet: the value after an outage is the starting point
            return "same"
        return "manual"

    def _coordinator_foreign_users(self) -> set[str]:
        return set(getattr(self._coordinator, "foreign_user_ids", None) or ())

    # ------------------------------------------------------------------ optimiser and supervisor

    @property
    def group(self) -> int:
        """Priority group (1 = highest)"""
        return self._group

    @property
    def default_value(self) -> float:
        """Configured value of 1 kWh delivered to this device (€/kWh)"""
        return self._default_value

    @property
    def measured_power_entity_id(self) -> str | None:
        return self._measured_power_entity_id

    def set_value_entity(self, period: str, entity) -> None:
        """Register the number entity holding the value for a tariff period ("peak"/"offpeak")"""
        self._value_entities[period] = entity

    def value(self, offpeak: bool) -> float:
        """Value of 1 kWh delivered to the device in the current tariff period"""
        entity = self._value_entities.get("offpeak" if offpeak else "peak")
        value = getattr(entity, "native_value", None) if entity is not None else None
        return float(value) if value is not None else self._default_value

    def levels_w(self) -> tuple[float, ...]:
        """Admissible levels in W, 0 included. On/off devices: (0, power_max). Modulable devices:
        the options of a select power entity (e.g. "6A".."32A" x the divide factor) within
        [power_min, power_max], or power_min..power_max by power_step."""
        power_max = float(self.power_max)
        if not self._can_change_power:
            return (0.0, power_max)
        levels = []
        state = self._hass.states.get(self._power_entity_id) if self._power_entity_id else None
        if self._power_entity_id and self._power_entity_id.startswith((SELECT_DOMAIN + ".", "input_select.")) and state is not None:
            for option in state.attributes.get("options") or []:
                number = _leading_number(option)
                if number is not None:
                    nominal = number * self._convert_power_divide_factor
                    if self._power_min - 1e-6 <= nominal <= power_max + 1e-6:
                        levels.append(self.units_to_w(number))
        if not levels and self._power_step > 0:
            watts = float(self._power_min)
            while watts <= power_max + 1e-6:
                levels.append(watts)
                watts += self._power_step
        return (0.0,) + tuple(sorted(set(levels)))

    def supervisor_params(self) -> DeviceParams:
        """Guards of the device for the supervisor (durations configured in minutes)"""
        return DeviceParams(
            levels=tuple(round(w / 1000, 4) for w in self.levels_w()),
            confirm_on=self._confirm_on_sec,
            confirm_off=self._confirm_off_sec,
            min_on=float(self._duration_sec),
            min_off=float(self._duration_stop_sec),
            max_starts_per_hour=self._max_starts_per_hour,
            level_interval=float(self._duration_power_sec) if self._can_change_power else 0.0,
            shed_reaction=self._shed_reaction_sec,
            verify_after=self._verify_after_sec,
            echo_window=self._echo_window_sec,
            not_absorbing_kw=None if self._not_absorbing_w is None else self._not_absorbing_w / 1000,
        )

    def level_w(self) -> float | None:
        """Current setting of a modulable device (W), read from its power entity (also when the
        device is off). None for on/off devices or without a value."""
        if not self._can_change_power or not self._power_entity_id:
            return None
        state = self._hass.states.get(self._power_entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        return self._power_from_state(state)

    def measured_w(self) -> float | None:
        """Measured power of the device (W), if a measure entity is configured and available"""
        if not self._measured_power_entity_id:
            return None
        state = self._hass.states.get(self._measured_power_entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return None
        unit = state.attributes.get("unit_of_measurement")
        return value * 1000 if unit == "kW" else value

    @property
    def is_reachable(self) -> bool:
        """The device entity is available"""
        state = self._hass.states.get(self._entity_id)
        return state is not None and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)

    @property
    def is_available_for_plan(self) -> bool:
        """d_i: the usable template, the battery threshold and the maximal on time per day (the
        timers are the supervisor's job, not part of this)"""
        try:
            if self._on_time_sec >= self.max_on_time_per_day_sec:
                return False
            if self._check_usable_template.async_render(context={}) is not True:
                return False
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.warning("%s - usable template failed: %s", self.name, err)
            return False
        if self._battery_soc is not None and self.battery_soc_threshold is not None and self._battery_soc < self.battery_soc_threshold:
            return False
        return True

    def forced_level_w(self, offpeak_on: bool) -> float | None:
        """Level forced by the minimal on time per day (off-peak backup), None if not forced.
        The off-peak window is the off-peak entity of the central configuration when there is
        one (offpeak_on), otherwise offpeak_time .. raz_time. The supervisor still applies all
        its guards to it."""
        try:
            if self.min_on_time_per_day_sec <= 0:
                return None
            raz = self._coordinator.raz_time
            entity_window = getattr(self._coordinator, "runtime", None) is not None and bool(self._coordinator.runtime.config.get("offpeak_entity_id"))
            if entity_window and (self._offpeak_time is None or raz is None):
                in_window = offpeak_on
            elif self._offpeak_time is None or raz is None:
                return None
            elif self._offpeak_time >= raz:
                in_window = self.now.time() >= self._offpeak_time or self.now.time() < raz
            else:
                in_window = self._offpeak_time <= self.now.time() < raz
            if entity_window:
                # both: the off-peak period, from offpeak_time (e.g. the night, not the morning)
                in_window = in_window and offpeak_on
            if not in_window:
                return None
            if self._on_time_sec >= min(self.min_on_time_per_day_sec, self.max_on_time_per_day_sec, self.max_on_time_offpeak_sec):
                return None
            if not self._can_change_power:
                return float(self.power_max)
            return float(self.units_to_w(round(float(self.offpeak_power) / self._convert_power_divide_factor)))
        except (ConfigurationError, TypeError, ValueError) as err:
            _LOGGER.error("%s - invalid off-peak configuration: %s", self._name, err)
            return None

    def next_raz_datetime(self, raz_time: time) -> datetime:
        """Next occurrence of the daily reset time after now"""
        now = self.now
        candidate = now.replace(hour=raz_time.hour, minute=raz_time.minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    def set_manual(self, raz_time: time | None, entity_id: str | None = None) -> None:
        """The device was changed by hand: Solar Optimizer leaves it alone until the end time (at
        most manual_max_hours, and never beyond the next daily reset unless manual_end_at_reset is
        off). The minimal durations count
        from this change, as if Solar Optimizer had made it."""
        if self.is_manual and self._manual_end_when_off and self._manual_started_on:
            # Already in manual after switching it on by hand: being switched off (or a flicker)
            # does not restart the manual mode; check_manual_expiry ends it after 60 s off
            return
        end = self.now + timedelta(hours=self._manual_max_hours)
        if raz_time is not None and self._manual_end_at_reset:
            end = min(end, self.next_raz_datetime(raz_time))
        self._manual_end_time = end
        self._manual_started_on = bool(self.is_active)
        self._manual_off_since = None
        if entity_id is not None and entity_id == self._power_entity_id and entity_id != self._entity_id:
            self.reset_next_date_available_power()
        else:
            self.reset_next_date_available(ACTION_ACTIVATE if self.is_active else ACTION_DEACTIVATE)
        _LOGGER.info("%s - Manual change detected on %s: manual mode until %s", self.name, entity_id, end)
        self._hass.bus.async_fire(
            EVENT_TYPE_SOLAR_OPTIMIZER_MANUAL_CHANGE,
            {"device_unique_id": self._unique_id, "entity_id": entity_id, "manual_end_time": end.isoformat(), "is_manual": True},
        )
        self.publish_enable_state_change()

    def end_manual(self, reason: str = "") -> None:
        """Back to automatic. No command is sent: the algorithm decides in the next cycles."""
        if self._manual_end_time is None:
            return
        _LOGGER.info("%s - Manual mode ended (%s): back to automatic, no command sent", self.name, reason)
        self._manual_end_time = None
        self._hass.bus.async_fire(
            EVENT_TYPE_SOLAR_OPTIMIZER_MANUAL_CHANGE,
            {"device_unique_id": self._unique_id, "entity_id": None, "manual_end_time": None, "is_manual": False, "reason": reason},
        )
        self.publish_enable_state_change()

    def check_manual_expiry(self) -> bool:
        """End the manual mode when its time is over or the release template is true"""
        if self._manual_end_time is None:
            return False
        if self.now >= self._manual_end_time:
            self.end_manual("timeout")
            return True
        if self._manual_end_when_off and self._manual_started_on and self.is_reachable:
            if self.is_active:
                self._manual_off_since = None
            elif self._manual_off_since is None:
                self._manual_off_since = self.now
            elif (self.now - self._manual_off_since).total_seconds() >= 60:
                self.end_manual("switched off")
                return True
        if self._manual_release_template is not None:
            try:
                if self._manual_release_template.async_render(context={}) is True:
                    self.end_manual("release condition")
                    return True
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.warning("%s - manual release template failed: %s", self.name, err)
        return False

    def set_manual_end_time(self, end_time: datetime | None, started_on: bool = False) -> None:
        """Restore the manual end time (used at HA restart)"""
        self._manual_end_time = end_time
        self._manual_started_on = started_on

    @property
    def manual_started_on(self) -> bool:
        """The manual mode started with the device switched on by hand"""
        return self._manual_started_on

    @property
    def is_manual(self) -> bool:
        """True while the device is in manual mode (changed by hand)"""
        return self._manual_end_time is not None

    @property
    def manual_end_time(self) -> datetime | None:
        """End of the manual mode, or None"""
        return self._manual_end_time

    @property
    def manual_detection(self) -> bool:
        """True if manual changes are detected for this device"""
        return self._manual_detection

    def set_current_power_with_device_state(self):
        """Set the current power according to the real device state"""
        if not self.is_active:
            self._current_power = 0
            _LOGGER.debug(
                "Set current_power to 0 for device %s cause not active", self._name
            )
            return

        if not self._can_change_power:
            self._current_power = self.power_max
            _LOGGER.debug(
                "Set current_power to %s for device %s cause active and not can_change_power",
                self._current_power,
                self._name,
            )
            return

        power_entity_state = self._hass.states.get(self._power_entity_id)
        if not power_entity_state or power_entity_state.state in [None, STATE_UNKNOWN, STATE_UNAVAILABLE]:
            self._current_power = self._power_min
            _LOGGER.debug(
                "Set current_power to %s for device %s cause can_change_power but state is %s",
                self._current_power,
                self._name,
                power_entity_state,
            )
            return

        if self._power_entity_id.startswith(POWERED_ENTITY_DOMAINS_NEED_ATTR):
            # TODO : move this part to device initialisation, make new instance variable
            service_name = self._change_power_service # retrieve attribute from power service
            parties = self._change_power_service.split("/")
            if len(parties) < 2:
                raise ConfigurationError(
                    f"Incorrect service declaration for power entity. Service {service_name} should be formatted with: 'domain/action/attribute'"
                )
            parameter = parties[2]
            power_entity_value = power_entity_state.attributes[parameter]
        else:
            power_entity_value = power_entity_state.state

        number = _leading_number(power_entity_value)
        if number is None:
            self._current_power = self._power_min
            return
        self._current_power = round(self.units_to_w(number))
        _LOGGER.debug(
            "Set current_power to %s for device %s cause can_change_power and amps is %s",
            self._current_power,
            self._name,
            power_entity_value,
        )

    def set_enable(self, enable: bool):
        """Enable or disable the ManagedDevice for Solar Optimizer"""
        _LOGGER.info("%s - Set enable=%s", self.name, enable)
        self._enable = enable
        self.publish_enable_state_change()

    def set_forced_end_time(self, end_time: datetime | None) -> None:
        """Restore the forced_end_time (used at HA restart)"""
        self._forced_end_time = end_time

    def set_on_time(self, on_time_sec: int):
        """Set the time the underlying device was on per day"""
        _LOGGER.info("%s - Set on_time=%s", self.name, on_time_sec)
        self._on_time_sec = on_time_sec

    def set_requested_power(self, requested_power: int):
        """Set the requested power of the ManagedDevice"""
        self._requested_power = requested_power

    @property
    def is_enabled(self) -> bool:
        """return true if the managed device is enabled for solar optimisation"""
        return self._enable

    @property
    def forced_end_time(self) -> "datetime | None":
        """Returns the end time of the forced activation, or None if no forced activation"""
        return self._forced_end_time

    @property
    def is_active(self) -> bool:
        """Check if device is active by getting the underlying state of the device"""
        result = self._check_active_template.async_render(context={})
        if result:
            _LOGGER.debug("%s is active", self._name)

        return result

    def check_usable(self, check_battery=True) -> bool:
        """Check if the device is usable. The battery is checked optionally"""
        if self._on_time_sec >= self.max_on_time_per_day_sec:
            _LOGGER.debug(
                "%s is not usable due to max_on_time_per_day_min exceeded %d >= %d",
                self._name,
                self._on_time_sec,
                self.max_on_time_per_day_sec,
            )
            result = False
        else:
            context = {}
            now = self.now
            result = self._check_usable_template.async_render(context)
            if self._can_change_power:
                result = result and now >= self._next_date_available_power
            else:
                result = result and now >= self._next_date_available

            if not result:
                _LOGGER.debug("%s is not usable", self._name)

            if result and check_battery and self._battery_soc is not None and self.battery_soc_threshold is not None:
                if self._battery_soc < self.battery_soc_threshold:
                    result = False
                    _LOGGER.debug(
                        "%s is not usable due to battery soc threshold (%s < %s)",
                        self._name,
                        self._battery_soc,
                        self.battery_soc_threshold,
                    )

        return result

    @property
    def is_usable(self) -> bool:
        """A device is usable for optimisation if the check_usable_template returns true and
        if the device is not waiting for the end of its cycle and if the battery_soc_threshold is >= battery_soc
        and the _max_on_time_per_day_sec is not exceeded"""
        return self.check_usable(True)

    @property
    def should_be_forced_offpeak(self) -> bool:
        """True is we are offpeak and the max_on_time is not exceeded"""
        try:
            if not self.check_usable(False) or self._offpeak_time is None:
                return False
            max_offpeak_sec = self.max_on_time_offpeak_sec
        except (ConfigurationError, TypeError, ValueError) as err:
            _LOGGER.error("%s - invalid off-peak configuration: %s", self._name, err)
            return False

        if self._offpeak_time >= self._coordinator.raz_time:
            return (
                (self.now.time() >= self._offpeak_time or self.now.time() < self._coordinator.raz_time)
                and self._on_time_sec < self.max_on_time_per_day_sec
                and self._on_time_sec < max_offpeak_sec
                and self._on_time_sec < self.min_on_time_per_day_sec
            )
        else:
            return (
                self.now.time() >= self._offpeak_time
                and self.now.time() < self._coordinator.raz_time
                and self._on_time_sec < self.max_on_time_per_day_sec
                and self._on_time_sec < max_offpeak_sec
                and self._on_time_sec < self.min_on_time_per_day_sec
            )

    @property
    def is_waiting(self):
        """A device is waiting if the device is waiting for the end of its cycle"""
        result = self.now < self._next_date_available

        if result:
            _LOGGER.debug("%s is waiting", self._name)

        return result

    @property
    def name(self):
        """The name of the ManagedDevice"""
        return self._name

    @property
    def unique_id(self):
        """The id of the ManagedDevice"""
        return self._unique_id

    @property
    def power_max(self):
        """The power max of the managed device"""
        return get_template_or_value(self._hass, self._power_max)

    @property
    def offpeak_power(self):
        """The explicit power setpoint used during off-peak forcing."""
        if not self._can_change_power:
            if self._offpeak_power is not None:
                raise ConfigurationError(CONF_OFFPEAK_POWER)
            return None

        configured = self._offpeak_power is not None
        value = self.power_max if not configured else get_template_or_value(self._hass, self._offpeak_power)
        try:
            value = float(value)
            power_max = float(self.power_max)
        except (TypeError, ValueError) as err:
            raise ConfigurationError(CONF_OFFPEAK_POWER) from err
        if (
            not math.isfinite(value)
            or value <= 0
            or value < self._power_min
            or value > power_max
            or (
                configured
                and not math.isclose(
                    (value - self._power_min) % self._power_step,
                    0,
                    abs_tol=1e-9,
                )
            )
        ):
            raise ConfigurationError(CONF_OFFPEAK_POWER)
        return value

    @property
    def power_min(self):
        """The power min of the managed device"""
        return self._power_min

    @property
    def power_step(self):
        """The power step of the managed device"""
        return self._power_step

    @property
    def duration_sec(self) -> int:
        """The duration a device is not available after a change of the managed device"""
        return self._duration_sec

    @property
    def duration_stop_sec(self) -> int:
        """The duration a device is not available after a change of the managed device to stop"""
        return self._duration_stop_sec

    @property
    def duration_power_sec(self) -> int:
        """The duration a device is not available after a change of the managed device for power change"""
        return self._duration_power_sec

    @property
    def entity_id(self) -> str:
        """The entity_id of the device"""
        return self._entity_id

    @property
    def power_entity_id(self) -> str:
        """The entity_id of the device which gives the current power"""
        return self._power_entity_id

    @property
    def current_power(self) -> int:
        """The current_power of the device"""
        return self._current_power

    @property
    def requested_power(self) -> int:
        """The requested_power of the device"""
        return self._requested_power

    @property
    def can_change_power(self) -> bool:
        """true is the device can change its power"""
        return self._can_change_power

    @property
    def next_date_available(self) -> datetime:
        """returns the next available date for state change"""
        return self._next_date_available

    @property
    def next_date_available_power(self) -> datetime:
        """return the next available date for power change"""
        return self._next_date_available_power

    @property
    def convert_power_divide_factor(self) -> int:
        """return"""
        return self._convert_power_divide_factor

    @property
    def max_on_time_per_day_sec(self) -> int:
        """The max_on_time_per_day_sec configured"""
        return get_template_or_value(self._hass, self._max_on_time_per_day_min) * 60

    @property
    def max_on_time_offpeak_sec(self) -> int:
        """The maximum duration allowed during off-peak forcing."""
        value = self._max_on_time_per_day_min if self._max_on_time_offpeak_min is None else self._max_on_time_offpeak_min
        try:
            value = float(get_template_or_value(self._hass, value))
            max_daily = float(self.max_on_time_per_day_sec) / 60
            min_daily = float(self.min_on_time_per_day_sec) / 60
        except (TypeError, ValueError) as err:
            raise ConfigurationError(CONF_MAX_ON_TIME_OFFPEAK_MIN) from err
        if not math.isfinite(value) or value < 0 or value > max_daily or (min_daily > 0 and value < min_daily):
            raise ConfigurationError(CONF_MAX_ON_TIME_OFFPEAK_MIN)
        return value * 60

    @property
    def min_on_time_per_day_sec(self) -> int:
        """The min_on_time_per_day_sec configured"""
        return get_template_or_value(self._hass, self._min_on_time_per_day_min) * 60

    @property
    def offpeak_time(self) -> int:
        """The offpeak_time configured"""
        return self._offpeak_time

    @property
    def battery_soc(self) -> int:
        """The battery soc"""
        return self._battery_soc

    @property
    def battery_soc_threshold(self) -> int:
        """The battery soc"""
        return get_template_or_value(self._hass, self._battery_soc_threshold)

    def set_battery_soc(self, battery_soc):
        """Define the battery soc. This is used with is_usable
        to determine if the device is usable"""
        self._battery_soc = battery_soc

    def publish_enable_state_change(self) -> None:
        """Publish an event when the state is changed"""

        self._hass.bus.fire(
            event_type=EVENT_TYPE_SOLAR_OPTIMIZER_ENABLE_STATE_CHANGE,
            event_data={
                "device_unique_id": self._unique_id,
                "is_enabled": self.is_enabled,
                "is_active": self.is_active,
                "is_usable": self.is_usable,
                "is_waiting": self.is_waiting,
                "forced_end_time": self._forced_end_time.isoformat() if self._forced_end_time else None,
                "manual_end_time": self._manual_end_time.isoformat() if self._manual_end_time else None,
            },
        )

    def set_priority_entity(self, entity: SelectEntity):
        """Set the priority entity"""
        self._priority_entity = entity

    @property
    def priority(self) -> int:
        """Get the priority"""
        if self._priority_entity is None:
            return 0
        return self._priority_entity.current_priority

    # For testing purpose only
    def _set_now(self, now: datetime):
        """Set the now timestamp. This is only for tests purpose"""
        self._now = now

    @property
    def now(self) -> datetime:
        """Get now. The local datetime or the overloaded _set_now date"""
        return self._now if self._now is not None else datetime.now(self._current_tz)
