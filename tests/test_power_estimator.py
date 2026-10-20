"""Power of the on/off consumers estimated from the steps of the house consumption, and the reason
of each decision (pure, simulated time)"""

from dataclasses import replace

import pytest

from custom_components.solar_optimizer.controller_core import Controller, DeviceConfig, DeviceInput, Inputs
from custom_components.solar_optimizer.power_estimator import PowerEstimator, Rejected, Step, window_mean
from custom_components.solar_optimizer.supervisor_core import DeviceState, GlobalParams

from .test_supervisor_core import AC, CAR, House, T0


def feed(est, t0, t1, house, pv=2.0, step=10):
    """Samples of a constant house consumption (callable of t) with a constant production"""
    for t in range(int(t0), int(t1), step):
        est.add(float(t), house(t) - pv, pv)


# ---------------------------------------------------------------------- estimator alone


def test_window_mean_holds_each_value():
    samples = [(0.0, 1.0), (10.0, 3.0)]
    assert window_mean(samples, 0, 20) == pytest.approx(2.0)
    assert window_mean(samples, 5, 10) == pytest.approx(1.0)
    assert window_mean(samples, -1, 10) is None  # not covered


@pytest.mark.parametrize("kw", [0.4, 1.2])
def test_start_step(kw):
    est = PowerEstimator()
    t_cmd, t_seen = T0, T0 + 40  # the cloud reports the start 40 s after the command
    feed(est, T0 - 600, T0 + 400, lambda t: 0.5 + (kw if t >= t_cmd + 20 else 0.0))
    est.transition("ac1", "on", t_seen, t_cmd)
    assert est.evaluate(t_seen + 299) == []  # the "after" window is not complete yet
    (res,) = est.evaluate(t_seen + 300)
    assert isinstance(res, Step) and res.kind == "on" and res.kw == pytest.approx(kw)


def test_stop_step_without_a_command_assumes_the_cloud_lag():
    est = PowerEstimator()
    t_seen = T0 + 50
    feed(est, T0 - 600, T0 + 400, lambda t: 0.5 + (0.0 if t >= T0 else 0.7))
    est.transition("ac1", "off", t_seen)
    (res,) = est.evaluate(t_seen + 300)
    assert isinstance(res, Step) and res.kw == pytest.approx(0.7)


def test_rejected_when_the_production_moves():
    est = PowerEstimator()
    for t in range(int(T0 - 600), int(T0 + 400), 10):
        pv = 2.0 if t < T0 else 1.5  # a cloud
        est.add(float(t), 0.5 + (0.8 if t >= T0 else 0.0) - pv, pv)
    est.transition("ac1", "on", T0, T0)
    (res,) = est.evaluate(T0 + 300)
    assert isinstance(res, Rejected) and "producción" in res.why


def test_rejected_when_another_consumer_changes():
    est = PowerEstimator()
    feed(est, T0 - 600, T0 + 400, lambda t: 0.5 + (0.8 if t >= T0 else 0.0))
    est.transition("ac1", "on", T0, T0)
    est.change("car", T0 + 200)
    (res,) = est.evaluate(T0 + 300)
    assert isinstance(res, Rejected) and "car" in res.why


def test_rejected_out_of_range_and_when_it_flickers():
    est = PowerEstimator()
    feed(est, T0 - 600, T0 + 310, lambda t: 0.5)
    est.transition("ac1", "on", T0, T0)
    (res,) = est.evaluate(T0 + 300)
    assert isinstance(res, Rejected) and "fuera de rango" in res.why
    # on and off again before the first step is evaluated: only the last change counts
    feed(est, T0 + 310, T0 + 760, lambda t: 0.5)
    est.transition("ac1", "on", T0 + 400, T0 + 400)
    est.transition("ac1", "off", T0 + 450)
    (res,) = est.evaluate(T0 + 750)
    assert isinstance(res, Rejected) and res.kind == "off"


def test_moving_average():
    est = PowerEstimator()
    assert est.update(None, 1.0) == 1.0
    assert est.update(1.0, 0.0) == pytest.approx(0.7)


# ---------------------------------------------------------------------- in the control loop


class AcHouse(House):
    """The air conditioners draw `ac_kw`, not the 0.8 kW nominal"""

    def __init__(self, ctrl, ac_kw, **kw):
        super().__init__(ctrl, **kw)
        self.ac_kw = ac_kw

    def power(self, name):
        if name != "car" and self.on[name]:
            return self.ac_kw
        return super().power(name)


def controller(use_estimated=False):
    ac = replace(AC, use_estimated=use_estimated)
    return Controller([DeviceConfig("car", 1, CAR, 0.13, is_car=True), DeviceConfig("ac1", 2, ac, 0.09), DeviceConfig("ac2", 2, ac, 0.08)], GlobalParams(warmup=0))


