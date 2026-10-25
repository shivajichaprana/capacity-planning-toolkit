"""Checks on the forecast and, above all, on the interval around it.

A capacity forecast is only as good as its interval, and the usual way one is
wrong is not that the central line is off but that the band around it is too
narrow -- so a plan gets made with confidence the data does not support. A
central line that is wrong is visible within a week. A band that is too narrow
is never visible at all: it simply produces decisions that were worse than they
looked. The checks here therefore spend most of their effort on the interval,
and they pin four claims.

* **Width grows with the horizon**, by each model's own closed-form error
  growth rather than by reusing one-step residual spread at every horizon. Both
  closed forms are checked as exact ratio identities, which is the only way to
  notice if one is ever replaced by the other's shape.
* **Degrees of freedom are charged for the seasonal profile.** A profile at lag
  L estimates L-1 free parameters, and a naive ``n - 2`` denominator understates
  sigma -- and therefore every interval -- by a factor the result publishes.
* **The one-sided question is answered explicitly.** The upper end of a
  two-sided 90% interval is the one-sided bound at 95%, and conflating the two
  is the single easiest way to under-provision; the identity is pinned.
* **Coverage is measured, not asserted.** An interval at a stated level is a
  claim about frequency, so the result reports what fraction of held-back
  actuals actually landed inside the band.

Every refusal is checked too. A refusal is the most useful thing this module
produces, because the alternative is a number derived from a window that could
not support one.
"""

from __future__ import annotations

import json
import math
import random
from datetime import timedelta

import pytest

from models.forecast import (
    MIN_OBSERVATIONS,
    PARTIAL_PERIOD_BIAS,
    ForecastError,
    ForecastResult,
    forecast_series,
    student_t_cdf,
    student_t_ppf,
)
from tests.support import daily_wave, make_series, noisy_ramp, ramp

#: Two-sided 97.5% t quantiles, as published.
T_CRITICAL = ((1, 12.7062), (2, 4.30265), (5, 2.57058), (10, 2.22814), (30, 2.04227))


def codes(result: ForecastResult) -> list[str]:
    return [finding.code for finding in result.findings]


# ---------------------------------------------------------------------------
# The t distribution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("df", "critical"), T_CRITICAL)
def test_t_quantiles_match_published_values(df: int, critical: float) -> None:
    assert student_t_ppf(0.975, df) == pytest.approx(critical, abs=5e-5)


def test_t_approaches_the_normal_quantile_at_large_degrees_of_freedom() -> None:
    assert student_t_ppf(0.975, 1_000_000) == pytest.approx(1.95996, abs=1e-4)


@pytest.mark.parametrize("probability", (0.55, 0.8, 0.9, 0.975, 0.999))
@pytest.mark.parametrize("df", (1, 4, 25, 400))
def test_quantile_and_distribution_function_invert_each_other(
    probability: float, df: int
) -> None:
    assert student_t_cdf(student_t_ppf(probability, df), df) == pytest.approx(
        probability, abs=1e-9
    )


def test_t_is_symmetric() -> None:
    assert student_t_ppf(0.5, 7) == 0.0
    assert student_t_ppf(0.2, 7) == pytest.approx(-student_t_ppf(0.8, 7))
    assert student_t_cdf(0.0, 7) == pytest.approx(0.5)
    assert student_t_cdf(-1.3, 7) == pytest.approx(1.0 - student_t_cdf(1.3, 7))


def test_t_inputs_are_validated() -> None:
    with pytest.raises(ForecastError, match="degrees of freedom"):
        student_t_cdf(1.0, 0.0)
    for probability in (0.0, 1.0, -0.5, 2.0):
        with pytest.raises(ForecastError, match="probability must be in"):
            student_t_ppf(probability, 10.0)


# ---------------------------------------------------------------------------
# The forecast itself
# ---------------------------------------------------------------------------


def test_a_trend_is_extrapolated_and_bracketed() -> None:
    series = make_series(noisy_ramp(200, base=20.0, slope=0.2, sd=2.0, seed=7))
    result = forecast_series(series, horizon=12)

    assert not result.refused
    assert len(result.points) == 12
    diagnostics = result.diagnostics
    assert diagnostics is not None
    assert diagnostics.method == "trend"

    for index, point in enumerate(result.points, start=1):
        assert point.horizon == index
        assert point.lower <= point.value <= point.upper
        assert point.value == pytest.approx(20.0 + 0.2 * (199 + index), abs=2.0)

    expected_stamps = [
        series.points[-1].timestamp + timedelta(seconds=series.period * index)
        for index in range(1, 13)
    ]
    assert [point.timestamp for point in result.points] == expected_stamps


