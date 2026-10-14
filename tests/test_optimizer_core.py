"""Mandatory cases of the enumeration + water-filling optimiser (pure, no Home Assistant)"""

import itertools
import random

import pytest

from custom_components.solar_optimizer.optimizer_core import Consumer, Group, solve_group, water_fill

PEAK, OFFPEAK, SELL = 0.26371, 0.1349, 0.06
CAR_LEVELS = (0.0,) + tuple(round(0.232 * k, 3) for k in range(6, 17))  # k_max = 16 >= 10
AC_LEVELS = (0.0, 0.8)
H = 10.0  # no contracted-power limit in these cases


def consumers(current=None):
    current = current or {}
    cars = [Consumer("car", 1, CAR_LEVELS, 0.13, current.get("car", 0.0))]
    acs = [Consumer(f"ac{i}", 2, AC_LEVELS, 0.08, current.get(f"ac{i}", 0.0)) for i in range(4)]
    return cars + acs


def groups(alpha_car):
    return [Group(1, alpha_car), Group(2, round(1 - alpha_car, 6))]


def n_acs(plan):
    return sum(1 for k, v in plan.levels.items() if k.startswith("ac") and v > 0)


def test_peak_3kw_shared():
    """Punta, Ê = 3.0 kW, α 0.6/0.4 -> car 9 A (2.088 kW) + 1 AC; imports 0; exports 0.112 kW"""
    plan = water_fill(consumers(), groups(0.6), 3.0, H, PEAK, SELL)
    assert plan.levels["car"] == pytest.approx(2.088)
    assert n_acs(plan) == 1
    assert plan.imported == pytest.approx(0.0, abs=1e-9)
    assert plan.exported == pytest.approx(0.112)


def test_peak_2kw_car_first():
    """Punta, Ê = 2.0 kW, α 1/0 -> car 8 A (1.856 kW); no AC; exports 0.144 kW"""
    plan = water_fill(consumers(), groups(1.0), 2.0, H, PEAK, SELL)
    assert plan.levels["car"] == pytest.approx(1.856)
    assert n_acs(plan) == 0
    assert plan.exported == pytest.approx(0.144)
    assert plan.imported == pytest.approx(0.0, abs=1e-9)


def test_peak_1kw_car_imports():
    """Punta, Ê = 1.0 kW, α 1/0 -> car 6 A importing 0.392 kW; no AC"""
    plan = water_fill(consumers(), groups(1.0), 1.0, H, PEAK, SELL)
    assert plan.levels["car"] == pytest.approx(1.392)
    assert plan.imported == pytest.approx(0.392)
    assert n_acs(plan) == 0


def test_peak_0_9kw_one_ac():
    """Punta, Ê = 0.9 kW, α 1/0 -> car stopped; 1 AC; exports 0.1 kW"""
    plan = water_fill(consumers(), groups(1.0), 0.9, H, PEAK, SELL)
    assert plan.levels["car"] == 0
    assert n_acs(plan) == 1
    assert plan.exported == pytest.approx(0.1)
    assert plan.imported == pytest.approx(0.0, abs=1e-9)


def test_offpeak_threshold():
    """Valle, Ê = 0.08 kW -> nothing; Ê = 0.10 kW -> car 6 A importing 1.292 kW"""
    plan = water_fill(consumers(), groups(1.0), 0.08, H, OFFPEAK, SELL)
    assert all(v == 0 for v in plan.levels.values())
    plan = water_fill(consumers(), groups(1.0), 0.10, H, OFFPEAK, SELL)
    assert plan.levels["car"] == pytest.approx(1.392)
    assert plan.imported == pytest.approx(1.292)
    assert n_acs(plan) == 0


@pytest.mark.parametrize("price", [PEAK, OFFPEAK])
@pytest.mark.parametrize("surplus", [0.0, -0.5, -3.0])
@pytest.mark.parametrize("alpha", [1.0, 0.6, 0.0])
def test_no_surplus_nothing_on(price, surplus, alpha):
    """Ê <= 0 in any period -> nothing on"""
    plan = water_fill(consumers(), groups(alpha), max(0.0, surplus), H, price, SELL)
    assert all(v == 0 for v in plan.levels.values())
    assert plan.imported == 0 and plan.exported == 0


def test_tie_keeps_current_plan():
    """Two plans of equal value: the current one is kept"""
    acs = [Consumer(f"ac{i}", 2, AC_LEVELS, 0.08, 0.8 if i == 2 else 0.0) for i in range(4)]
    res = solve_group(acs, 0.9, H, PEAK, SELL)
    assert res.plan == {"ac0": 0.0, "ac1": 0.0, "ac2": 0.8, "ac3": 0.0}
    acs = [Consumer(f"ac{i}", 2, AC_LEVELS, 0.08, 0.8 if i == 3 else 0.0) for i in range(4)]
    res = solve_group(acs, 0.9, H, PEAK, SELL)
    assert res.plan["ac3"] == 0.8 and sum(res.plan.values()) == pytest.approx(0.8)


