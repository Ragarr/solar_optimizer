"""Economic optimiser: exhaustive enumeration per group and water-filling between groups.

Pure Python (no Home Assistant import): used by the integration, the tests and the offline
simulator. Powers are in kW and prices in €/kWh.

Model
-----
The controllable consumers are split in groups ordered by priority (group 1 first). Each consumer
``i`` has a set of admissible levels ``L_i`` (kW, 0 = off), already restricted by the supervisor
(minimal on/off times...), a value ``v_i`` of 1 kWh delivered to it, and its current level.

For a group ``g`` with a surplus budget ``b`` and an import margin ``h``, every plan ``x`` of the
cartesian product of the levels is evaluated::

    S = sum(x_i);  u = min(S, b);  m = max(0, S - b);  r = b - u
    J = sum(v_i * x_i) - p_exp * u - p_imp * m          (subject to m <= min(h, cap_g))

``u`` is the surplus used (it would otherwise be sold at ``p_exp``), ``m`` the energy imported at
``p_imp`` and ``r`` the surplus left. The best plan maximises ``J``; ties keep the current plan,
then prefer the plan with the largest ``S``.

Groups share the surplus with the water-filling of ``water_fill``: each group first receives its
share ``alpha_g * E+``; what a group does not use is then offered to the groups in priority order
until no plan changes.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field

EPS = 1e-9


@dataclass(frozen=True)
class Consumer:
    """A controllable consumer as seen by the optimiser"""

    id: str
    group: int
    levels: tuple[float, ...]  # admissible levels in kW (L_i^ef); 0 = off
    value: float  # €/kWh delivered to this consumer (v_i for the current tariff period)
    current: float = 0.0  # current level in kW (tie-break: keep the current plan)


@dataclass(frozen=True)
class Group:
    """A priority group"""

    id: int
    alpha: float  # share of the surplus (sum of the alphas = 1)
    import_cap: float | None = None  # optional maximal import of the group (kW)


@dataclass
class GroupResult:
    """Best plan of a group for a budget and an import margin"""

    plan: dict[str, float]
    total: float  # S_g
    used: float  # u: surplus used
    imported: float  # m
    left: float  # r: surplus left
    objective: float  # J_g


@dataclass
class Plan:
    """Result of the optimisation"""

    levels: dict[str, float] = field(default_factory=dict)  # consumer -> level (kW)
    imported: float = 0.0  # import caused by the plan (kW)
    exported: float = 0.0  # surplus left, exported (kW)
    objective: float = 0.0  # sum of J_g (€/h)
    groups: dict[int, GroupResult] = field(default_factory=dict)
    passes: int = 0


def _evaluate(plan_levels, values, b, p_imp, p_exp):
    total = sum(plan_levels)
    used = min(total, b)
    imported = max(0.0, total - b)
    objective = sum(v * x for v, x in zip(values, plan_levels)) - p_exp * used - p_imp * imported
    return total, used, imported, b - used, objective


def solve_group(
    consumers: list[Consumer], budget: float, margin: float, p_imp: float, p_exp: float, cap: float | None = None, hysteresis: float = 0.0
) -> GroupResult:
    """Best plan of a group by exhaustive enumeration (F_g(b, h)).

    ``budget`` (b) is the surplus offered to the group, ``margin`` (h) the import still allowed by
    the contracted power and ``cap`` the optional import cap of the group. When no plan respects the
    import limit (the supervisor can forbid switching a consumer off), the plan that imports the
    least is chosen. ``hysteresis`` (€/h): the current plan is kept unless the best one is worth
    more than this over it (avoids changing level for a negligible gain when the surplus hovers
    around a threshold)."""
    budget = max(0.0, budget)
    limit = margin if cap is None else min(margin, cap)
    if not consumers:
        return GroupResult({}, 0.0, 0.0, 0.0, budget, 0.0)
    ids = [c.id for c in consumers]
    values = [c.value for c in consumers]
    current = tuple(c.current for c in consumers)

    best = None  # (feasible, objective, is_current, total, -imported) ordering key
    best_levels = None
    best_eval = None
    current_eval = None
    for levels in itertools.product(*(c.levels for c in consumers)):
        total, used, imported, left, objective = _evaluate(levels, values, budget, p_imp, p_exp)
        feasible = imported <= limit + EPS
        if feasible and _same(levels, current):
            current_eval = (total, used, imported, left, objective)
        if feasible:
            key = (1, objective, 0.0, 0.0)
        else:
            # Not feasible: least import first, then the objective
            key = (0, -imported, objective, 0.0)
        if best is not None and not _better(key, best, levels, best_levels, current, total, best_eval[0]):
            continue
        best, best_levels, best_eval = key, levels, (total, used, imported, left, objective)
    if hysteresis > 0 and current_eval is not None and best[0] == 1 and best_eval[4] - current_eval[4] <= hysteresis + EPS:
        best_levels, best_eval = current, current_eval
    total, used, imported, left, objective = best_eval
    return GroupResult(dict(zip(ids, best_levels)), total, used, imported, left, objective)


def _close(a, b):
    return math.isclose(a, b, rel_tol=0.0, abs_tol=EPS)


def _better(key, best, levels, best_levels, current, total, best_total) -> bool:
    """Is the candidate (key, levels) strictly better than the best so far?"""
    if key[0] != best[0]:
        return key[0] > best[0]
    for a, b in zip(key[1:3], best[1:3]):
        if not _close(a, b):
            return a > b
    # Tie: the current plan first, then the largest total power
    is_current = _same(levels, current)
    best_is_current = _same(best_levels, current)
    if is_current != best_is_current:
        return is_current
    if not _close(total, best_total):
        return total > best_total
    return False


def _same(levels, current) -> bool:
    return all(_close(a, b) for a, b in zip(levels, current))


def water_fill(
    consumers: list[Consumer],
    groups: list[Group],
    surplus: float,
    margin: float,
    p_imp: float,
    p_exp: float,
    max_passes: int = 10,
    hysteresis: float = 0.0,
) -> Plan:
    """Share the surplus between the groups (water-filling).

    ``surplus`` is E+ (kW, >= 0), ``margin`` the import margin H up to the contracted power.

    Phase 1: each group g, in priority order, gets b_g = alpha_g * E+ and the import margin left
    by the groups already solved.
    Phase 2: the surplus left by all the groups is offered again to the groups in priority order
    (each one keeps what it uses and passes on the rest) until no plan changes, at most
    ``max_passes`` passes."""
    surplus = max(0.0, surplus)
    by_group: dict[int, list[Consumer]] = {}
    for c in consumers:
        by_group.setdefault(c.group, []).append(c)
    group_ids = sorted({g.id for g in groups} | set(by_group))
    alphas = {g.id: max(0.0, g.alpha) for g in groups}
    total_alpha = sum(alphas.get(g, 0.0) for g in group_ids)
    if total_alpha <= EPS:
        # No share configured: everything to the first group
        alphas = {g: (1.0 if i == 0 else 0.0) for i, g in enumerate(group_ids)}
        total_alpha = 1.0
    caps = {g.id: g.import_cap for g in groups}

    results: dict[int, GroupResult] = {}

    def margin_for(gid):
        return max(0.0, margin - sum(r.imported for g, r in results.items() if g != gid))

    # Phase 1
    for gid in group_ids:
        budget = surplus * alphas.get(gid, 0.0) / total_alpha
        results[gid] = solve_group(by_group.get(gid, []), budget, margin_for(gid), p_imp, p_exp, caps.get(gid), hysteresis)

    # Phase 2
    passes = 0
    for passes in range(1, max_passes + 1):
        left = sum(r.left for r in results.values())
        budgets = {gid: r.used for gid, r in results.items()}
        changed = False
        for gid in group_ids:
            budget = budgets[gid] + left
            res = solve_group(by_group.get(gid, []), budget, margin_for(gid), p_imp, p_exp, caps.get(gid), hysteresis)
            if any(not _close(res.plan[k], results[gid].plan[k]) for k in res.plan):
                changed = True
            results[gid] = res
            left = res.left
            # The group keeps what it uses; what it leaves is offered to the next one. The
            # leftover is then held by this group until the next pass.
            res.left = 0.0
        # The final leftover (after the last group) is attributed to the last group
        results[group_ids[-1]].left = left if group_ids else 0.0
        if not changed:
            break

    plan = Plan(passes=passes)
    for gid in group_ids:
        r = results[gid]
        plan.levels.update(r.plan)
        plan.imported += r.imported
        plan.objective += r.objective
        plan.groups[gid] = r
    plan.exported = max(0.0, surplus - sum(r.used for r in results.values()))
    return plan
