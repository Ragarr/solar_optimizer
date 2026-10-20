"""Supervisor and control cycle (pure, simulated time): permissions, confirmations, invariants
I1-I6, load shedding, verification, robustness and restore after a restart"""

import pytest

from custom_components.solar_optimizer.controller_core import Controller, DeviceConfig, DeviceInput, Inputs
from custom_components.solar_optimizer.supervisor_core import Command, DeviceParams, DeviceState, GlobalParams, GlobalState, Supervisor

PEAK, SELL = 0.26371, 0.06
CAR_LEVELS = (0.0,) + tuple(round(0.232 * a, 3) for a in (6, 8, 10, 13, 16, 20, 24, 32))
CAR = DeviceParams(levels=CAR_LEVELS, confirm_on=60, confirm_off=300, min_on=600, min_off=600, max_starts_per_hour=3, level_interval=30, shed_reaction=10, not_absorbing_kw=0.3)
AC = DeviceParams(levels=(0.0, 0.8), confirm_on=120, confirm_off=300, min_on=900, min_off=600, max_starts_per_hour=2, shed_reaction=30, verify_after=60, echo_window=90)
T0 = 1_800_000_000.0


def controller(gparams=None):
    return Controller(
        [DeviceConfig("car", 1, CAR, 0.13, is_car=True), DeviceConfig("ac1", 2, AC, 0.09), DeviceConfig("ac2", 2, AC, 0.08)],
        gparams or GlobalParams(warmup=0),
    )


