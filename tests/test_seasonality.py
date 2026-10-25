"""Checks on phase, decomposition and the gates that decide what is seasonal.

The expensive faults in this module are not crashes. They are a profile that is
estimated successfully and is wrong by a rotation, and a cycle that is declared
real when the measure behind it would have called pure noise seasonal too. Both
produce a forecast that looks better than it is, so the checks here are built
around four claims the module makes and one it refuses to make.

* **Phase comes from the clock, not from the index.** A profile keyed on
  position in the sample rotates by one phase for every hole upstream of it.
* **A daily cycle is a cycle in local time.** The cost is visible rather than
  hidden: at an offset change one phase loses a sample and another gains one.
* **An even-width trend window needs half weights on its ends**, or the trend
  is displaced by half a period and the displacement lands in the profile.
* **Explained variance measures a profile's flexibility, not seasonality.** A
  profile with as many phases as observations reproduces every one of them, so
  the refusal to score a saturated profile is doing real work.

The last of those is why the false-positive checks run over several seeds: a
gate that refuses noise on one draw and admits it on the next has not been
demonstrated to discriminate at all.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

import pytest

from ingest.store import SeriesPoint
from models.seasonality import (
    CANDIDATE_CYCLES,
    Finding,
    MIN_CYCLES_FOR_MODE_TEST,
    SeasonalityError,
    centred_moving_average,
    choose_mode,
    deseasonalise,
    detect_on_grid,
    detect_seasonality,
    f_survival,
    grid_from_points,
    lag_for,
    phase_of,
    regularised_incomplete_beta,
    reseasonalise,
    resolve_zone,
    to_grid,
)
from tests.support import (
    DAY,
    EPOCH,
    HOUR,
    daily_wave,
    make_points,
    make_series,
    noise,
    ramp,
    series_from_points,
    weekend_step,
)

ZONE = "Europe/Amsterdam"


def codes(findings: Sequence[Finding]) -> list[str]:
    return [finding.code for finding in findings]


# ---------------------------------------------------------------------------
# The fixture's own assumption
# ---------------------------------------------------------------------------


def test_the_fixture_epoch_is_midnight_on_a_monday() -> None:
    # Every phase check below reads slot zero as phase zero of both cycles,
    # which is only true because the grid starts here.
    assert EPOCH.weekday() == 0
    assert (EPOCH.hour, EPOCH.minute, EPOCH.second) == (0, 0, 0)


# ---------------------------------------------------------------------------
# Lag and phase
# ---------------------------------------------------------------------------


def test_lag_is_the_sample_count_of_one_cycle() -> None:
    assert lag_for(DAY, HOUR) == 24
    assert lag_for(DAY, 300) == 288
    assert lag_for(604_800, HOUR) == 168


def test_a_cycle_that_is_not_a_whole_number_of_periods_is_refused() -> None:
    # Rounding here is the quietest possible bug: the profile drifts one phase
    # per cycle and reads as weak, slowly rotating seasonality.
    with pytest.raises(SeasonalityError, match="whole number"):
        lag_for(DAY, 7_000)
    with pytest.raises(SeasonalityError, match="period must be positive"):
        lag_for(DAY, 0)


def test_daily_phase_is_the_hour_of_the_clock() -> None:
    phases = [phase_of(EPOCH + timedelta(hours=hour), HOUR, DAY, "UTC") for hour in range(26)]
    assert phases[:24] == list(range(24))
    assert phases[24:] == [0, 1]


def test_weekly_phase_is_weekday_and_hour() -> None:
    assert phase_of(EPOCH, HOUR, 604_800, "UTC") == 0
    assert phase_of(EPOCH + timedelta(hours=25), HOUR, 604_800, "UTC") == 25
    assert phase_of(EPOCH + timedelta(days=7), HOUR, 604_800, "UTC") == 0


def test_phase_follows_local_time_across_an_offset_change() -> None:
    try:
        resolve_zone(ZONE)
    except SeasonalityError as exc:  # pragma: no cover - host without tz data
        pytest.skip(f"no time zone database on this host: {exc}")

    before = datetime(2026, 3, 28, 12, 0, tzinfo=timezone.utc)
    after = datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc)

    # Same instant-of-day in UTC, different local hour: the phase moves, which
    # is the whole point. A profile keyed on elapsed seconds would have called
    # these the same phase and smeared two different load levels together.
    assert phase_of(before, HOUR, DAY, ZONE) == 13
    assert phase_of(after, HOUR, DAY, ZONE) == 14

    # Same local hour on either side of the change: same phase, which is what
    # makes a profile survive the transition as a gap in one phase rather than
    # as a one-hour rotation of every phase.
    same_local_hour = datetime(2026, 3, 28, 13, 0, tzinfo=timezone.utc)
    assert phase_of(same_local_hour, HOUR, DAY, ZONE) == phase_of(after, HOUR, DAY, ZONE)


def test_an_unknown_zone_is_refused_rather_than_defaulted() -> None:
    with pytest.raises(SeasonalityError, match="unknown timezone"):
        resolve_zone("Mars/Olympus_Mons")


def test_utc_needs_no_time_zone_database() -> None:
    assert resolve_zone("UTC") is timezone.utc
    assert resolve_zone("utc") is timezone.utc


# ---------------------------------------------------------------------------
# The grid
# ---------------------------------------------------------------------------


def test_a_missing_sample_becomes_a_hole_and_not_a_value() -> None:
    grid = grid_from_points(make_points(ramp(10), skip=(4,)), HOUR)
    assert grid.slots == 10
    assert grid.observed == 9
    assert grid.values[4] is None
    assert grid.observed_slots() == [0, 1, 2, 3, 5, 6, 7, 8, 9]


def test_points_off_the_sampling_grid_are_counted_not_rounded() -> None:
    points = list(make_points(ramp(30)))
    points.append(SeriesPoint(timestamp=EPOCH + timedelta(seconds=HOUR * 25 + 1_800),
                              value=99.0))
    grid = to_grid(series_from_points(points))
    assert grid.off_grid == 1
    assert grid.slots == 30


def test_an_off_grid_point_is_reported_as_a_mixed_resolution_warning() -> None:
    points = list(make_points(daily_wave(24 * 6, sd=1.0, seed=11)))
    points.append(SeriesPoint(timestamp=EPOCH + timedelta(seconds=HOUR * 10 + 900),
                              value=55.0))
    analysis = detect_on_grid(to_grid(series_from_points(points)))
    assert "F108" in codes(analysis.findings)


def test_a_grid_slice_keeps_absolute_timestamps() -> None:
    grid = grid_from_points(make_points(ramp(20)), HOUR)
    window = grid.slice(5, 12)
    assert window.slots == 7
    assert window.start == grid.timestamps[5]
    assert window.timestamps[0] == grid.timestamps[5]
    assert window.values == grid.values[5:12]


def test_an_impossible_slice_is_refused() -> None:
    grid = grid_from_points(make_points(ramp(20)), HOUR)
    for begin, end in ((-1, 5), (0, 21), (5, 5), (7, 3)):
        with pytest.raises(SeasonalityError, match="invalid grid slice"):
            grid.slice(begin, end)


# ---------------------------------------------------------------------------
# The trend window
# ---------------------------------------------------------------------------


def test_odd_window_is_the_plain_average() -> None:
    values: list[float | None] = [1.0, 2.0, 10.0, 2.0, 1.0]
    trend = centred_moving_average(values, 3)
    assert trend[0] is None and trend[-1] is None
    assert trend[2] == pytest.approx((2.0 + 10.0 + 2.0) / 3.0)


def test_even_window_half_weights_are_what_keep_the_trend_centred() -> None:
    # On a straight line the trend estimate must equal the line. The 2 x m
    # weighting achieves that; the plain even-width mean is displaced by half a
    # period, and that displacement would be absorbed into the profile.
    slope = 3.0
    line = ramp(11, base=5.0, slope=slope)
    trend = centred_moving_average(list(line), 4)
    for index in (2, 5, 8):
        assert trend[index] == pytest.approx(line[index])

    plain = sum(line[3:7]) / 4.0
    assert plain == pytest.approx(line[5] - slope / 2.0)
    assert plain != pytest.approx(line[5])


def test_a_window_containing_a_hole_yields_no_trend_value() -> None:
    # Averaging across the hole would bias the trend toward whichever phases
    # survived, which is precisely the invented number this module refuses.
    values: list[float | None] = list(ramp(9))
    values[4] = None
    trend = centred_moving_average(values, 3)
    assert trend[3] is None and trend[4] is None and trend[5] is None
    assert trend[7] is not None


def test_trend_window_lag_is_validated() -> None:
    with pytest.raises(SeasonalityError, match="lag must be positive"):
        centred_moving_average([1.0, 2.0, 3.0], 0)


# ---------------------------------------------------------------------------
# Detection: what it finds
# ---------------------------------------------------------------------------


def test_a_real_daily_cycle_is_found_through_noise() -> None:
    analysis = detect_seasonality(make_series(daily_wave(24 * 8, amplitude=10.0, sd=3.0,
                                                         seed=2)))
    assert analysis.is_seasonal
    chosen = analysis.chosen
    assert chosen is not None
    assert chosen.cycle_name == "daily"
    assert chosen.lag == 24
    assert chosen.strength > 0.5
    assert chosen.p_value is not None and chosen.p_value < 0.05
    assert chosen.min_phase_support >= 2
    assert "F111" in codes(analysis.findings)


def test_a_weekly_step_is_found_even_though_nothing_fits_a_step() -> None:
    # The profile is estimated per phase rather than fitted to an assumed
    # shape, so a weekend level change is as findable as a sine.
    analysis = detect_seasonality(make_series(weekend_step(24 * 28, step=20.0, sd=5.0,
                                                           seed=5)))
    chosen = analysis.chosen
    assert chosen is not None
    assert chosen.cycle_name == "weekly"
    assert chosen.lag == 168
    assert chosen.strength > 0.5


def test_an_additive_profile_does_not_move_the_level() -> None:
    analysis = detect_seasonality(make_series(daily_wave(24 * 10, sd=1.0, seed=9)))
    chosen = analysis.chosen
    assert chosen is not None
    assert chosen.mode == "additive"
    assert math.fsum(chosen.profile) == pytest.approx(0.0, abs=1e-9)
    assert chosen.amplitude > 0.0


def test_a_multiplicative_profile_averages_one() -> None:
    values = daily_wave(24 * 10, base=50.0, amplitude=10.0, slope=0.5, sd=0.5, seed=4,
                        multiplicative=True)
    analysis = detect_seasonality(make_series(values))
    chosen = analysis.chosen
    assert chosen is not None
    assert chosen.mode == "multiplicative"
    average = math.fsum(chosen.profile) / len(chosen.profile)
    assert average == pytest.approx(1.0, abs=1e-9)


def test_an_offset_change_inside_the_window_is_reported() -> None:
    try:
        resolve_zone(ZONE)
    except SeasonalityError as exc:  # pragma: no cover - host without tz data
        pytest.skip(f"no time zone database on this host: {exc}")
    start = datetime(2026, 3, 27, tzinfo=timezone.utc)
    analysis = detect_seasonality(
        make_series(daily_wave(24 * 6, sd=1.0, seed=2), start=start),
        timezone_name=ZONE,
    )
    assert "F113" in codes(analysis.findings)


# ---------------------------------------------------------------------------
# Detection: what it refuses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", (1, 2, 3, 4, 5))
def test_stationary_noise_is_never_declared_seasonal(seed: int) -> None:
    # Several seeds, because a gate that refuses one draw of noise and admits
    # the next has not been shown to discriminate. The naive explained-variance
    # measure fails this check at every seed.
    analysis = detect_seasonality(make_series(noise(24 * 20, sd=5.0, seed=seed)))
    assert not analysis.is_seasonal
    assert "F112" in codes(analysis.findings)


def test_a_saturated_profile_is_refused_however_strong_it_looks() -> None:
    # Two cycles of hourly history leave 24 detrended observations for a
    # 24-phase profile, so the profile reproduces its input and raw strength is
    # 1.0 for any series whatsoever -- noise included.
    analysis = detect_seasonality(make_series(daily_wave(48)))
    assert not analysis.is_seasonal
    assert "F114" in codes(analysis.findings)
    daily = next(candidate for candidate in analysis.candidates
                 if candidate.cycle_name == "daily")
    assert daily.is_saturated
    assert daily.raw_strength == pytest.approx(1.0)


def test_a_profile_resting_on_single_observations_is_refused() -> None:
    # A phase with one observation contributes no residual variance, so the
    # test of the decomposition has nothing to weigh it against.
    analysis = detect_seasonality(make_series(daily_wave(60)))
    assert not analysis.is_seasonal
    assert "F110" in codes(analysis.findings)


def test_a_cycle_longer_than_the_history_is_reported_as_unavailable() -> None:
    analysis = detect_seasonality(make_series(daily_wave(24 * 8, sd=2.0, seed=6)))
    # The weekly candidate cannot be estimated from eight cycles of the daily
    # one, and says so rather than being silently absent.
    assert "F102" in codes(analysis.findings)
    assert [name for name, _ in CANDIDATE_CYCLES] == ["daily", "weekly"]


def test_an_empty_series_is_refused_with_an_error() -> None:
    analysis = detect_seasonality(make_series([]))
    assert not analysis.is_seasonal
    assert codes(analysis.findings) == ["F100"]
    assert analysis.findings[0].severity == "error"


def test_a_cycle_that_is_not_representable_is_reported() -> None:
    grid = grid_from_points(make_points(ramp(40), period=7_000), 7_000)
    analysis = detect_on_grid(grid)
    assert "F101" in codes(analysis.findings)
    assert not analysis.is_seasonal


# ---------------------------------------------------------------------------
# Mode
# ---------------------------------------------------------------------------


def test_mode_is_additive_when_amplitude_does_not_track_level() -> None:
    grid = grid_from_points(make_points(daily_wave(24 * 6, amplitude=8.0, slope=0.4)), HOUR)
    mode, finding = choose_mode(grid, 24, "daily")
    assert mode == "additive"
    assert finding.code == "F107"


def test_mode_is_multiplicative_when_amplitude_tracks_level() -> None:
    values = daily_wave(24 * 6, base=50.0, amplitude=10.0, slope=1.0, multiplicative=True)
    grid = grid_from_points(make_points(values), HOUR)
    mode, finding = choose_mode(grid, 24, "daily")
    assert mode == "multiplicative"
    assert finding.code == "F106"


def test_mode_test_is_unavailable_below_three_cycles() -> None:
    # A correlation over two points is +/-1 whatever the data, so the test is
    # refused rather than answered.
    grid = grid_from_points(make_points(daily_wave(48)), HOUR)
    mode, finding = choose_mode(grid, 24, "daily")
    assert mode == "additive"
    assert finding.code == "F104"
    assert str(MIN_CYCLES_FOR_MODE_TEST) in finding.message


def test_a_non_positive_value_rules_out_a_multiplicative_factor() -> None:
    values = [10.0, -2.0] * 40
    grid = grid_from_points(make_points(values), HOUR)
    mode, finding = choose_mode(grid, 24, "daily")
    assert mode == "additive"
    assert finding.code == "F103"


def test_mode_finding_names_its_own_cycle() -> None:
    # A result can carry one mode verdict per candidate, and a message naming
    # neither cycle reads as a contradiction.
    grid = grid_from_points(make_points(daily_wave(24 * 6, slope=0.4)), HOUR)
    for name in ("daily", "weekly"):
        _, finding = choose_mode(grid, 24, name)
        assert name in finding.message


# ---------------------------------------------------------------------------
# Applying a profile
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("multiplicative", (False, True))
def test_removing_and_restoring_a_profile_is_exact(multiplicative: bool) -> None:
    values = daily_wave(24 * 10, base=50.0, amplitude=10.0, slope=0.3, sd=0.4, seed=8,
                        multiplicative=multiplicative)
    series = make_series(values)
    analysis = detect_seasonality(series)
    chosen = analysis.chosen
    assert chosen is not None
    assert chosen.mode == ("multiplicative" if multiplicative else "additive")

    stamps = [point.timestamp for point in series.points]
    measured = [point.value for point in series.points]
    flat = deseasonalise(stamps, measured, chosen, series.period)
    restored = reseasonalise(stamps, flat, chosen, series.period)
    for original, round_tripped in zip(measured, restored):
        assert round_tripped == pytest.approx(original, abs=1e-9)


def test_deseasonalising_moves_the_shape_and_not_the_level() -> None:
    series = make_series(daily_wave(24 * 10, sd=0.5, seed=12))
    analysis = detect_seasonality(series)
    chosen = analysis.chosen
    assert chosen is not None
    stamps = [point.timestamp for point in series.points]
    measured = [point.value for point in series.points]
    flat = deseasonalise(stamps, measured, chosen, series.period)

    assert math.fsum(flat) / len(flat) == pytest.approx(
        math.fsum(measured) / len(measured), abs=0.5
    )
    spread_before = max(measured) - min(measured)
    spread_after = max(flat) - min(flat)
    assert spread_after < spread_before


# ---------------------------------------------------------------------------
# The distribution machinery the gates rest on
# ---------------------------------------------------------------------------


def test_incomplete_beta_boundaries_and_symmetry() -> None:
    assert regularised_incomplete_beta(2.0, 3.0, 0.0) == 0.0
    assert regularised_incomplete_beta(2.0, 3.0, 1.0) == 1.0
    for x in (0.05, 0.3, 0.5, 0.8, 0.95):
        mirrored = 1.0 - regularised_incomplete_beta(3.0, 2.0, 1.0 - x)
        assert regularised_incomplete_beta(2.0, 3.0, x) == pytest.approx(mirrored)


def test_f_survival_is_a_decreasing_probability() -> None:
    assert f_survival(0.0, 3, 20) == pytest.approx(1.0)
    previous = 1.0
    for statistic in (0.5, 1.0, 2.0, 5.0, 20.0):
        tail = f_survival(statistic, 3, 20)
        assert 0.0 <= tail <= previous
        previous = tail
    assert f_survival(1.0, 3, 20) > 0.05


def test_f_survival_at_one_numerator_degree_is_the_two_sided_t_tail() -> None:
    # The identity F(1, d) = t(d)^2 is what lets one continued fraction serve
    # both the seasonality gate and the forecast interval.
    from models.forecast import student_t_cdf

    for value, df in ((1.0, 5), (2.228, 10), (0.4, 30)):
        two_sided = 2.0 * (1.0 - student_t_cdf(value, df))
        assert f_survival(value * value, 1, df) == pytest.approx(two_sided, rel=1e-9)
