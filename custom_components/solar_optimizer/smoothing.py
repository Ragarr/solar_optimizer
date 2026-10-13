"""Time-window smoothing of a power signal (pure Python, no Home Assistant dependency).

The optimisation objective only depends on the net consumption (grid import / export), so this is
the signal that has to be smoothed: a passing cloud or a kettle should not flip devices. The filter
is a time-weighted moving average over the last `window_sec` seconds: each sample is held until the
next one, so irregular sampling (event-driven refreshes) does not bias the mean.
"""

from __future__ import annotations

from collections import deque


class TimeWindowAverage:
    """Time-weighted moving average of a step signal over a sliding window."""

    def __init__(self, window_sec: float):
        self._window = float(window_sec)
        self._samples: deque[tuple[float, float]] = deque()  # (timestamp, value)

    @property
    def window_sec(self) -> float:
        """The window length in seconds"""
        return self._window

    def reset(self) -> None:
        """Forget all the samples"""
        self._samples.clear()

    def add(self, t: float, value: float) -> None:
        """Add a sample taken at `t` (epoch seconds). Samples older than the window are dropped,
        except the last one before the window start, which defines the value at that start."""
        if self._samples and t < self._samples[-1][0]:
            # Clock went backwards (should not happen): restart from this sample
            self._samples.clear()
        if self._samples and t == self._samples[-1][0]:
            self._samples.pop()
        self._samples.append((t, float(value)))
        start = t - self._window
        while len(self._samples) >= 2 and self._samples[1][0] <= start:
            self._samples.popleft()

    def mean(self, t: float) -> float | None:
        """Mean value over [t - window, t], or None without samples. With a window of 0 (or a
        single sample) it is the last value."""
        if not self._samples:
            return None
        if self._window <= 0 or len(self._samples) == 1:
            return self._samples[-1][1]
        start = max(t - self._window, self._samples[0][0])
        total = 0.0
        for i, (ts, value) in enumerate(self._samples):
            seg_start = max(ts, start)
            seg_end = self._samples[i + 1][0] if i + 1 < len(self._samples) else t
            seg_end = min(seg_end, t)
            if seg_end > seg_start:
                total += value * (seg_end - seg_start)
        span = t - start
        return total / span if span > 0 else self._samples[-1][1]
