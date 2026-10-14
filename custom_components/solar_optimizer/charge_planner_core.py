"""Charge planner for the car: reach "X % at time t" at the lowest cost.

Pure Python + numpy, shared by Home Assistant, the tests and the simulator. Powers in kW, energies
in kWh measured at the charger (what the meter counts), prices in €/kWh, times in epoch seconds.

Model (one slot = 15 min, from now to the last target):
- decision: the charger level P(a_t) of the slot, one of the measured levels (0 included);
- the house without the car: n_t = base - sun_t (import positive). In the current slot the
  measured value replaces the forecast (nowcast);
- marginal cost of the car in the slot: the part of P covered by the sun costs the export price
  (it is not sold), the rest the import price of the slot;
- n_t + P <= limit (contracted power with margin): a level over it is forbidden;
- targets "SoC >= X_k at t_k": soft constraints, a shortfall costs ``shortfall_penalty`` per kWh
  (best effort when unreachable);
- switching: a penalty per start and per level change (the supervisor adds its minimal times).

The levels are discrete and the lowest one is far from 0 (6 A ~ 1.4 kW), so the problem is not
convex and a greedy fill is not optimal: exact dynamic programming (Bellman) on (energy charged,
previous level), forward, with backtracking. Receding horizon: only the level of the current slot is
applied, and the plan is computed again every few minutes with the new SoC and measures.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo

import numpy as np

HOUR = 3600.0
EPS = 1e-6
WINTER = timezone(timedelta(hours=1))  # the off-peak window of the tariff is in winter time


# ---------------------------------------------------------------------------- parameters


@dataclass(frozen=True)
class PlannerParams:
    slot: float = 900.0
    energy_step: float = 0.1  # kWh per DP bucket
    start_penalty: float = 0.05  # € per start
    change_penalty: float = 0.005  # € per level change
    shortfall_penalty: float = 1.0  # € per kWh missing at a target
    margin: float = 0.05  # the energy missing is planned with this margin ...
    margin_penalty: float = 0.2  # ... a soft one: worth the sun or the off-peak grid, not the peak
    early: float = 1800.0  # aim this long before each target
    horizon: float = 7 * 86400.0  # targets further away are ignored
    lead: float = 18 * HOUR  # the car is held from this long before a target ...
    lead_factor: float = 1.5  # ... or earlier: hours needed at full power x this
    forecast_max_age: float = 6 * HOUR
    sun_learn_min_kwh: float = 0.5  # forecast already elapsed today to correct it with the real sun
    kwh_per_pct_min: float = 0.55
    kwh_per_pct_max: float = 1.1
    kwh_per_pct_alpha: float = 0.3
    soc_learn_min_pct: float = 3.0
    efficiency: float = 0.9  # meter kWh reaching the battery (starting point of the learning)
    publish_hours: int = 7 * 24


# ---------------------------------------------------------------------------- inputs / outputs


@dataclass(frozen=True)
class Target:
    t: float
    pct: float
    source: str  # "lun", "mar", ... or "fecha 1", ...


@dataclass
class PlannerInputs:
    now: float
    tz: tzinfo
    targets: list[Target]
    soc: float | None  # SoC reported by the car (%)
    soc_time: float | None  # when the car measured it
    car_kw: float | None  # measured power of the charger
    plugged: bool | None  # None: unknown
    manual: bool = False  # the car is in manual mode in the optimiser
    enabled: bool = True  # the planner switch (off: preview only)
    forecast: dict[float, float] | None = None  # {hour start: kWh} (Forecast.Solar)
    forecast_time: float | None = None
    pv_kw: float | None = None  # production now
    nowcast_kw: float | None = None  # house import now without the car and the optimiser's consumers
    price_offpeak: float = 0.1349
    price_peak: float = 0.26371
    price_export: float = 0.06
    offpeak_window: tuple[int, int] = (22 * 60, 12 * 60)  # minutes, winter time, may cross midnight
    base_kw: float = 0.4
    limit_kw: float = 4.14
    levels_kw: tuple[float, ...] = (0.0,)
    confidence: float = 0.8
    lead: float | None = None  # seconds; None: params.lead
    battery_kwh: float = 68.8


@dataclass
class PlannerOutput:
    state: str = "Sin objetivo"
    held: bool = False
    level: float | None = None  # kW for the current slot while held
    attrs: dict = field(default_factory=dict)


@dataclass
class Slot:
    t0: float
    t1: float
    sun: float  # kW
    base: float  # kW
    price: float  # import €/kWh
    offpeak: bool

    @property
    def hours(self) -> float:
        return (self.t1 - self.t0) / HOUR

    @property
    def net(self) -> float:
        return self.base - self.sun


@dataclass
class PlanResult:
    levels: list[float]  # kW per slot
    grid: list[float]  # kWh from the grid per slot
    sun: list[float]  # kWh from the sun per slot
    cost: float  # € (sun at the export price)
    shortfall: list[float]  # kWh missing per milestone
    energy: float  # kWh charged in the horizon


# ---------------------------------------------------------------------------- tariff / forecast


def is_offpeak(t: float, window: tuple[int, int]) -> bool:
    d = datetime.fromtimestamp(t, WINTER)
    m, (i, f) = d.hour * 60 + d.minute, window
    return (m >= i or m < f) if i > f else (i <= m < f)


def parse_forecast(response: dict | None) -> dict[float, float]:
    """``forecast_solar.get_forecast`` (resolution hourly) -> {start of the hour: kWh}. The key of
    each hour is the END of the period (9:00 is what is produced from 8 to 9)."""
    out: dict[float, float] = {}
    for key, value in ((response or {}).get("wh_period") or {}).items():
        try:
            t = datetime.fromisoformat(str(key)).timestamp()
            x = float(value)
        except (TypeError, ValueError):
            continue
        start = (t - 1) // HOUR * HOUR
        out[start] = out.get(start, 0.0) + x / 1000
    return out


def forecast_kw(fc: dict[float, float], t: float) -> float:
    """Mean kW of the hour containing t. The days after the last forecast repeat the last day."""
    if not fc:
        return 0.0
    hour = t // HOUR * HOUR
    last = max(fc)
    while hour > last:
        hour -= 86400
    return fc.get(hour, 0.0)


# ---------------------------------------------------------------------------- trackers


@dataclass
class SocTracker:
    """SoC estimated now: the car reports it late (cloud); the meter is integrated from the time of
    the report and the meter kWh per 1 % are learned when the reported SoC goes up."""

    cum: float = 0.0
    last_t: float | None = None
    history: list = field(default_factory=list)  # (t, cum) every minute, 6 h
    report: tuple | None = None  # (soc, time, cum at that time)
    anchor: tuple | None = None  # (soc, cum)
    kwh_per_pct: float | None = None

    def update(self, p: PlannerParams, now: float, car_kw: float | None, soc: float | None, soc_time: float | None, battery_kwh: float) -> float | None:
        if self.last_t is not None:
            self.cum += max(car_kw or 0.0, 0.0) * min(now - self.last_t, 60.0) / HOUR
        self.last_t = now
        if not self.history or now - self.history[-1][0] >= 60:
            self.history.append((now, self.cum))
            self.history = [h for h in self.history if now - h[0] <= 6 * HOUR]
        if self.kwh_per_pct is None or not p.kwh_per_pct_min <= self.kwh_per_pct <= p.kwh_per_pct_max:
            self.kwh_per_pct = min(p.kwh_per_pct_max, max(p.kwh_per_pct_min, battery_kwh / 100 / p.efficiency))
        if soc is None:
            return None
        ts = min(soc_time if soc_time is not None else now, now)
        if self.report is None or self.report[1] != ts or self.report[0] != soc:
            cum = self._cum_at(ts)
            if cum is None:
                cum = self.cum  # report older than the memory: nothing added (prudent)
            if self.anchor is not None and soc < self.anchor[0]:
                self.anchor = None  # went down: the car was used
            if self.anchor is None:
                self.anchor = (soc, cum)
            elif soc - self.anchor[0] >= p.soc_learn_min_pct:
                r = (cum - self.anchor[1]) / (soc - self.anchor[0])
                if p.kwh_per_pct_min <= r <= p.kwh_per_pct_max:
                    self.kwh_per_pct += p.kwh_per_pct_alpha * (r - self.kwh_per_pct)
                self.anchor = (soc, cum)
            self.report = (soc, ts, cum)
        soc0, _, cum0 = self.report
        return min(100.0, soc0 + (self.cum - cum0) / self.kwh_per_pct)

    def _cum_at(self, t: float) -> float | None:
        if not self.history or t < self.history[0][0]:
            return None
        best = self.history[0][1]
        for ht, c in self.history:
            if ht > t:
                break
            best = c
        return best


@dataclass
class SunTracker:
    """Real / forecast sun so far today: if the day goes worse than forecast, the rest of the day
    is corrected with it (never better than the configured confidence)."""

    day: str | None = None
    real: float = 0.0
    fc: float = 0.0
    last_t: float | None = None

    def update(self, p: PlannerParams, now: float, tz: tzinfo, pv_kw: float | None, fc: dict[float, float] | None) -> float | None:
        day = datetime.fromtimestamp(now, tz).date().isoformat()
        if self.day != day:
            self.day, self.real, self.fc = day, 0.0, 0.0
        if self.last_t is not None and pv_kw is not None and fc:
            dt = min(now - self.last_t, 60.0) / HOUR
            self.real += max(pv_kw, 0.0) * dt
            self.fc += forecast_kw(fc, now) * dt
        self.last_t = now
        return self.real / self.fc if self.fc >= p.sun_learn_min_kwh else None


# ---------------------------------------------------------------------------- slots and model


def build_slots(inp: PlannerInputs, p: PlannerParams, end: float, ratio: float | None) -> list[Slot]:
    fresh = inp.forecast and inp.forecast_time is not None and inp.now - inp.forecast_time <= p.forecast_max_age
    fc = inp.forecast if fresh else {}
    today = datetime.fromtimestamp(inp.now, inp.tz).date()
    conf_today = inp.confidence if ratio is None else min(inp.confidence, ratio)
    slots: list[Slot] = []
    t = inp.now
    while t < end - 1:
        t1 = min(end, (t // p.slot + 1) * p.slot)
        conf = conf_today if datetime.fromtimestamp(t, inp.tz).date() == today else inp.confidence
        sun = forecast_kw(fc, t) * conf
        off = is_offpeak(t, inp.offpeak_window)
        slots.append(Slot(t, t1, sun, inp.base_kw, inp.price_offpeak if off else inp.price_peak, off))
        t = t1
    if slots and inp.nowcast_kw is not None:
        # Current slot: the measure, not the forecast
        slots[0].sun, slots[0].base = max(0.0, -inp.nowcast_kw), max(0.0, inp.nowcast_kw)
    return slots


def split(slot: Slot, level: float) -> tuple[float, float]:
    """(sun kWh, grid kWh) used by the car at this level in the slot"""
    n = slot.net
    grid = max(0.0, n + level) - max(0.0, n)
    return (level - grid) * slot.hours, grid * slot.hours


def slot_cost(slot: Slot, level: float, price_export: float) -> float:
    sun, grid = split(slot, level)
    return sun * price_export + grid * slot.price


def solve(slots: list[Slot], levels: tuple[float, ...], price_export: float, limit: float, milestones: list[tuple[int, float]], p: PlannerParams, current: float = 0.0) -> PlanResult:
    """Exact DP. ``milestones``: (index of the last slot before the target, kWh needed by then);
    ``current``: level of the charger now (0: off)."""
    lv = tuple(sorted({0.0, *levels}))
    L, T = len(lv), len(slots)
    if T == 0:
        return PlanResult([], [], [], 0.0, [m[1] for m in milestones], 0.0)
    q = p.energy_step
    milestones = [(m[0], m[1], m[2] if len(m) > 2 else p.shortfall_penalty) for m in milestones]
    top = max((m[1] for m in milestones), default=0.0)
    N = max(1, int(math.ceil(top / q))) + 1  # buckets 0..N-1, the last one = "enough"
    INF = 1e18

    inc = np.zeros((T, L), dtype=np.int64)
    cost = np.full((T, L), INF)
    for t, s in enumerate(slots):
        for j, x in enumerate(lv):
            if x > EPS and s.net + x > limit + EPS:
                continue
            cost[t, j] = slot_cost(s, x, price_export)
            inc[t, j] = int(round(x * s.hours / q))
    trans = np.zeros((L, L))
    for a in range(L):
        for b in range(L):
            if b != a and lv[b] > EPS:
                trans[a, b] = p.start_penalty if lv[a] <= EPS else p.change_penalty

    by_slot: dict[int, list[tuple[float, float]]] = {}
    for idx, need, pen in milestones:
        by_slot.setdefault(max(0, min(T - 1, idx)), []).append((need, pen))
    buckets = np.arange(N) * q

    start = min(range(L), key=lambda j: abs(lv[j] - current))
    C = np.full((L, N), INF)
    C[start, 0] = 0.0
    parent_l = np.zeros((T, L, N), dtype=np.int8)
    parent_e = np.zeros((T, L, N), dtype=np.int32)
    idx_e = np.arange(N)
    for t in range(T):
        nxt = np.full((L, N), INF)
        for b in range(L):
            if cost[t, b] >= INF:
                continue
            # best previous level for every energy bucket
            m = C + trans[:, b][:, None]
            best_l = np.argmin(m, axis=0)
            best = m[best_l, idx_e] + cost[t, b]
            k = int(inc[t, b])
            if k == 0:
                nxt[b] = best
                parent_l[t, b] = best_l
                parent_e[t, b] = idx_e
                continue
            shifted = np.full(N, INF)
            pe = np.zeros(N, dtype=np.int32)
            if k < N:
                shifted[k:] = best[: N - k]
                pe[k:] = idx_e[: N - k]
            # the last bucket also collects everything that overflows
            lo = max(0, N - 1 - k)
            tail = best[lo:]
            j = int(np.argmin(tail))
            if tail[j] < shifted[N - 1]:
                shifted[N - 1] = tail[j]
                pe[N - 1] = lo + j
            nxt[b] = shifted
            parent_l[t, b] = best_l[pe]
            parent_e[t, b] = pe
        if t in by_slot:
            for need, pen in by_slot[t]:
                nxt += pen * np.maximum(0.0, need - buckets)[None, :]
        C = nxt

    flat = int(np.argmin(C))
    b, e = divmod(flat, N)
    path = [0] * T
    for t in range(T - 1, -1, -1):
        path[t] = b
        b, e = int(parent_l[t, b, e]), int(parent_e[t, b, e])
    levels_out = [lv[j] for j in path]
    suns, grids = zip(*(split(s, x) for s, x in zip(slots, levels_out)))
    cum = np.cumsum([x * s.hours for s, x in zip(slots, levels_out)])
    shortfall = [max(0.0, need - float(cum[max(0, min(T - 1, idx))])) for idx, need, _ in milestones]
    real_cost = sum(su * price_export + g * s.price for s, su, g in zip(slots, suns, grids))
    return PlanResult(levels_out, list(grids), list(suns), real_cost, shortfall, float(cum[-1]))


# ---------------------------------------------------------------------------- planner


DAYS = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")


class ChargePlanner:
    """The planner with its memory (SoC and sun trackers, held state)"""

    def __init__(self, params: PlannerParams | None = None):
        self.p = params or PlannerParams()
        self.soc = SocTracker()
        self.sun = SunTracker()
        self.held_until: float | None = None  # target the car is held for
        self.skipped: set[float] = set()

    # persistence ---------------------------------------------------------------
    def export_state(self) -> dict:
        return {"kwh_por_pct": self.soc.kwh_per_pct, "omitidos": sorted(self.skipped), "retenido_hasta": self.held_until}

    def restore_state(self, data: dict | None) -> None:
        data = data or {}
        k = data.get("kwh_por_pct")
        if isinstance(k, (int, float)) and self.p.kwh_per_pct_min <= k <= self.p.kwh_per_pct_max:
            self.soc.kwh_per_pct = float(k)
        self.skipped = {float(x) for x in data.get("omitidos") or [] if isinstance(x, (int, float))}
        held = data.get("retenido_hasta")
        self.held_until = float(held) if isinstance(held, (int, float)) else None

    def skip_next(self, targets: list[Target], now: float) -> Target | None:
        pending = sorted((x for x in targets if x.t > now and x.t not in self.skipped), key=lambda x: x.t)
        if not pending:
            return None
        self.skipped.add(pending[0].t)
        if self.held_until == pending[0].t:
            self.held_until = None
        return pending[0]

    # one run -------------------------------------------------------------------
    def plan(self, inp: PlannerInputs) -> PlannerOutput:
        p, now = self.p, inp.now
        out = PlannerOutput()
        avisos: list[str] = []
        soc = self.soc.update(p, now, inp.car_kw, inp.soc, inp.soc_time, inp.battery_kwh)
        ratio = self.sun.update(p, now, inp.tz, inp.pv_kw, inp.forecast)
        k = self.soc.kwh_per_pct
        self.skipped = {t for t in self.skipped if t > now}
        a = out.attrs
        a.update(kwh_por_pct=round(k, 3), avisos=avisos, retenido=False)
        if ratio is not None:
            a["sol_real_frente_a_previsto"] = round(ratio, 2)
        targets = sorted((x for x in inp.targets if now < x.t <= now + p.horizon and x.t not in self.skipped), key=lambda x: x.t)
        a["objetivos"] = [{"cuando": _when(x.t, now, inp.tz), "hora": int(x.t), "pct": x.pct, "origen": x.source} for x in targets]
        if self.held_until is not None and self.held_until <= now:
            self.held_until = None
        if not targets:
            self.held_until = None
            out.state = "Sin objetivo"
            return out
        if soc is None:
            out.state = "Sin datos del coche"
            return out
        a.update(soc_coche=inp.soc, soc_estimado=round(soc, 1))
        needs = [max(0.0, x.pct - soc) * k for x in targets]
        if all(n <= EPS for n in needs):
            self.held_until = None
            out.state = "Cumplido"
            return out
        a["falta_kwh"] = round(max(needs), 2)

        # Plan from now to the last target
        end = targets[-1].t
        slots = build_slots(inp, p, end, ratio)
        if not (inp.forecast and inp.forecast_time is not None and now - inp.forecast_time <= p.forecast_max_age):
            avisos.append("Sin previsión solar reciente: se planifica sin sol")
        if ratio is not None and ratio < inp.confidence:
            avisos.append(f"Hoy hay menos sol del previsto ({ratio:.0%}): se corrige el resto del día")
        milestones = []
        for x, need in zip(targets, needs):
            aim = max(now, x.t - p.early)
            idx = max(0, sum(1 for s in slots if s.t1 <= aim + 1) - 1)
            milestones.append((idx, need, p.shortfall_penalty))
            milestones.append((idx, need * (1 + p.margin), p.margin_penalty))
        current = inp.car_kw if inp.car_kw and inp.car_kw > 0.3 else 0.0
        res = solve(slots, inp.levels_kw, inp.price_export, inp.limit_kw, milestones, p, current)

        # Window: hold the car from (first pending target - lead)
        first = next(i for i, n in enumerate(needs) if n > EPS)
        t1, need1 = targets[first].t, needs[first]
        top = max((x for x in inp.levels_kw if x <= inp.limit_kw - inp.base_kw + EPS), default=0.0)
        lead = inp.lead if inp.lead is not None else p.lead
        if top > EPS:
            lead = max(lead, p.lead_factor * need1 / top * HOUR)
        since = t1 - lead
        a["ventana_desde"] = datetime.fromtimestamp(since, inp.tz).isoformat(timespec="minutes")
        before = [i for i, s in enumerate(slots) if s.t0 < t1]
        grid_before = sum(res.grid[i] for i in before)
        unreachable = any(x > 0.05 for x in res.shortfall[::2])  # the real needs, not the margins
        in_window = now >= since
        if self.held_until is not None and self.held_until not in {x.t for x, n in zip(targets, needs) if n > EPS}:
            self.held_until = None  # its target was reached or skipped
        # Held only when the plan needs the grid (or cannot reach): with the sun alone the
        # optimiser does it as well and keeps its freedom. Once held, until the target.
        hold = in_window and (grid_before > 0.05 or unreachable or self.held_until is not None)
        if hold and self.held_until is None:
            self.held_until = t1

        # Car availability
        if inp.plugged is False:
            hold = False
            self.held_until = None
            if in_window:
                avisos.append("Coche desenchufado")
        if inp.manual:
            hold = False
            avisos.append("Coche en manual: el plan no lo controla")

        a.update(_timeline(slots, res, soc, k, now, inp.tz, inp.price_export, p.publish_hours))
        a.update(
            sol_previsto_kwh=round(sum(res.sun), 2),
            red_prevista_kwh=round(sum(res.grid), 2),
            coste_total_estimado=round(res.cost, 2),
            nivel_ahora_w=round(res.levels[0] * 1000) if res.levels else 0,
        )
        if unreachable:
            best = min(100.0, soc + res.energy / k)
            out.state = f"No llega: máx. {best:.0f} %"
        elif inp.manual:
            out.state = "Pausa (manual)"
        elif hold and inp.enabled:
            out.state = "Activo"
        else:
            out.state = "Vista previa"
        if any(not slots[i].offpeak and res.grid[i] > 0.01 for i in range(len(slots))):
            avisos.append("Hace falta red en horas no promocionadas")
        out.held = hold and inp.enabled
        a["retenido"] = out.held
        out.level = res.levels[0] if out.held and res.levels else None
        return out


# ---------------------------------------------------------------------------- publication


def _hhmm(t: float, tz: tzinfo) -> str:
    d = datetime.fromtimestamp(t, tz)
    return f"{d.hour}:{d.minute:02d}"


def _when(t: float, now: float, tz: tzinfo) -> str:
    d = datetime.fromtimestamp(t, tz)
    today = datetime.fromtimestamp(now, tz).date()
    if d.date() == today:
        return f"hoy {_hhmm(t, tz)}"
    if d.date() == today + timedelta(days=1):
        return f"mañana {_hhmm(t, tz)}"
    return f"{DAYS[d.weekday()]} {d.day} {_hhmm(t, tz)}"


def _span(a: float, b: float, now: float, tz: tzinfo) -> str:
    da, db = datetime.fromtimestamp(a, tz), datetime.fromtimestamp(b, tz)
    today = datetime.fromtimestamp(now, tz).date()
    day = "" if da.date() == today else f"{DAYS[da.weekday()]} "
    same = db.date() == da.date() or (db - timedelta(seconds=1)).date() == da.date()
    end = _hhmm(b, tz) if same else f"{DAYS[db.weekday()]} {_hhmm(b, tz)}"
    return f"{day}{_hhmm(a, tz)}–{end}"


def _timeline(slots: list[Slot], res: PlanResult, soc: float, k: float, now: float, tz: tzinfo, price_export: float, max_hours: int) -> dict:
    """Same attributes as the former plan (the panel chart reads them):
    - ``horas``: [start (s), sun kW, off-peak grid kW, peak grid kW, price, SoC forecast] per hour;
    - ``tramos``: consecutive slots with the same source (sun / grid), kW, kWh, price and cost;
    - ``resumen``: per source, hours, kWh, price and cost."""
    hours: dict[int, list] = {}
    pct = soc
    groups: list[dict] = []
    totals: dict[str, dict] = {}
    for s, x, su, g in zip(slots, res.levels, res.sun, res.grid):
        pct = min(100.0, pct + (su + g) / k)
        h = int(s.t0 // HOUR * HOUR)
        row = hours.setdefault(h, [h, 0.0, 0.0, 0.0, s.price, pct])
        row[1] += su
        row[2 if s.offpeak else 3] += g
        row[4], row[5] = s.price, pct
        grid_name = "Red promocionada" if s.offpeak else "Red no promocionada"
        for name, kwh, price in (("Sol", su, price_export), (grid_name, g, s.price)):
            if kwh > 0.005:
                r = totals.setdefault(name, {"que": name, "horas": 0.0, "kwh": 0.0, "precio": price, "coste": 0.0})
                r["horas"] += s.hours
                r["kwh"] += kwh
                r["coste"] += kwh * price
        kind = " + ".join(n for n, v in (("Sol", su), (grid_name, g)) if v > 0.005)
        if not kind:
            continue
        last = groups[-1] if groups and groups[-1]["que"] == kind and groups[-1]["_t1"] == s.t0 else None
        if last is None:
            last = {"que": kind, "_t0": s.t0, "_t1": s.t1, "kwh": 0.0, "coste": 0.0, "precio": s.price if g > 0.005 else price_export}
            groups.append(last)
        last["_t1"] = s.t1
        last["kwh"] += su + g
        last["coste"] += su * price_export + g * s.price
    rows = []
    for h in sorted(hours)[:max_hours]:
        r = hours[h]
        span = min(1.0, (min(h + HOUR, slots[-1].t1) - max(h, slots[0].t0)) / HOUR) or 1.0
        rows.append([r[0], round(r[1] / span, 2), round(r[2] / span, 2), round(r[3] / span, 2), round(r[4], 5), round(r[5], 1)])
    tramos = [
        {
            "cuando": _span(g["_t0"], g["_t1"], now, tz),
            "que": g["que"],
            "kw": round(g["kwh"] / ((g["_t1"] - g["_t0"]) / HOUR), 2),
            "kwh": round(g["kwh"], 2),
            "precio": g["precio"],
            "coste": round(g["coste"], 2),
        }
        for g in groups
    ]
    order = ("Sol", "Red promocionada", "Red no promocionada")
    resumen = [
        {**r, "horas": round(r["horas"], 1), "kwh": round(r["kwh"], 2), "coste": round(r["coste"], 2)}
        for r in sorted(totals.values(), key=lambda r: order.index(r["que"]))
    ]
    return {"horas": rows, "tramos": tramos, "resumen": resumen}
