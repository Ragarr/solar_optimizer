"""Power of an on/off consumer without a measure, estimated from the step it leaves in the house
consumption (pure Python, no Home Assistant dependency).

The house consumption is X = net + P (grid import plus production). When a consumer starts or
stops, X steps by its power. The step is the mean of X after the change minus the mean before it
(the opposite for a stop):

    before: [t_cmd - 3 min, t_cmd]        t_cmd: the command (or the reported change - 1 min)
    after:  [t_seen + 2 min, t_seen + 5 min]  t_seen: the change reported by the device

so the cloud delay and the compressor start fall between the two windows. A step is discarded when
anything else may have moved X: the production mean changed by more than ``max_production_change``,
another consumer changed (state or level) inside the windows, the same consumer changed again, or
the step is outside [min_kw, max_kw].
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

BEFORE = 180.0
CMD_LAG = 60.0  # reported change without a command of ours: assume it happened this long before
AFTER_START = 120.0
AFTER_END = 300.0
KEEP = 900.0


@dataclass(frozen=True)
class EstimatorParams:
    alpha: float = 0.3  # weight of a new step in the moving average
    min_kw: float = 0.1
    max_kw: float = 2.5
    max_production_change: float = 0.3  # kW between the windows


@dataclass(frozen=True)
class Step:
    """An accepted step: the consumer's power at a start or at a stop"""

    device: str
    kind: str  # "on" | "off"
    kw: float
    time: float


@dataclass(frozen=True)
class Rejected:
    device: str
    kind: str
    why: str
    time: float


def window_mean(samples, a: float, b: float) -> float | None:
    """Time-weighted mean of a step signal [(t, v), ...] (each value held until the next sample)
    over [a, b]; None if the samples do not cover the start of the window"""
    if b <= a or not samples or samples[0][0] > a:
        return None
    total = 0.0
    for i, (t, v) in enumerate(samples):
        end = samples[i + 1][0] if i + 1 < len(samples) else b
        lo, hi = max(t, a), min(end, b)
        if hi > lo:
            total += v * (hi - lo)
    return total / (b - a)


class PowerEstimator:
    """Collects the samples of X and P and the changes of every consumer; returns the steps"""

    def __init__(self, params: EstimatorParams | None = None):
        self.p = params or EstimatorParams()
        self._x: deque[tuple[float, float]] = deque()
        self._prod: deque[tuple[float, float]] = deque()
        self._changes: deque[tuple[float, str]] = deque()  # (time, device): any change of any consumer
        self._pending: list[dict] = []  # changes to evaluate once the "after" window is complete

    def add(self, t: float, net: float, production: float) -> None:
        for buf, v in ((self._x, net + production), (self._prod, production)):
            if buf and t <= buf[-1][0]:
                if t < buf[-1][0]:
                    buf.clear()
                else:
                    buf.pop()
            buf.append((t, v))
            while len(buf) >= 2 and buf[1][0] <= t - KEEP:
                buf.popleft()

    def change(self, device: str, t: float) -> None:
        """Any change of a consumer (start, stop, level): it spoils the steps of the others"""
        self._changes.append((t, device))
        while self._changes and self._changes[0][0] < t - KEEP:
            self._changes.popleft()

    def transition(self, device: str, kind: str, t_seen: float, t_cmd: float | None = None) -> None:
        """A start ("on") or a stop ("off") to estimate. ``t_cmd``: our command, if it caused it."""
        self.change(device, t_seen)
        t0 = t_cmd if t_cmd is not None and t_cmd <= t_seen else t_seen - CMD_LAG
        self._pending = [x for x in self._pending if x["device"] != device]  # a new change spoils the previous one
        self._pending.append({"device": device, "kind": kind, "t0": t0, "t_seen": t_seen})

    def drop(self, device: str) -> None:
        self._pending = [x for x in self._pending if x["device"] != device]

    def evaluate(self, now: float) -> list[Step | Rejected]:
        out: list[Step | Rejected] = []
        keep = []
        for x in self._pending:
            a0, a1 = x["t0"] - BEFORE, x["t0"]
            b0, b1 = x["t_seen"] + AFTER_START, x["t_seen"] + AFTER_END
            if now < b1:
                keep.append(x)
                continue
            out.append(self._step(x, a0, a1, b0, b1))
        self._pending = keep
        return out

    def _step(self, x: dict, a0: float, a1: float, b0: float, b1: float) -> Step | Rejected:
        dev, kind = x["device"], x["kind"]

        def rej(why: str) -> Rejected:
            return Rejected(dev, kind, why, b1)

        for t, other in self._changes:
            if a0 <= t <= b1 and (other != dev or abs(t - x["t_seen"]) > 1e-6):
                return rej(f"{other} cambió dentro de la ventana")
        xa, xb = window_mean(self._x, a0, a1), window_mean(self._x, b0, b1)
        pa, pb = window_mean(self._prod, a0, a1), window_mean(self._prod, b0, b1)
        if None in (xa, xb, pa, pb):
            return rej("sin medidas suficientes")
        if abs(pb - pa) > self.p.max_production_change:
            return rej(f"la producción cambió {pb - pa:+.2f} kW")
        kw = (xb - xa) if kind == "on" else (xa - xb)
        if not self.p.min_kw <= kw <= self.p.max_kw:
            return rej(f"escalón de {kw:.2f} kW fuera de rango")
        return Step(dev, kind, kw, b1)

    def update(self, current: float | None, kw: float) -> float:
        """New moving average after an accepted step"""
        return kw if current is None else current + self.p.alpha * (kw - current)
