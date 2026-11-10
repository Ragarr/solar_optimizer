"""Charge plan of the car by periods (manual): what the user schedules and what the battery will be.

Pure Python, shared by Home Assistant and the tests. Powers in kW, energies in kWh measured at the
charger, prices in EUR/kWh, times in epoch seconds.

The user defines periods (a date range, or a weekly repetition):
- ``fixed``: the charger at a fixed power (the supervisor applies its guards and the contracted limit);
- ``sun``: the car first: it takes all the surplus (the optimiser gives the air conditioners only
  what the car cannot absorb);
- ``away``: the car is away and spends ``pct`` % of battery along the period (it does not charge).
Outside the periods the optimiser runs as usual: the car only gets its share of the surplus.

``simulate`` walks 15 min slots from now and returns the estimated battery curve and, for each
period, what it charges and costs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo

HOUR = 3600.0
EPS = 1e-6
WINTER = timezone(timedelta(hours=1))  # the off-peak window of the tariff is in winter time
SLOT = 900.0
HALF_HOUR = 1800.0
MODES = ("fixed", "sun", "away")


@dataclass(frozen=True)
class PlanParams:
    horizon: float = 7 * 86400.0
    forecast_max_age: float = 6 * HOUR
    sun_learn_min_kwh: float = 0.5  # forecast already elapsed today to correct it with the real sun
    kwh_per_pct_min: float = 0.55
    kwh_per_pct_max: float = 1.1
    kwh_per_pct_alpha: float = 0.3
    soc_learn_min_pct: float = 3.0
    efficiency: float = 0.9  # meter kWh reaching the battery (starting point of the learning)


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

    def update(self, p: PlanParams, now: float, car_kw: float | None, soc: float | None, soc_time: float | None, battery_kwh: float) -> float | None:
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

    def update(self, p: PlanParams, now: float, tz: tzinfo, pv_kw: float | None, fc: dict[float, float] | None) -> float | None:
        day = datetime.fromtimestamp(now, tz).date().isoformat()
        if self.day != day:
            self.day, self.real, self.fc = day, 0.0, 0.0
        if self.last_t is not None and pv_kw is not None and fc:
            dt = min(now - self.last_t, 60.0) / HOUR
            self.real += max(pv_kw, 0.0) * dt
            self.fc += forecast_kw(fc, now) * dt
        self.last_t = now
        return self.real / self.fc if self.fc >= p.sun_learn_min_kwh else None


# ---------------------------------------------------------------------------- periods


@dataclass(frozen=True)
class Period:
    """A scheduled period. One-off: ``start``/``end`` (epoch). Weekly: ``weekdays`` (0 = Monday)
    with ``start_min``/``end_min`` (minutes of the local day; end <= start crosses midnight)."""

    id: str
    mode: str
    kw: float = 0.0  # fixed
    pct: float = 0.0  # away: battery % spent along the period
    enabled: bool = True
    start: float | None = None
    end: float | None = None
    weekdays: tuple[int, ...] = ()
    start_min: int = 0
    end_min: int = 0
    updated: float = 0.0

    @property
    def weekly(self) -> bool:
        return self.start is None

    def to_dict(self) -> dict:
        return {
            "id": self.id, "mode": self.mode, "kw": self.kw, "pct": self.pct, "enabled": self.enabled,
            "start": self.start, "end": self.end, "weekdays": list(self.weekdays),
            "start_min": self.start_min, "end_min": self.end_min, "updated": self.updated,
        }

    @staticmethod
    def from_dict(d: dict) -> "Period | None":
        """None when the data is not a valid period"""
        try:
            mode = d["mode"]
            if mode not in MODES:
                return None
            start, end = d.get("start"), d.get("end")
            weekdays = tuple(sorted({int(x) for x in d.get("weekdays") or [] if 0 <= int(x) <= 6}))
            if start is not None:
                start, end = float(start), float(end)
                if end <= start:
                    return None
            elif not weekdays:
                return None
            return Period(
                id=str(d["id"]), mode=mode, kw=max(0.0, float(d.get("kw") or 0.0)),
                pct=min(100.0, max(0.0, float(d.get("pct") or 0.0))), enabled=bool(d.get("enabled", True)),
                start=start, end=end, weekdays=weekdays,
                start_min=int(d.get("start_min") or 0) % 1440, end_min=int(d.get("end_min") or 0) % 1440,
                updated=float(d.get("updated") or 0.0),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(frozen=True)
class Interval:
    t0: float
    t1: float
    period: Period


def expand(periods: list[Period], now: float, tz: tzinfo, horizon: float = 7 * 86400.0) -> list[Interval]:
    """Concrete intervals of the enabled periods that touch [now, now + horizon], sorted by start"""
    out: list[Interval] = []
    end = now + horizon
    today = datetime.fromtimestamp(now, tz).date()
    for p in periods:
        if not p.enabled:
            continue
        if not p.weekly:
            if p.end > now and p.start < end:
                out.append(Interval(p.start, p.end, p))
            continue
        for d in range(-1, int(horizon // 86400) + 2):
            day = today + timedelta(days=d)
            if day.weekday() not in p.weekdays:
                continue
            t0 = _at(day, p.start_min, tz)
            t1 = _at(day + timedelta(days=1 if p.end_min <= p.start_min else 0), p.end_min, tz)
            if t1 > now and t0 < end:
                out.append(Interval(t0, t1, p))
    return sorted(out, key=lambda i: (i.t0, i.period.id))


def _at(day: date, minutes: int, tz: tzinfo) -> float:
    return datetime(day.year, day.month, day.day, minutes // 60, minutes % 60, tzinfo=tz).timestamp()


def active(intervals: list[Interval], t: float) -> Interval | None:
    """The interval ruling at t: a one-off wins over a weekly one, then the last edited"""
    best = None
    for i in intervals:
        if i.t0 <= t < i.t1 and (best is None or (not i.period.weekly, i.period.updated) > (not best.period.weekly, best.period.updated)):
            best = i
    return best


# ---------------------------------------------------------------------------- simulation


@dataclass
class Slot:
    t0: float
    t1: float
    sun: float  # kW
    base: float  # kW
    price: float  # import EUR/kWh
    offpeak: bool

    @property
    def hours(self) -> float:
        return (self.t1 - self.t0) / HOUR

    @property
    def net(self) -> float:
        return self.base - self.sun


@dataclass
class PlanInputs:
    now: float
    tz: tzinfo
    periods: list[Period]
    soc: float | None  # estimated battery % now
    kwh_per_pct: float = 0.7  # meter kWh per 1 % of battery
    forecast: dict[float, float] | None = None
    forecast_time: float | None = None
    nowcast_kw: float | None = None  # house import now without the car (negative: surplus)
    ratio: float | None = None  # real / forecast sun so far today
    price_offpeak: float = 0.1349
    price_peak: float = 0.26371
    price_export: float = 0.06
    offpeak_window: tuple[int, int] = (22 * 60, 12 * 60)
    base_kw: float = 0.4
    limit_kw: float = 4.14
    levels_kw: tuple[float, ...] = (0.0,)
    confidence: float = 0.8
    share: float = 1.0  # share of the surplus the car gets outside the periods (optimiser)
    max_pct: float = 100.0  # the car stops charging here


@dataclass
class PeriodResult:
    id: str
    t0: float
    t1: float
    mode: str
    pct_start: float
    pct_end: float
    sun_kwh: float = 0.0
    grid_kwh: float = 0.0
    cost: float = 0.0


@dataclass
class PlanResult:
    curve: list[tuple[float, float]] = field(default_factory=list)  # (t, battery %) every 30 min
    hours: list[tuple[int, float, int]] = field(default_factory=list)  # (hour start, sun kW, off-peak 0/1)
    results: list[PeriodResult] = field(default_factory=list)
    pct_end: float | None = None
    pct_min: float | None = None
    sun_kwh: float = 0.0
    grid_kwh: float = 0.0
    cost: float = 0.0
    warnings: list[str] = field(default_factory=list)


def build_slots(inp: PlanInputs, p: PlanParams, end: float) -> list[Slot]:
    fresh = bool(inp.forecast) and inp.forecast_time is not None and inp.now - inp.forecast_time <= p.forecast_max_age
    fc = inp.forecast if fresh else {}
    today = datetime.fromtimestamp(inp.now, inp.tz).date()
    conf_today = inp.confidence if inp.ratio is None else min(inp.confidence, inp.ratio)
    slots: list[Slot] = []
    t = inp.now
    while t < end - 1:
        t1 = min(end, (t // SLOT + 1) * SLOT)
        conf = conf_today if datetime.fromtimestamp(t, inp.tz).date() == today else inp.confidence
        off = is_offpeak(t, inp.offpeak_window)
        slots.append(Slot(t, t1, forecast_kw(fc, t) * conf, inp.base_kw, inp.price_offpeak if off else inp.price_peak, off))
        t = t1
    if slots and inp.nowcast_kw is not None:
        slots[0].sun, slots[0].base = max(0.0, -inp.nowcast_kw), max(0.0, inp.nowcast_kw)  # the measure
    return slots


def split(slot: Slot, level: float) -> tuple[float, float]:
    """(sun kWh, grid kWh) used by the car at this level in the slot"""
    n = slot.net
    grid = max(0.0, n + level) - max(0.0, n)
    return (level - grid) * slot.hours, grid * slot.hours


def pick_level(levels: tuple[float, ...], cap: float) -> float:
    """The highest charger level that does not exceed cap (0 when none fits)"""
    return max((x for x in levels if x <= cap + EPS), default=0.0)


def simulate(inp: PlanInputs, p: PlanParams | None = None) -> PlanResult:
    p = p or PlanParams()
    res = PlanResult()
    if inp.soc is None:
        res.warnings.append("Sin dato de la batería del coche")
        return res
    end = inp.now + p.horizon
    intervals = expand(inp.periods, inp.now, inp.tz, p.horizon)
    slots = build_slots(inp, p, end)
    fresh = bool(inp.forecast) and inp.forecast_time is not None and inp.now - inp.forecast_time <= p.forecast_max_age
    if not fresh:
        res.warnings.append("Sin previsión solar reciente: se estima sin sol")
    pct = min(100.0, max(0.0, inp.soc))
    res.pct_min = pct
    by_key: dict[tuple, PeriodResult] = {}
    mark = inp.now
    last_hour = None
    res.curve.append((inp.now, round(pct, 1)))
    k = max(inp.kwh_per_pct, EPS)
    for s in slots:
        hour = int(s.t0 // HOUR * HOUR)
        if hour != last_hour:
            last_hour = hour
            res.hours.append((hour, round(s.sun, 2), 1 if s.offpeak else 0))
        iv = active(intervals, (s.t0 + s.t1) / 2)
        mode = iv.period.mode if iv else None
        key = (iv.period.id, iv.t0) if iv else None
        if iv and key not in by_key:
            by_key[key] = PeriodResult(iv.period.id, iv.t0, iv.t1, mode, round(pct, 1), round(pct, 1))
        level = 0.0
        if mode == "fixed":
            level = pick_level(inp.levels_kw, min(iv.period.kw, inp.limit_kw - s.net))
        elif mode == "sun":
            level = pick_level(inp.levels_kw, -s.net)
        elif mode is None:
            level = pick_level(inp.levels_kw, -s.net * inp.share)
        if level > EPS:
            room_kwh = max(0.0, (inp.max_pct - pct) * k)
            kwh = min(level * s.hours, room_kwh)
            if kwh < level * s.hours - EPS:
                level = kwh / s.hours
            sun, grid = split(s, level)
            pct += kwh / k
            cost = sun * inp.price_export + grid * s.price
            res.sun_kwh += sun
            res.grid_kwh += grid
            res.cost += cost
            if iv:
                r = by_key[key]
                r.sun_kwh += sun
                r.grid_kwh += grid
                r.cost += cost
        elif mode == "away":
            pct -= iv.period.pct * (s.t1 - s.t0) / (iv.t1 - iv.t0)
            if pct < 0:
                pct = 0.0
                if "Batería a 0 % en una franja «fuera»" not in res.warnings:
                    res.warnings.append("Batería a 0 % en una franja «fuera»")
        pct = min(100.0, max(0.0, pct))
        res.pct_min = min(res.pct_min, pct)
        if iv:
            by_key[key].pct_end = round(pct, 1)
        if s.t1 - mark >= HALF_HOUR - 1:
            mark = s.t1
            res.curve.append((s.t1, round(pct, 1)))
    res.results = sorted(by_key.values(), key=lambda r: r.t0)
    res.pct_end = round(pct, 1)
    res.pct_min = round(res.pct_min, 1)
    return res
