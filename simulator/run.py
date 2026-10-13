"""Runs simulated days and reports invariant violations, switches per device and the extra cost
against the ideal plan (optimiser on the instantaneous surplus, no guards, instant devices)."""

from __future__ import annotations

import argparse
import json
import random
import statistics
from dataclasses import dataclass, field

from .core import controller_core as cc
from .core import optimizer_core as oc
from .core import supervisor_core as sc
from .world import DAY, BaseLoad, Profile, SimAC, SimCar, Weather, clear_sky, profiles

PEAK, OFFPEAK, SELL = 0.26371, 0.1349, 0.06
LIMIT = 4.6 * 0.9
SENSOR_PERIOD, CYCLE = 10, 30
CAR = "car"
ACS = ("salon_comedor", "salon_tv", "habitacion_enar", "habitacion_raul")
VALUES = {"car": 0.13, "salon_comedor": 0.09, "salon_tv": 0.09, "habitacion_enar": 0.08, "habitacion_raul": 0.08}
MANUAL_HOURS = 2 * 3600

CAR_PARAMS = sc.DeviceParams(
    levels=(0.0,) + tuple(round(a * SimCar.W_PER_A, 3) for a in SimCar.STEPS_A),
    confirm_on=60,
    confirm_off=300,
    min_on=600,
    min_off=600,
    max_starts_per_hour=3,
    level_interval=30,
    shed_reaction=10,
    verify_after=45,
    not_absorbing_kw=0.3,
)
AC_PARAMS = sc.DeviceParams(
    levels=(0.0, SimAC.KW),
    confirm_on=120,
    confirm_off=300,
    min_on=900,
    min_off=600,
    max_starts_per_hour=2,
    shed_reaction=30,
    verify_after=60,
    echo_window=90,
)


def is_offpeak(t):
    h = (t % DAY) / 3600
    return h < 12 or h >= 22


def price(t):
    return OFFPEAK if is_offpeak(t) else PEAK


def device_configs():
    out = [cc.DeviceConfig(CAR, 1, CAR_PARAMS, VALUES[CAR], is_car=True)]
    out += [cc.DeviceConfig(n, 2, AC_PARAMS, VALUES[n]) for n in ACS]
    return out


# ---------------------------------------------------------------------- invariant checker


@dataclass
class Checker:
    """Independent check of I1-I6 on the real (true) transitions of the devices"""

    params: dict
    true_on: dict = field(default_factory=dict)  # device -> list of real start times
    true_off: dict = field(default_factory=dict)
    level_changes: dict = field(default_factory=dict)
    starts_cmd: list = field(default_factory=list)
    shed_times: list = field(default_factory=list)
    violations: list = field(default_factory=list)
    over_since: float | None = None

    def transition(self, name, t, on):
        (self.true_on if on else self.true_off).setdefault(name, []).append(t)

    def level_change(self, name, t):
        self.level_changes.setdefault(name, []).append(t)

    def command(self, cmd, t, level_before):
        p = self.params[cmd.device]
        last = lambda xs: max((x for x in xs if x <= t), default=None)  # noqa: E731
        if cmd.kind == "on":
            off = last(self.true_off.get(cmd.device, []))
            if off is not None and t - off < p.min_off - 1e-6:
                self.violations.append(("I1", cmd.device, t))
            n = sum(1 for x in self.true_on.get(cmd.device, []) if t - 3600 < x <= t)
            if n + 1 > p.max_starts_per_hour:
                self.violations.append(("I3", cmd.device, t))
            prev = max(self.starts_cmd, default=None)
            if prev is not None and t - prev < 60 - 1e-6:
                self.violations.append(("I6", cmd.device, t))
            self.starts_cmd.append(t)
        elif cmd.kind == "off" and not cmd.shed:
            on = last(self.true_on.get(cmd.device, []))
            if on is not None and t - on < p.min_on - 1e-6:
                self.violations.append(("I2", cmd.device, t))
        elif cmd.kind == "level" and not (cmd.shed and cmd.value < level_before):
            lc = last(self.level_changes.get(cmd.device, []))
            if lc is not None and t - lc < p.level_interval - 1e-6:
                self.violations.append(("I4", cmd.device, t))
        if cmd.shed:
            self.shed_times.append(t)

    def overload(self, t, imported, controllable_on):
        """I5: during an overload, while a controllable device is on and not yet shed, a shedding
        action every reaction time (30 s max) + sensor period + grace"""
        if imported <= LIMIT:
            self.over_since = None
            return
        if self.over_since is None:
            self.over_since = t
        if not controllable_on:
            return
        ref = max([self.over_since] + [x for x in self.shed_times if x >= self.over_since])
        if t - ref > 30 + SENSOR_PERIOD + 10:
            self.violations.append(("I5", "grid", t))
            self.over_since = t  # one per window


