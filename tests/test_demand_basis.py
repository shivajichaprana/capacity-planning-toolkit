"""A demand multiplier is a bare number, so its basis has to be pinned.

The saturation module reports how much more demand a fleet can absorb -- 1.37x,
say -- and that figure is a multiplier on its **operating point**, the planning
percentile of the observed utilization. The recommendation then divides each
projected value by a reference level to get a multiplier it can compare against
it. Nothing in either number carries its basis, so if the reference is measured
any other way the comparison is silently scaled, the output looks entirely
ordinary, and the error shows up as a fleet size rather than as a failure.

The obvious wrong reference is the window mean, and it is wrong by exactly the
peak-to-mean ratio. On a series that peaks at half again its mean, every
projected multiplier is inflated by 1.5x, so a fleet with a third of its growth
still available reports as out of headroom at its first daily peak and the
recommendation is to buy capacity it does not need.

The planning target has the same shape of problem one step later. A projection
is a track, and a cyclical track ends wherever the horizon happens to stop, so
reading the target off the last projected period makes the recommended fleet a
function of that accident rather than of the demand.

These checks pin both bases: the reference is the level at the operating point
and a reference measured differently is refused; and the planning target is the
peak over the horizon, which is the same track the run-out crossing is read
off, so the two statements cannot contradict each other.
"""

from __future__ import annotations

import math
from dataclasses import replace

import pytest

from models.forecast import forecast_series
from models.saturation import CapacityLimit, SaturationResult, analyse_saturation
from recommend.headroom import (
    Fleet,
    RecommendationError,
    demand_reference,
    recommend_headroom,
)
from tests.support import daily_wave, make_key, make_series

LIMIT = CapacityLimit(value=100.0, unit="Percent", provenance="hard")

#: A flat daily cycle with a peak-to-mean of about 1.46 against an absorbable
#: 1.37x. Those two figures straddling each other is the whole point: on the
#: operating-point basis the projection never reaches the ceiling, and on the
#: mean basis it reaches it at the first peak. A fixture where both bases agree
#: would pass whichever one the code used.
VALUES = daily_wave(24 * 21, base=40.0, amplitude=18.0, slope=0.0, sd=0.5)


def _analysis() -> SaturationResult:
    return analyse_saturation(make_series(VALUES), LIMIT, servers=1)


def _window_mean() -> float:
    return math.fsum(VALUES) / len(VALUES)


# ---------------------------------------------------------------------------
# What the reference is
# ---------------------------------------------------------------------------


def test_the_reference_is_the_level_at_the_operating_point() -> None:
    saturation = _analysis()
    assert saturation.profile is not None
    assert demand_reference(saturation) == pytest.approx(
        saturation.profile.planning_value * LIMIT.value
    )


def test_the_reference_is_not_the_window_mean() -> None:
    """And the two differ by the peak-to-mean ratio, not by rounding."""
    saturation = _analysis()
    assert saturation.profile is not None
    ratio = demand_reference(saturation) / _window_mean()
    assert ratio == pytest.approx(saturation.profile.peak_to_mean)
    assert ratio > 1.4, "the fixture must peak well above its mean to be a test at all"


def test_an_analysis_with_no_operating_point_has_no_reference() -> None:
    """A refusal, not a fallback. There is no level to be a multiple of."""
    saturation = _analysis()
    with pytest.raises(RecommendationError, match="no operating point"):
        demand_reference(replace(saturation, profile=None))
    with pytest.raises(RecommendationError, match="no operating point"):
        demand_reference(replace(saturation, limit=None))


# ---------------------------------------------------------------------------
# What it changes
# ---------------------------------------------------------------------------


def test_a_fleet_with_headroom_is_not_told_to_scale_out() -> None:
    """The behavioural consequence, and the fixture's discrimination with it.

    The second assertion is what makes the first mean something: it shows that
    the mean basis would have crossed the ceiling inside this horizon, so the
    recommendation of no change is the basis doing work rather than a series
    too quiet to distinguish the two.
    """
    saturation = _analysis()
    forecast = forecast_series(make_series(VALUES), horizon=168, level=0.9)
    recommendation = recommend_headroom(
        forecast=forecast, saturation=saturation, fleet=Fleet(units=1, unit_name="node")
    )
    assert saturation.headroom is not None
    absorbable = saturation.headroom.demand_multiplier
    assert absorbable is not None

    assert recommendation.action == "none"
    assert recommendation.run_out is not None
    assert recommendation.run_out.upper_crossing is None
    assert recommendation.sizing is not None
    assert recommendation.sizing.units_required == 1

    peak = max(point.upper_one_sided for point in forecast.points)
    assert peak / demand_reference(saturation) < absorbable
    assert peak / _window_mean() > absorbable


