"""Charge plan by periods: expansion, precedence and simulation (pure core)"""

from datetime import datetime
from zoneinfo import ZoneInfo

from custom_components.solar_optimizer.charge_plan_core import (
    HOUR,
    PlanInputs,
    PlanParams,
    Period,
    expand,
    active,
    simulate,
)

TZ = ZoneInfo("Europe/Madrid")
LEVELS = (0.0, 1.4, 2.2, 3.5, 7.0)


def ts(s):
    return datetime.fromisoformat(s).replace(tzinfo=TZ).timestamp()


def inputs(now, periods, **kw):
    base = dict(now=now, tz=TZ, periods=periods, soc=50.0, kwh_per_pct=0.7, levels_kw=LEVELS, base_kw=0.0, limit_kw=10.0)
    base.update(kw)
    return PlanInputs(**base)


def test_weekly_period_crossing_midnight():
    # Mon-Fri 22:00 -> 06:00; now Wednesday 2026-11-11 12:00
    p = Period("n", "fixed", kw=3.5, weekdays=(0, 1, 2, 3, 4), start_min=22 * 60, end_min=6 * 60)
    iv = expand([p], ts("2026-11-11T12:00"), TZ, 7 * 86400)
    starts = [datetime.fromtimestamp(i.t0, TZ).strftime("%a %H:%M") for i in iv]
    assert starts[:3] == ["Wed 22:00", "Thu 22:00", "Fri 22:00"]
    assert datetime.fromtimestamp(iv[0].t1, TZ).strftime("%a %H:%M") == "Thu 06:00"
    # a night that started before now and is still running is included
    iv = expand([p], ts("2026-11-12T03:00"), TZ, 86400)
    assert datetime.fromtimestamp(iv[0].t0, TZ).strftime("%a %H:%M") == "Wed 22:00"


def test_weekly_period_across_dst_change():
    # the clocks go back on 2026-10-25: 22:00 -> 06:00 lasts 9 h
    p = Period("n", "fixed", weekdays=(5,), start_min=22 * 60, end_min=6 * 60)
    iv = expand([p], ts("2026-10-24T12:00"), TZ, 86400 * 2)
    assert (iv[0].t1 - iv[0].t0) / HOUR == 9


def test_one_off_wins_over_weekly_then_last_edited():
    weekly = Period("w", "sun", weekdays=(2,), start_min=0, end_min=0, updated=5)  # whole Wednesday
    once = Period("o", "away", pct=10, start=ts("2026-11-11T10:00"), end=ts("2026-11-11T14:00"), updated=1)
    iv = expand([weekly, once], ts("2026-11-11T08:00"), TZ)
    assert active(iv, ts("2026-11-11T09:00")).period.id == "w"
    assert active(iv, ts("2026-11-11T11:00")).period.id == "o"
    newer = Period("n", "fixed", kw=2.2, start=ts("2026-11-11T10:00"), end=ts("2026-11-11T14:00"), updated=9)
    iv = expand([once, newer], ts("2026-11-11T08:00"), TZ)
    assert active(iv, ts("2026-11-11T11:00")).period.id == "n"


def test_disabled_and_expired_periods_are_ignored():
    off = Period("a", "fixed", kw=3.5, enabled=False, start=ts("2026-11-11T10:00"), end=ts("2026-11-11T12:00"))
    past = Period("b", "fixed", kw=3.5, start=ts("2026-11-10T10:00"), end=ts("2026-11-10T12:00"))
    assert expand([off, past], ts("2026-11-11T08:00"), TZ) == []


def test_fixed_period_charges_at_the_level():
    now = ts("2026-11-11T08:00")
    p = Period("f", "fixed", kw=3.5, start=ts("2026-11-11T10:00"), end=ts("2026-11-11T12:00"))
    res = simulate(inputs(now, [p], soc=50))
    r = res.results[0]
    assert r.pct_start == 50 and r.pct_end == 60.0  # 2 h x 3.5 kW / 0.7 kWh per %
    assert round(r.grid_kwh, 2) == 7.0 and r.sun_kwh == 0
    assert res.pct_end == 60.0 and res.pct_min == 50.0


def test_fixed_period_is_limited_by_the_contracted_power():
    now = ts("2026-11-11T08:00")
    p = Period("f", "fixed", kw=7.0, start=ts("2026-11-11T10:00"), end=ts("2026-11-11T11:00"))
    res = simulate(inputs(now, [p], base_kw=2.0, limit_kw=5.0))  # room 3 kW -> level 2.2
    assert round(res.results[0].grid_kwh, 2) == 2.2


