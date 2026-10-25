"""Checks on the saturation analysis: the limit, the distribution, the ceiling.

Utilization is not headroom. The gap between them is the elasticity
``rho / (1 - rho)`` -- 1 at 50% load, 4 at 80%, 9 at 90% -- so reading 50%
utilization as "twice the room" is wrong by a factor this module is built to
report. Most of what can go wrong here is therefore not an exception but an
over-confident number, and the checks below are organised around the four
places confidence can exceed the evidence.

* **The denominator.** An observed maximum is the limit people reach for when
  nothing is documented, and it is not a limit: it bounds one from below. Using
  it silently inflates nothing and understates everything, so its provenance
  travels with it and changes the conclusion.
* **The percentile.** A sample of n order statistics speaks to nothing above
  ``1 - 1/n``, and a percentile resting on the top two observations describes
  those two observations rather than a tail.
* **Sustained time, not sample count.** A hundred isolated spikes and one
  sustained hour give the same count above a threshold, and only the second
  builds a queue. A hole in the grid ends a run rather than extending it.
* **The assumptions behind the ceiling.** Every one of them fails toward
  optimism, so a ceiling carrying an applicability finding is an upper bound on
  the safe operating point rather than an estimate of it.

The refusals are checked as carefully as the arithmetic. In particular, a
series that exceeds its own declared limit is refused outright: the limit is
wrong, the metric is not what it claims to be, or both, and every headroom
number derivable from that window is meaningless until that is settled.
"""

from __future__ import annotations

import json
import math

import pytest

from models.queueing import QueueingError
from models.saturation import (
    BURSTY_PEAK_TO_MEAN,
    CLUSTERED_AUTOCORRELATION,
    DEFAULT_ELASTICITY_TARGET,
    FINDING_CODES,
    MIN_OBSERVATIONS,
    NON_STATIONARY_TREND,
    Applicability,
    CapacityLimit,
    SaturationError,
    SaturationResult,
    analyse_saturation,
    check_little_law,
    coefficient_of_variation,
    highest_resolvable_percentile,
    lag1_autocorrelation,
    linear_trend,
    percentile,
)
from tests.support import make_series, noise, ramp

LIMIT = CapacityLimit(value=100.0, unit="Percent", provenance="hard")


def codes(result: SaturationResult) -> list[str]:
    return [finding.code for finding in result.findings]


def flat(value: float, count: int = 100) -> list[float]:
    return [value] * count


# ---------------------------------------------------------------------------
# The limit
# ---------------------------------------------------------------------------


def test_a_limit_must_be_positive() -> None:
    for value in (0.0, -1.0):
        with pytest.raises(SaturationError, match="must be positive"):
            CapacityLimit(value=value, unit="Percent", provenance="hard")


def test_an_observed_maximum_is_not_a_real_limit() -> None:
    observed = CapacityLimit(value=100.0, unit="Percent", provenance="observed")
    assert not observed.is_a_real_limit
    for provenance in ("hard", "quota", "configured"):
        limit = CapacityLimit(value=100.0, unit="Percent", provenance=provenance)
        assert limit.is_a_real_limit


def test_the_limits_provenance_changes_what_is_reported() -> None:
    series = make_series(flat(50.0))

    observed = analyse_saturation(
        series, CapacityLimit(value=100.0, unit="Percent", provenance="observed")
    )
    assert "F505" in codes(observed)

    quota = analyse_saturation(
        series, CapacityLimit(value=100.0, unit="Percent", provenance="quota")
    )
    assert "F506" in codes(quota)

    hard = analyse_saturation(series, LIMIT)
    assert not {"F505", "F506"} & set(codes(hard))


def test_a_unit_mismatch_is_reported_rather_than_resolved() -> None:
    result = analyse_saturation(
        make_series(flat(50.0)), CapacityLimit(value=100.0, unit="Count",
                                               provenance="hard")
    )
    assert "F504" in codes(result)


# ---------------------------------------------------------------------------
# Percentiles
# ---------------------------------------------------------------------------