def test_tie_without_current_prefers_largest_total():
    """Equal value and no current plan among them: the largest total power"""
    # With v = p_exp, using surplus is worth exactly as much as selling it: J = 0 for all the
    # plans within the budget -> the largest one that does not import
    # (the current levels are not admissible any more, so the current plan is not a candidate)
    a = Consumer("a", 1, (0.0, 0.5), SELL, current=0.7)
    b = Consumer("b", 1, (0.0, 0.3), SELL, current=0.7)
    res = solve_group([a, b], 1.0, H, PEAK, SELL)
    assert res.plan == {"a": 0.5, "b": 0.3}


def test_import_margin_and_group_cap():
    """The import is limited by the margin H and by the cap of the group"""
    res = solve_group([Consumer("car", 1, CAR_LEVELS, 0.13)], 0.10, 0.5, OFFPEAK, SELL)
    assert res.plan["car"] == 0  # 6 A would import 1.292 kW > 0.5
    res = solve_group([Consumer("car", 1, CAR_LEVELS, 0.13)], 0.10, 10, OFFPEAK, SELL, cap=0.2)
    assert res.plan["car"] == 0
    plan = water_fill(consumers(), [Group(1, 1.0, 0.2), Group(2, 0.0)], 1.0, H, PEAK, SELL)
    assert plan.levels["car"] == 0  # would import 0.392 > cap 0.2


def test_supervisor_restrictions_are_respected():
    """Only the levels allowed by the supervisor are used; with no feasible plan, least import"""
    car = Consumer("car", 1, tuple(x for x in CAR_LEVELS if x > 0), 0.13, 1.392)  # cannot stop yet
    res = solve_group([car], 0.0, 0.5, PEAK, SELL)
    assert res.plan["car"] == pytest.approx(1.392)  # least import among the allowed levels
    car = Consumer("car", 1, (0.0,), 0.13)  # cannot start
    plan = water_fill([car], [Group(1, 1.0)], 5.0, H, PEAK, SELL)
    assert plan.levels["car"] == 0 and plan.exported == pytest.approx(5.0)


def test_enumeration_is_exhaustive():
    """The chosen plan is the best of all the plans (brute force check on random cases)"""
    rnd = random.Random(1)
    for _ in range(200):
        cs = [
            Consumer(f"c{i}", 1, (0.0,) + tuple(sorted({round(rnd.uniform(0.2, 3), 3) for _ in range(rnd.randint(1, 3))})), rnd.uniform(0.07, 0.13))
            for i in range(rnd.randint(1, 4))
        ]
        b, h, p = rnd.uniform(0, 5), rnd.uniform(0, 4), rnd.choice((PEAK, OFFPEAK))
        res = solve_group(cs, b, h, p, SELL)
        best = max(
            (sum(c.value * x for c, x in zip(cs, xs)) - SELL * min(sum(xs), b) - p * max(0, sum(xs) - b))
            for xs in itertools.product(*(c.levels for c in cs))
            if max(0, sum(xs) - b) <= h + 1e-9
        )
        assert res.objective == pytest.approx(best)


def test_water_filling_terminates_and_balances():
    """Water-filling: the surplus is either used or exported, never lost or duplicated"""
    rnd = random.Random(2)
    for _ in range(100):
        e = rnd.uniform(0, 6)
        plan = water_fill(consumers(), groups(rnd.choice((1.0, 0.6, 0.3, 0.0))), e, rnd.uniform(0, 4), rnd.choice((PEAK, OFFPEAK)), SELL)
        used = sum(r.used for r in plan.groups.values())
        assert used + plan.exported == pytest.approx(e)
        assert sum(plan.levels.values()) == pytest.approx(used + plan.imported)
        assert plan.passes <= 10


def test_real_charger_steps():
    """With a charger that only accepts 6, 8, 10, 13, 16, 20, 24, 32 A, the first case gives
    10 A + 1 AC importing 0.12 kW (9 A does not exist)"""
    steps = (0.0,) + tuple(round(0.232 * a, 3) for a in (6, 8, 10, 13, 16, 20, 24, 32))
    cs = [Consumer("car", 1, steps, 0.13)] + [Consumer(f"ac{i}", 2, AC_LEVELS, 0.08) for i in range(4)]
    plan = water_fill(cs, groups(0.6), 3.0, H, PEAK, SELL)
    assert plan.levels["car"] == pytest.approx(2.32)
    assert n_acs(plan) == 1
    assert plan.imported == pytest.approx(0.12)


def test_hysteresis_keeps_current_plan_for_small_gains():
    """With hysteresis, the current plan is kept unless the best one is worth more than it"""
    car = Consumer("car", 1, CAR_LEVELS, 0.13, current=round(0.232 * 8, 3))
    # 2.1 kW of surplus at peak: 9 A (2.088) is best, 8 A (1.856) is current
    best = solve_group([car], 2.1, H, PEAK, SELL)
    assert best.plan["car"] == pytest.approx(2.088)
    kept = solve_group([car], 2.1, H, PEAK, SELL, hysteresis=0.04)
    assert kept.plan["car"] == pytest.approx(1.856)
    # a large gain still changes the plan
    moved = solve_group([car], 3.5, H, PEAK, SELL, hysteresis=0.04)
    assert moved.plan["car"] > 3.0
    # an infeasible current plan (over the import margin) is never kept
    over = Consumer("car", 1, CAR_LEVELS, 0.13, current=round(0.232 * 16, 3))
    assert solve_group([over], 0.0, 0.5, PEAK, SELL, hysteresis=10).plan["car"] == 0