# ---------------------------------------------------------------------- simulation


def simulate(profile: Profile, seed: int, start: float = 5 * 3600, end: float = 23.5 * 3600) -> dict:
    rnd = random.Random(seed)
    weather, base = Weather(profile, rnd), BaseLoad(rnd)
    car = SimCar(rnd)
    acs = {n: SimAC(rnd) for n in ACS}
    ctrl = cc.Controller(device_configs())
    checker = Checker({c.name: c.params for c in device_configs()})
    manual_until: dict[str, float] = {}
    alphas = {1: profile.alpha_car, 2: round(1 - profile.alpha_car, 6)}

    stats = {
        "cost": 0.0,
        "ideal_cost": 0.0,
        "import_kwh": 0.0,
        "export_kwh": 0.0,
        "ideal_import_kwh": 0.0,
        "ideal_export_kwh": 0.0,
        "delivered_kwh": 0.0,
        "ideal_delivered_kwh": 0.0,
    }
    switches = {n: 0 for n in (CAR,) + ACS}
    shed_commands = 0
    commands = 0
    modes: dict[str, int] = {}

    ideal_levels: dict[str, float] = {}
    last_reading = None
    prev_true = {n: False for n in switches}
    prev_amps = car.amps
    pv = 0.0
    for t in range(int(start), int(end)):
        # --- physics
        car.full = profile.full_car_at is not None and t >= profile.full_car_at
        car.tick(t)
        for ac in acs.values():
            ac.tick(t)
        for when, name, on in profile.manual:
            if when == t:
                dev = car if name == CAR else acs[name]
                dev.on = on
                manual_until[name] = t + MANUAL_HOURS
        pv = clear_sky(t, profile.peak_kw) * weather.factor(t)
        house = base.power(t)
        dev_power = {CAR: car.power(), **{n: ac.power() for n, ac in acs.items()}}
        net = house + sum(dev_power.values()) - pv
        true_on = {CAR: car.on, **{n: ac.on for n, ac in acs.items()}}
        for n, on in true_on.items():
            if on != prev_true[n]:
                checker.transition(n, t, on)
                if on:
                    switches[n] += 1
                prev_true[n] = on
        if car.amps != prev_amps:
            checker.level_change(CAR, t)
            prev_amps = car.amps
        controllable_on = any(true_on[n] and manual_until.get(n, 0) <= t for n in true_on)
        checker.overload(t, max(0.0, net), controllable_on)

        p_imp = price(t)
        value_delivered = sum(VALUES[n] * dev_power[n] for n in dev_power)
        stats["cost"] += (max(0, net) * p_imp - max(0, -net) * SELL - value_delivered) / 3600
        stats["import_kwh"] += max(0, net) / 3600
        stats["export_kwh"] += max(0, -net) / 3600
        stats["delivered_kwh"] += sum(dev_power.values()) / 3600

        # --- ideal plan (no guards, instantaneous surplus, instant devices), every sensor period
        if t % SENSOR_PERIOD == 0:
            e = pv - house
            consumers = [oc.Consumer(c.name, c.group, c.params.levels, c.value_peak, ideal_levels.get(c.name, 0.0)) for c in device_configs()]
            groups = [oc.Group(g, a) for g, a in alphas.items()]
            house_import = max(0.0, -e)
            plan = oc.water_fill(consumers, groups, max(0.0, e), max(0.0, LIMIT - house_import), p_imp, SELL)
            ideal_levels = plan.levels
        ideal_x = {n: (x * 0.95 if n == CAR else x) for n, x in ideal_levels.items()}
        if car.full:
            ideal_x[CAR] = 0.0
        ideal_net = house + sum(ideal_x.values()) - pv
        stats["ideal_cost"] += (max(0, ideal_net) * p_imp - max(0, -ideal_net) * SELL - sum(VALUES[n] * x for n, x in ideal_x.items())) / 3600
        stats["ideal_import_kwh"] += max(0, ideal_net) / 3600
        stats["ideal_export_kwh"] += max(0, -ideal_net) / 3600
        stats["ideal_delivered_kwh"] += sum(ideal_x.values()) / 3600

        # --- sensors and control
        if t % SENSOR_PERIOD != 0:
            continue
        gap = profile.sensor_gap is not None and profile.sensor_gap[0] <= t < profile.sensor_gap[1]
        frozen = profile.sensor_frozen is not None and profile.sensor_frozen[0] <= t < profile.sensor_frozen[1]
        reading = None if gap else (last_reading if frozen and last_reading is not None else net)
        last_reading = reading
        inputs = cc.Inputs(
            now=float(t),
            net=reading,
            production=pv,
            price_import=p_imp,
            price_export=SELL,
            offpeak=is_offpeak(t),
            limit=LIMIT,
            alphas=alphas,
            day=str(int(t // DAY)),
            devices={
                CAR: cc.DeviceInput(manual=manual_until.get(CAR, 0) > t, on=car.on, level=car.level, measured=car.power()),
                **{n: cc.DeviceInput(manual=manual_until.get(n, 0) > t, on=acs[n].reported(t)) for n in ACS},
            },
        )
        out = ctrl.step(inputs) if t % CYCLE == 0 else ctrl.fast(inputs)
        modes[out.mode] = modes.get(out.mode, 0) + 1
        for cmd in out.commands:
            commands += 1
            shed_commands += cmd.shed
            checker.command(cmd, t, car.level if cmd.device == CAR else 0)
            dev = car if cmd.device == CAR else acs[cmd.device]
            dev.command(t, cmd.kind, cmd.value)
        for v in out.violations:
            checker.violations.append((v.code, v.device, t, "supervisor"))

    hours = (end - start) / 3600
    return {
        "profile": profile.name,
        "seed": seed,
        "violations": checker.violations,
        "supervisor_violations": ctrl.g.violations,
        "switches": switches,
        "commands": commands,
        "shed_commands": shed_commands,
        "modes": modes,
        "cost_eur": round(stats["cost"], 3),
        "ideal_cost_eur": round(stats["ideal_cost"], 3),
        "extra_cost_eur": round(stats["cost"] - stats["ideal_cost"], 3),
        "import_kwh": round(stats["import_kwh"], 2),
        "export_kwh": round(stats["export_kwh"], 2),
        "ideal_import_kwh": round(stats["ideal_import_kwh"], 2),
        "ideal_export_kwh": round(stats["ideal_export_kwh"], 2),
        "delivered_kwh": round(stats["delivered_kwh"], 2),
        "ideal_delivered_kwh": round(stats["ideal_delivered_kwh"], 2),
        "hours": hours,
    }


def report(results: list[dict]) -> str:
    lines = [
        "| Perfil | Semillas | Violaciones | Conmutaciones/día (coche · aires) | Órdenes | Deslastres | Coste real € | Ideal € | Sobrecoste € | Importa kWh (ideal) | Exporta kWh (ideal) |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    by = {}
    for r in results:
        by.setdefault(r["profile"], []).append(r)
    for name, rs in by.items():
        m = lambda k: statistics.mean(r[k] for r in rs)  # noqa: E731
        car_sw = statistics.mean(r["switches"][CAR] for r in rs)
        ac_sw = statistics.mean(sum(r["switches"][a] for a in ACS) / len(ACS) for r in rs)
        viol = sum(len(r["violations"]) for r in rs)
        lines.append(
            f"| {name} | {len(rs)} | {viol} | {car_sw:.1f} · {ac_sw:.1f} | {m('commands'):.0f} | {m('shed_commands'):.1f} | {m('cost_eur'):.2f} | {m('ideal_cost_eur'):.2f} | {m('extra_cost_eur'):.2f} | {m('import_kwh'):.1f} ({m('ideal_import_kwh'):.1f}) | {m('export_kwh'):.1f} ({m('ideal_export_kwh'):.1f}) |"
        )
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--profile", action="append")
    ap.add_argument("--json")
    args = ap.parse_args(argv)
    chosen = [p for p in profiles() if not args.profile or p.name in args.profile]
    results = [simulate(p, s) for p in chosen for s in range(args.seeds)]
    print(report(results))
    bad = [(r["profile"], r["seed"], v) for r in results for v in r["violations"]]
    print(f"\nViolaciones de invariantes: {len(bad)}")
    for b in bad[:20]:
        print("  ", b)
    if args.json:
        with open(args.json, "w") as f:
            json.dump(results, f, indent=1, default=str)
    return 1 if bad else 0