def test_the_interval_widens_with_the_horizon() -> None:
    result = forecast_series(
        make_series(noisy_ramp(200, slope=0.2, sd=2.0, seed=7)), horizon=20
    )
    widths = [point.width for point in result.points]
    assert all(later > earlier for earlier, later in zip(widths, widths[1:]))


def test_drift_interval_follows_its_own_closed_form() -> None:
    # sd(h) = sigma * sqrt(h * (1 + h/m)): h independent steps accumulate, and
    # the drift estimate's own error acts over h of them. Reusing one-step
    # residual spread at every horizon would make this ratio flat.
    generator = random.Random(3)
    walk = [100.0]
    for _ in range(199):
        walk.append(walk[-1] + generator.gauss(0.3, 2.0))

    result = forecast_series(make_series(walk), horizon=10)
    diagnostics = result.diagnostics
    assert diagnostics is not None
    assert diagnostics.method == "drift"

    differences = diagnostics.fitted_observations - 1
    widths = [point.width for point in result.points]

    def scale(horizon: int) -> float:
        return math.sqrt(horizon * (1.0 + horizon / differences))

    for horizon in range(1, 11):
        assert widths[horizon - 1] / widths[0] == pytest.approx(
            scale(horizon) / scale(1), rel=1e-9
        )


def test_trend_interval_follows_its_own_closed_form() -> None:
    # sd(h) = sigma * sqrt(1 + 1/n + (x_h - xbar)^2 / Sxx): residual spread,
    # plus the level's uncertainty, plus the slope's -- the last growing with
    # distance from the centre of the fitted window.
    result = forecast_series(make_series(noisy_ramp(100, sd=2.0, seed=11)), horizon=10)
    diagnostics = result.diagnostics
    assert diagnostics is not None
    assert diagnostics.method == "trend"

    count = diagnostics.fitted_observations
    centre = (count - 1) / 2.0
    spread = math.fsum((slot - centre) ** 2 for slot in range(count))
    widths = [point.width for point in result.points]

    def scale(horizon: int) -> float:
        position = count - 1 + horizon
        return math.sqrt(1.0 + 1.0 / count + (position - centre) ** 2 / spread)

    for horizon in range(1, 11):
        assert widths[horizon - 1] / widths[0] == pytest.approx(
            scale(horizon) / scale(1), rel=1e-9
        )


def test_the_one_sided_bound_is_the_upper_end_of_a_wider_interval() -> None:
    # "When do we run out" wants a bound, not an interval. The upper end of a
    # two-sided 90% interval already is the one-sided 95% bound, and reporting
    # both separately is what stops a reader conflating them.
    series = make_series(noisy_ramp(200, slope=0.2, sd=2.0, seed=7))
    narrow = forecast_series(series, horizon=6, level=0.8)
    wide = forecast_series(series, horizon=6, level=0.9)

    for narrow_point, wide_point in zip(narrow.points, wide.points):
        assert narrow_point.upper == pytest.approx(wide_point.upper_one_sided)
        assert wide_point.upper_one_sided < wide_point.upper

    diagnostics = wide.diagnostics
    assert diagnostics is not None
    assert diagnostics.one_sided_level == pytest.approx(0.95)


def test_a_wider_level_produces_a_wider_band() -> None:
    series = make_series(noisy_ramp(120, sd=2.0, seed=13))
    for lower_level, higher_level in ((0.5, 0.8), (0.8, 0.95), (0.95, 0.99)):
        narrow = forecast_series(series, horizon=4, level=lower_level)
        wide = forecast_series(series, horizon=4, level=higher_level)
        assert wide.points[0].width > narrow.points[0].width


# ---------------------------------------------------------------------------
# Seasonality and the cost of a profile
# ---------------------------------------------------------------------------