def test_charge_stops_at_the_limit():
    now = ts("2026-11-11T08:00")
    p = Period("f", "fixed", kw=7.0, start=ts("2026-11-11T09:00"), end=ts("2026-11-11T14:00"))
    res = simulate(inputs(now, [p], soc=75, max_pct=80))
    assert res.pct_end == 80.0


def test_away_spends_the_battery_and_warns_at_zero():
    now = ts("2026-11-11T08:00")
    p = Period("a", "away", pct=15, start=ts("2026-11-11T09:00"), end=ts("2026-11-11T17:00"))
    res = simulate(inputs(now, [p], soc=60))
    assert res.results[0].pct_end == 45.0 and res.pct_end == 45.0
    res = simulate(inputs(now, [p], soc=10))
    assert res.pct_end == 0.0 and res.warnings


def test_sun_period_takes_all_the_surplus():
    now = ts("2026-11-11T08:00")
    fc = {ts("2026-11-11T12:00"): 4.0, ts("2026-11-11T13:00"): 4.0}  # kWh per hour
    p = Period("s", "sun", start=ts("2026-11-11T12:00"), end=ts("2026-11-11T14:00"))
    res = simulate(inputs(now, [p], forecast=fc, forecast_time=now, confidence=1.0, base_kw=0.5))
    r = res.results[0]
    # surplus 3.5 kW -> level 3.5, all of it from the sun
    assert round(r.sun_kwh, 1) == 7.0 and r.grid_kwh == 0
    assert round(r.cost, 2) == round(7.0 * 0.06, 2)


def test_gap_gets_only_the_share():
    now = ts("2026-11-11T08:00")
    fc = {ts("2026-11-11T12:00"): 4.0}
    day = PlanParams(horizon=86400.0)  # one day: over weeks both would end at the limit
    full = simulate(inputs(now, [], forecast=fc, forecast_time=now, confidence=1.0, share=1.0), day)
    half = simulate(inputs(now, [], forecast=fc, forecast_time=now, confidence=1.0, share=0.5), day)
    assert full.sun_kwh > half.sun_kwh > 0


def test_no_soc_gives_a_warning():
    res = simulate(inputs(ts("2026-11-11T08:00"), [], soc=None))
    assert res.pct_end is None and res.warnings


def test_invalid_periods_are_rejected():
    assert Period.from_dict({"id": "x", "mode": "turbo"}) is None
    assert Period.from_dict({"id": "x", "mode": "sun"}) is None  # neither dates nor weekdays
    assert Period.from_dict({"id": "x", "mode": "sun", "start": 10, "end": 5}) is None
    p = Period("x", "fixed", kw=3.5, weekdays=(0, 2), start_min=60, end_min=120)
    assert Period.from_dict(p.to_dict()) == p


def test_curve_every_half_hour_and_hourly_sun_and_tariff():
    now = ts("2026-11-11T08:00")
    fc = {ts("2026-11-11T12:00"): 4.0}
    res = simulate(inputs(now, [], forecast=fc, forecast_time=now, confidence=1.0))
    assert res.curve[1][0] - res.curve[0][0] == 1800
    hours = {t: (sun, promo) for t, sun, promo in res.hours}
    assert hours[int(ts("2026-11-11T12:00"))][0] == 4.0
    assert hours[int(ts("2026-11-11T08:00"))][1] == 1  # 08:00 is off-peak (22:00-12:00)
    assert hours[int(ts("2026-11-11T13:00"))][1] == 0
    assert len(res.hours) in (240, 241)  # 10 days published
    assert len(res.curve) < 1600  # 7 days every 30 min, then hourly



def test_long_horizon_weekly_periods_are_fast_and_ruled_by_precedence():
    import time

    now = ts("2026-11-11T08:00")
    weekly = [Period(f"w{i}", "fixed", kw=2.2, weekdays=tuple(range(7)), start_min=60 * i, end_min=60 * i + 30, updated=i) for i in range(6)]
    once = Period("o", "away", pct=10, start=ts("2026-12-01T09:00"), end=ts("2026-12-01T18:00"), updated=99)
    t = time.time()
    res = simulate(inputs(now, weekly + [once]))
    assert time.time() - t < 1.0
    assert res.curve[-1][0] - res.curve[0][0] >= 55 * 86400
    away = [r for r in res.results if r.id == "o"]
    assert away and away[0].mode == "away"
