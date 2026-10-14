"""Home Assistant side of the charge planner (``charge_planner_core``).

Reads the targets (weekly: a switch and a % per day of the week at a common time; dates: a few
slots with date, time and %), the SoC of the car, the forecast (Forecast.Solar) and the tariff,
runs the planner every few minutes (and soon after a relevant change) and publishes the result.
While the planner holds the car, ``SupervisorRuntime.build_inputs`` passes its level to the
controller: the optimiser no longer counts the car, the supervisor commands it.

The entities read are configurable in YAML (``solar_optimizer: charge_planner:``); the defaults
are the helpers of the homelab package ``plan_carga.yaml``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Callable

from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from .charge_planner_core import ChargePlanner, PlannerInputs, PlannerOutput, Target, parse_forecast

if TYPE_CHECKING:
    from .supervisor_runtime import SupervisorRuntime

_LOGGER = logging.getLogger(__name__)

DAYS = ("lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo")
SHORT = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")

DEFAULTS = {
    "weekly_switch": "input_boolean.plan_carga_{day}",
    "weekly_pct": "input_number.plan_carga_{day}",
    "weekly_time": "input_datetime.plan_carga_hora",
    "date_count": 3,
    "date_switch": "input_boolean.plan_carga_evento_{n}",
    "date_time": "input_datetime.plan_carga_evento_{n}",
    "date_pct": "input_number.plan_carga_evento_{n}",
    "lead_hours": "input_number.plan_carga_antelacion",
    "confidence": "input_number.plan_carga_confianza",
    "base_power": "input_number.plan_carga_consumo_base",
    "battery_kwh": "input_number.coche_bateria_kwh",
    "soc": "sensor.deepal_battery",
    "soc_time": "sensor.deepal_vehicle_data_timestamp",
    "charger_state": "sensor.cargador_ev_estado",
    "plugged_states": ["plugged_in", "charging", "waiting", "paused", "charged"],
    "price_offpeak": "input_number.precio_luz_promocionado",
    "price_peak": "input_number.precio_luz_normal",
    "price_export": "input_number.precio_luz_excedente",
    "offpeak_start": "input_datetime.precio_luz_promocionado_inicio",
    "offpeak_end": "input_datetime.precio_luz_promocionado_fin",
}

PERIOD = 300.0  # run every 5 min (from the optimiser's cycle) ...
DEBOUNCE = 10.0  # ... or at the first cycle 10 s after a relevant change
FORECAST_REFRESH = 1800.0


def _f(hass: HomeAssistant, entity_id: str | None, default=None):
    state = hass.states.get(entity_id) if entity_id else None
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return default
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return default


def _on(hass: HomeAssistant, entity_id: str) -> bool:
    state = hass.states.get(entity_id)
    return state is not None and state.state == STATE_ON


def _minutes(hass: HomeAssistant, entity_id: str, default: int) -> int:
    state = hass.states.get(entity_id)
    if state is None or state.attributes.get("hour") is None:
        return default
    return int(state.attributes["hour"]) * 60 + int(state.attributes.get("minute") or 0)


class ChargePlannerRuntime:
    """Runs the planner for the car of a SupervisorRuntime"""

    def __init__(self, hass: HomeAssistant, runtime: "SupervisorRuntime", config: dict | None = None):
        self.hass = hass
        self.runtime = runtime
        self.cfg = {**DEFAULTS, **(config or {})}
        self.planner = ChargePlanner()
        self.output = PlannerOutput()
        self.switch_entity = None  # the planner switch (off: preview only)
        self._listeners: list[Callable[[], None]] = []
        self._unsubs: list = []
        self._forecast: dict | None = None
        self._forecast_time: float | None = None
        self._forecast_try: float = 0.0
        self._last_run: float | None = None
        self._dirty_at: float | None = None
        self._lock = asyncio.Lock()
        self._last_state: str | None = None

    # ------------------------------------------------------------------ setup

    @property
    def started(self) -> bool:
        return bool(self._unsubs)

    def start(self) -> None:
        self.stop()
        watched = [self.cfg["weekly_time"], self.cfg["charger_state"], self.cfg["soc"], self.cfg["lead_hours"]]
        watched += [self.cfg["weekly_switch"].format(day=d) for d in DAYS] + [self.cfg["weekly_pct"].format(day=d) for d in DAYS]
        for n in range(1, int(self.cfg["date_count"]) + 1):
            watched += [self.cfg[k].format(n=n) for k in ("date_switch", "date_time", "date_pct")]
        self._unsubs.append(async_track_state_change_event(self.hass, watched, self._on_change))

    def stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []

    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    @property
    def enabled(self) -> bool:
        entity = self.switch_entity
        return bool(entity is not None and entity.is_on)

    def held_level(self, device_name: str) -> float | None:
        """Level asked for the device (kW) while the planner holds it, else None"""
        car = self.car()
        if car is None or car.name != device_name or not self.output.held or self.output.level is None:
            return None
        return self.output.level

    def car(self):
        return next((d for d in self.runtime.coordinator.devices if d.can_change_power), None)

    # ------------------------------------------------------------------ triggers

    @callback
    def _on_change(self, event: Event) -> None:
        if self._dirty_at is None:
            self._dirty_at = self.runtime.coordinator.now.timestamp() + DEBOUNCE

    def request_run(self) -> None:
        """Run at the next cycle of the optimiser"""
        self._dirty_at = 0.0
        self.hass.async_create_task(self.runtime.coordinator.async_request_refresh())

    async def async_maybe_run(self, now: datetime) -> None:
        """Called by every cycle of the optimiser"""
        t = now.timestamp()
        due = self._last_run is None or t - self._last_run >= PERIOD or (self._dirty_at is not None and t >= self._dirty_at)
        if due:
            await self.async_run()

    # ------------------------------------------------------------------ inputs

    def targets(self, now: datetime) -> list[Target]:
        hass, cfg = self.hass, self.cfg
        out = []
        hh = _minutes(hass, cfg["weekly_time"], -1)
        if hh >= 0:
            today = now.date()
            for d in range(8):
                day = today + timedelta(days=d)
                name = DAYS[day.weekday()]
                pct = _f(hass, cfg["weekly_pct"].format(day=name))
                if not _on(hass, cfg["weekly_switch"].format(day=name)) or pct is None:
                    continue
                t = datetime(day.year, day.month, day.day, hh // 60, hh % 60, tzinfo=now.tzinfo)
                if t > now:
                    out.append(Target(t.timestamp(), pct, SHORT[day.weekday()]))
        for n in range(1, int(cfg["date_count"]) + 1):
            when = self._date_time(cfg["date_time"].format(n=n))
            pct = _f(hass, cfg["date_pct"].format(n=n))
            if _on(hass, cfg["date_switch"].format(n=n)) and when is not None and pct is not None and when > now:
                out.append(Target(when.timestamp(), pct, f"fecha {n}"))
        return out

    def _date_time(self, entity_id: str) -> datetime | None:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        value = dt_util.parse_datetime(state.state)
        if value is None:
            return None
        return value if value.tzinfo else value.replace(tzinfo=dt_util.get_default_time_zone())

    async def _expire_dates(self, now: datetime) -> None:
        """A date slot whose time is over switches itself off (free for the next one)"""
        cfg = self.cfg
        for n in range(1, int(cfg["date_count"]) + 1):
            switch = cfg["date_switch"].format(n=n)
            when = self._date_time(cfg["date_time"].format(n=n))
            if _on(self.hass, switch) and when is not None and when <= now:
                await self.hass.services.async_call("input_boolean", "turn_off", {"entity_id": switch})

    async def _refresh_forecast(self, now: float) -> None:
        if self._forecast_time is not None and now - self._forecast_time < FORECAST_REFRESH:
            return
        if now - self._forecast_try < 60:
            return
        self._forecast_try = now
        entries = self.hass.config_entries.async_entries("forecast_solar")
        if not entries:
            return
        try:
            response = await self.hass.services.async_call(
                "forecast_solar", "get_forecast", {"config_entry": entries[0].entry_id, "resolution": "hourly"}, blocking=True, return_response=True
            )
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.warning("Charge planner: forecast not available (%s)", err)
            return
        self._forecast = parse_forecast(response)
        self._forecast_time = now

    def build_inputs(self, now: datetime) -> PlannerInputs:
        hass, cfg, runtime = self.hass, self.cfg, self.runtime
        car = self.car()
        ts = now.timestamp()
        soc_time = None
        state = hass.states.get(cfg["soc_time"])
        if state is not None and (parsed := dt_util.parse_datetime(state.state or "")) is not None:
            soc_time = parsed.timestamp()
        elif (soc_state := hass.states.get(cfg["soc"])) is not None:
            soc_time = soc_state.last_updated.timestamp()
        charger = hass.states.get(cfg["charger_state"])
        plugged = None if charger is None or charger.state in (STATE_UNAVAILABLE, STATE_UNKNOWN) else charger.state in cfg["plugged_states"]
        measured = car.measured_w() if car is not None else None
        car_kw = None if measured is None else measured / 1000
        levels = car.supervisor_params().levels if car is not None else (0.0,)
        # Now: the house import without the car and without the optimiser's consumers
        nowcast = None
        out = runtime.last_output
        if out is not None and out.surplus is not None:
            nowcast = -out.surplus - ((car_kw or 0.0) if self.output.held else 0.0)
        lead = _f(hass, cfg["lead_hours"])
        return PlannerInputs(
            now=ts,
            tz=now.tzinfo,
            targets=self.targets(now),
            soc=_f(hass, cfg["soc"]),
            soc_time=soc_time,
            car_kw=car_kw,
            plugged=plugged,
            manual=bool(car is not None and car.is_manual),
            enabled=self.enabled and car is not None and car.is_enabled,
            forecast=self._forecast,
            forecast_time=self._forecast_time,
            pv_kw=(_f(hass, runtime.coordinator.power_production_entity_id) or 0.0) / 1000 if runtime.coordinator.power_production_entity_id else None,
            nowcast_kw=nowcast,
            price_offpeak=_f(hass, cfg["price_offpeak"], 0.1349),
            price_peak=_f(hass, cfg["price_peak"], 0.26371),
            price_export=_f(hass, cfg["price_export"], 0.06),
            offpeak_window=(_minutes(hass, cfg["offpeak_start"], 22 * 60), _minutes(hass, cfg["offpeak_end"], 12 * 60)),
            base_kw=_f(hass, cfg["base_power"], 400.0) / 1000,
            limit_kw=min(runtime._limit_kw(), 1e3),  # pylint: disable=protected-access
            levels_kw=levels,
            confidence=_f(hass, cfg["confidence"], 80.0) / 100,
            lead=None if lead is None else lead * 3600,
            battery_kwh=_f(hass, cfg["battery_kwh"], 68.8),
        )

    # ------------------------------------------------------------------ run

    async def async_run(self) -> None:
        if self._lock.locked():
            return
        async with self._lock:
            now = self.runtime.coordinator.now
            self._last_run, self._dirty_at = now.timestamp(), None
            await self._expire_dates(now)
            await self._refresh_forecast(now.timestamp())
            inputs = self.build_inputs(now)
            was_held = self.output.held
            try:
                self.output = await self.hass.async_add_executor_job(self.planner.plan, inputs)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Charge planner failed")
                return
        for cb in list(self._listeners):
            cb()
        await self._notify(inputs, was_held)
        if was_held != self.output.held:
            _LOGGER.info("Charge planner: car %s", "held" if self.output.held else "released")

    async def skip_next(self) -> None:
        now = self.runtime.coordinator.now
        target = self.planner.skip_next(self.targets(now), now.timestamp())
        if target is not None:
            _LOGGER.info("Charge planner: target %s skipped", target)
        self.request_run()

    async def _notify(self, inputs: PlannerInputs, was_held: bool) -> None:
        state, a = self.output.state, self.output.attrs
        changed = state != self._last_state
        self._last_state = state
        if not changed:
            return
        objetivo = (a.get("objetivos") or [{}])[0]
        if state == "Activo" and not was_held:
            await self.runtime.notify(
                f"Plan de carga: {objetivo.get('pct', 0):g} % {objetivo.get('cuando', '')}, ~{a.get('coste_total_estimado', 0):.2f} € "
                f"(sol {a.get('sol_previsto_kwh', 0):.0f} kWh, red {a.get('red_prevista_kwh', 0):.0f} kWh)"
            )
        elif state.startswith("No llega") and inputs.enabled:
            await self.runtime.notify(f"Plan de carga: {state} para {objetivo.get('cuando', '')}")
        elif "Coche desenchufado" in (a.get("avisos") or []) and inputs.enabled:
            await self.runtime.notify(f"Plan de carga: coche desenchufado con un objetivo {objetivo.get('cuando', '')}")

    # ------------------------------------------------------------------ persistence

    def export_state(self) -> dict:
        return self.planner.export_state()

    def restore_state(self, data: dict | None) -> None:
        self.planner.restore_state(data)