def test_percentile_follows_the_stated_convention() -> None:
    # The (n - 1) * p convention, read off the order statistics and
    # interpolated between neighbours. The nine common definitions disagree
    # exactly at the top of a short sample, which is where this module looks.
    values = [0.0, 10.0, 20.0, 30.0, 40.0]
    assert percentile(values, 0.0) == pytest.approx(0.0)
    assert percentile(values, 0.25) == pytest.approx(10.0)
    assert percentile(values, 0.5) == pytest.approx(20.0)
    assert percentile(values, 0.9) == pytest.approx(36.0)
    assert percentile(values, 1.0) == pytest.approx(40.0)


def test_percentile_never_extrapolates_beyond_the_sample() -> None:
    values = list(noise(50, base=10.0, sd=2.0, seed=4))
    for probability in (0.0, 0.5, 0.99, 1.0):
        assert min(values) <= percentile(values, probability) <= max(values)


def test_percentile_is_order_independent() -> None:
    values = [5.0, 1.0, 9.0, 3.0, 7.0]
    assert percentile(values, 0.75) == pytest.approx(percentile(sorted(values), 0.75))


def test_percentile_inputs_are_validated() -> None:
    with pytest.raises(SaturationError):
        percentile([], 0.5)
    for probability in (-0.1, 1.1):
        with pytest.raises(SaturationError):
            percentile([1.0, 2.0], probability)


def test_the_highest_resolvable_percentile_is_one_minus_one_over_n() -> None:
    # 100 samples resolve p99 and no more. Asking for p99.9 of 100 samples
    # returns the maximum while implying a thousand-sample claim.
    assert highest_resolvable_percentile(100) == pytest.approx(0.99)
    assert highest_resolvable_percentile(1_000) == pytest.approx(0.999)
    assert highest_resolvable_percentile(2) == pytest.approx(0.5)
    assert highest_resolvable_percentile(1) == 0.0
    assert highest_resolvable_percentile(0) == 0.0


def test_a_percentile_finer_than_the_sample_is_downgraded_and_said_so() -> None:
    result = analyse_saturation(
        make_series(list(ramp(20, base=50.0, slope=0.1))), LIMIT,
        planning_percentile=0.999,
    )
    assert "F510" in codes(result)
    profile = result.profile
    assert profile is not None
    assert profile.planning_percentile == pytest.approx(0.95)
    assert profile.resolvable_percentile == pytest.approx(0.95)


def test_a_percentile_resting_on_the_top_two_samples_is_marked() -> None:
    result = analyse_saturation(make_series(flat(50.0)), LIMIT)
    profile = result.profile
    assert profile is not None
    assert profile.determined_by_top_two
    assert "F511" in codes(result)


def test_peak_to_mean_is_the_means_under_provisioning_factor() -> None:
    result = analyse_saturation(make_series(flat(10.0, 90) + flat(90.0, 10)), LIMIT)
    profile = result.profile
    assert profile is not None
    assert profile.mean == pytest.approx(0.18)
    assert profile.peak_to_mean is not None
    assert profile.peak_to_mean == pytest.approx(profile.planning_value / profile.mean)
    assert "F512" in codes(result)


# ---------------------------------------------------------------------------
# Series statistics
# ---------------------------------------------------------------------------


def test_autocorrelation_signs_are_what_they_should_be() -> None:
    assert lag1_autocorrelation([0.5, 0.6] * 50) == pytest.approx(-0.99, abs=0.01)
    rising = lag1_autocorrelation(list(ramp(50, base=0.1, slope=0.001)))
    assert rising is not None and rising > 0.9
    assert lag1_autocorrelation([1.0, 2.0]) is None