class House:
    """Minimal world: devices apply the commands instantly; net = base + devices - pv"""

    def __init__(self, ctrl, base=0.4, pv=0.0):
        self.ctrl, self.base, self.pv = ctrl, base, pv
        self.on = {"car": False, "ac1": False, "ac2": False}
        self.car_level = 13 * 0.232
        self.log = []
        self.inputs_extra = {}

    def power(self, name):
        if not self.on[name]:
            return 0.0
        return self.car_level if name == "car" else 0.8

    def inputs(self, t, **kw):
        # a little noise: a constant reading would look like a frozen sensor
        net = self.base + 0.003 * ((t // 10) % 5) + sum(self.power(n) for n in self.on) - self.pv
        devices = {
            "car": DeviceInput(on=self.on["car"], level=self.car_level, measured=self.power("car")),
            "ac1": DeviceInput(on=self.on["ac1"]),
            "ac2": DeviceInput(on=self.on["ac2"]),
        }
        for name, extra in self.inputs_extra.items():
            for k, v in extra.items():
                setattr(devices[name], k, v)
        args = dict(
            now=t, net=net, production=self.pv + 0.01 * ((t // 10) % 7), price_import=PEAK, price_export=SELL, offpeak=False, limit=4.14, alphas={1: 1.0, 2: 0.0}, devices=devices
        )
        args.update(kw)
        return Inputs(**args)

    def apply(self, out, t):
        for c in out.commands:
            self.log.append((t, c))
            if c.kind == "on":
                self.on[c.device] = True
            elif c.kind == "off":
                self.on[c.device] = False
            elif c.device == "car":
                self.car_level = c.value

    def run(self, t0, t1, step=10, cycle=30, dry_run=False, **kw):
        """Runs the loop; returns the output of the last cycle (not of the fast path)"""
        last = None
        for t in range(int(t0), int(t1), step):
            inp = self.inputs(float(t), **kw)
            out = self.ctrl.step(inp, dry_run) if t % cycle == 0 else self.ctrl.fast(inp, dry_run)
            if t % cycle == 0:
                last = out
            if not dry_run:
                self.apply(out, t)
        return last

    def commands(self, device=None, kind=None):
        return [(t, c) for t, c in self.log if (device is None or c.device == device) and (kind is None or c.kind == kind)]


# ---------------------------------------------------------------------- permissions and levels


def test_allowed_levels():
    sup, g = Supervisor(GlobalParams()), GlobalState()
    dev = DeviceState(on=True, level=1.392, t_on=T0)
    assert 0.0 not in sup.allowed_levels(dev, CAR, g, T0 + 599)  # T_on not reached
    assert 0.0 in sup.allowed_levels(dev, CAR, g, T0 + 600)
    off = DeviceState(on=False, t_off=T0)
    assert sup.allowed_levels(off, CAR, g, T0 + 599) == (0.0,)  # T_off
    assert sup.allowed_levels(off, CAR, g, T0 + 600) == CAR_LEVELS
    assert sup.allowed_levels(off, CAR, g, T0 + 600, available=False) == (0.0,)
    g.last_start = T0 + 590
    assert sup.allowed_levels(off, CAR, g, T0 + 600) == (0.0,)  # start separation
    full = DeviceState(on=False, starts=[T0, T0 + 1000, T0 + 2000])
    assert sup.allowed_levels(full, CAR, GlobalState(), T0 + 3000) == (0.0,)  # 3 starts/h
    assert sup.allowed_levels(full, CAR, GlobalState(), T0 + 3601) == CAR_LEVELS
    assert sup.start_possible_at(full, CAR, GlobalState(), T0 + 3000) == T0 + 3600


def test_confirmations_and_change_of_mind():
    """ON after the "on" confirmation; a change of mind of the plan restarts it"""
    sup, g = Supervisor(GlobalParams()), GlobalState()
    dev = DeviceState(level=1.392)
    assert sup.decide("ac", dev, AC, g, 0.8, T0) is None
    assert sup.decide("ac", dev, AC, g, 0.8, T0 + 110) is None
    assert sup.decide("ac", dev, AC, g, 0.0, T0 + 115) is None  # change of mind
    assert sup.decide("ac", dev, AC, g, 0.8, T0 + 120) is None
    assert sup.decide("ac", dev, AC, g, 0.8, T0 + 230) is None
    cmd = sup.decide("ac", dev, AC, g, 0.8, T0 + 240)
    assert cmd is not None and cmd.kind == "on"


def test_level_prepared_before_start_then_interval():
    sup, g = Supervisor(GlobalParams()), GlobalState()
    dev = DeviceState(level=13 * 0.232)
    cmd = sup.decide("car", dev, CAR, g, 6 * 0.232, T0)
    assert cmd.kind == "level" and cmd.value == pytest.approx(1.392)
    sup.register(cmd, dev, g, T0)
    sup.observe(dev, CAR, False, 1.392, T0 + 5)
    assert sup.verify("car", dev, CAR, T0 + 5) == (None, None) and dev.pending is None
    assert sup.decide("car", dev, CAR, g, 1.392, T0 + 20) is None  # command separation
    cmd = sup.decide("car", dev, CAR, g, 1.392, T0 + 60)
    assert cmd.kind == "on"


# ---------------------------------------------------------------------- invariants


@pytest.mark.parametrize(
    "dev,cmd,now,code",
    [
        (DeviceState(on=False, t_off=T0), Command("car", "on", 1.392), T0 + 599, "I1"),
        (DeviceState(on=True, t_on=T0), Command("car", "off"), T0 + 599, "I2"),
        (DeviceState(on=False, starts=[T0, T0 + 10, T0 + 20]), Command("car", "on", 1.392), T0 + 3000, "I3"),
        (DeviceState(on=True, level=2.0, last_level_change=T0), Command("car", "level", 2.32), T0 + 29, "I4"),
    ],
)
def test_check_flags_violations(dev, cmd, now, code):
    sup = Supervisor(GlobalParams())
    assert [v.code for v in sup.check(cmd, dev, CAR, GlobalState(), now)] == [code]


def test_check_exceptions_and_i6():
    sup = Supervisor(GlobalParams())
    # Shedding may stop before T_on and lower the level at once
    assert sup.check(Command("car", "off", shed=True), DeviceState(on=True, t_on=T0), CAR, GlobalState(), T0 + 5) == []
    assert sup.check(Command("car", "level", 1.392, shed=True), DeviceState(on=True, level=3.0, last_level_change=T0), CAR, GlobalState(), T0 + 5) == []
    # ... but never start before T_off, and two starts are 60 s apart
    g = GlobalState(last_start=T0)
    assert [v.code for v in sup.check(Command("ac", "on", 0.8), DeviceState(), AC, g, T0 + 59)] == ["I6"]


def test_controller_blocks_and_counts_a_violating_command():
    """A command that would break an invariant is not sent; it is counted and notified"""
    ctrl = controller()
    out_inp = House(ctrl).inputs(T0)
    ctrl.states["car"] = DeviceState(on=False, t_off=T0 - 10)
    from custom_components.solar_optimizer.controller_core import Output

    out = Output()
    ctrl._emit(Command("car", "on", 1.392), out_inp, out, dry_run=False)
    assert out.commands == [] and len(out.blocked) == 1
    assert ctrl.g.violations == 1 and out.violations[0].code == "I1"
    assert out.notifications[0][0] == "violation:car:I1"


def test_full_days_never_violate():
    """Square clouds with several periods: the invariants hold on every command sent"""
    sup = Supervisor(GlobalParams())
    for period in (30, 60, 120, 300, 600):
        ctrl = controller()
        house = House(ctrl)
        for t in range(int(T0), int(T0 + 6 * 3600), 10):
            house.pv = 5.0 if (t // (period // 2)) % 2 == 0 else 0.8
            inp = house.inputs(float(t))
            before = {n: DeviceState.from_dict(s.to_dict()) for n, s in ctrl.states.items()}
            g_before = GlobalState.from_dict(ctrl.g.to_dict())
            out = ctrl.step(inp) if t % 30 == 0 else ctrl.fast(inp)
            for c in out.commands:
                params = CAR if c.device == "car" else AC
                assert sup.check(c, before[c.device], params, g_before, float(t)) == [], (period, t, c)
            house.apply(out, t)
        assert ctrl.g.violations == 0


# ---------------------------------------------------------------------- shedding


def test_load_shedding_sequence():
    """Over the limit: car to 6 A, then car off, then the ACs from the lowest value; waits the
    reaction time between steps; never touches manual devices; notifies when nothing is left"""
    ctrl = controller()
    house = House(ctrl, base=0.4, pv=0.0)
    house.on.update(car=True, ac1=True, ac2=True)
    house.car_level = 16 * 0.232
    for name in ("car", "ac1", "ac2"):
        ctrl.states[name] = DeviceState(on=True, level=house.power(name), t_on=T0 - 60)
    house.inputs_extra["ac1"] = {"manual": True}
    house.base = 3.5  # 3.5 + 3.712 + 0.8 + 0.8 > 4.14, and still 3.5 + 0.8 (ac1, manual) after shedding
    out = ctrl.fast(house.inputs(T0))
    assert [(c.kind, c.value) for c in out.commands] == [("level", pytest.approx(1.392))]
    house.apply(out, T0)
    assert ctrl.fast(house.inputs(T0 + 5)).commands == []  # reaction time of the car: 10 s
    out = ctrl.fast(house.inputs(T0 + 10))
    assert [(c.device, c.kind) for c in out.commands] == [("car", "off")]
    house.apply(out, T0 + 10)
    out = ctrl.fast(house.inputs(T0 + 20))
    assert [(c.device, c.kind) for c in out.commands] == [("ac2", "off")]  # ac1 is manual
    house.apply(out, T0 + 20)
    out = ctrl.fast(house.inputs(T0 + 50))
    assert out.commands == [] and any(k == "shed" for k, _ in out.notifications)
    assert ctrl.g.violations == 0
    # T_off still applies after shedding: the car cannot restart for 10 min
    assert ctrl.sup.allowed_levels(ctrl.states["car"], CAR, ctrl.g, T0 + 300) == (0.0,)


def test_i5_detected_when_shedding_does_not_act():
    sup = Supervisor(GlobalParams())
    g = GlobalState(over_since=T0, shed_wait_until=T0)
    dev = DeviceState(on=True, level=0.8)
    assert sup.check_overload([("ac", dev, AC, 0.08)], g, 5.0, 4.14, T0 + 4) is None
    v = sup.check_overload([("ac", dev, AC, 0.08)], g, 5.0, 4.14, T0 + 6)
    assert v is not None and v.code == "I5"


# ---------------------------------------------------------------------- verification and robustness


def test_retries_then_faulty():
    """A device that does not respond: 3 retries with growing waits, then faulty and notified"""
    ctrl = controller()
    house = House(ctrl, pv=3.5)  # plan: 13 A, the current setting (no level to prepare)
    sent = []
    for t in range(int(T0), int(T0 + 1200), 10):
        out = ctrl.step(house.inputs(float(t))) if t % 30 == 0 else ctrl.fast(house.inputs(float(t)))
        sent += [(t - T0, c.kind, c.retry) for c in out.commands if c.device == "car"]  # never applied
        if any(k == "car:faulty" for k, _ in out.notifications):
            break
    ons = [s for s in sent if s[1] == "on"]
    assert len(ons) == 4 and [s[2] for s in ons] == [False, True, True, True]
    gaps = [b[0] - a[0] for a, b in zip(ons, ons[1:])]
    assert gaps == sorted(gaps)  # growing waits
    assert ctrl.states["car"].faulty


def test_not_absorbing_car():
    ctrl = controller()
    house = House(ctrl, pv=4.0)
    house.on["car"] = True
    ctrl.states["car"] = DeviceState(on=True, level=house.car_level, t_on=T0 - 900)
    house.inputs_extra["car"] = {"measured": 0.05}
    notes = []
    for t in range(int(T0), int(T0 + 700), 10):  # 150 s to detect it + 300 s of "off" confirmation
        out = ctrl.step(house.inputs(float(t))) if t % 30 == 0 else ctrl.fast(house.inputs(float(t)))
        notes += [k for k, _ in out.notifications]
        house.apply(out, t)
    assert "car:not_absorbing" in notes
    assert ctrl.states["car"].not_absorbing_until is not None
    assert any(c.device == "car" and c.kind == "off" for _, c in house.log)


def test_sensor_hold_and_safe_state():
    """Grid sensor unavailable: hold (no command); after 10 min, safe state: the car stops
    (respecting T_on). A frozen sensor with sun is treated the same way"""
    ctrl = controller()
    house = House(ctrl, pv=4.0)
    house.on["car"] = True
    ctrl.states["car"] = DeviceState(on=True, level=house.car_level, t_on=T0 - 3600)
    out = house.run(T0, T0 + 300, net=None)
    assert out.mode == "hold" and house.log == []
    out = house.run(T0 + 300, T0 + 700, net=None)
    assert out.mode == "safe"
    assert [(c.device, c.kind) for _, c in house.log] == [("car", "off")]

    ctrl = controller()
    house = House(ctrl, pv=4.0)
    out = None
    for t in range(int(T0), int(T0 + 400), 30):
        out = ctrl.step(house.inputs(float(t), net=-2.0, production=4.0))
    assert out.mode == "hold"


def test_warmup_after_restart():
    ctrl = controller(GlobalParams(warmup=120))
    house = House(ctrl, pv=4.0)
    house.run(T0, T0 + 110)
    assert house.log == []
    house.run(T0 + 110, T0 + 400)
    assert house.log != []


def test_dry_run_publishes_without_registering():
    ctrl = controller()
    house = House(ctrl, pv=4.0)
    out = house.run(T0, T0 + 400, dry_run=True)
    assert [(c.device, c.kind) for c in out.commands] == [("car", "on")]  # published with its level
    assert all(s.pending is None and s.last_command is None for s in ctrl.states.values())
    assert ctrl.g.last_start is None


def test_restore_state():
    ctrl = controller()
    house = House(ctrl, pv=4.0)
    house.run(T0, T0 + 400)
    assert house.on["car"]
    saved = ctrl.export_state()
    t_on = ctrl.states["car"].t_on

    new = controller()
    new.restore_state(saved, T0 + 500, real_on={"car": True, "ac1": True, "ac2": False})
    assert new.states["car"].t_on == t_on  # same real state: timers restored
    assert new.states["ac1"].on and new.states["ac1"].t_on == T0 + 500  # changed while down
    assert new.states["car"].pending is None and new.states["car"].obj_on is None


def test_flicker_within_echo_window_is_ignored():
    """A cloud device that briefly shows its previous state after applying a command does not
    count as a stop/start"""
    sup = Supervisor(GlobalParams())
    ctrl = controller()
    st = ctrl.states["ac1"]
    cmd = Command("ac1", "on", 0.8)
    sup.register(cmd, st, ctrl.g, T0)
    house = House(ctrl)
    house.on["ac1"] = True
    ctrl.observe(house.inputs(T0 + 30), __import__("custom_components.solar_optimizer.controller_core", fromlist=["Output"]).Output())
    house.on["ac1"] = False  # flicker
    ctrl.observe(house.inputs(T0 + 40), __import__("custom_components.solar_optimizer.controller_core", fromlist=["Output"]).Output())
    assert st.on and st.t_off is None and st.starts == [T0 + 30]


def _cloud_timeline(kind, reports, until=T0 + 180):
    """The cycle sends `kind` to ac1 at T0 and air_cloud reports `reports` [(t, on), ...]. As in
    Home Assistant: the fast path runs on every grid measure (every 10 s, from T0 + 6) and the cycle
    every 30 s. Dry run around it, so the only command is this one."""
    ctrl = controller()
    st = ctrl.states["ac1"]
    initial = kind == "off"

    def inputs(t):
        on = initial
        for tt, v in reports:
            if t >= tt:
                on = v
        devices = {"car": DeviceInput(available=False), "ac1": DeviceInput(on=on, available=kind == "on"), "ac2": DeviceInput(available=False)}
        return Inputs(now=t, net=-1.5 + 0.001 * (t % 7), production=1.9 + 0.001 * (t % 5), price_import=PEAK, price_export=SELL, offpeak=False, limit=4.14, alphas={1: 0.0, 2: 1.0}, devices=devices)

    for t in range(int(T0 - 1800), int(T0), 30):
        ctrl.step(inputs(float(t)), dry_run=True)
    starts_before = list(st.starts)
    ctrl.sup.register(Command("ac1", kind, 0.8 if kind == "on" else 0.0, "test"), st, ctrl.g, T0)
    events = [(T0 + 6 + 10 * k, "fast") for k in range(int((until - T0) // 10))] + [(T0 + 30 * k, "step") for k in range(1, int((until - T0) // 30) + 1)]
    for t, path in sorted(events):
        inp = inputs(float(t))
        if path == "step":
            ctrl.step(inp, dry_run=True)
        else:
            ctrl.fast(inp, dry_run=True)
    return ctrl, st, starts_before


@pytest.mark.parametrize(
    "off_after, back_after",
    [(8, 1), (8, 61), (7, 6), (8, 39), (8, 1)],
    ids=["comedor-18-10-11h14", "comedor-18-10-12h43", "comedor-19-10-17h20", "comedor-20-10-15h10", "raul-10-10-16h19"],
)
def test_cloud_flicker_after_the_verification_is_ignored(off_after, back_after):
    """air_cloud, ~8 s after showing the start, reports a stale "off" for 1-61 s (real cases of
    Oct 2026). The cycle verifies the command in between: the stale state must still be ignored for
    the whole echo window, not count as a stop and a second start."""
    on_at = T0 + 6
    ctrl, st, _ = _cloud_timeline("on", [(on_at, True), (on_at + off_after, False), (on_at + off_after + back_after, True)])
    assert st.on and st.t_off is None
    assert st.starts == [on_at] and st.t_on == on_at
    assert [d for _, d in ctrl.estimator._changes] == ["ac1"]  # one transition for the estimator


def test_cloud_flicker_after_a_stop_is_ignored():
    """The same after an OFF command: a stale "on" is not a start"""
    ctrl, st, starts_before = _cloud_timeline("off", [(T0 + 6, False), (T0 + 14, True), (T0 + 53, False)])
    assert not st.on and st.t_off == T0 + 6
    assert st.starts == starts_before


def test_real_change_after_the_echo_window_counts():
    """A stop seen after the echo window (90 s) is a real stop"""
    ctrl, st, _ = _cloud_timeline("on", [(T0 + 6, True), (T0 + 120, False)])
    assert not st.on and st.t_off == T0 + 120 and st.starts == [T0 + 6]


# ---------------------------------------------------------------------- held by the charge planner


def test_held_car_is_not_counted_and_follows_the_planner():
    """Held car: its power stays in R (the surplus for the ACs does not include it), the
    supervisor drives it to the planner's level after the confirmations, the optimiser does not
    plan it"""
    ctrl = controller()
    house = House(ctrl, base=0.4, pv=3.0)
    house.inputs_extra["car"] = {"held_level": 13 * 0.232}
    out = house.run(T0, T0 + 600)
    assert house.on["car"] and house.car_level == pytest.approx(13 * 0.232)
    assert out.status["car"].held and not out.status["car"].controllable
    assert "car" not in out.plan.levels
    # pv 3.0 - base 0.4 - car 3.016 < 0: no surplus left for the ACs
    assert not house.on["ac1"] and not house.on["ac2"]
    assert all(c.reason.startswith("plan de carga") for _, c in house.commands("car"))
    assert ctrl.g.violations == 0


def test_held_car_level_capped_by_contracted_power():
    ctrl = controller()
    house = House(ctrl, base=2.0, pv=0.0)
    house.inputs_extra["car"] = {"held_level": 32 * 0.232}
    house.run(T0, T0 + 600)
    assert house.on["car"]
    assert 2.0 + house.car_level <= 4.14 + 0.05


def test_held_car_released_back_to_optimiser():
    ctrl = controller()
    house = House(ctrl, base=0.4, pv=0.0)
    house.inputs_extra["car"] = {"held_level": 6 * 0.232}
    house.run(T0, T0 + 300)
    assert house.on["car"]
    house.inputs_extra["car"] = {"held_level": None}
    out = house.run(T0 + 300, T0 + 1500)
    assert out.status["car"].controllable and not out.status["car"].held
    assert not house.on["car"]  # no sun: the optimiser stops it (after min_on)


def test_manual_car_is_not_driven_by_the_planner():
    ctrl = controller()
    house = House(ctrl, base=0.4, pv=0.0)
    house.inputs_extra["car"] = {"held_level": 6 * 0.232, "manual": True}
    out = house.run(T0, T0 + 600)
    assert not house.on["car"] and not out.status["car"].held


def test_surplus_filter_resets_when_the_controllable_set_changes():
    """The car goes manual while charging: its power must stop counting as surplus at once, not
    after the 2 min window has forgotten the old samples"""
    ctrl = controller()
    house = House(ctrl, base=0.4, pv=5.0)
    house.on["car"] = True
    out = house.run(T0, T0 + 120)
    assert out.surplus == pytest.approx(4.6, abs=0.2)
    house.inputs_extra["car"] = {"manual": True}
    out = house.run(T0 + 120, T0 + 130)
    # now R includes the car (pv 5.0 - base 0.4 - the car's power); without the reset it stays ~4.4
    assert out.surplus < 1.0


def _car_plugs_in_at_night(self_acting, manual=False):
    """No sun; the charger turns itself on when the car is plugged in; returns the seconds until we
    stop it (None: never) and the commands sent to the car"""
    ctrl = controller()
    house = House(ctrl, base=0.4, pv=0.0)
    house.run(T0, T0 + 600)
    house.on["car"] = True
    house.inputs_extra["car"] = {"self_acting": self_acting, "manual": manual}
    house.run(T0 + 600, T0 + 630)
    house.inputs_extra["car"] = {"manual": manual}
    house.run(T0 + 630, T0 + 1500)
    cmds = [(t, c) for t, c in house.log if c.device == "car"]
    offs = [t for t, c in cmds if c.kind == "off"]
    return ((offs[0] - (T0 + 600)) if offs else None), cmds


def test_charger_started_by_itself_on_plugging_can_be_stopped_at_once():
    secs, _ = _car_plugs_in_at_night(True)
    assert secs is not None and secs < 120


def test_charger_started_by_itself_does_not_count_as_one_of_our_starts():
    ctrl = controller()
    house = House(ctrl, base=0.4, pv=0.0)
    house.run(T0, T0 + 600)
    house.on["car"] = True
    house.inputs_extra["car"] = {"self_acting": True}
    house.run(T0 + 600, T0 + 630)
    assert ctrl.states["car"].starts == [] and ctrl.states["car"].t_on is None


def test_unexplained_start_keeps_the_minimum_on_time():
    """Without the device's own-doing flag nothing changes: the minimum on-time still applies"""
    secs, _ = _car_plugs_in_at_night(False)
    assert secs is None or secs >= 600


def test_manual_car_is_left_alone_when_it_starts_by_itself():
    _, cmds = _car_plugs_in_at_night(True, manual=True)
    assert cmds == []


def test_our_own_start_keeps_the_minimum_on_time_even_if_the_flag_is_on():
    ctrl = controller()
    house = House(ctrl, base=0.4, pv=5.0)
    house.inputs_extra["car"] = {"self_acting": True}
    house.run(T0, T0 + 300)
    ons = [t for t, c in house.log if c.device == "car" and c.kind == "on"]
    assert ons, "the car should start with the sun"
    house.pv = 0.0
    house.run(T0 + 300, T0 + 2400)
    offs = [t for t, c in house.log if c.device == "car" and c.kind == "off"]
    assert offs and offs[0] - ons[0] >= 600


@pytest.mark.parametrize("panel, expected", [({}, "ac1"), ({"ac2": {"value": 0.12}}, "ac2"), ({"ac1": {"value": 0.05}}, "ac2")])
def test_panel_value_overrides_the_configured_one(panel, expected):
    """Surplus for one air conditioner only: the value set in the panel decides which one (ac1 is
    configured at 0.09 and ac2 at 0.08)"""
    ctrl = controller()
    house = House(ctrl, base=0.4, pv=1.4)
    house.inputs_extra = {"car": {"available": False}, **panel}
    house.run(T0, T0 + 300)
    assert {c.device for _, c in house.commands(kind="on")} == {expected}