def test_a_seasonal_series_is_fitted_with_its_profile() -> None:
    series = make_series(
        daily_wave(24 * 20, base=60.0, amplitude=15.0, slope=0.05, sd=2.0, seed=21)
    )
    result = forecast_series(series, horizon=24)
    diagnostics = result.diagnostics
    assert diagnostics is not None
    assert diagnostics.method == "trend+seasonal"
    assert result.seasonal is not None
    assert result.seasonal.cycle_name == "daily"

    scores = dict(diagnostics.candidate_scores)
    assert scores["trend+seasonal"] < scores["trend"]


def test_degrees_of_freedom_are_charged_for_the_seasonal_profile() -> None:
    # A profile at lag L estimates L-1 free parameters. Dividing by n-2 as
    # though it cost nothing understates sigma, and the result publishes the
    # factor a naive count would have missed rather than leaving it implicit.
    result = forecast_series(
        make_series(daily_wave(24 * 20, amplitude=15.0, sd=2.0, seed=21)), horizon=12
    )
    diagnostics = result.diagnostics
    assert diagnostics is not None
    assert diagnostics.seasonal_parameters == 23

    naive = diagnostics.degrees_of_freedom + diagnostics.seasonal_parameters
    assert diagnostics.sigma_understatement == pytest.approx(
        math.sqrt(naive / diagnostics.degrees_of_freedom)
    )
    assert diagnostics.sigma_understatement > 1.0
    assert "F407" in codes(result)
    # The profile is treated as known, so its own estimation error is excluded
    # from the width. That is a caveat, and it travels with the result.
    assert "F406" in codes(result)


def test_a_profile_costs_nothing_when_there_is_no_profile() -> None:
    result = forecast_series(make_series(noisy_ramp(100, sd=2.0, seed=11)), horizon=4)
    diagnostics = result.diagnostics
    assert diagnostics is not None
    assert diagnostics.seasonal_parameters == 0
    assert diagnostics.sigma_understatement == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Measured coverage
# ---------------------------------------------------------------------------


def test_coverage_is_measured_on_held_back_history() -> None:
    result = forecast_series(
        make_series(noisy_ramp(200, slope=0.2, sd=2.0, seed=7)), horizon=6
    )
    diagnostics = result.diagnostics
    assert diagnostics is not None
    assert diagnostics.holdout_observations > 0
    assert diagnostics.holdout_mae is not None
    coverage = diagnostics.holdout_interval_coverage
    assert coverage is not None
    assert 0.0 <= coverage <= 1.0
    # Measured coverage is reported either way: F401 when it falls short of
    # the nominal level, F408 when it does not. One of the two is always there,
    # because silence would read as a coverage claim nobody checked.
    assert {"F401", "F408"} & set(codes(result))


def test_too_short_a_history_to_hold_any_back_is_reported() -> None:
    result = forecast_series(make_series(noisy_ramp(MIN_OBSERVATIONS, sd=1.0)), horizon=2)
    assert not result.refused
    assert "F301" in codes(result)
    diagnostics = result.diagnostics
    assert diagnostics is not None
    assert diagnostics.holdout_observations == 0
    assert diagnostics.holdout_interval_coverage is None


# ---------------------------------------------------------------------------
# Refusals and warnings
# ---------------------------------------------------------------------------


def test_an_empty_series_is_refused() -> None:
    result = forecast_series(make_series([]), horizon=3)
    assert result.refused
    assert codes(result) == ["F200"]
    assert result.points == ()
    assert result.diagnostics is None


def test_too_few_observations_is_refused() -> None:
    result = forecast_series(make_series(ramp(MIN_OBSERVATIONS - 1)), horizon=3)
    assert result.refused
    assert codes(result) == ["F203"]


def test_a_hole_is_refused_unless_it_is_explicitly_allowed() -> None:
    series = make_series(noisy_ramp(100, sd=1.0), skip=(40,))
    refused = forecast_series(series, horizon=3)
    assert refused.refused
    assert codes(refused) == ["F201"]

    allowed = forecast_series(series, horizon=3, allow_holes=True)
    assert not allowed.refused
    assert "F202" in codes(allowed)
    warning = next(f for f in allowed.findings if f.code == "F202")
    assert warning.severity == "warning"


def test_a_series_with_no_sampling_period_is_refused() -> None:
    series = make_series(ramp(20), period=0)
    result = forecast_series(series, horizon=3)
    assert result.refused
    assert codes(result) == ["F207"]