def test_a_series_constant_within_rounding_has_no_measurable_autocorrelation() -> None:
    # A flat resource is not evidence of independence, so None is right. The
    # trap is that 80 against a limit of 100 is not exactly representable: the
    # residual spread is a few parts in ten quadrillion, and correlating it
    # returns 0.99 and reports a flat series as one that arrives in clusters.
    assert lag1_autocorrelation(flat(0.5)) is None
    assert lag1_autocorrelation([80.0 / 100.0] * 100) is None
    assert lag1_autocorrelation([value / 100.0 for value in flat(80.0)]) is None

    result = analyse_saturation(make_series(flat(80.0)), LIMIT)
    applicability = result.applicability
    assert applicability is not None
    assert applicability.lag1_autocorrelation is None
    assert "F602" not in codes(result)


def test_a_real_spread_is_still_measured() -> None:
    measured = lag1_autocorrelation([0.5 + 1e-6 * (index % 2) for index in range(100)])
    assert measured is not None
    assert measured < -0.5


def test_linear_trend_is_the_slope_per_sample() -> None:
    assert linear_trend(list(ramp(40, base=5.0, slope=0.25))) == pytest.approx(0.25)
    assert linear_trend(flat(3.0, 40)) == pytest.approx(0.0)
    assert linear_trend([1.0, 2.0]) is None


def test_coefficient_of_variation_is_relative_spread() -> None:
    assert coefficient_of_variation(flat(4.0, 20)) == pytest.approx(0.0, abs=1e-12)
    assert coefficient_of_variation([0.0, 0.0]) is None
    assert coefficient_of_variation([1.0]) is None
    values = [8.0, 12.0]
    expected = math.sqrt(((8.0 - 10.0) ** 2 + (12.0 - 10.0) ** 2) / 1.0) / 10.0
    assert coefficient_of_variation(values) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Sustained time above a threshold
# ---------------------------------------------------------------------------


def test_sustained_and_spiky_windows_are_not_the_same_risk() -> None:
    sustained = analyse_saturation(make_series(flat(50.0, 80) + flat(85.0, 20)), LIMIT)
    spiky = analyse_saturation(
        make_series([50.0, 85.0] * 10 + flat(50.0, 80)), LIMIT
    )

    first = sustained.exceedance
    second = spiky.exceedance
    assert first is not None and second is not None
    assert first.points_above == 20
    assert first.run_count == 1
    assert first.longest_run_points == 20
    assert first.longest_run_seconds == 20 * sustained.period

    assert second.points_above == 10
    assert second.run_count == 10
    assert second.longest_run_points == 1


def test_a_hole_ends_a_run_rather_than_bridging_it() -> None:
    # A gap is not evidence of being above the threshold and not evidence of
    # being below it, so the run stops and the fact is counted.
    result = analyse_saturation(
        make_series(flat(85.0), skip=(25,)), LIMIT, allow_holes=True
    )
    exceedance = result.exceedance
    assert exceedance is not None
    assert exceedance.run_count == 2
    assert exceedance.runs_ended_by_hole == 1
    assert exceedance.longest_run_points == 74
    assert "F502" in codes(result)


def test_time_above_the_threshold_is_reported_when_there_is_any() -> None:
    result = analyse_saturation(make_series(flat(50.0, 80) + flat(85.0, 20)), LIMIT)
    assert "F513" in codes(result)
    quiet = analyse_saturation(make_series(flat(20.0)), LIMIT)
    assert "F513" not in codes(quiet)


# ---------------------------------------------------------------------------
# Applicability of the closed form
# ---------------------------------------------------------------------------


def build_applicability(
    *,
    peak_to_mean: float | None = 1.0,
    autocorrelation: float | None = 0.0,
    trend_fraction: float | None = 0.0,
) -> Applicability:
    return Applicability(
        peak_to_mean=peak_to_mean,
        coefficient_of_variation=0.1,
        lag1_autocorrelation=autocorrelation,
        trend_per_period=0.0,
        trend_over_window=0.0,
        trend_fraction_of_mean=trend_fraction,
        source_statistic="Average",
        period_seconds=3_600,
        service_cv_squared_measured=True,
    )


def test_assumptions_hold_only_when_nothing_measured_refutes_them() -> None:
    assert build_applicability().assumptions_hold