def test_the_window_mean_is_refused_as_a_reference() -> None:
    """The check that would have caught the basis error, with its own message."""
    saturation = _analysis()
    forecast = forecast_series(make_series(VALUES), horizon=24, level=0.9)
    with pytest.raises(RecommendationError, match="operating point"):
        recommend_headroom(
            forecast=forecast,
            saturation=saturation,
            reference_level=_window_mean(),
            fleet=Fleet(units=1),
        )


def test_restating_the_derived_reference_is_accepted() -> None:
    """An override may restate the basis; it may not change it."""
    saturation = _analysis()
    forecast = forecast_series(make_series(VALUES), horizon=24, level=0.9)
    fleet = Fleet(units=1)
    derived = recommend_headroom(forecast=forecast, saturation=saturation, fleet=fleet)
    restated = recommend_headroom(
        forecast=forecast,
        saturation=saturation,
        reference_level=demand_reference(saturation),
        fleet=fleet,
    )
    assert restated.action == derived.action
    assert restated.run_out is not None and derived.run_out is not None
    assert restated.run_out.reference_level == derived.run_out.reference_level


def test_a_non_positive_reference_is_still_refused() -> None:
    saturation = _analysis()
    forecast = forecast_series(make_series(VALUES), horizon=24, level=0.9)
    with pytest.raises(RecommendationError, match="positive"):
        recommend_headroom(
            forecast=forecast,
            saturation=saturation,
            reference_level=0.0,
            fleet=Fleet(units=1),
        )


def test_a_forecast_of_another_series_is_refused() -> None:
    """The unit error the basis parameter used to be justified by, caught directly."""
    saturation = _analysis()
    forecast = forecast_series(make_series(VALUES), horizon=24, level=0.9)
    elsewhere = replace(forecast, key=make_key(metric="MemoryUtilization"))
    with pytest.raises(RecommendationError, match="different series"):
        recommend_headroom(
            forecast=elsewhere, saturation=saturation, fleet=Fleet(units=1)
        )


# ---------------------------------------------------------------------------
# What it says
# ---------------------------------------------------------------------------


def test_the_basis_is_disclosed_in_the_findings() -> None:
    """A reader must be able to tell which basis produced the multipliers."""
    saturation = _analysis()
    forecast = forecast_series(make_series(VALUES), horizon=24, level=0.9)
    recommendation = recommend_headroom(
        forecast=forecast, saturation=saturation, fleet=Fleet(units=1)
    )
    disclosure = [f for f in recommendation.findings if f.code == "F702"]
    assert len(disclosure) == 1
    message = disclosure[0].message
    assert "operating point" in message
    assert "window mean" in message
    assert f"{demand_reference(saturation):.6g}" in message


# ---------------------------------------------------------------------------
# The planning target's basis
# ---------------------------------------------------------------------------

#: A rising daily cycle. Across horizons 90 to 102 the peak of the projected
#: upper track does not move at all, while the value at the horizon's last
#: period sweeps from 0.73x to 1.07x -- a swing of about half the figure. That
#: separation is what makes the fixture able to tell the two readings apart.
#: Growth is strong enough that the peak sits just above the absorbable
#: multiplier, so the fleet size responds to the target rather than being
#: pinned by having plenty of room.
RISING = daily_wave(24 * 28, base=44.0, amplitude=14.0, slope=0.035, sd=0.6)

#: Horizons over which the projected peak is constant for ``RISING``.
STABLE_HORIZONS = range(90, 103)


def _rising_analysis() -> SaturationResult:
    return analyse_saturation(make_series(RISING), LIMIT, servers=4)


