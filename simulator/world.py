"""Synthetic house: PV, base load, a car charger and air conditioners with realistic latencies.

Everything is driven by a seeded random generator, so a run is reproducible."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

DAY = 86400


# ---------------------------------------------------------------------- profiles


@dataclass
class Profile:
    """Weather and incidents of a simulated day"""

    name: str
    kind: str  # "square" | "walk" | "clear"
    period: float = 0.0  # square wave period (s)
    low: float = 0.3  # cloud factor when shaded
    sensor_gap: tuple[float, float] | None = None  # grid sensor unavailable (start, end) in s of day
    sensor_frozen: tuple[float, float] | None = None  # grid sensor frozen (start, end)
    peak_kw: float = 6.0
    alpha_car: float = 1.0
    manual: list[tuple[float, str, bool]] = field(default_factory=list)  # (time, device, on)
    full_car_at: float | None = None  # the car stops absorbing (battery full)


def profiles() -> list[Profile]:
    out = [Profile(f"clouds_{p}", "square", period=p) for p in (30, 60, 120, 300, 600)]
    out += [
        Profile("random_walk", "walk"),
        Profile("clear_sky", "clear"),
        Profile("sensor_drop", "walk", sensor_gap=(13 * 3600, 13 * 3600 + 20 * 60), sensor_frozen=(15 * 3600, 15 * 3600 + 15 * 60)),
        Profile("shared_alpha", "walk", alpha_car=0.6),
        Profile("manual_and_full_car", "square", period=300, manual=[(11 * 3600, "salon_comedor", True), (16 * 3600, "car", False)], full_car_at=14.5 * 3600),
    ]
    return out


def clear_sky(t: float, peak: float) -> float:
    """Bell-shaped clear-sky production between 7:30 and 19:30 (kW)"""
    sunrise, sunset = 7.5 * 3600, 19.5 * 3600
    if t <= sunrise or t >= sunset:
        return 0.0
    x = (t - sunrise) / (sunset - sunrise)
    return peak * math.sin(math.pi * x) ** 1.5


class Weather:
    def __init__(self, profile: Profile, rnd: random.Random):
        self.p, self.rnd, self.walk = profile, rnd, 0.8

    def factor(self, t: float) -> float:
        if self.p.kind == "square":
            return 1.0 if int(t // (self.p.period / 2)) % 2 == 0 else self.p.low
        if self.p.kind == "walk":
            self.walk = min(1.0, max(0.15, self.walk + self.rnd.gauss(0, 0.02)))
            return self.walk
        return 1.0


# ---------------------------------------------------------------------- devices


@dataclass
class Order:
    at: float  # when it takes effect
    kind: str
    value: float | None


class SimCar:
    """Car charger: commands take effect after a few seconds; the car draws ~95 % of nominal"""

    STEPS_A = (6, 8, 10, 13, 16, 20, 24, 32)
    W_PER_A = 0.232

    def __init__(self, rnd):
        self.rnd = rnd
        self.on = False
        self.amps = 13
        self.queue: list[Order] = []
        self.full = False

    @property
    def level(self):
        return self.amps * self.W_PER_A

    def command(self, now, kind, value):
        delay = self.rnd.uniform(2, 8)
        self.queue.append(Order(now + delay, kind, value))

    def tick(self, now):
        for o in [o for o in self.queue if o.at <= now]:
            self.queue.remove(o)
            if o.kind == "on":
                self.on = True
            elif o.kind == "off":
                self.on = False
            else:
                self.amps = min(self.STEPS_A, key=lambda a: abs(a * self.W_PER_A - o.value))

    def power(self):
        if not self.on or self.full:
            return 0.0
        return self.level * 0.95


class SimAC:
    """Cloud air conditioner: commands take 10-60 s; right after applying, the reported state may
    flicker back to the previous value for a few seconds (as air_cloud does)"""

    KW = 0.8

    def __init__(self, rnd):
        self.rnd = rnd
        self.on = False
        self.queue: list[Order] = []
        self.flicker_until = 0.0
        self.flicker_value = False

    def command(self, now, kind, value):
        self.queue.append(Order(now + self.rnd.uniform(10, 55), kind, value))

    def tick(self, now):
        for o in [o for o in self.queue if o.at <= now]:
            self.queue.remove(o)
            prev = self.on
            self.on = o.kind == "on"
            if prev != self.on and self.rnd.random() < 0.3:
                self.flicker_until, self.flicker_value = now + 20 + self.rnd.uniform(0, 10), prev

    def reported(self, now):
        return self.flicker_value if now < self.flicker_until else self.on

    def power(self):
        return self.KW if self.on else 0.0


class BaseLoad:
    """Base consumption with random appliances (kettle, oven, washing machine)"""

    def __init__(self, rnd):
        self.rnd = rnd
        self.events: list[tuple[float, float, float]] = []
        t = 6 * 3600
        while t < 23 * 3600:
            t += rnd.expovariate(1 / 3600)
            kw, dur = rnd.choice(((2.0, 180), (1.8, 2400), (2.2, 1800), (1.2, 600)))
            self.events.append((t, t + dur, kw))

    def power(self, t):
        base = 0.35 + 0.1 * math.sin(t / 5000)
        return base + sum(kw for a, b, kw in self.events if a <= t < b)