@pytest.mark.parametrize(
    "overrides",
    (
        {"peak_to_mean": BURSTY_PEAK_TO_MEAN + 0.5},
        {"autocorrelation": CLUSTERED_AUTOCORRELATION + 0.1},
        {"trend_fraction": NON_STATIONARY_TREND + 0.1},
        {"trend_fraction": -(NON_STATIONARY_TREND + 0.1)},
    ),
)
def test_each_gate_fails_on_its_own(overrides: dict[str, float]) -> None:
    # Three independent departures, each decisive by itself. A single combined
    # test would pass while two of the three were dead.
    assert not build_applicability(**overrides).assumptions_hold


def test_an_unmeasured_quantity_cannot_refute_anything() -> None:
    assert build_applicability(
        peak_to_mean=None, autocorrelation=None, trend_fraction=None
    ).assumptions_hold


def test_burstiness_clustering_and_drift_are_each_reported() -> None:
    bursty = analyse_saturation(make_series(flat(10.0, 90) + flat(90.0, 10)), LIMIT)
    assert "F601" in codes(bursty)

    drifting = analyse_saturation(make_series(list(ramp(100, base=20.0, slope=0.5))), LIMIT)
    assert "F603" in codes(drifting)
    applicability = drifting.applicability
    assert applicability is not None
    assert applicability.trend_fraction_of_mean is not None
    assert applicability.trend_fraction_of_mean > NON_STATIONARY_TREND


def test_assumed_service_variability_is_disclosed_and_measured_is_not() -> None:
    assumed = analyse_saturation(make_series(flat(50.0)), LIMIT)
    assert "F604" in codes(assumed)
    measured = analyse_saturation(make_series(flat(50.0)), LIMIT, service_cv_squared=1.0)
    assert "F604" not in codes(measured)


def test_an_averaging_statistic_divides_bursts_shorter_than_its_window() -> None:
    averaged = analyse_saturation(make_series(flat(50.0), statistic="Average"), LIMIT)
    assert "F605" in codes(averaged)
    assert "F606" not in codes(averaged)

    peaks = analyse_saturation(make_series(flat(50.0), statistic="Maximum"), LIMIT)
    assert "F606" in codes(peaks)
    assert "F605" not in codes(peaks)


# ---------------------------------------------------------------------------
# Headroom
# ---------------------------------------------------------------------------


def test_usable_headroom_is_smaller_than_the_linear_reading() -> None:
    # At 50% utilization the linear reading promises 2.0x of growth. The
    # elasticity ceiling of 80% says 1.6x, and the 1.25x gap between them is
    # the number a plan is otherwise wrong by.
    result = analyse_saturation(make_series(flat(50.0)), LIMIT)
    headroom = result.headroom
    assert headroom is not None
    assert headroom.elasticity_target == pytest.approx(DEFAULT_ELASTICITY_TARGET)
    assert headroom.ceiling_from_elasticity == pytest.approx(0.8)
    assert headroom.demand_multiplier == pytest.approx(1.6)
    assert headroom.naive_demand_multiplier == pytest.approx(2.0)
    assert headroom.overstatement_factor == pytest.approx(1.25)
    assert headroom.growth_headroom_percent == pytest.approx(60.0)
    assert not headroom.past_ceiling
    assert "F521" in codes(result)


def test_an_operating_point_at_the_ceiling_has_no_headroom_to_allocate() -> None:
    result = analyse_saturation(make_series(flat(80.0)), LIMIT)
    headroom = result.headroom
    assert headroom is not None
    assert headroom.past_ceiling
    assert headroom.demand_multiplier == pytest.approx(1.0)
    assert "F520" in codes(result)
    assert any(finding.code == "F520" and finding.severity == "error"
               for finding in result.findings)


def test_elasticity_at_the_operating_point_is_the_exchange_rate() -> None:
    for utilization, elasticity in ((0.5, 1.0), (0.75, 3.0), (0.9, 9.0)):
        result = analyse_saturation(make_series(flat(utilization * 100.0)), LIMIT)
        headroom = result.headroom
        assert headroom is not None
        assert headroom.elasticity_now == pytest.approx(elasticity, rel=1e-9)