def test_the_planning_target_is_the_peak_and_not_the_last_period() -> None:
    saturation = _rising_analysis()
    forecast = forecast_series(make_series(RISING), horizon=96, level=0.9)
    recommendation = recommend_headroom(
        forecast=forecast, saturation=saturation, fleet=Fleet(units=4, unit_name="node")
    )
    run_out = recommendation.run_out
    assert run_out is not None
    assert recommendation.sizing is not None

    peak = max(point.upper_one_sided for point in forecast.points)
    assert run_out.upper_multiplier_peak == pytest.approx(
        peak / demand_reference(saturation)
    )
    assert recommendation.sizing.target_multiplier == pytest.approx(
        max(1.0, run_out.upper_multiplier_peak)
    )
    # And the fixture discriminates: the two readings differ materially, so
    # sizing on the wrong one would have produced a different fleet.
    assert run_out.upper_multiplier_peak > run_out.upper_multiplier_at_horizon * 1.1


def test_the_fleet_size_does_not_depend_on_where_the_horizon_stops() -> None:
    """The property the peak basis exists to provide.

    Across horizons whose projected peak is the same, the recommended fleet has
    to be the same. Under the horizon-end reading it was not: the size tracked
    the phase the last period happened to land on.
    """
    saturation = _rising_analysis()
    fleet = Fleet(units=4, unit_name="node")
    peaks: set[float] = set()
    sizes: set[int] = set()
    last_period: set[float] = set()
    for horizon in STABLE_HORIZONS:
        forecast = forecast_series(make_series(RISING), horizon=horizon, level=0.9)
        recommendation = recommend_headroom(
            forecast=forecast, saturation=saturation, fleet=fleet
        )
        assert recommendation.sizing is not None
        assert recommendation.run_out is not None
        peaks.add(round(recommendation.run_out.upper_multiplier_peak, 9))
        sizes.add(recommendation.sizing.units_required)
        last_period.add(round(recommendation.run_out.upper_multiplier_at_horizon, 3))

    # The premise: these horizons genuinely share a peak, so the fleet size has
    # nothing legitimate to vary with.
    assert len(peaks) == 1, f"the horizons tested do not share a peak: {sorted(peaks)}"
    # The discrimination: they disagree sharply about the last projected period,
    # so a size driven by that reading would have moved.
    assert max(last_period) / min(last_period) > 1.4, (
        "the horizons tested must disagree about the last projected period, or "
        f"this proves nothing about which reading is used: {sorted(last_period)}"
    )
    # The property.
    assert len(sizes) == 1, (
        f"fleet sizes {sorted(sizes)} across horizons that share a projected "
        "peak: the size depends on where the horizon stopped"
    )


def test_a_run_out_inside_the_horizon_cannot_coexist_with_no_change() -> None:
    """The contradiction the peak basis removes.

    The crossing is read off the projected track's peak. If the target were the
    last period's value, a review could report that the ceiling is reached on
    Thursday and that no change is needed within the horizon, in adjacent
    lines. Tying both to the same track makes that unrepresentable.
    """
    saturation = _rising_analysis()
    fleet = Fleet(units=4, unit_name="node")
    crossings = 0
    for horizon in STABLE_HORIZONS:
        forecast = forecast_series(make_series(RISING), horizon=horizon, level=0.9)
        recommendation = recommend_headroom(
            forecast=forecast, saturation=saturation, fleet=fleet
        )
        assert recommendation.run_out is not None
        if recommendation.run_out.upper_crossing is None:
            continue
        crossings += 1
        assert recommendation.action != "none", (
            f"horizon {horizon}: run-out is inside the horizon yet no change is "
            "recommended"
        )
    assert crossings, "no horizon tested produced a crossing, so nothing was checked"


def test_both_readings_are_reported_so_a_reader_can_tell_them_apart() -> None:
    saturation = _rising_analysis()
    forecast = forecast_series(make_series(RISING), horizon=96, level=0.9)
    recommendation = recommend_headroom(
        forecast=forecast, saturation=saturation, fleet=Fleet(units=4)
    )
    published = recommendation.as_dict()["run_out"]
    for field in (
        "upper_multiplier_peak",
        "central_multiplier_peak",
        "upper_multiplier_at_horizon",
        "central_multiplier_at_horizon",
    ):
        assert field in published, f"{field} is not published"
