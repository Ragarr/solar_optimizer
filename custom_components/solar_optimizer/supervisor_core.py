"""Supervisor: executes the optimiser plan while protecting the devices.

Pure Python (no Home Assistant import), times are epoch seconds and powers kW.

The supervisor never rewrites the plan: it only delays it (confirmations) or holds it (blocks).
Each cycle it gives the optimiser the admissible levels L_i^ef of every consumer:

    can_start_i = s_i = OFF and t - t_off_i >= T_off and starts in 60 min < N_max
                  and t - last start of any consumer >= start separation
    can_stop_i  = s_i = ON and (t - t_on_i >= T_on or load shedding)
    L_i^ef: ON and not can_stop  -> L_i without 0
            OFF and not can_start -> {0}
            otherwise             -> L_i

and turns the plan into commands:

    switch on  if the plan asks ON  during the "on" confirmation  and can_start
    switch off if the plan asks OFF during the "off" confirmation and can_stop
    change the level (modulable devices) if the level interval has passed since the last change

Rules, by priority: 1. contracted power (load shedding, can break anything except rule 2: on the
30 min moving average of the import, or at once over the hard limit of the instantaneous import);
2. minimal off time (no exception); 3. minimal on time and starts per hour; 4. confirmations;
5. the plan.

Invariants checked before every command (a command that would break one is not sent, the
violation is counted and notified):
    I1  between a stop and the next start of i: at least T_off
    I2  between a start and the next stop: at least T_on (except load shedding)
    I3  starts of i in any 60 min window <= N_max
    I4  level changes separated by the level interval (except shedding decreases)
    I6  two starts of any consumers separated by the start separation
I5 (while the overload lasts, every reaction time the shedding acts on a consumer still on) is
checked over time by ``Supervisor.check_overload``.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

HOUR = 3600.0
EPS = 1e-6

ON, OFF, LEVEL = "on", "off", "level"


@dataclass(frozen=True)
class DeviceParams:
    """Configuration of a consumer for the supervisor"""

    levels: tuple[float, ...]  # admissible levels (kW), 0 included
    confirm_on: float = 60.0
    confirm_off: float = 300.0
    min_on: float = 600.0
    min_off: float = 600.0
    max_starts_per_hour: int = 3
    level_interval: float = 30.0  # minimal time between two level changes (modulable devices)
    command_separation: float = 30.0  # minimal time between two commands to the device
    shed_reaction: float = 10.0  # time to see the effect of a shedding action
    verify_after: float = 45.0  # read the state back this long after a command
    echo_window: float = 60.0  # after a command, a reported state going back to the previous value is a flicker
    retry_delays: tuple[float, ...] = (30.0, 60.0, 120.0)
    not_absorbing_kw: float | None = None  # below this measured power while ON -> not absorbing
    not_absorbing_after: float = 150.0
    not_absorbing_retry: float = 1800.0
    use_estimated: bool = False  # on/off without a measure: count its estimated power, not the nominal

    @property
    def modulable(self) -> bool:
        return len([x for x in self.levels if x > EPS]) > 1

    @property
    def min_level(self) -> float:
        return min((x for x in self.levels if x > EPS), default=0.0)


@dataclass(frozen=True)
class GlobalParams:
    """Configuration shared by all the consumers"""

    start_separation: float = 60.0
    warmup: float = 120.0  # after a restart, no command until this long of valid measures
    sensor_hold_after: float = 300.0  # frozen sensor (unchanged with sun) after this long
    safe_state_after: float = 600.0  # sensors bad for this long -> safe state
    plan_stale_periods: int = 3
    period: float = 30.0
    shed_notify_every: float = 900.0
    # Load shedding. The grid operator only trips on a sustained excess, so the normal shedding
    # acts on the moving average of the import; a hard limit on the instantaneous import keeps a
    # fast cut for large peaks.
    shed_hard_limit: float = 6.5  # kW, instantaneous import: fast shedding above it
    shed_avg_window: float = 1800.0  # moving average of the import compared with the limit
    shed_avg_min_history: float = 300.0  # no shedding on the average with less history than this
    shed_slow_reaction: float = 60.0  # wait between two steps of the shedding on the average
    shed_slow_margin: float = 0.1  # kW below the limit aimed at by a proportional decrease


@dataclass
class DeviceState:
    """Persisted state of a consumer (survives a restart)"""

    on: bool = False
    level: float = 0.0  # current setting (kW); for a modulable device kept while OFF
    t_on: float | None = None  # last real start
    t_off: float | None = None  # last real stop
    starts: list[float] = field(default_factory=list)  # real starts of the last hour
    obj_on: bool | None = None  # what the plan asks (ON/OFF) ...
    since: float | None = None  # ... without interruption since
    last_command: float | None = None
    last_level_change: float | None = None
    pending: dict | None = None  # command waiting to be verified
    # last ON/OFF command {"kind", "t"}: a reported state going back to the previous one within the
    # echo window is a flicker. Apart from `pending`, which the verification clears as soon as the
    # command looks applied (cloud devices may report a stale state after that)
    echo: dict | None = None
    faulty: bool = False
    low_since: float | None = None  # measured power under the threshold since (not absorbing)
    not_absorbing_until: float | None = None
    est_on: float | None = None  # power estimated from the steps at its starts (kW) ...
    est_off: float | None = None  # ... and at its stops (power_estimator)
    est_steps: int = 0  # accepted steps

    @property
    def est(self) -> float | None:
        """Estimated power (kW): the mean of the start and stop estimates available"""
        values = [x for x in (self.est_on, self.est_off) if x is not None]
        return sum(values) / len(values) if values else None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> "DeviceState":
        state = cls()
        for key, value in (data or {}).items():
            if hasattr(state, key):
                setattr(state, key, list(value) if key == "starts" and value is not None else value)
        return state


@dataclass
class GlobalState:
    """State shared by all the consumers"""

    last_start: float | None = None  # last start commanded (any consumer)
    last_command: float | None = None  # last command (any consumer): measures right after are discarded
    started_at: float | None = None
    sensors_bad_since: float | None = None
    last_plan: float | None = None
    shed_wait_until: float = 0.0
    # Ceiling (kW, 0 = stopped) of each device shed during the current overload: the plan cannot
    # raise it above that until the overload ends
    shed_caps: dict[str, float] = field(default_factory=dict)
    shed_mode: str | None = None  # "hard" (instantaneous peak) | "avg" (moving average) | None
    over_since: float | None = None
    shed_notified: float | None = None
    violations: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> "GlobalState":
        state = cls()
        for key, value in (data or {}).items():
            if hasattr(state, key):
                setattr(state, key, value)
        return state


@dataclass(frozen=True)
class Command:
    device: str
    kind: str  # "on" | "off" | "level"
    value: float | None = None  # level (kW) for "level", and the level wanted for "on"
    reason: str = ""
    shed: bool = False
    retry: bool = False


@dataclass(frozen=True)
class Violation:
    device: str
    code: str  # I1..I6
    message: str
    time: float


def _close(a, b):
    return a is not None and b is not None and math.isclose(a, b, rel_tol=0.0, abs_tol=1e-3)


class Supervisor:
    """Guards of the consumers. Stateless: all the state is in DeviceState / GlobalState."""

    def __init__(self, params: GlobalParams | None = None):
        self.p = params or GlobalParams()

    # ------------------------------------------------------------------ observation

    def observe(self, dev: DeviceState, p: DeviceParams, on: bool, level: float | None, now: float) -> None:
        """Real state read from the device. The timers count from the real changes, whoever made
        them (Solar Optimizer, a person, the device itself)."""
        if on and not dev.on:
            dev.t_on = now
            dev.starts.append(now)
        elif dev.on and not on:
            dev.t_off = now
        dev.on = on
        if level is not None and level > EPS:
            if dev.level > EPS and not _close(level, dev.level):
                dev.last_level_change = now
            dev.level = level
        elif not p.modulable:
            dev.level = p.min_level if on else 0.0
        dev.starts = [t for t in dev.starts if now - t < HOUR]

    def observe_measured(self, dev: DeviceState, p: DeviceParams, measured: float | None, now: float) -> str | None:
        """Detects a consumer that does not absorb (e.g. a full car): ON with a level, but measured
        power under the threshold for a while. Returns a notification text when detected."""
        if p.not_absorbing_kw is None or measured is None:
            return None
        if dev.on and dev.level >= p.min_level - EPS and measured < p.not_absorbing_kw:
            if dev.low_since is None:
                dev.low_since = now
            elif now - dev.low_since >= p.not_absorbing_after and dev.not_absorbing_until is None:
                dev.not_absorbing_until = now + p.not_absorbing_retry
                return f"no absorbe ({measured:.2f} kW con consigna {dev.level:.2f} kW)"
        else:
            dev.low_since = None
        return None

    def reset_not_absorbing(self, dev: DeviceState) -> None:
        dev.not_absorbing_until = None
        dev.low_since = None

    # ------------------------------------------------------------------ permissions

    def starts_last_hour(self, dev: DeviceState, now: float) -> int:
        n = sum(1 for t in dev.starts if now - t < HOUR)
        if dev.pending and dev.pending["kind"] == ON and not dev.on:
            n += 1  # commanded, not yet seen
        return n

    def can_start(self, dev: DeviceState, p: DeviceParams, g: GlobalState, now: float) -> bool:
        return (
            not dev.on
            and (dev.t_off is None or now - dev.t_off >= p.min_off)
            and self.starts_last_hour(dev, now) < p.max_starts_per_hour
            and (g.last_start is None or now - g.last_start >= self.p.start_separation)
        )

    def can_stop(self, dev: DeviceState, p: DeviceParams, now: float, shedding: bool = False) -> bool:
        return dev.on and (shedding or dev.t_on is None or now - dev.t_on >= p.min_on)

    def start_possible_at(self, dev: DeviceState, p: DeviceParams, g: GlobalState, now: float) -> float | None:
        """When the consumer could be started (None if it is ON)"""
        if dev.on:
            return None
        t = now
        if dev.t_off is not None:
            t = max(t, dev.t_off + p.min_off)
        recent = sorted(x for x in dev.starts if now - x < HOUR)
        if len(recent) >= p.max_starts_per_hour:
            t = max(t, recent[len(recent) - p.max_starts_per_hour] + HOUR)
        if g.last_start is not None:
            t = max(t, g.last_start + self.p.start_separation)
        return t

    def stop_possible_at(self, dev: DeviceState, p: DeviceParams, now: float) -> float | None:
        """When the consumer could be stopped (None if it is OFF)"""
        if not dev.on:
            return None
        return now if dev.t_on is None else max(now, dev.t_on + p.min_on)

    def allowed_levels(self, dev: DeviceState, p: DeviceParams, g: GlobalState, now: float, available: bool = True) -> tuple[float, ...]:
        """L_i^ef: the levels the optimiser may choose"""
        if dev.on:
            if self.can_stop(dev, p, now):
                return p.levels if available else (0.0,)
            return tuple(x for x in p.levels if x > EPS)
        if available and not dev.faulty and self.can_start(dev, p, g, now):
            return p.levels
        return (0.0,)

    # ------------------------------------------------------------------ invariants

    def check(self, cmd: Command, dev: DeviceState, p: DeviceParams, g: GlobalState, now: float) -> list[Violation]:
        """Invariants I1-I4 and I6 for a command about to be sent"""
        out = []

        def v(code, msg):
            out.append(Violation(cmd.device, code, msg, now))

        if cmd.kind == ON:
            if dev.t_off is not None and now - dev.t_off < p.min_off - EPS:
                v("I1", f"arranque {now - dev.t_off:.0f} s después de parar (mínimo {p.min_off:.0f} s)")
            if self.starts_last_hour(dev, now) + 1 > p.max_starts_per_hour:
                v("I3", f"más de {p.max_starts_per_hour} arranques en 60 min")
            if g.last_start is not None and now - g.last_start < self.p.start_separation - EPS:
                v("I6", f"arranque {now - g.last_start:.0f} s después de otro (mínimo {self.p.start_separation:.0f} s)")
        elif cmd.kind == OFF and not cmd.shed:
            if dev.t_on is not None and now - dev.t_on < p.min_on - EPS:
                v("I2", f"parada {now - dev.t_on:.0f} s después de arrancar (mínimo {p.min_on:.0f} s)")
        elif cmd.kind == LEVEL and not (cmd.shed and cmd.value is not None and cmd.value < dev.level):
            if dev.last_level_change is not None and now - dev.last_level_change < p.level_interval - EPS:
                v("I4", f"cambio de nivel {now - dev.last_level_change:.0f} s después del anterior (mínimo {p.level_interval:.0f} s)")
        return out

    # ------------------------------------------------------------------ commands

    def register(self, cmd: Command, dev: DeviceState, g: GlobalState, now: float) -> None:
        """The command is sent: remember it (verification, separations)"""
        dev.last_command = now
        g.last_command = now
        attempt = (dev.pending or {}).get("attempt", 0) if cmd.retry else 0
        dev.pending = {"kind": cmd.kind, "value": cmd.value, "t": now, "attempt": attempt, "retry_at": None}
        if cmd.kind in (ON, OFF):
            dev.echo = {"kind": cmd.kind, "t": now}
        if cmd.kind == ON:
            g.last_start = now
        if cmd.kind == LEVEL:
            dev.last_level_change = now

    def verify(self, name: str, dev: DeviceState, p: DeviceParams, now: float) -> tuple[Command | None, str | None]:
        """Read back the state after a command. Returns (retry command, notification)."""
        pend = dev.pending
        if pend is None:
            return None, None
        ok = (pend["kind"] == ON and dev.on) or (pend["kind"] == OFF and not dev.on) or (pend["kind"] == LEVEL and _close(dev.level, pend["value"]))
        if ok:
            dev.pending = None
            return None, None
        if pend.get("retry_at") is None:
            if now - pend["t"] < p.verify_after:
                return None, None
            if pend["attempt"] >= len(p.retry_delays):
                dev.pending = None
                dev.faulty = True
                return None, f"no responde tras {pend['attempt']} reintentos: marcado como averiado"
            pend["retry_at"] = now + p.retry_delays[pend["attempt"]]
            return None, None
        if now >= pend["retry_at"]:
            pend["attempt"] += 1
            return Command(name, pend["kind"], pend["value"], f"reintento {pend['attempt']}", retry=True), None
        return None, None

    def decide(self, name: str, dev: DeviceState, p: DeviceParams, g: GlobalState, planned: float, now: float, dry_run: bool = False) -> Command | None:
        """Command for the planned level, after confirmations and guards (None: nothing now).
        In dry run nothing is applied, so the level is not prepared before a start (the start is
        published directly, with its level)."""
        want_on = planned > EPS
        if dev.obj_on != want_on:
            dev.obj_on, dev.since = want_on, now
        if dev.pending is not None:
            return None
        if dev.last_command is not None and now - dev.last_command < p.command_separation:
            return None
        held = now - (dev.since if dev.since is not None else now)
        if want_on and not dev.on:
            if not dry_run and p.modulable and not _close(planned, dev.level) and self._level_change_allowed(dev, p, now):
                # Prepare the level while the start is being confirmed
                return Command(name, LEVEL, planned, "nivel previo al arranque")
            if held >= p.confirm_on and self.can_start(dev, p, g, now):
                return Command(name, ON, planned, "arranque confirmado")
            return None
        if not want_on and dev.on:
            if held >= p.confirm_off and self.can_stop(dev, p, now):
                return Command(name, OFF, 0.0, "parada confirmada")
            return None
        if want_on and dev.on and p.modulable and not _close(planned, dev.level) and self._level_change_allowed(dev, p, now):
            return Command(name, LEVEL, planned, "cambio de nivel")
        return None

    def _level_change_allowed(self, dev: DeviceState, p: DeviceParams, now: float) -> bool:
        return dev.last_level_change is None or now - dev.last_level_change >= p.level_interval

    # ------------------------------------------------------------------ load shedding

    def overload_mode(self, imported: float, avg: float | None, limit: float) -> str | None:
        """"hard" when the instantaneous import exceeds the hard limit, "avg" when its moving
        average exceeds the limit, None otherwise"""
        if imported > self.p.shed_hard_limit + EPS:
            return "hard"
        if avg is not None and avg > limit + EPS:
            return "avg"
        return None

    def shed(
        self,
        devices: list[tuple[str, DeviceState, DeviceParams, float]],
        g: GlobalState,
        imported: float,
        avg: float | None,
        recent: float | None,
        limit: float,
        now: float,
        car: str | None = None,
    ) -> tuple[Command | None, str | None]:
        """Load shedding, on every new grid measure. ``devices``: (name, state, params, value) of
        the AUTO, included consumers; ``imported`` the instantaneous import, ``avg`` its moving
        average (None without enough history) and ``recent`` its short average.

        - hard (instantaneous import over the hard limit): car to its minimal level, stop the car,
          stop the other consumers from the lowest to the highest value, waiting the reaction time
          of each action before the next one;
        - avg (moving average over the limit): only while the recent import is still over the
          limit (otherwise the average is already going down), the car goes down to the highest
          level that brings the recent import under the limit, step after step, then it stops,
          then the others stop; ``shed_slow_reaction`` between two steps.

        Each shed device keeps a ceiling until the overload ends. Returns (command, notification)."""
        mode = self.overload_mode(imported, avg, limit)
        if mode is None:
            g.over_since = None
            g.shed_caps = {}
            g.shed_mode = None
            return None, None
        if g.over_since is None:
            g.over_since = now
        g.shed_mode = mode
        if now < g.shed_wait_until:
            return None, None
        hard = mode == "hard"
        if not hard and (recent is None or recent <= limit + EPS):
            return None, None
        reason = "deslastre: pico de importación" if hard else "deslastre: media de importación"
        modulable = [d for d in devices if d[2].modulable and d[1].on]
        if car is not None:
            modulable.sort(key=lambda d: d[0] != car)
        for name, dev, p, _ in modulable:
            cap = g.shed_caps.get(name)
            if dev.level <= p.min_level + EPS or (cap is not None and cap < dev.level - EPS):
                continue  # at its minimum, or already commanded down (the verification follows it)
            if hard:
                target, wait = p.min_level, p.shed_reaction
            else:
                aim = dev.level - (recent - limit) - self.p.shed_slow_margin
                lower = [x for x in p.levels if p.min_level - EPS <= x < dev.level - EPS]
                fit = [x for x in lower if x <= aim + EPS]
                target, wait = (max(fit) if fit else p.min_level), self.p.shed_slow_reaction
            g.shed_caps[name] = target
            g.shed_wait_until = now + wait
            return Command(name, LEVEL, target, reason, shed=True), None
        ordered = sorted(devices, key=lambda d: (not d[2].modulable, d[0] != car, d[3]))
        for name, dev, p, _ in ordered:
            cap = g.shed_caps.get(name)
            if dev.on and (cap is None or cap > EPS):
                g.shed_caps[name] = 0.0
                g.shed_wait_until = now + (p.shed_reaction if hard else self.p.shed_slow_reaction)
                return Command(name, OFF, 0.0, reason, shed=True), None
        if g.shed_notified is None or now - g.shed_notified >= self.p.shed_notify_every:
            g.shed_notified = now
            if hard:
                return None, f"importación {imported:.2f} kW por encima del corte rápido {self.p.shed_hard_limit:.2f} kW y no queda nada que deslastrar"
            minutes = self.p.shed_avg_window / 60
            return None, f"importación media ({minutes:.0f} min) {avg:.2f} kW por encima del límite {limit:.2f} kW y no queda nada que deslastrar"
        return None, None

    def check_overload(
        self,
        devices: list[tuple[str, DeviceState, DeviceParams, float]],
        g: GlobalState,
        imported: float,
        avg: float | None,
        recent: float | None,
        limit: float,
        now: float,
        grace: float = 5.0,
    ) -> Violation | None:
        """I5: the overload that calls for shedding (instantaneous import over the hard limit, or
        average and recent import over the limit) is still there after the reaction time of the
        last shedding action while a consumer that could be shed is still ON and not stopped (the
        shedding did not act). A consumer that ignores its command is handled by the verification."""
        if g.over_since is None:
            return None
        mode = self.overload_mode(imported, avg, limit)
        if mode is None or (mode == "avg" and (recent is None or recent <= limit + EPS)):
            return None
        if now <= max(g.shed_wait_until, g.over_since) + grace:
            return None
        late = [name for name, dev, _, _ in devices if dev.on and (g.shed_caps.get(name) is None or g.shed_caps[name] > EPS)]
        if late:
            if mode == "hard":
                what = f"importación {imported:.2f} kW > {self.p.shed_hard_limit:.2f} kW"
            else:
                what = f"importación media {avg:.2f} kW > {limit:.2f} kW"
            return Violation(late[0], "I5", f"{what} durante {now - g.over_since:.0f} s sin deslastrar", now)
        return None