def test_pooling_raises_the_ceiling_and_the_headroom_with_it() -> None:
    single = analyse_saturation(make_series(flat(50.0)), LIMIT)
    pooled = analyse_saturation(make_series(flat(50.0)), LIMIT, servers=8)
    assert single.headroom is not None and pooled.headroom is not None
    assert pooled.headroom.ceiling_from_elasticity is not None
    assert single.headroom.ceiling_from_elasticity is not None
    assert pooled.headroom.ceiling_from_elasticity > single.headroom.ceiling_from_elasticity
    assert pooled.queue is not None and pooled.queue.discipline == "mmc"


def test_service_variability_lowers_the_ceiling() -> None:
    exponential = analyse_saturation(make_series(flat(50.0)), LIMIT, service_cv_squared=1.0)
    variable = analyse_saturation(make_series(flat(50.0)), LIMIT, service_cv_squared=9.0)
    assert exponential.headroom is not None and variable.headroom is not None
    assert variable.headroom.ceiling_from_elasticity is not None
    assert exponential.headroom.ceiling_from_elasticity is not None
    assert (
        variable.headroom.ceiling_from_elasticity
        < exponential.headroom.ceiling_from_elasticity
    )


def test_a_response_budget_needs_a_service_time_to_mean_anything() -> None:
    without = analyse_saturation(make_series(flat(50.0)), LIMIT, response_budget=1.0)
    assert "F523" in codes(without)
    assert without.headroom is not None
    assert without.headroom.ceiling_from_response is None


def test_a_budget_below_the_service_time_is_a_sizing_problem() -> None:
    result = analyse_saturation(
        make_series(flat(50.0)), LIMIT, service_time=2.0, response_budget=0.5
    )
    assert "F524" in codes(result)
    assert result.headroom is not None
    assert result.headroom.ceiling_from_response is None


def test_the_binding_ceiling_is_the_lower_of_the_two() -> None:
    result = analyse_saturation(
        make_series(flat(50.0)), LIMIT, service_time=0.1, response_budget=0.15
    )
    headroom = result.headroom
    assert headroom is not None
    assert headroom.ceiling_from_response is not None
    assert headroom.ceiling_from_elasticity is not None
    assert headroom.binding_ceiling == pytest.approx(
        min(headroom.ceiling_from_response, headroom.ceiling_from_elasticity)
    )


def test_an_operating_point_at_full_utilization_cannot_be_queued() -> None:
    # Exactly at the limit there is no steady state, so the queue is refused
    # rather than evaluated at a clamped utilization.
    result = analyse_saturation(make_series(flat(100.0)), LIMIT)
    assert "F522" in codes(result)
    assert result.queue is None
    assert result.headroom is not None
    assert math.isinf(result.headroom.elasticity_now)


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_an_empty_series_is_refused() -> None:
    result = analyse_saturation(make_series([]), LIMIT)
    assert result.refused
    assert codes(result) == ["F500"]
    assert result.profile is None and result.headroom is None


def test_too_few_observations_is_refused() -> None:
    result = analyse_saturation(make_series(flat(50.0, MIN_OBSERVATIONS - 1)), LIMIT)
    assert result.refused
    assert codes(result) == ["F501"]


def test_a_hole_is_refused_unless_it_is_explicitly_allowed() -> None:
    series = make_series(flat(50.0), skip=(40,))
    refused = analyse_saturation(series, LIMIT)
    assert refused.refused
    assert codes(refused) == ["F502"]
    assert refused.findings[0].severity == "error"

    allowed = analyse_saturation(series, LIMIT, allow_holes=True)
    assert not allowed.refused
    assert any(f.code == "F502" and f.severity == "warning" for f in allowed.findings)


