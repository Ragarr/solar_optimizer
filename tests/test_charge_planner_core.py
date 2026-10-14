"""Charge planner: exact DP against brute force, typical cases, holding window"""

# pylint: disable=protected-access
import itertools
import random
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.solar_optimizer.charge_planner_core import (
    ChargePlanner,
    PlannerInputs,
    PlannerParams,
    Slot,
    Target,
    is_offpeak,
    parse_forecast,
    slot_cost,
    solve,
)

TZ = ZoneInfo("Europe/Madrid")
LEVELS = (0.0, 1.39, 1.73, 2.18, 2.81, 3.46, 4.39, 5.3, 7.0)
P = PlannerParams()


def ts(s: str) -> float:
    return datetime.fromisoformat(s).replace(tzinfo=TZ).timestamp()


def brute(slots, levels, pe, limit, milestones, p, current=0.0):
    lv = tuple(sorted({0.0, *levels}))
    best = None
    for combo in itertools.product(lv, repeat=len(slots)):
        if any(x > 0 and s.net + x > limit + 1e-6 for s, x in zip(slots, combo)):
            continue
        c, prev, cum = 0.0, min(lv, key=lambda x: abs(x - current)), 0.0
        cums = []
        for s, x in zip(slots, combo):
            c += slot_cost(s, x, pe)
            if x != prev and x > 0:
                c += p.start_penalty if prev == 0 else p.change_penalty
            prev = x
            cum += round(x * s.hours / p.energy_step) * p.energy_step
            cums.append(cum)
        for idx, need in milestones:
            c += p.shortfall_penalty * max(0.0, need - min(cums[idx], max(n for _, n in milestones) + p.energy_step))
        if best is None or c < best[0] - 1e-9:
            best = (c, combo)
    return best


@pytest.mark.parametrize("seed", range(6))
def test_dp_matches_brute_force(seed):
    rnd = random.Random(seed)
    slots = []
    t = 0.0
    for _ in range(5):
        sun = rnd.choice([0.0, 0.5, 2.0, 4.0])
        slots.append(Slot(t, t + 3600, sun, 0.4, rnd.choice([0.1349, 0.26371]), True))
        t += 3600
    levels = (0.0, 1.4, 2.2, 3.5)
    need = rnd.choice([2.0, 5.0, 9.0])
    ms = [(4, need)]
    res = solve(slots, levels, 0.06, 4.14, ms, P)
    bf = brute(slots, levels, 0.06, 4.14, ms, P)
    # same objective (ties may pick another path)
    ours = sum(slot_cost(s, x, 0.06) for s, x in zip(slots, res.levels))
    prev = 0.0
    for x in res.levels:
        if x != prev and x > 0:
            ours += P.start_penalty if prev == 0 else P.change_penalty
        prev = x
    cum = sum(round(x * s.hours / P.energy_step) * P.energy_step for s, x in zip(slots, res.levels))
    ours += P.shortfall_penalty * max(0.0, need - cum)
    assert ours == pytest.approx(bf[0], abs=0.02)


def test_prefers_sun_then_offpeak():
    # 4 h: two of sun (4 kW), one peak night, one off-peak night
    slots = [
        Slot(0, 3600, 4.0, 0.4, 0.26371, False),
        Slot(3600, 7200, 4.0, 0.4, 0.26371, False),
        Slot(7200, 10800, 0.0, 0.4, 0.26371, False),
        Slot(10800, 14400, 0.0, 0.4, 0.1349, True),
    ]
    res = solve(slots, LEVELS, 0.06, 4.14, [(3, 9.0)], P)
    assert res.levels[2] == 0.0  # never the expensive grid
    assert sum(res.sun) == pytest.approx(min(9.0, sum(res.sun) + 0), abs=0.5) or sum(res.sun) > 5
    assert sum(res.grid[:3]) < 1.0
    assert res.shortfall == [pytest.approx(0.0, abs=0.11)]


def test_contracted_limit():
    slots = [Slot(0, 3600, 0.0, 1.0, 0.1349, True)]
    res = solve(slots, LEVELS, 0.06, 4.14, [(0, 30.0)], P)
    assert res.levels[0] <= 3.14 + 1e-6
    assert res.shortfall[0] > 20


def test_offpeak_window_winter_time():
    w = (22 * 60, 12 * 60)
    assert is_offpeak(ts("2026-12-01T23:00"), w)
    assert not is_offpeak(ts("2026-12-01T13:00"), w)
    # summer: the window moves one hour later
    assert is_offpeak(ts("2026-07-01T12:30"), w)
    assert not is_offpeak(ts("2026-07-01T22:30"), w)


def test_parse_forecast_end_of_period():
    fc = parse_forecast({"wh_period": {"2026-10-14T09:00:00+02:00": 1500}})
    assert list(fc.values()) == [1.5]
    assert datetime.fromtimestamp(next(iter(fc)), TZ).hour == 8


def _inputs(now, targets, soc=50.0, **kw):
    base = dict(
        now=now,
        tz=TZ,
        targets=targets,
        soc=soc,
        soc_time=now,
        car_kw=0.0,
        plugged=True,
        forecast={},
        forecast_time=now,
        levels_kw=LEVELS,
    )
    base.update(kw)
    return PlannerInputs(**base)


def test_night_plan_holds_and_uses_offpeak():
    now = ts("2026-12-01T20:00")
    pl = ChargePlanner()
    out = pl.plan(_inputs(now, [Target(ts("2026-12-02T06:00"), 80, "mié")]))
    assert out.held and out.state == "Activo"
    hours = out.attrs["horas"]
    # 20:00-22:00 is peak: nothing there
    assert hours[0][3] == 0 and hours[1][3] == 0
    assert out.attrs["red_prevista_kwh"] > 15
    assert out.level == 0.0  # waits for the off-peak window