def start_ac1(ctrl, ac_kw=0.4):
    """10 min of measures with ac1 not asking, then it asks and starts"""
    house = AcHouse(ctrl, ac_kw, base=0.3, pv=1.5)
    house.inputs_extra = {"car": {"available": False}, "ac1": {"available": False}, "ac2": {"available": False}}
    house.run(T0 - 600, T0)
    del house.inputs_extra["ac1"]
    house.run(T0, T0 + 1200)
    return house


def test_estimate_learned_in_the_loop_shadow_by_default():
    ctrl = controller()
    house = start_ac1(ctrl)
    assert house.commands("ac1", "on")
    st = ctrl.states["ac1"]
    assert st.est_steps == 1 and st.est_on == pytest.approx(0.4, abs=0.02)
    # shadow: the surplus still counts the nominal 0.8 kW
    out = house.run(T0 + 1200, T0 + 1230)
    assert out.surplus_inst == pytest.approx(0.8 - (0.3 + 0.4 - 1.5), abs=0.02)


def test_estimate_used_when_configured():
    ctrl = controller(use_estimated=True)
    house = start_ac1(ctrl)
    out =house.run(T0 + 1200, T0 + 1230)
    assert out.surplus_inst == pytest.approx(1.5 - 0.3, abs=0.03)
    assert out.plan.levels["ac1"] == pytest.approx(0.4, abs=0.02)
    # the supervisor keeps the nominal level: no command because of the estimate
    assert all(c.kind != "level" for _, c in house.commands("ac1"))


def test_estimate_persisted():
    st = DeviceState(est_on=0.5, est_off=0.3, est_steps=4)
    back = DeviceState.from_dict(st.to_dict())
    assert back.est == pytest.approx(0.4) and back.est_steps == 4


def test_unreachable_device_spoils_the_steps():
    ctrl = controller()
    house = AcHouse(ctrl, 0.4, base=0.3, pv=1.5)
    house.inputs_extra = {"car": {"available": False}, "ac1": {"available": False}, "ac2": {"reachable": False}}
    house.run(T0 - 600, T0)
    del house.inputs_extra["ac1"]
    house.run(T0, T0 + 30)
    house.inputs_extra["ac2"] = {"reachable": True, "available": False}
    house.run(T0 + 30, T0 + 600)
    assert house.commands("ac1", "on")
    assert ctrl.states["ac1"].est_steps == 0  # ac2 came back inside the windows of ac1's start
    # without that, the same start is accepted
    ctrl2 = controller()
    start_ac1(ctrl2)
    assert ctrl2.states["ac1"].est_steps == 1


# ---------------------------------------------------------------------- reasons


def reasons(out):
    return {n: s.reason_key for n, s in out.status.items()}


def test_reasons():
    ctrl = controller()
    house = AcHouse(ctrl, 0.8, base=0.3, pv=0.5)
    house.inputs_extra = {"car": {"available": False}, "ac2": {"manual": True}}
    out = house.run(T0, T0 + 30)
    assert reasons(out) == {"car": "no disponible", "ac1": "excedente", "ac2": "manual"}
    house.pv = 1.6
    out = house.run(T0 + 30, T0 + 90)
    assert reasons(out)["ac1"] == "confirmando arranque"
    out = house.run(T0 + 90, T0 + 400)
    assert reasons(out)["ac1"] == "en marcha"
    house.pv = 0.3
    out = house.run(T0 + 400, T0 + 460)
    assert reasons(out)["ac1"] == "en marcha"  # the minimum on time keeps it in the plan
    out = house.run(T0 + 460, T0 + 1200)
    assert reasons(out)["ac1"] == "confirmando parada (no compensa)"
    out = house.run(T0 + 1200, T0 + 1500)
    assert reasons(out)["ac1"] == "T_off"
    assert "puede arrancar en" in out.status["ac1"].reason


def test_device_unreachable_since_the_start_spoils_nothing():
    """The charger offline the whole time, also when the controller starts (a restart) inside the
    windows of ac1's start: the step is still valid"""
    ctrl = controller()
    house = AcHouse(ctrl, 0.4, base=0.3, pv=1.5)
    house.inputs_extra = {"car": {"reachable": False}, "ac1": {"available": False}, "ac2": {"available": False}}
    house.run(T0 - 60, T0)  # the controller starts 3 min before ac1's command (T0 + 120)
    del house.inputs_extra["ac1"]
    house.run(T0, T0 + 1200)
    ((t_on, _),) = house.commands("ac1", "on")
    assert t_on == T0 + 120
    assert ctrl.states["ac1"].est_steps == 1


def test_device_losing_the_connection_spoils_the_steps():
    ctrl = controller()
    house = AcHouse(ctrl, 0.4, base=0.3, pv=1.5)
    house.inputs_extra = {"car": {"available": False}, "ac1": {"available": False}, "ac2": {"available": False}}
    house.run(T0 - 600, T0)
    del house.inputs_extra["ac1"]
    house.run(T0, T0 + 200)
    house.inputs_extra["car"] = {"reachable": False}
    house.run(T0 + 200, T0 + 1200)
    assert house.commands("ac1", "on")
    assert ctrl.states["ac1"].est_steps == 0
