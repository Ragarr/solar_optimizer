"""One control cycle: measures -> filtered surplus -> admissible levels -> optimiser -> commands.

Pure Python, shared by the Home Assistant coordinator, the tests and the offline simulator.
Powers in kW, prices in €/kWh, times in epoch seconds.

Surplus: E = P - R with R = X - sum(y_i) (house consumption without the controllable consumers,
y_i their measured power or their nominal level). With the net consumption N = X - P (grid
import, negative when exporting): E = sum(y_i) - N. Consumers that are excluded, in manual mode
or faulty are not controllable: their consumption stays in R. Neither is a consumer held by the
charge planner (the car while it plans "X % at time t"): its consumption stays in R too, but the
supervisor still commands it (the planner's level), verifies it and sheds it.

Ê is the time-weighted mean of E over the smoothing window (2 min); samples taken right after a
command are discarded (the measures are still moving). E+ = max(0, Ê). The import margin H is the
contracted limit minus the import the house would have without the controllable consumers (the
worst of Ê and the instantaneous E).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .optimizer_core import Consumer, Group, Plan, water_fill
from .smoothing import TimeWindowAverage
from .supervisor_core import EPS, LEVEL, OFF, ON, Command, DeviceParams, DeviceState, GlobalParams, GlobalState, Supervisor, Violation


@dataclass(frozen=True)
class DeviceConfig:
    """A controllable consumer"""

    name: str
    group: int
    params: DeviceParams
    value_peak: float
    value_offpeak: float | None = None  # None: same as value_peak
    is_car: bool = False  # first to be shed (level then stop)


@dataclass
class DeviceInput:
    """Measures and flags of a consumer for one cycle"""

    included: bool = True  # "Included in the plan" (Enable)
    manual: bool = False
    available: bool = True  # d_i: may be used now (plugged, zone asking, ...)
    reachable: bool = True  # the device entity is available
    on: bool = False
    level: float | None = None  # current setting (kW); None for on/off devices
    measured: float | None = None  # measured power (kW) if any
    force_level: float | None = None  # forced level (off-peak minimum, start_device)
    held_level: float | None = None  # held by the charge planner: the level it asks (kW)
    self_acting: bool = False  # the device acts on its own now (e.g. a charger starting when plugged)


@dataclass
class Inputs:
    now: float
    net: float | None  # grid import (kW), negative when exporting
    production: float | None  # PV (kW)
    price_import: float | None
    price_export: float | None
    offpeak: bool
    limit: float  # contracted power minus the margin (kW)
    alphas: dict[int, float]
    caps: dict[int, float | None] = field(default_factory=dict)
    devices: dict[str, DeviceInput] = field(default_factory=dict)
    day: str | None = None  # changes at the daily reset: clears the faulty flags


@dataclass
class DeviceStatus:
    levels: tuple[float, ...] = ()
    planned: float = 0.0
    controllable: bool = False
    start_at: float | None = None
    stop_at: float | None = None
    held: bool = False


@dataclass
class Output:
    commands: list[Command] = field(default_factory=list)
    blocked: list[Command] = field(default_factory=list)
    plan: Plan | None = None
    surplus_inst: float | None = None
    surplus: float | None = None  # Ê
    margin: float | None = None  # H
    status: dict[str, DeviceStatus] = field(default_factory=dict)
    notifications: list[tuple[str, str]] = field(default_factory=list)  # (key, text)
    violations: list[Violation] = field(default_factory=list)
    mode: str = "ok"  # ok | warmup | hold | safe


@dataclass(frozen=True)
class ControllerParams:
    smoothing_window: float = 120.0
    discard_after_command: float = 25.0
    frozen_production_kw: float = 0.05  # "with sun": production above this
    hysteresis: float = 0.0  # €/h: keep the current plan unless the best one is worth more than this


class Controller:
    """The control loop, with its persisted state"""

    def __init__(self, devices: list[DeviceConfig], gparams: GlobalParams | None = None, cparams: ControllerParams | None = None):
        self.devices = {d.name: d for d in devices}
        self.cp = cparams or ControllerParams()
        self.sup = Supervisor(gparams)
        self.states: dict[str, DeviceState] = {d.name: DeviceState() for d in devices}
        self.g = GlobalState()
        self.filter = TimeWindowAverage(self.cp.smoothing_window)
        self._last_values: dict[str, tuple[float, float]] = {}
        self._ctrl_set: tuple[str, ...] | None = None
        self._day: str | None = None

    # ------------------------------------------------------------------ persistence

    def export_state(self) -> dict:
        return {"devices": {k: v.to_dict() for k, v in self.states.items()}, "global": self.g.to_dict()}

    def restore_state(self, data: dict, now: float, real_on: dict[str, bool] | None = None) -> None:
        """Restore after a restart. A consumer whose real state differs from the saved one changed
        while Home Assistant was down: it counts as just switched."""
        for name, saved in (data or {}).get("devices", {}).items():
            if name not in self.states:
                continue
            st = DeviceState.from_dict(saved)
            st.pending = None
            st.obj_on, st.since = None, None
            if real_on is not None and name in real_on and real_on[name] != st.on:
                st.on = real_on[name]
                if st.on:
                    st.t_on = now
                    st.starts.append(now)
                else:
                    st.t_off = now
            self.states[name] = st
        g = GlobalState.from_dict((data or {}).get("global"))
        self.g.last_start = g.last_start
        self.g.violations = g.violations

    # ------------------------------------------------------------------ helpers

    def _managed(self, name: str, di: DeviceInput) -> bool:
        """Commanded by Solar Optimizer (the optimiser or the charge planner)"""
        st = self.states[name]
        return di.included and not di.manual and di.reachable and not st.faulty

    def _controllable(self, name: str, di: DeviceInput) -> bool:
        """Part of the optimiser's problem"""
        return self._managed(name, di) and di.held_level is None

    def _held(self, name: str, di: DeviceInput) -> bool:
        return self._managed(name, di) and di.held_level is not None

    def _y(self, name: str, di: DeviceInput) -> float:
        st = self.states[name]
        if di.measured is not None:
            return max(0.0, di.measured)
        return st.level if st.on else 0.0

    def surplus_now(self, inp: Inputs) -> float | None:
        if inp.net is None:
            return None
        y = sum(self._y(n, di) for n, di in inp.devices.items() if n in self.states and self._controllable(n, di))
        return y - inp.net

    def _frozen(self, key: str, value: float | None, now: float, sunny: bool) -> bool:
        if value is None:
            self._last_values.pop(key, None)
            return False
        prev = self._last_values.get(key)
        if prev is None or abs(prev[0] - value) > 1e-9:
            self._last_values[key] = (value, now)
            return False
        return sunny and now - prev[1] >= self.sup.p.sensor_hold_after

    def _shed_list(self, inp: Inputs):
        out = []
        for name, cfg in self.devices.items():
            di = inp.devices.get(name)
            if di is None or not self._managed(name, di):
                continue
            value = cfg.value_peak if not inp.offpeak or cfg.value_offpeak is None else cfg.value_offpeak
            out.append((name, self.states[name], cfg.params, value))
        return out

    def _car(self):
        return next((n for n, c in self.devices.items() if c.is_car), None)

    # ------------------------------------------------------------------ observation

    def observe(self, inp: Inputs, out: Output) -> None:
        for name, cfg in self.devices.items():
            di = inp.devices.get(name)
            if di is None or not di.reachable:
                continue
            st = self.states[name]
            on = di.on
            pend = st.pending
            if pend is not None and pend["kind"] in (ON, OFF) and inp.now - pend["t"] < cfg.params.echo_window and on != (pend["kind"] == ON):
                # Within the echo window only a change towards the commanded value counts: going
                # back to the previous value is a flicker of the device (cloud devices do it)
                on = st.on
            started_by_device = on and not st.on and di.self_acting and (pend is None or pend["kind"] != ON)
            self.sup.observe(st, cfg.params, on, di.level, inp.now)
            if started_by_device:
                # Not our start: no minimum on-time to wait for (the car just plugged in at night
                # must be stoppable at once), and it does not count as one of our starts
                st.t_on = None
                if st.starts:
                    st.starts.pop()
            msg = self.sup.observe_measured(st, cfg.params, di.measured, inp.now)
            if msg and self._managed(name, di):
                out.notifications.append((f"{name}:not_absorbing", f"{name}: {msg}; se reintentará más tarde"))
            if st.not_absorbing_until is not None and inp.now >= st.not_absorbing_until:
                self.sup.reset_not_absorbing(st)

    def add_sample(self, inp: Inputs) -> float | None:
        """Add a surplus sample to the filter (also called by the fast path on every new
        measure). Samples right after a command are discarded."""
        e = self.surplus_now(inp)
        if e is None:
            return None
        # Ê counts the controllable consumers' own power: when that set changes (a device goes
        # manual, or the charge planner takes the car) the old samples no longer describe it
        ctrl_set = tuple(sorted(n for n, di in inp.devices.items() if n in self.states and self._controllable(n, di)))
        if self._ctrl_set is not None and ctrl_set != self._ctrl_set:
            self.filter.reset()
        self._ctrl_set = ctrl_set
        if self.g.last_command is None or inp.now - self.g.last_command >= self.cp.discard_after_command:
            self.filter.add(inp.now, e)
        return e

    # ------------------------------------------------------------------ fast path

    def fast(self, inp: Inputs, dry_run: bool = False) -> Output:
        """Load shedding, on every new grid measure (independent of the cycle)"""
        out = Output()
        self.observe(inp, out)
        self.add_sample(inp)
        if inp.net is None:
            return out
        self._shed(inp, out, dry_run)
        return out

    def _shed(self, inp: Inputs, out: Output, dry_run: bool) -> None:
        imported = max(0.0, inp.net)
        devices = self._shed_list(inp)
        if not dry_run:
            v = self.sup.check_overload(devices, self.g, imported, inp.limit, inp.now)
            if v is not None:
                self._violation(v, out)
        cmd, msg = self.sup.shed(devices, self.g, imported, inp.limit, inp.now, self._car())
        if msg:
            out.notifications.append(("shed", msg))
        if cmd is not None:
            self._emit(cmd, inp, out, dry_run)

    # ------------------------------------------------------------------ cycle

    def step(self, inp: Inputs, dry_run: bool = False) -> Output:
        out = Output()
        now = inp.now
        if self.g.started_at is None:
            self.g.started_at = now
        if inp.day is not None and inp.day != self._day:
            if self._day is not None:
                for st in self.states.values():
                    st.faulty = False
            self._day = inp.day

        self.observe(inp, out)
        e_inst = self.add_sample(inp)
        out.surplus_inst = e_inst

        # Sensors: unavailable or frozen -> hold; for long -> safe state
        sunny = inp.production is not None and inp.production > self.cp.frozen_production_kw
        frozen = self._frozen("net", inp.net, now, sunny) | self._frozen("production", inp.production, now, sunny)
        bad = inp.net is None or inp.production is None or frozen or inp.price_import is None or inp.price_export is None
        if bad:
            if self.g.sensors_bad_since is None:
                self.g.sensors_bad_since = now
        else:
            self.g.sensors_bad_since = None
        if inp.net is not None:
            self._shed(inp, out, dry_run)

        # Verification of the commands sent (retries, faulty devices)
        for name, cfg in self.devices.items():
            di = inp.devices.get(name)
            st = self.states[name]
            if di is None or not self._managed(name, di):
                st.pending = None
                continue
            retry, msg = self.sup.verify(name, st, cfg.params, now)
            if msg:
                out.notifications.append((f"{name}:faulty", f"{name}: {msg}"))
            if retry is not None:
                self._emit(retry, inp, out, dry_run)

        if bad:
            out.mode = "hold"
            if now - self.g.sensors_bad_since >= self.sup.p.safe_state_after:
                out.mode = "safe"
                car = self._car()
                if car is not None:
                    st, cfg, di = self.states[car], self.devices[car], inp.devices.get(car)
                    if di is not None and self._managed(car, di) and st.on and st.pending is None and self.sup.can_stop(st, cfg.params, now):
                        self._emit(Command(car, OFF, 0.0, "estado seguro: sensores sin datos"), inp, out, dry_run)
            self._status(inp, out, None)
            return out

        e_hat = self.filter.mean(now)
        if e_hat is None:
            e_hat = e_inst
        out.surplus = e_hat
        surplus = max(0.0, e_hat)
        house_import = max(0.0, -min(e_hat, e_inst))
        margin = max(0.0, inp.limit - house_import)
        out.margin = margin

        consumers, groups = [], []
        for name, cfg in self.devices.items():
            di = inp.devices.get(name)
            if di is None or not self._controllable(name, di):
                continue
            st = self.states[name]
            available = di.available and st.not_absorbing_until is None
            levels = self.sup.allowed_levels(st, cfg.params, self.g, now, available)
            if di.force_level is not None:
                forced = tuple(x for x in levels if abs(x - di.force_level) < 1e-3)
                if forced:
                    levels = forced
            value = cfg.value_offpeak if inp.offpeak and cfg.value_offpeak is not None else cfg.value_peak
            consumers.append(Consumer(name, cfg.group, levels, value, st.level if st.on else 0.0))
        for gid in sorted({c.group for c in consumers} | set(inp.alphas)):
            groups.append(Group(gid, inp.alphas.get(gid, 0.0), inp.caps.get(gid)))

        plan = water_fill(consumers, groups, surplus, margin, inp.price_import, inp.price_export, hysteresis=self.cp.hysteresis)
        out.plan = plan
        self.g.last_plan = now

        warm = now - self.g.started_at < self.sup.p.warmup
        out.mode = "warmup" if warm else "ok"
        for c in consumers:
            st, cfg = self.states[c.id], self.devices[c.id]
            planned = plan.levels.get(c.id, 0.0)
            cmd = self.sup.decide(c.id, st, cfg.params, self.g, planned, now, dry_run)
            if cmd is not None and not warm:
                self._emit(cmd, inp, out, dry_run)
        held = self._drive_held(inp, out, dry_run, warm)
        self._status(inp, out, plan, consumers, held)
        return out

    def _drive_held(self, inp: Inputs, out: Output, dry_run: bool, warm: bool) -> dict[str, float]:
        """The consumers held by the charge planner: its level, within what the guards allow and
        what fits under the contracted limit now (the house as measured, the device apart)"""
        held = {}
        for name, cfg in self.devices.items():
            di = inp.devices.get(name)
            if di is None or not self._held(name, di):
                continue
            st = self.states[name]
            available = di.available and st.not_absorbing_until is None
            levels = self.sup.allowed_levels(st, cfg.params, self.g, inp.now, available)
            room = inp.limit - (inp.net - self._y(name, di)) if inp.net is not None else 0.0
            fit = [x for x in levels if x <= min(di.held_level, room) + EPS]
            planned = max(fit) if fit else min(levels)
            held[name] = planned
            cmd = self.sup.decide(name, st, cfg.params, self.g, planned, inp.now, dry_run)
            if cmd is not None and not warm:
                cmd = Command(cmd.device, cmd.kind, cmd.value, f"plan de carga: {cmd.reason}", retry=cmd.retry, shed=cmd.shed)
                self._emit(cmd, inp, out, dry_run)
        return held

    def _status(self, inp: Inputs, out: Output, plan: Plan | None, consumers=None, held: dict[str, float] | None = None) -> None:
        levels = {c.id: c.levels for c in consumers or []}
        held = held or {}
        for name, cfg in self.devices.items():
            st = self.states[name]
            di = inp.devices.get(name)
            out.status[name] = DeviceStatus(
                levels=levels.get(name, ()),
                planned=held[name] if name in held else (plan.levels.get(name, 0.0) if plan else 0.0),
                controllable=di is not None and self._controllable(name, di),
                held=di is not None and self._held(name, di),
                start_at=self.sup.start_possible_at(st, cfg.params, self.g, inp.now),
                stop_at=self.sup.stop_possible_at(st, cfg.params, inp.now),
            )

    # ------------------------------------------------------------------ commands

    def _violation(self, v: Violation, out: Output) -> None:
        self.g.violations += 1
        out.violations.append(v)
        out.notifications.append((f"violation:{v.device}:{v.code}", f"Invariante {v.code} en {v.device}: {v.message}"))

    def _emit(self, cmd: Command, inp: Inputs, out: Output, dry_run: bool) -> None:
        """Invariants, then register the command (unless dry run: the command is only published)"""
        st, cfg = self.states[cmd.device], self.devices[cmd.device]
        if dry_run:
            out.commands.append(cmd)
            return
        violations = self.sup.check(cmd, st, cfg.params, self.g, inp.now)
        if violations:
            for v in violations:
                self._violation(v, out)
            out.blocked.append(cmd)
            return
        self.sup.register(cmd, st, self.g, inp.now)
        out.commands.append(cmd)
