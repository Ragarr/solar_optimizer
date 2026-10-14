"""Home Assistant side of the enumeration optimiser + supervisor.

Builds the inputs of ``controller_core`` from the states, sends the commands through the managed
devices (each one with its own Context, so its echoes are recognised), publishes the plan, sends
the notifications and keeps the state that survives a restart. Every command goes through
``_execute``: in dry run nothing is sent to the devices.
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event

from .const import (
    CONF_CONTRACTED_POWER_ENTITY_ID,
    CONF_CONTRACTED_POWER_MARGIN,
    CONF_DISCARD_AFTER_COMMAND_SEC,
    CONF_FOREIGN_USER_IDS,
    CONF_GROUP_COUNT,
    CONF_GROUP_IMPORT_CAPS,
    CONF_NOTIFY_SERVICE,
    CONF_OFFPEAK_ENTITY_ID,
    CONF_SMOOTHING_WINDOW_SEC,
    CONF_START_SEPARATION_SEC,
    CONF_WARMUP_SEC,
    DEFAULT_CONTRACTED_POWER_MARGIN,
    DEFAULT_DISCARD_AFTER_COMMAND_SEC,
    DEFAULT_GROUP_COUNT,
    DEFAULT_SMOOTHING_WINDOW_SEC,
    DEFAULT_START_SEPARATION_SEC,
    DEFAULT_WARMUP_SEC,
    EVENT_MOBILE_APP_NOTIFICATION_ACTION,
    EVENT_TYPE_SOLAR_OPTIMIZER_MANUAL_CHANGE,
    NOTIFICATION_ACTION_END_MANUAL,
)
from .controller_core import Controller, ControllerParams, DeviceConfig, DeviceInput, Inputs, Output
from .supervisor_core import LEVEL, OFF, ON, GlobalParams

if TYPE_CHECKING:
    from .managed_device import ManagedDevice

_LOGGER = logging.getLogger(__name__)

NOTIFY_REPEAT_SEC = 1800


def _float_state(hass: HomeAssistant, entity_id: str | None) -> float | None:
    if not entity_id or (state := hass.states.get(entity_id)) is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _kw(hass: HomeAssistant, entity_id: str | None) -> float | None:
    """Power state in kW (W, kW or MW sensors)"""
    value = _float_state(hass, entity_id)
    if value is None:
        return None
    unit = (hass.states.get(entity_id).attributes.get("unit_of_measurement") or "W").strip()
    return value * {"W": 0.001, "kW": 1.0, "MW": 1000.0}.get(unit, 0.001)


def parse_caps(text: str | None) -> dict[int, float]:
    """ "2:1.5, 3:0" -> {2: 1.5, 3: 0.0} (kW)"""
    caps = {}
    for part in (text or "").replace(";", ",").split(","):
        if ":" in part:
            group, value = part.split(":", 1)
            try:
                caps[int(group.strip())] = float(value.strip())
            except ValueError:
                _LOGGER.warning("Invalid group import cap %r", part)
    return caps


class SupervisorRuntime:
    """Runs the controller for a coordinator"""

    def __init__(self, hass: HomeAssistant, coordinator):
        self.hass = hass
        self.coordinator = coordinator
        self.config: dict = {}
        self.controller: Controller | None = None
        self._signature = None
        self._restore_devices: dict[str, dict] = {}
        self._restore_global: dict | None = None
        self._lock = asyncio.Lock()
        self._notified: dict[str, float] = {}
        self._unsubs: list = []
        self.dry_run_entity = None
        self.alpha_entities: dict[int, object] = {}
        self.last_output: Output | None = None
        self.last_inputs: Inputs | None = None
        self.foreign_user_ids: set[str] = set()

    # ------------------------------------------------------------------ setup

    def configure(self, data: dict) -> None:
        self.config = dict(data)
        self.foreign_user_ids = {u.strip() for u in (data.get(CONF_FOREIGN_USER_IDS) or "").split(",") if u.strip()}
        self._signature = None  # rebuild the controller with the new parameters
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []
        if (entity := self.coordinator.power_consumption_entity_id) is not None:
            self._unsubs.append(async_track_state_change_event(self.hass, [entity], self._on_grid_change))
        self._unsubs.append(self.hass.bus.async_listen(EVENT_TYPE_SOLAR_OPTIMIZER_MANUAL_CHANGE, self._on_manual_change))
        self._unsubs.append(self.hass.bus.async_listen(EVENT_MOBILE_APP_NOTIFICATION_ACTION, self._on_notification_action))

    def unload(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []

    @property
    def group_count(self) -> int:
        return int(self.config.get(CONF_GROUP_COUNT) or DEFAULT_GROUP_COUNT)

    @property
    def dry_run(self) -> bool:
        """Dry run (shadow mode) unless its switch says otherwise: never send a command by
        mistake while the switch is not loaded yet"""
        entity = self.dry_run_entity
        return True if entity is None else bool(entity.is_on)

    def restore_device(self, name: str, data: dict | None) -> None:
        """State persisted by the device switch, applied when the controller is built"""
        if data:
            self._restore_devices[name] = data

    def restore_global(self, data: dict | None) -> None:
        if data:
            self._restore_global = data

    def _get_controller(self, now: float) -> Controller:
        devices: list[ManagedDevice] = self.coordinator.devices
        configs = [DeviceConfig(d.name, d.group, d.supervisor_params(), d.default_value, None, d.can_change_power) for d in devices]
        signature = tuple((c.name, c.group, c.params, c.is_car) for c in configs) + (tuple(sorted(self.config.items(), key=str)),)
        if self.controller is not None and signature == self._signature:
            return self.controller
        previous = self.controller.export_state() if self.controller is not None else None
        gparams = GlobalParams(
            start_separation=float(self.config.get(CONF_START_SEPARATION_SEC) or DEFAULT_START_SEPARATION_SEC),
            warmup=float(self.config.get(CONF_WARMUP_SEC, DEFAULT_WARMUP_SEC) or 0),
            period=float(self.coordinator.update_interval.total_seconds()) if self.coordinator.update_interval else 30.0,
        )
        window = self.config.get(CONF_SMOOTHING_WINDOW_SEC)
        cparams = ControllerParams(
            smoothing_window=DEFAULT_SMOOTHING_WINDOW_SEC if window is None else float(window),
            discard_after_command=float(self.config.get(CONF_DISCARD_AFTER_COMMAND_SEC, DEFAULT_DISCARD_AFTER_COMMAND_SEC) or 0),
        )
        controller = Controller(configs, gparams, cparams)
        real_on = {d.name: bool(d.is_active) for d in devices if d.is_reachable}
        if previous is not None:
            controller.restore_state(previous, now, real_on)
            controller.g = self.controller.g
        else:
            saved = {"devices": dict(self._restore_devices), "global": self._restore_global or {}}
            controller.restore_state(saved, now, real_on)
        self._restore_devices.clear()
        self.controller, self._signature = controller, signature
        return controller

    # ------------------------------------------------------------------ inputs

    def _limit_kw(self) -> float:
        entity = self.config.get(CONF_CONTRACTED_POWER_ENTITY_ID)
        value = _float_state(self.hass, entity)
        if value is None:
            return 1e9
        unit = (self.hass.states.get(entity).attributes.get("unit_of_measurement") or "").strip()
        kw = value / 1000 if unit == "W" or (not unit and value > 100) else value
        margin = float(self.config.get(CONF_CONTRACTED_POWER_MARGIN, DEFAULT_CONTRACTED_POWER_MARGIN) or 0)
        return kw * (1 - margin / 100)

    def offpeak(self) -> bool:
        entity = self.config.get(CONF_OFFPEAK_ENTITY_ID)
        state = self.hass.states.get(entity) if entity else None
        return state is not None and state.state == STATE_ON

    def alphas(self) -> dict[int, float]:
        out = {}
        for group in range(1, self.group_count + 1):
            entity = self.alpha_entities.get(group)
            value = getattr(entity, "native_value", None) if entity is not None else None
            out[group] = float(value) if value is not None else (1.0 if group == 1 else 0.0)
        return out

    def day_key(self, now: datetime) -> str:
        raz = self.coordinator.raz_time
        shifted = now - timedelta(hours=raz.hour, minutes=raz.minute) if raz else now
        return shifted.date().isoformat()

    def build_inputs(self, now: datetime) -> Inputs:
        hass = self.hass
        c = self.coordinator
        sell = _float_state(hass, c.sell_cost_entity_id)
        tax = _float_state(hass, c.sell_tax_percent_entity_id) or 0.0
        offpeak = self.offpeak()
        devices = {}
        for d in c.devices:
            level = d.level_w()
            measured = d.measured_w()
            force = d.forced_level_w(offpeak) if hasattr(d, "forced_level_w") else None
            devices[d.name] = DeviceInput(
                included=d.is_enabled,
                manual=d.is_manual,
                available=d.is_available_for_plan,
                reachable=d.is_reachable,
                on=bool(d.is_active),
                level=None if level is None else level / 1000,
                measured=None if measured is None else measured / 1000,
                force_level=None if force is None else force / 1000,
            )
        return Inputs(
            now=now.timestamp(),
            net=_kw(hass, c.power_consumption_entity_id),
            production=_kw(hass, c.power_production_entity_id),
            price_import=_float_state(hass, c.buy_cost_entity_id),
            price_export=None if sell is None else sell * (1 - tax / 100),
            offpeak=offpeak,
            limit=self._limit_kw(),
            alphas=self.alphas(),
            caps=parse_caps(self.config.get(CONF_GROUP_IMPORT_CAPS)),
            devices=devices,
            day=self.day_key(now),
        )

    # ------------------------------------------------------------------ cycle

    async def async_step(self, now: datetime) -> dict:
        """One control cycle; returns the data published by the sensors"""
        async with self._lock:
            inputs = self.build_inputs(now)
            controller = self._get_controller(inputs.now)
            out = controller.step(inputs, self.dry_run)
            self.last_output, self.last_inputs = out, inputs
            await self._execute(out)
        await self._notify_all(out)
        return self.published(out, inputs)

    @callback
    def _on_grid_change(self, event: Event) -> None:
        """Fast path: load shedding on every new grid measure"""
        if self.controller is None:
            return
        self.hass.async_create_task(self._async_fast())

    async def _async_fast(self) -> None:
        if self._lock.locked():
            return
        async with self._lock:
            inputs = self.build_inputs(self.coordinator.now)
            out = self.controller.fast(inputs, self.dry_run)
            await self._execute(out)
        await self._notify_all(out)
        if out.commands or out.violations:
            self.coordinator.async_update_listeners()

    async def _execute(self, out: Output) -> None:
        """Send the commands (the only place where the devices are commanded)"""
        if self.dry_run:
            return
        for cmd in out.commands:
            device = self.coordinator.get_device_by_name(cmd.device)
            if device is None:
                continue
            _LOGGER.info("%s - %s %s (%s)", device.name, cmd.kind, cmd.value, cmd.reason)
            try:
                if cmd.kind == ON:
                    await device.activate(None if cmd.value is None else round(cmd.value * 1000))
                elif cmd.kind == OFF:
                    await device.deactivate()
                elif cmd.kind == LEVEL:
                    await device.change_requested_power(round(cmd.value * 1000))
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.error("%s - command %s failed: %s", device.name, cmd.kind, err)

    # ------------------------------------------------------------------ publication

    def published(self, out: Output, inputs: Inputs) -> dict:
        plan = out.plan
        controller = self.controller
        devices = {}
        for d in self.coordinator.devices:
            st = controller.states.get(d.name) if controller else None
            status = out.status.get(d.name)
            if not d.is_enabled:
                mode = "excluded"
            elif not d.is_reachable:
                mode = "offline"
            elif d.is_manual:
                mode = "manual"
            elif st is not None and st.faulty:
                mode = "faulty"
            else:
                mode = "auto"
            measured = d.measured_w()
            on = bool(st.on) if st is not None else bool(d.is_active)
            power_w = measured if measured is not None else (st.level * 1000 if st is not None and on else 0.0)
            start_at = status.start_at if status else None
            stop_at = status.stop_at if status else None
            devices[d.name] = {
                "mode": mode,
                "is_on": on,
                "power_w": round(power_w),
                "waiting": bool((not on and start_at is not None and start_at > inputs.now) or (on and stop_at is not None and stop_at > inputs.now)),
                "manual_end_time": d.manual_end_time.isoformat() if d.manual_end_time else None,
                "group": d.group,
                "value": d.value(inputs.offpeak),
                "level_w": round((st.level if st and st.on else 0.0) * 1000),
                "planned_w": round((status.planned if status else 0.0) * 1000),
                "allowed_w": [round(x * 1000) for x in (status.levels if status else ())],
                "start_possible_at": _iso(status.start_at if status else None, d),
                "stop_possible_at": _iso(status.stop_at if status else None, d),
                "not_absorbing_until": _iso(st.not_absorbing_until if st else None, d),
            }
        commands = [f"{c.device}: {c.kind}{'' if c.value is None or c.kind == OFF else f' {round(c.value * 1000)} W'} ({c.reason})" for c in out.commands]
        return {
            "plan": _plan_text(plan, self.coordinator.devices),
            "plan_levels_w": {k: round(v * 1000) for k, v in (plan.levels.items() if plan else [])},
            "surplus": None if out.surplus is None else round(out.surplus * 1000),
            "surplus_instant": None if out.surplus_inst is None else round(out.surplus_inst * 1000),
            "import_margin": None if out.margin is None else round(out.margin * 1000),
            "planned_import": round(plan.imported * 1000) if plan else None,
            "planned_export": round(plan.exported * 1000) if plan else None,
            "objective": round(plan.objective, 4) if plan else None,
            "violations": controller.g.violations if controller else 0,
            "last_violations": [f"{v.code} {v.device}: {v.message}" for v in out.violations],
            "mode": out.mode,
            "dry_run": self.dry_run,
            "commands": commands,
            "offpeak": inputs.offpeak,
            "price_import": inputs.price_import,
            "alphas": inputs.alphas,
            "devices": devices,
        }

    def device_state(self, name: str) -> dict | None:
        """Supervisor state of a device, persisted in its switch attributes"""
        if self.controller is None or name not in self.controller.states:
            return None
        return self.controller.states[name].to_dict()

    def global_state(self) -> dict | None:
        return None if self.controller is None else self.controller.g.to_dict()

    # ------------------------------------------------------------------ notifications

    async def _notify_all(self, out: Output) -> None:
        now = self.coordinator.now.timestamp()
        for key, text in out.notifications:
            last = self._notified.get(key)
            if last is not None and now - last < NOTIFY_REPEAT_SEC:
                continue
            self._notified[key] = now
            _LOGGER.warning("Solar Optimizer: %s", text)
            await self.notify(text)

    async def notify(self, message: str, actions: list[dict] | None = None) -> None:
        service = (self.config.get(CONF_NOTIFY_SERVICE) or "").strip()
        if not service:
            return
        domain, _, name = service.partition(".")
        if not name:
            domain, name = "notify", domain
        data = {"title": "Solar Optimizer", "message": message}
        if actions:
            data["data"] = {"actions": actions}
        try:
            await self.hass.services.async_call(domain, name, data)
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.warning("Notification with %s failed: %s", service, err)

    async def _on_manual_change(self, event: Event) -> None:
        if not event.data.get("is_manual"):
            return
        device = self.coordinator.get_device_by_unique_id(event.data.get("device_unique_id"))
        if device is None or device.manual_end_time is None:
            return
        end = device.manual_end_time.strftime("%H:%M")
        await self.notify(
            f"{device.name} en manual hasta las {end}",
            [{"action": NOTIFICATION_ACTION_END_MANUAL + device.unique_id, "title": "Volver a automático"}],
        )

    async def _on_notification_action(self, event: Event) -> None:
        action = event.data.get("action") or ""
        if not action.startswith(NOTIFICATION_ACTION_END_MANUAL):
            return
        device = self.coordinator.get_device_by_unique_id(action[len(NOTIFICATION_ACTION_END_MANUAL) :])
        if device is not None:
            self.back_to_auto(device, "notification")

    def back_to_auto(self, device, reason: str) -> None:
        """End of the manual mode, of the faulty mark and of the "not absorbing" pause. No
        command is sent."""
        device.end_manual(reason)
        if self.controller is not None and device.name in self.controller.states:
            st = self.controller.states[device.name]
            st.faulty = False
            self.controller.sup.reset_not_absorbing(st)
            st.obj_on, st.since = None, None


def _iso(ts, device) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=device.now.tzinfo).isoformat(timespec="seconds")


def _plan_text(plan, devices) -> str:
    if plan is None:
        return "—"
    on = [f"{d.name} {round(plan.levels[d.name] * 1000)} W" for d in devices if plan.levels.get(d.name, 0) > 0]
    return ", ".join(on) if on else "nada"