def test_far_target_is_preview_only():
    now = ts("2026-12-01T08:00")
    pl = ChargePlanner()
    out = pl.plan(_inputs(now, [Target(ts("2026-12-05T06:00"), 90, "fecha 1")]))
    assert not out.held
    assert out.state == "Vista previa"
    assert out.attrs["coste_total_estimado"] > 0


def test_sun_only_plan_does_not_hold():
    now = ts("2026-07-01T09:00")
    fc = {ts(f"2026-07-01T{h:02d}:00"): 5.0 for h in range(9, 19)}
    pl = ChargePlanner()
    out = pl.plan(_inputs(now, [Target(ts("2026-07-01T20:00"), 60, "mié")], forecast=fc))
    assert not out.held
    assert out.attrs["red_prevista_kwh"] == 0


def test_reached_releases_and_unplugged_releases():
    now = ts("2026-12-01T20:00")
    t = [Target(ts("2026-12-02T06:00"), 80, "mié")]
    pl = ChargePlanner()
    assert pl.plan(_inputs(now, t)).held
    out = pl.plan(_inputs(now + 60, t, soc=85))
    assert not out.held and out.state == "Cumplido"
    pl = ChargePlanner()
    assert pl.plan(_inputs(now, t)).held
    assert not pl.plan(_inputs(now + 60, t, plugged=False)).held


def test_two_targets_both_met():
    now = ts("2026-12-01T20:00")
    t = [Target(ts("2026-12-02T04:00"), 100, "fecha 1"), Target(ts("2026-12-02T06:00"), 60, "mié")]
    out = ChargePlanner().plan(_inputs(now, t, soc=70))
    assert out.held
    assert out.attrs["horas"][-1][5] >= 99


def test_unreachable_best_effort():
    now = ts("2026-12-01T20:00")
    out = ChargePlanner().plan(_inputs(now, [Target(ts("2026-12-01T22:00"), 100, "fecha 1")], soc=20))
    assert out.state.startswith("No llega")
    assert out.held and out.level is not None and out.level > 3


def test_disabled_is_preview():
    now = ts("2026-12-01T20:00")
    out = ChargePlanner().plan(_inputs(now, [Target(ts("2026-12-02T06:00"), 80, "mié")], enabled=False))
    assert not out.held and out.state == "Vista previa"


def test_skip_next():
    now = ts("2026-12-01T20:00")
    t = [Target(ts("2026-12-02T06:00"), 80, "mié"), Target(ts("2026-12-03T06:00"), 80, "jue")]
    pl = ChargePlanner()
    pl.skip_next(t, now)
    out = pl.plan(_inputs(now, t))
    assert [o["origen"] for o in out.attrs["objetivos"]] == ["jue"]


def test_soc_estimation_and_learning():
    pl = ChargePlanner()
    now = ts("2026-12-01T01:00")
    pl.soc.update(P, now, 3.0, 50, now, 68.8)
    est = None
    for i in range(1, 61):  # one hour at 3 kW
        est = pl.soc.update(P, now + i * 60, 3.0, 50, now, 68.8)
    assert est == pytest.approx(50 + 3.0 / pl.soc.kwh_per_pct, abs=0.2)
    k0 = pl.soc.kwh_per_pct
    pl.soc.update(P, now + 3600, 3.0, 54, now + 3600, 68.8)  # reported +4 % for 3 kWh -> 0.75
    assert pl.soc.kwh_per_pct < k0


def test_dp_speed_7_days():
    import time

    now = ts("2026-12-01T08:00")
    t0 = time.perf_counter()
    ChargePlanner().plan(_inputs(now, [Target(ts("2026-12-08T06:00"), 100, "fecha 1")], soc=5))
    assert time.perf_counter() - t0 < 10


@pytest.mark.parametrize("sun_factor", [0.5, 1.0, 1.3])
def test_receding_horizon_reaches_target_with_forecast_error(sun_factor):
    """MPC over 22 h: replans every 5 min with the estimated SoC; the real sun is the forecast x
    sun_factor (the forecast is 60 % of it with confidence). The target is met and the expensive
    grid is only used when the sun failed a lot."""
    start = ts("2026-07-01T08:00")
    deadline = ts("2026-07-02T06:00")
    fc = {}
    for h in range(7, 21):
        fc[ts(f"2026-07-01T{h:02d}:00")] = 4.0 * max(0.0, 1 - abs(h + 0.5 - 14) / 7)
    pl = ChargePlanner()
    soc, k = 30.0, 0.764
    pl.soc.kwh_per_pct = k
    level, peak_kwh, t = 0.0, 0.0, start
    while t < deadline:
        if (t - start) % 300 == 0:
            real_sun = sun_factor * next((v for h, v in fc.items() if h <= t < h + 3600), 0.0)
            out = pl.plan(
                _inputs(t, [Target(deadline, 80, "jue")], soc=soc, soc_time=t, car_kw=level, forecast=fc, pv_kw=real_sun, nowcast_kw=0.4 - real_sun)
            )
            level = (out.level or 0.0) if out.held else max([x for x in LEVELS if x <= max(0.0, real_sun - 0.4)], default=0.0)
        real_sun = sun_factor * next((v for h, v in fc.items() if h <= t < h + 3600), 0.0)
        grid = max(0.0, 0.4 + level - real_sun) - max(0.0, 0.4 - real_sun)
        if not is_offpeak(t, (22 * 60, 12 * 60)):
            peak_kwh += grid * 60 / 3600
        soc = min(100.0, soc + level * 60 / 3600 / k)
        t += 60
    assert soc >= 79.5
    if sun_factor >= 1.0:
        assert peak_kwh < 1.0