def test_a_series_that_exceeds_its_own_limit_is_refused() -> None:
    # Either the limit is wrong or the metric does not measure what the limit
    # bounds. Both make every headroom number from this window meaningless.
    result = analyse_saturation(make_series(flat(50.0, 99) + [120.0]), LIMIT)
    assert result.refused
    assert codes(result) == ["F507"]
    assert "20%" in result.findings[0].message


def test_arguments_are_validated_before_any_work_is_done() -> None:
    series = make_series(flat(50.0))
    for probability in (0.0, 1.0, 1.5):
        with pytest.raises(SaturationError, match="planning percentile"):
            analyse_saturation(series, LIMIT, planning_percentile=probability)
    for threshold in (0.0, 1.5):
        with pytest.raises(SaturationError, match="exceedance threshold"):
            analyse_saturation(series, LIMIT, exceedance_threshold=threshold)
    with pytest.raises(SaturationError, match="service time must be positive"):
        analyse_saturation(series, LIMIT, service_time=0.0)


def test_an_unmodellable_queue_is_refused_as_an_argument_error() -> None:
    # There is no exact several-server result for general service times. The
    # alternative to refusing here is a QueueingError raised from the middle of
    # an analysis that has already recorded findings about the window.
    with pytest.raises(SaturationError, match="cannot be modelled across"):
        analyse_saturation(
            make_series(flat(50.0)), LIMIT, servers=4, service_cv_squared=9.0
        )
    # One server with the same variability is a model that exists.
    single = analyse_saturation(
        make_series(flat(50.0)), LIMIT, servers=1, service_cv_squared=9.0
    )
    assert single.queue is not None
    assert single.queue.discipline == "mg1"


def test_the_queueing_layer_still_refuses_the_same_combination() -> None:
    with pytest.raises(QueueingError, match="single server"):
        from models.queueing import QueueModel

        QueueModel(servers=4, service_time=1.0, utilization=0.5, cv_squared=9.0)


# ---------------------------------------------------------------------------
# Little's law
# ---------------------------------------------------------------------------


def test_little_law_agreement_is_reported_as_a_note() -> None:
    findings = check_little_law(concurrency=10.0, throughput=2.0, latency=5.0)
    assert [finding.code for finding in findings] == ["F531"]
    assert findings[0].severity == "note"


def test_little_law_disagreement_is_a_measurement_fault() -> None:
    findings = check_little_law(concurrency=10.0, throughput=2.0, latency=2.0)
    assert [finding.code for finding in findings] == ["F532"]
    assert findings[0].severity == "warning"
    assert "cannot fail" in findings[0].message


def test_little_law_tolerance_is_respected() -> None:
    within = check_little_law(concurrency=10.0, throughput=2.0, latency=4.6)
    assert [finding.code for finding in within] == ["F531"]
    outside = check_little_law(concurrency=10.0, throughput=2.0, latency=4.4)
    assert [finding.code for finding in outside] == ["F532"]


def test_little_law_cannot_be_checked_without_concurrency() -> None:
    findings = check_little_law(concurrency=0.0, throughput=2.0, latency=5.0)
    assert [finding.code for finding in findings] == ["F530"]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_a_result_renders_as_json() -> None:
    result = analyse_saturation(make_series(flat(50.0)), LIMIT, service_time=0.2)
    rendered = result.as_dict()
    json.dumps(rendered)
    assert rendered["refused"] is False
    assert rendered["limit"]["provenance"] == "hard"
    assert rendered["findings"]


def test_a_refusal_renders_as_json_too() -> None:
    rendered = analyse_saturation(make_series([]), LIMIT).as_dict()
    json.dumps(rendered)
    assert rendered["refused"] is True
    assert rendered["utilization"] is None
    assert rendered["headroom"] is None


def test_every_emitted_code_is_registered_with_a_description() -> None:
    result = analyse_saturation(make_series(flat(85.0), skip=(25,)), LIMIT,
                                allow_holes=True)
    for finding in result.findings:
        assert finding.code in FINDING_CODES
        assert FINDING_CODES[finding.code]