def test_a_horizon_past_half_the_history_is_flagged() -> None:
    result = forecast_series(make_series(noisy_ramp(40, sd=1.0)), horizon=30)
    assert "F305" in codes(result)


def test_a_noiseless_history_is_flagged_rather_than_given_a_zero_width_band() -> None:
    # Residual spread of zero means the model reproduced the history, which is
    # a statement about the model and not about the future.
    result = forecast_series(make_series(ramp(60)), horizon=5)
    assert not result.refused
    assert "F404" in codes(result)


def test_arguments_are_validated_before_any_work_is_done() -> None:
    series = make_series(noisy_ramp(40, sd=1.0))
    with pytest.raises(ForecastError, match="horizon must be at least"):
        forecast_series(series, horizon=0)
    for level in (0.4, 1.0, 1.5):
        with pytest.raises(ForecastError, match="level must be in"):
            forecast_series(series, horizon=3, level=level)


# ---------------------------------------------------------------------------
# The open period at the end of the window
# ---------------------------------------------------------------------------


def test_an_open_period_is_dropped_for_a_statistic_it_biases() -> None:
    # A Sum over a period that has not closed is low by construction, and a
    # trend fitted through it bends downward at exactly the end that matters.
    series = make_series(noisy_ramp(40, sd=1.0), statistic="Sum")
    now = series.points[-1].timestamp + timedelta(seconds=1_800)
    result = forecast_series(series, horizon=3, now=now)
    assert "F205" in codes(result)
    diagnostics = result.diagnostics
    assert diagnostics is not None
    assert diagnostics.fitted_observations == 39


def test_an_open_period_is_kept_for_a_statistic_it_does_not_displace() -> None:
    series = make_series(noisy_ramp(40, sd=1.0), statistic="Average")
    now = series.points[-1].timestamp + timedelta(seconds=1_800)
    result = forecast_series(series, horizon=3, now=now)
    assert "F206" in codes(result)
    diagnostics = result.diagnostics
    assert diagnostics is not None
    assert diagnostics.fitted_observations == 40


def test_every_biased_statistic_names_its_direction() -> None:
    assert set(PARTIAL_PERIOD_BIAS) == {"Sum", "SampleCount", "Maximum", "Minimum"}
    assert set(PARTIAL_PERIOD_BIAS.values()) <= {"low", "high"}
    assert PARTIAL_PERIOD_BIAS["Minimum"] == "high"


# ---------------------------------------------------------------------------
# The zero floor
# ---------------------------------------------------------------------------


def test_a_clipped_lower_bound_is_marked_and_the_coverage_loss_is_stated() -> None:
    declining = make_series([max(0.5, 60.0 - 1.5 * index) for index in range(40)])
    clipped = forecast_series(declining, horizon=30)
    assert clipped.points[-1].clipped
    assert clipped.points[-1].lower == pytest.approx(0.0)
    assert "F403" in codes(clipped)

    # Without the floor the band is free to go negative, which is honest about
    # the model even where it is impossible about the metric.
    unclipped = forecast_series(declining, horizon=30, non_negative=False)
    assert unclipped.points[-1].lower < 0.0
    assert not unclipped.points[-1].clipped


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_a_result_renders_as_json() -> None:
    result = forecast_series(make_series(noisy_ramp(80, sd=2.0, seed=4)), horizon=5)
    rendered = result.as_dict()
    json.dumps(rendered)
    assert rendered["refused"] is False
    assert len(rendered["points"]) == 5
    assert rendered["findings"]


def test_a_refusal_renders_as_json_too() -> None:
    rendered = forecast_series(make_series([]), horizon=5).as_dict()
    json.dumps(rendered)
    assert rendered["refused"] is True
    assert rendered["points"] == []


def test_findings_are_partitioned_by_severity() -> None:
    result = forecast_series(make_series(noisy_ramp(40, sd=1.0)), horizon=30)
    severities = {finding.severity for finding in result.findings}
    assert severities <= {"error", "warning", "note"}
    assert all(finding.severity == "error" for finding in result.errors)
    assert all(finding.severity == "warning" for finding in result.warnings)
    assert set(result.errors) | set(result.warnings) <= set(result.findings)
