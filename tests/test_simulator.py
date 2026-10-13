"""Offline simulator: whole synthetic days must show no invariant violation"""

import pytest

from simulator.run import simulate
from simulator.world import profiles


@pytest.mark.parametrize("profile", profiles(), ids=lambda p: p.name)
def test_simulated_day_has_no_violation(profile):
    result = simulate(profile, seed=0)
    assert result["violations"] == []
    assert result["supervisor_violations"] == 0
    # guards keep the switching reasonable (minimal times, starts per hour)
    assert result["switches"]["car"] <= 3 * 18
    assert all(n <= 2 * 18 for d, n in result["switches"].items() if d != "car")
