"""Home Assistant side of the charge plan by periods (``charge_plan_core``).

Keeps the periods the user schedules (a ``Store``), follows the SoC of the car and the sun, runs
the simulation every few minutes (and soon after a change) and publishes the result. While a
``fixed`` period is active (and the plan switch is on) the car is held at its power: the
controller no longer plans it, the supervisor drives it with its guards. While a ``sun`` period
is active the car is the first group: it gets all the surplus (the air conditioners only get what
it cannot absorb). Manual mode always wins.

The entities read are configurable in YAML (``solar_optimizer: charge_plan:``).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Callable

import voluptuous as vol
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .charge_plan_core import (
    MODES,
    Interval,
    Period,
    PlanInputs,
    PlanParams,
    PlanResult,
    SocTracker,
    SunTracker,
    active,
    expand,
    parse_forecast,
    simulate,
)

if TYPE_CHECKING:
    from .supervisor_runtime import SupervisorRuntime

_LOGGER = logging.getLogger(__name__)

DEFAULTS = {
    "confidence": "input_number.plan_coche_confianza",
    "base_power": "input_number.plan_coche_consumo_base",
    "share": "input_number.excedente_reparto_coche",
    "max_pct": "input_number.coche_objetivo_carga",
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

STORAGE_KEY = "solar_optimizer.charge_periods"
STORAGE_VERSION = 1
PERIOD = 300.0  # simulate every 5 min ...
DEBOUNCE = 5.0  # ... or at the first cycle 5 s after a change
FORECAST_REFRESH = 1800.0

SERVICE_SET_PERIOD = "set_charge_period"
SERVICE_REMOVE_PERIOD = "remove_charge_period"

_HHMM = vol.Match(r"^([01]?\d|2[0-3]):[0-5]\d(:[0-5]\d)?$")
SET_SCHEMA = vol.Schema(
    {
        vol.Optional("id"): cv.string,
        vol.Required("mode"): vol.In(MODES),
        vol.Optional("kw"): vol.All(vol.Coerce(float), vol.Range(min=0, max=50)),
        vol.Optional("pct"): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
        vol.Optional("enabled"): cv.boolean,
        vol.Optional("start"): vol.Coerce(float),
        vol.Optional("end"): vol.Coerce(float),
        vol.Optional("weekdays"): vol.All(cv.ensure_list, [vol.All(vol.Coerce(int), vol.Range(min=0, max=6))]),
        vol.Optional("start_time"): _HHMM,
        vol.Optional("end_time"): _HHMM,
    }
)
REMOVE_SCHEMA = vol.Schema({vol.Required("id"): cv.string})


def _f(hass: HomeAssistant, entity_id: str | None, default=None):
    state = hass.states.get(entity_id) if entity_id else None
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return default
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return default


def _minutes(hass: HomeAssistant, entity_id: str, default: int) -> int:
    state = hass.states.get(entity_id)
    if state is None or state.attributes.get("hour") is None:
        return default
    return int(state.attributes["hour"]) * 60 + int(state.attributes.get("minute") or 0)


def _hhmm_to_min(value: str) -> int:
    h, m = value.split(":")[:2]
    return int(h) * 60 + int(m)


def _clock(t: float, tz) -> str:
    return datetime.fromtimestamp(t, tz).strftime("%H:%M")


class ChargePlanRuntime:
    """The periods of the car of a SupervisorRuntime, their simulation and their execution"""

    def __init__(self, hass: HomeAssistant, runtime: "SupervisorRuntime", config: dict | None = None):
        self.hass = hass
        self.runtime = runtime
        self.cfg = {**DEFAULTS, **(config or {})}
        self.params = PlanParams()
        self.periods: list[Period] = []
        self.result = PlanResult()
        self.state = "Sin franjas"
        self.attrs: dict = {}
        self.switch_entity = None  # off: preview only
        self.soc_tracker = SocTracker()
        self.sun_tracker = SunTracker()
        self.soc_now: float | None = None
        self.sun_ratio: float | None = None
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._loaded = False
        self._listeners: list[Callable[[], None]] = []
        self._unsubs: list = []
        self._forecast: dict | None = None
        self._forecast_time: float | None = None
        self._forecast_try = 0.0
        self._last_run: float | None = None
        self._dirty_at: float | None = None
        self._intervals: list[Interval] = []
        self._active: Interval | None = None

    # ------------------------------------------------------------------ setup

    def start(self) -> None:
        self.stop()
        watched = [self.cfg[k] for k in ("charger_state", "soc", "max_pct", "share", "confidence", "base_power")]
        self._unsubs.append(async_track_state_change_event(self.hass, watched, self._on_change))
        if not self._loaded:
            self.hass.async_create_task(self.async_load())

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

    def car(self):
        return next((d for d in self.runtime.coordinator.devices if d.can_change_power), None)

    # ------------------------------------------------------------------ storage and services

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.periods = [p for p in (Period.from_dict(d) for d in data.get("periods") or []) if p is not None]
        k = data.get("kwh_per_pct")
        if isinstance(k, (int, float)) and self.params.kwh_per_pct_min <= k <= self.params.kwh_per_pct_max:
            self.soc_tracker.kwh_per_pct = float(k)
        self._loaded = True
        self.request_run()

    def _save(self) -> None:
        self._store.async_delay_save(
            lambda: {"periods": [p.to_dict() for p in self.periods], "kwh_per_pct": self.soc_tracker.kwh_per_pct}, 2.0
        )

    async def async_set_period(self, data: dict) -> Period:
        """Create or edit a period; raises ValueError when it is not valid"""
        now = self.runtime.coordinator.now
        old = next((p for p in self.periods if p.id == data.get("id")), None)
        raw = old.to_dict() if old else {"kw": 0.0, "pct": 0.0, "enabled": True}
        raw.update({k: v for k, v in data.items() if k not in ("start_time", "end_time")})
        raw["id"] = data.get("id") or uuid.uuid4().hex[:8]
        if "start_time" in data:
            raw["start_min"] = _hhmm_to_min(data["start_time"])
        if "end_time" in data:
            raw["end_min"] = _hhmm_to_min(data["end_time"])
        if data.get("start") is not None and "weekdays" not in data:
            raw["weekdays"] = []  # edited into a one-off period
        if raw.get("weekdays"):
            raw["start"] = raw["end"] = None  # weekly
        raw["updated"] = now.timestamp()
        period = Period.from_dict(raw)
        if period is None:
            raise ValueError("invalid charge period")
        self.periods = [p for p in self.periods if p.id != period.id] + [period]
        self._save()
        self.request_run()
        return period

    async def async_remove_period(self, period_id: str) -> None:
        self.periods = [p for p in self.periods if p.id != period_id]
        self._save()
        self.request_run()

    # ------------------------------------------------------------------ execution (every cycle)

    def _plugged(self) -> bool | None:
        charger = self.hass.states.get(self.cfg["charger_state"])
        if charger is None or charger.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        return charger.state in self.cfg["plugged_states"]

    def _controlled(self) -> bool:
        car = self.car()
        return bool(self.enabled and car is not None and car.is_enabled and not car.is_manual)

    def held_level(self, device_name: str) -> float | None:
        """Level asked for the car (kW) while a fixed period rules, else None (optimiser)"""
        car = self.car()
        iv = self._active
        if car is None or car.name != device_name or iv is None or iv.period.mode != "fixed":
            return None
        if not self._controlled() or self._plugged() is False:
            return None
        return iv.period.kw

    def sun_group(self) -> int | None:
        """Group of the car while a sun period rules (it gets all the surplus), else None"""
        car = self.car()
        iv = self._active
        if car is None or iv is None or iv.period.mode != "sun" or not self._controlled():
            return None
        return car.group

    async def async_step(self, now: datetime) -> None:
        """Called by every cycle of the optimiser"""
        if not self._loaded:
            return
        t = now.timestamp()
        self._track(now)
        self._intervals = expand(self.periods, t, now.tzinfo, self.params.horizon)
        was = self._active.period.id if self._active else None
        self._active = active(self._intervals, t)
        if (self._active.period.id if self._active else None) != was:
            self._dirty_at = t
        if self._last_run is None or t - self._last_run >= PERIOD or (self._dirty_at is not None and t >= self._dirty_at):
            await self.async_run(now)

    def _track(self, now: datetime) -> None:
        """SoC and sun trackers: they integrate at most 60 s per call, so they run every cycle"""
        hass, cfg, t = self.hass, self.cfg, now.timestamp()
        car = self.car()
        measured = car.measured_w() if car is not None else None
        soc_time = None
        state = hass.states.get(cfg["soc_time"])
        if state is not None and (parsed := dt_util.parse_datetime(state.state or "")) is not None:
            soc_time = parsed.timestamp()
        elif (soc_state := hass.states.get(cfg["soc"])) is not None:
            soc_time = soc_state.last_updated.timestamp()
        self.soc_now = self.soc_tracker.update(
            self.params, t, None if measured is None else measured / 1000, _f(hass, cfg["soc"]), soc_time, _f(hass, cfg["battery_kwh"], 68.8)
        )
        production = self.runtime.coordinator.power_production_entity_id
        pv = None if not production else (_f(hass, production) or 0.0) / 1000
        self.sun_ratio = self.sun_tracker.update(self.params, t, now.tzinfo, pv, self._forecast)

    def request_run(self) -> None:
        """Simulate at the next cycle of the optimiser"""
        self._dirty_at = 0.0
        self.hass.async_create_task(self.runtime.coordinator.async_request_refresh())

    @callback
    def _on_change(self, event: Event) -> None:
        if self._dirty_at is None:
            self._dirty_at = self.runtime.coordinator.now.timestamp() + DEBOUNCE

    # ------------------------------------------------------------------ simulation

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
            _LOGGER.warning("Charge plan: forecast not available (%s)", err)
            return
        self._forecast = parse_forecast(response)
        self._forecast_time = now

    def build_inputs(self, now: datetime) -> PlanInputs:
        hass, cfg, runtime = self.hass, self.cfg, self.runtime
        car = self.car()
        nowcast = None
        out = runtime.last_output
        if out is not None and out.surplus is not None:
            measured = car.measured_w() if car is not None else None
            held = self.held_level(car.name) is not None if car is not None else False
            nowcast = -out.surplus - ((measured or 0.0) / 1000 if held else 0.0)
        return PlanInputs(
            now=now.timestamp(),
            tz=now.tzinfo,
            periods=self.periods,
            soc=self.soc_now if self.soc_now is not None else _f(hass, cfg["soc"]),
            kwh_per_pct=self.soc_tracker.kwh_per_pct or 0.7,
            forecast=self._forecast,
            forecast_time=self._forecast_time,
            nowcast_kw=nowcast,
            ratio=self.sun_ratio,
            price_offpeak=_f(hass, cfg["price_offpeak"], 0.1349),
            price_peak=_f(hass, cfg["price_peak"], 0.26371),
            price_export=_f(hass, cfg["price_export"], 0.06),
            offpeak_window=(_minutes(hass, cfg["offpeak_start"], 22 * 60), _minutes(hass, cfg["offpeak_end"], 12 * 60)),
            base_kw=_f(hass, cfg["base_power"], 400.0) / 1000,
            limit_kw=min(runtime._limit_kw(), 1e3),  # pylint: disable=protected-access
            levels_kw=car.supervisor_params().levels if car is not None else (0.0,),
            confidence=_f(hass, cfg["confidence"], 80.0) / 100,
            share=min(1.0, max(0.0, _f(hass, cfg["share"], 100.0) / 100)),
            max_pct=_f(hass, cfg["max_pct"], 100.0) or 100.0,
        )

    async def async_run(self, now: datetime) -> None:
        t = now.timestamp()
        self._last_run, self._dirty_at = t, None
        self._expire(t)
        await self._refresh_forecast(t)
        inputs = self.build_inputs(now)
        self.result = simulate(inputs, self.params)
        self._publish(inputs, now)
        for cb in list(self._listeners):
            cb()

    def _expire(self, t: float) -> None:
        """One-off periods already over are forgotten"""
        keep = [p for p in self.periods if p.weekly or p.end > t]
        if len(keep) != len(self.periods):
            self.periods = keep
            self._save()

    def _publish(self, inputs: PlanInputs, now: datetime) -> None:
        res, car, iv, tz = self.result, self.car(), self._active, now.tzinfo
        warnings = list(res.warnings)
        plugged = self._plugged()
        if iv is not None and iv.period.mode in ("fixed", "sun") and plugged is False:
            warnings.append("Coche sin enchufar en una franja de carga")
        if not self.periods:
            self.state = "Sin franjas"
        elif car is not None and car.is_manual:
            self.state = "Pausa (manual)"
        elif iv is None:
            self.state = "Optimizador"
        elif iv.period.mode == "fixed":
            self.state = f"Fija {iv.period.kw:g} kW".replace(".", ",")
        elif iv.period.mode == "sun":
            self.state = "Sol"
        else:
            self.state = "Fuera"
        if self.periods and not self.enabled:
            self.state = "Vista previa"
        self.attrs = {
            "ejecutando": self.enabled,
            "periodos": [p.to_dict() for p in self.periods],
            "intervalos": [[r.id, int(r.t0), int(r.t1), r.mode, r.pct_start, r.pct_end, round(r.sun_kwh, 2), round(r.grid_kwh, 2), round(r.cost, 2)] for r in res.results],
            "curva": [[int(t), v] for t, v in res.curve],
            "horas": [list(h) for h in res.hours],
            "ahora": int(inputs.now),
            "soc_estimado": None if self.soc_now is None else round(self.soc_now, 1),
            "soc_final": res.pct_end,
            "soc_minimo": res.pct_min,
            "sol_kwh": round(res.sun_kwh, 1),
            "red_kwh": round(res.grid_kwh, 1),
            "coste_total_estimado": round(res.cost, 2),
            "niveles_kw": list(inputs.levels_kw),
            "kwh_por_pct": round(inputs.kwh_per_pct, 3),
            "avisos": warnings,
        }
        if self.sun_ratio is not None:
            self.attrs["sol_real_frente_a_previsto"] = round(self.sun_ratio, 2)


# ---------------------------------------------------------------------------- services


def async_register_services(hass: HomeAssistant, get_runtime: Callable[[], "ChargePlanRuntime | None"]) -> None:
    async def _set(call: ServiceCall) -> None:
        plan = get_runtime()
        if plan is None:
            raise ServiceValidationError("Solar Optimizer is not configured")
        try:
            await plan.async_set_period(dict(call.data))
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err

    async def _remove(call: ServiceCall) -> None:
        plan = get_runtime()
        if plan is not None:
            await plan.async_remove_period(call.data["id"])

    hass.services.async_register(DOMAIN, SERVICE_SET_PERIOD, _set, schema=SET_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_REMOVE_PERIOD, _remove, schema=REMOVE_SCHEMA)

