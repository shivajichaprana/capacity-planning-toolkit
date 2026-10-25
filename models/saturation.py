"""Saturation analysis: turning a utilization number into a statement about headroom.

A monitoring dashboard reports utilization. A capacity plan needs headroom.
These are not the same quantity, and the gap between them is where most
capacity work goes wrong. This module closes it, and it is deliberately
pessimistic in four specific places where the convenient reading is wrong.

**The mean is the wrong operating point.** A resource averaging 40% over a week
may spend an hour every afternoon at 95%, and it is the hour that decides
whether the service stays up. So the operating point here is a chosen upper
percentile of the observed utilization, the mean is reported next to it, and
the ratio between them -- the peak-to-mean -- is reported as the factor by which
planning to the mean under-provisions. Provisioning is a statement about the
peak; only cost is a statement about the mean.

**A percentile the sample cannot resolve is not reported as a percentile.** The
99th percentile of fifty samples is the largest of fifty samples, which is an
estimate of roughly the 98th percentile and has no information about the 99th.
Order statistics can resolve a percentile ``p`` only when the sample holds more
than ``1 / (1 - p)`` points, so a p99 over a short window is refused and the
highest resolvable percentile is reported instead of being quietly substituted.

**An averaging window hides the burst that saturates the resource.** A series of
five-minute averages cannot show a thirty-second spike; the spike is divided by
ten before it reaches the series. Every observed peak is therefore a lower bound
on the instantaneous peak, and the bound is weaker the coarser the series. The
dilution is reported with the sampling period that caused it, and a series of
averages is flagged for it rather than treated as a record of peaks.

**A limit needs provenance.** Utilization is a ratio, and the denominator is an
assumption. A hard architectural limit, a raisable service quota, a configured
pool size and the largest value ever observed are four different denominators,
and the last of these is not a limit at all -- it is a lower bound on one, so
any utilization computed against it is an upper bound on the truth. The limit is
therefore a typed value carrying where it came from, and an observed maximum is
flagged every time.

With those four corrections made, the headroom question is handed to the
queueing module, which converts the operating point into the demand multiplier
the resource can absorb before a stated latency sensitivity is exceeded. That
multiplier is reported beside the one you get by treating utilization as linear
headroom -- ``1 / rho`` -- because the ratio between them is the size of the
error this module exists to prevent. At 80% utilization the linear reading
promises 1.25x growth; an elasticity ceiling of 4 says there is none left at
all.

The queueing conversion assumes Poisson arrivals and a stationary rate, and a
measured series satisfies neither. Those assumptions are tested here rather than
assumed: burstiness, lag-one autocorrelation and trend over the window are all
measured on the real series and reported as applicability findings, because a
ceiling derived under an assumption the data refutes should arrive with that
refutation attached. Where the assumptions fail, they fail in the direction of
optimism, so every ceiling here is an upper bound on the safe operating point.

Finding codes: ``F5xx`` is saturation evidence -- the limit, the percentiles,
the dilution, the exceedance runs. ``F6xx`` is queueing applicability -- every
measured reason the closed-form ceiling is more generous than reality.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Final, Literal

from ingest.runtime import add_common_arguments, configure_logging
from ingest.store import (
    MetricSeries,
    ObservationStore,
    SeriesKey,
    canonical_entity,
)
from models.queueing import (
    QueueModel,
    QueueingError,
    little_law_residual,
    utilization_for_elasticity,
    utilization_for_mean_response,
)
from models.seasonality import Finding, SeriesGrid, to_grid

_LOG = logging.getLogger(__name__)

#: Where the denominator of a utilization ratio came from.
#:
#: ``hard``
#:     A limit of the architecture or the hardware: the core count, the
#:     provisioned IOPS, the memory on the box. Not raisable without changing
#:     the thing being measured.
#: ``quota``
#:     A provider-side limit that can be raised by asking. Saturation against a
#:     quota is a procurement problem, not an engineering one, and the
#:     distinction changes what the recommendation should say.
#: ``configured``
#:     A limit this service set on itself: a pool size, a thread count, a
#:     concurrency cap. Raisable by deploying, and often raisable into a
#:     downstream limit that is harder to see.
#: ``observed``
#:     The largest value ever seen. This is not a limit. It is a lower bound on
#:     one, so utilization computed against it is an upper bound and the real
#:     headroom is at least what is reported.
Provenance = Literal["hard", "quota", "configured", "observed"]

#: Fewest observations before any percentile or trend statement is attempted.
MIN_OBSERVATIONS: Final[int] = 12

#: Default operating percentile. High enough to be about the peak rather than
#: the body of the distribution, low enough that a few hours of one-minute
#: samples can resolve it.
DEFAULT_PLANNING_PERCENTILE: Final[float] = 0.99

#: Default utilization above which a point counts toward an exceedance run.
DEFAULT_EXCEEDANCE_THRESHOLD: Final[float] = 0.8

#: Default elasticity ceiling: how many percent of latency a percent of demand
#: growth may cost. 4 is 80% utilization for a single exponential server, which
#: is the conventional ceiling -- stated here as the sensitivity it actually
#: encodes rather than as a bare number everyone repeats.
DEFAULT_ELASTICITY_TARGET: Final[float] = 4.0

#: Peak-to-mean above which the Poisson assumption is reported as refuted. A
#: Poisson count process over a window holding ``n`` expected arrivals has a
#: peak-to-mean near ``1 + 3/sqrt(n)``; sustained ratios well above 2 indicate
#: arrivals that cluster, which raises delay at any given utilization.
BURSTY_PEAK_TO_MEAN: Final[float] = 2.0

#: Lag-one autocorrelation above which arrivals are reported as clustered.
#: Independent arrivals give a correlation near zero; high values mean a busy
#: period predicts another, which is exactly the regime where the closed-form
#: wait understates the real one.
CLUSTERED_AUTOCORRELATION: Final[float] = 0.5

#: Trend over the window, as a fraction of the window mean, above which the
#: series is reported as non-stationary.
NON_STATIONARY_TREND: Final[float] = 0.25

#: Relative disagreement in a Little's-law triple worth reporting. Below this,
#: the gap is plausibly measurement noise and window misalignment.
LITTLE_LAW_TOLERANCE: Final[float] = 0.1

#: Spread, as a fraction of the series' own level, below which a series is
#: constant as far as any correlation is concerned. What remains below this is
#: floating-point representation error rather than variation in the workload.
NEGLIGIBLE_RELATIVE_SPREAD: Final[float] = 1e-12

#: Every finding this module can emit, grouped by what it is about, so a report
#: can resolve a code without reading the source and a reviewer can see the
#: whole set of objections at once.
#:
#: ``F50x`` concern the input and the limit, ``F51x`` the utilization
#: distribution, ``F52x`` the headroom conversion, ``F53x`` the Little's-law
#: cross-check, and ``F6xx`` the measured departures from the queueing
#: assumptions.
FINDING_CODES: Final[dict[str, str]] = {
    "F500": "the series holds no observations",
    "F501": "too few observations to support a percentile or a trend",
    "F502": "coverage gap: a peak inside the hole would be invisible",
    "F503": "observations that do not sit on the sampling grid",
    "F504": "the series unit and the limit unit disagree",
    "F505": "the limit is an observed maximum, which is not a limit",
    "F506": "the limit is a service quota, raisable by request",
    "F507": "observations exceed the declared limit",
    "F510": "the requested percentile is finer than the sample can resolve",
    "F511": "the reported percentile rests on only the top two samples",
    "F512": "peak-to-mean: how far planning to the mean under-provisions",
    "F513": "sustained time above the exceedance threshold",
    "F520": "the operating point is already at or above the planning ceiling",
    "F521": "reading utilization as linear headroom overstates usable growth",
    "F522": "the operating point cannot be evaluated as a queue",
    "F523": "a response budget was given without a mean service time",
    "F524": "the response budget is below the mean service time",
    "F530": "concurrency is zero, so Little's law cannot be checked",
    "F531": "Little's law holds across the three measurements",
    "F532": "Little's law is violated, so one of the three is mismeasured",
    "F601": "arrivals are burstier than Poisson",
    "F602": "arrivals cluster: lag-one autocorrelation is high",
    "F603": "utilization drifts across the window: no stationary operating point",
    "F604": "service-time variability was assumed rather than measured",
    "F605": "an averaging statistic divides bursts shorter than its window",
    "F606": "collection resolution bounds the peak from below",
}


class SaturationError(ValueError):
    """Raised when a saturation analysis cannot be performed as asked."""


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CapacityLimit:
    """The denominator of a utilization ratio, with where it came from.

    Provenance is required because the four kinds of limit lead to four
    different recommendations, and because an observed maximum -- the one
    people reach for when no real limit is documented -- is not a limit and
    must not be silently used as one.
    """

    value: float
    unit: str
    provenance: Provenance
    note: str = ""

    def __post_init__(self) -> None:
        if self.value <= 0.0:
            raise SaturationError(f"capacity limit must be positive, got {self.value}")

    @property
    def is_a_real_limit(self) -> bool:
        """False for an observed maximum, which only bounds the limit below."""
        return self.provenance != "observed"


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def highest_resolvable_percentile(observations: int) -> float:
    """The highest percentile that ``observations`` order statistics can resolve.

    With ``n`` samples the largest is an estimate of the ``1 - 1/n`` quantile,
    and nothing in the sample speaks to anything above it. So 100 samples
    resolve p99 and no more, and asking for p99.9 from 100 samples returns the
    maximum while implying a thousand-sample claim.
    """
    if observations < 2:
        return 0.0
    return 1.0 - 1.0 / observations


def percentile(values: Sequence[float], probability: float) -> float:
    """Percentile by linear interpolation between order statistics.

    The method is stated rather than inherited because the nine common
    percentile definitions disagree by a meaningful amount in exactly the place
    this module cares about -- the top of a short sample. This is the
    ``(n - 1) * p`` convention: the value is read off the order statistics at
    position ``(n - 1) * p`` and interpolated between its neighbours, so it
    never extrapolates beyond the observed range and returns the sample
    maximum only at ``p = 1``.

    Resolvability is a separate question from which value comes back, and
    :func:`highest_resolvable_percentile` answers it: a percentile above
    ``1 - 1/n`` is a claim the sample size cannot support however the
    interpolation is done.
    """
    if not values:
        raise SaturationError("cannot take a percentile of an empty sample")
    if not 0.0 <= probability <= 1.0:
        raise SaturationError(f"probability must be in [0, 1], got {probability}")

    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower_index = math.floor(position)
    upper_index = min(lower_index + 1, len(ordered) - 1)
    weight = position - lower_index
    return ordered[lower_index] * (1.0 - weight) + ordered[upper_index] * weight


def lag1_autocorrelation(values: Sequence[float]) -> float | None:
    """Sample correlation between consecutive points, or ``None`` if undefined.

    Undefined for a constant series, where the denominator vanishes. A constant
    series is not evidence of independence, so ``None`` is returned rather than
    zero.

    "Constant" has to mean constant *within rounding* rather than bit-identical.
    A series of equal measurements that are not exactly representable -- any
    reading of 80 against a limit of 100, for one -- leaves a spread of a few
    parts in ten quadrillion, and a correlation computed from that spread is a
    correlation of representation error: it comes back near 1.0 and reports a
    flat resource as one whose load arrives in clusters. So the spread is
    compared against the level it sits on before it is believed.
    """
    if len(values) < 3:
        return None
    mean = sum(values) / len(values)
    centred = [value - mean for value in values]
    denominator = math.fsum(value * value for value in centred)
    if denominator <= 0.0:
        return None
    root_mean_square = math.sqrt(denominator / len(values))
    if root_mean_square <= NEGLIGIBLE_RELATIVE_SPREAD * max(abs(mean), 1.0):
        return None
    numerator = sum(
        centred[index] * centred[index + 1] for index in range(len(centred) - 1)
    )
    return numerator / denominator


def linear_trend(values: Sequence[float]) -> float | None:
    """Least-squares slope per sample, or ``None`` when it cannot be fitted.

    Used only to judge stationarity, so the intercept is not returned. The
    slope is per sample rather than per unit time, because the caller knows the
    period and the comparison that matters -- total drift against the window
    mean -- is made in :func:`_applicability`.
    """
    count = len(values)
    if count < 3:
        return None
    mean_index = (count - 1) / 2.0
    mean_value = sum(values) / count
    covariance = sum(
        (index - mean_index) * (value - mean_value) for index, value in enumerate(values)
    )
    variance = sum((index - mean_index) ** 2 for index in range(count))
    if variance <= 0.0:  # pragma: no cover - requires count < 2
        return None
    return covariance / variance


def coefficient_of_variation(values: Sequence[float]) -> float | None:
    """Standard deviation over mean, or ``None`` when the mean is not positive."""
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    if mean <= 0.0:
        return None
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(variance) / mean


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UtilizationProfile:
    """The distribution of utilization over the window, summarised honestly."""

    observations: int
    mean: float
    median: float
    planning_percentile: float
    planning_value: float
    maximum: float
    minimum: float
    resolvable_percentile: float
    determined_by_top_two: bool

    @property
    def peak_to_mean(self) -> float | None:
        """Planning percentile over the mean: the mean's under-provisioning factor."""
        if self.mean <= 0.0:
            return None
        return self.planning_value / self.mean

    @property
    def max_to_mean(self) -> float | None:
        if self.mean <= 0.0:
            return None
        return self.maximum / self.mean


@dataclass(frozen=True, slots=True)
class Exceedance:
    """How long the resource stayed above a threshold, not merely how often.

    Counting points above a threshold conflates a hundred isolated spikes with
    one sustained hour, and a queue only builds in the second case. Runs are
    therefore measured as consecutive runs on the sampling grid, and a hole in
    the grid ends a run rather than extending it: a gap is not evidence of
    being above the threshold and not evidence of being below it.
    """

    threshold: float
    points_above: int
    fraction_above: float
    run_count: int
    longest_run_points: int
    longest_run_seconds: int
    total_seconds_above: int
    runs_ended_by_hole: int


@dataclass(frozen=True, slots=True)
class Applicability:
    """Measured departures from the assumptions the queueing ceiling rests on."""

    peak_to_mean: float | None
    coefficient_of_variation: float | None
    lag1_autocorrelation: float | None
    trend_per_period: float | None
    trend_over_window: float | None
    trend_fraction_of_mean: float | None
    source_statistic: str
    period_seconds: int
    service_cv_squared_measured: bool

    @property
    def assumptions_hold(self) -> bool:
        """True when nothing measured here refutes the closed-form model."""
        if self.peak_to_mean is not None and self.peak_to_mean > BURSTY_PEAK_TO_MEAN:
            return False
        if (
            self.lag1_autocorrelation is not None
            and self.lag1_autocorrelation > CLUSTERED_AUTOCORRELATION
        ):
            return False
        if (
            self.trend_fraction_of_mean is not None
            and abs(self.trend_fraction_of_mean) > NON_STATIONARY_TREND
        ):
            return False
        return True


@dataclass(frozen=True, slots=True)
class Headroom:
    """What the operating point leaves, and what a linear reading would claim."""

    utilization: float
    elasticity_target: float
    elasticity_now: float
    ceiling_from_elasticity: float | None
    ceiling_from_response: float | None
    binding_ceiling: float | None
    demand_multiplier: float | None
    naive_demand_multiplier: float
    overstatement_factor: float | None
    past_ceiling: bool

    @property
    def growth_headroom_percent(self) -> float | None:
        """Demand growth available before the ceiling, as a percentage."""
        if self.demand_multiplier is None:
            return None
        return (self.demand_multiplier - 1.0) * 100.0


@dataclass(frozen=True, slots=True)
class SaturationResult:
    """A saturation analysis, or an explicit refusal to produce one."""

    key: SeriesKey
    period: int
    refused: bool
    limit: CapacityLimit | None
    profile: UtilizationProfile | None
    exceedance: Exceedance | None
    applicability: Applicability | None
    headroom: Headroom | None
    queue: QueueModel | None
    findings: tuple[Finding, ...]

    @property
    def errors(self) -> tuple[Finding, ...]:
        return tuple(finding for finding in self.findings if finding.severity == "error")

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(finding for finding in self.findings if finding.severity == "warning")

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready form, for a report generator or a pipeline step."""
        return {
            "series": self.key.label(),
            "period_seconds": self.period,
            "refused": self.refused,
            "limit": (
                None
                if self.limit is None
                else {
                    "value": self.limit.value,
                    "unit": self.limit.unit,
                    "provenance": self.limit.provenance,
                    "is_a_real_limit": self.limit.is_a_real_limit,
                    "note": self.limit.note,
                }
            ),
            "utilization": (
                None
                if self.profile is None
                else {
                    "observations": self.profile.observations,
                    "mean": self.profile.mean,
                    "median": self.profile.median,
                    "planning_percentile": self.profile.planning_percentile,
                    "planning_value": self.profile.planning_value,
                    "maximum": self.profile.maximum,
                    "minimum": self.profile.minimum,
                    "highest_resolvable_percentile": self.profile.resolvable_percentile,
                    "percentile_determined_by_top_two": (
                        self.profile.determined_by_top_two
                    ),
                    "peak_to_mean": self.profile.peak_to_mean,
                    "max_to_mean": self.profile.max_to_mean,
                }
            ),
            "exceedance": (
                None
                if self.exceedance is None
                else {
                    "threshold": self.exceedance.threshold,
                    "points_above": self.exceedance.points_above,
                    "fraction_above": self.exceedance.fraction_above,
                    "run_count": self.exceedance.run_count,
                    "longest_run_points": self.exceedance.longest_run_points,
                    "longest_run_seconds": self.exceedance.longest_run_seconds,
                    "total_seconds_above": self.exceedance.total_seconds_above,
                    "runs_ended_by_hole": self.exceedance.runs_ended_by_hole,
                }
            ),
            "applicability": (
                None
                if self.applicability is None
                else {
                    "assumptions_hold": self.applicability.assumptions_hold,
                    "peak_to_mean": self.applicability.peak_to_mean,
                    "coefficient_of_variation": (
                        self.applicability.coefficient_of_variation
                    ),
                    "lag1_autocorrelation": self.applicability.lag1_autocorrelation,
                    "trend_per_period": self.applicability.trend_per_period,
                    "trend_over_window": self.applicability.trend_over_window,
                    "trend_fraction_of_mean": self.applicability.trend_fraction_of_mean,
                    "source_statistic": self.applicability.source_statistic,
                    "period_seconds": self.applicability.period_seconds,
                    "service_cv_squared_measured": (
                        self.applicability.service_cv_squared_measured
                    ),
                }
            ),
            "headroom": (
                None
                if self.headroom is None
                else {
                    "utilization": self.headroom.utilization,
                    "elasticity_target": self.headroom.elasticity_target,
                    "elasticity_now": self.headroom.elasticity_now,
                    "ceiling_from_elasticity": self.headroom.ceiling_from_elasticity,
                    "ceiling_from_response": self.headroom.ceiling_from_response,
                    "binding_ceiling": self.headroom.binding_ceiling,
                    "demand_multiplier": self.headroom.demand_multiplier,
                    "naive_demand_multiplier": self.headroom.naive_demand_multiplier,
                    "overstatement_factor": self.headroom.overstatement_factor,
                    "growth_headroom_percent": self.headroom.growth_headroom_percent,
                    "past_ceiling": self.headroom.past_ceiling,
                }
            ),
            "queue": None if self.queue is None else self.queue.as_dict(),
            "findings": [
                {"code": f.code, "severity": f.severity, "message": f.message}
                for f in self.findings
            ],
        }


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def _refuse(
    key: SeriesKey,
    period: int,
    limit: CapacityLimit | None,
    findings: Sequence[Finding],
) -> SaturationResult:
    """Build a refusal carrying the reason, so a caller never sees a bare failure."""
    return SaturationResult(
        key=key,
        period=period,
        refused=True,
        limit=limit,
        profile=None,
        exceedance=None,
        applicability=None,
        headroom=None,
        queue=None,
        findings=tuple(findings),
    )


def _profile(
    utilizations: Sequence[float], planning_percentile: float
) -> tuple[UtilizationProfile, list[Finding]]:
    """Summarise the utilization distribution and say what it cannot support."""
    findings: list[Finding] = []
    count = len(utilizations)
    resolvable = highest_resolvable_percentile(count)

    requested = planning_percentile
    effective = requested
    if requested > resolvable:
        effective = resolvable
        findings.append(
            Finding(
                "F510",
                "warning",
                f"p{requested * 100:.4g} needs more than {math.ceil(1.0 / (1.0 - requested))} "
                f"samples to resolve and the window holds {count}; reporting "
                f"p{resolvable * 100:.4g}, the highest this sample supports",
            )
        )

    value = percentile(utilizations, effective)
    maximum = max(utilizations)
    # Position of the requested percentile among the order statistics. When it
    # sits at or above n - 2, the reported value is interpolated between the
    # two largest samples and nothing else in the window informs it, so it
    # describes those two observations rather than a tail.
    position = (count - 1) * effective
    top_two = position >= count - 2
    if top_two and count >= 2:
        findings.append(
            Finding(
                "F511",
                "note",
                f"p{effective * 100:.4g} over {count} samples is interpolated between the "
                "two largest observations, so it describes those two points rather than a "
                "tail the window resolves; a longer window is the only fix",
            )
        )

    profile = UtilizationProfile(
        observations=count,
        mean=sum(utilizations) / count,
        median=percentile(utilizations, 0.5),
        planning_percentile=effective,
        planning_value=value,
        maximum=maximum,
        minimum=min(utilizations),
        resolvable_percentile=resolvable,
        determined_by_top_two=top_two,
    )

    peak_to_mean = profile.peak_to_mean
    if peak_to_mean is not None and peak_to_mean > 1.2:
        findings.append(
            Finding(
                "F512",
                "note",
                f"peak-to-mean is {peak_to_mean:.2f}: provisioning against the window mean "
                f"of {profile.mean:.1%} would under-provision the operating point of "
                f"{value:.1%} by that factor",
            )
        )
    return profile, findings


def _exceedance(grid: SeriesGrid, limit: float, threshold: float) -> Exceedance:
    """Measure sustained time above a utilization threshold on the sampling grid."""
    points_above = 0
    run_count = 0
    longest = 0
    current = 0
    ended_by_hole = 0
    in_run = False

    for value in grid.values:
        if value is None:
            if in_run:
                ended_by_hole += 1
                in_run = False
                current = 0
            continue
        if value / limit >= threshold:
            points_above += 1
            if not in_run:
                run_count += 1
                in_run = True
                current = 0
            current += 1
            longest = max(longest, current)
        else:
            in_run = False
            current = 0

    observed = grid.observed
    return Exceedance(
        threshold=threshold,
        points_above=points_above,
        fraction_above=(points_above / observed) if observed else 0.0,
        run_count=run_count,
        longest_run_points=longest,
        longest_run_seconds=longest * grid.period,
        total_seconds_above=points_above * grid.period,
        runs_ended_by_hole=ended_by_hole,
    )


def _applicability(
    utilizations: Sequence[float],
    grid: SeriesGrid,
    profile: UtilizationProfile,
    statistic: str,
    cv_squared_measured: bool,
) -> tuple[Applicability, list[Finding]]:
    """Test the queueing assumptions against the series, and report what fails."""
    findings: list[Finding] = []

    slope = linear_trend(utilizations)
    over_window = None if slope is None else slope * max(0, len(utilizations) - 1)
    fraction = (
        None
        if over_window is None or profile.mean <= 0.0
        else over_window / profile.mean
    )

    applicability = Applicability(
        peak_to_mean=profile.peak_to_mean,
        coefficient_of_variation=coefficient_of_variation(utilizations),
        lag1_autocorrelation=lag1_autocorrelation(utilizations),
        trend_per_period=slope,
        trend_over_window=over_window,
        trend_fraction_of_mean=fraction,
        source_statistic=statistic,
        period_seconds=grid.period,
        service_cv_squared_measured=cv_squared_measured,
    )

    if (
        applicability.peak_to_mean is not None
        and applicability.peak_to_mean > BURSTY_PEAK_TO_MEAN
    ):
        findings.append(
            Finding(
                "F601",
                "warning",
                f"peak-to-mean of {applicability.peak_to_mean:.2f} exceeds "
                f"{BURSTY_PEAK_TO_MEAN:.1f}: arrivals are burstier than Poisson, and a "
                "closed-form wait computed from the mean rate is a lower bound on the "
                "delay this resource actually sees",
            )
        )
    if (
        applicability.lag1_autocorrelation is not None
        and applicability.lag1_autocorrelation > CLUSTERED_AUTOCORRELATION
    ):
        findings.append(
            Finding(
                "F602",
                "warning",
                f"lag-one autocorrelation is {applicability.lag1_autocorrelation:.2f}: busy "
                "samples predict busy samples, so load arrives in clusters rather than "
                "independently and queues build further than the model predicts",
            )
        )
    if fraction is not None and abs(fraction) > NON_STATIONARY_TREND:
        direction = "upward" if fraction > 0 else "downward"
        findings.append(
            Finding(
                "F603",
                "warning",
                f"utilization drifts {direction} by {abs(fraction):.0%} of its mean across "
                "the window, so there is no single stationary operating point and this "
                "analysis describes a load the resource is moving away from",
            )
        )
    if not cv_squared_measured:
        findings.append(
            Finding(
                "F604",
                "note",
                "service-time variability was assumed exponential rather than measured; "
                "queueing delay scales with (1 + cv2) / 2, so a service whose work mixes "
                "fast and slow paths queues proportionally longer than reported here",
            )
        )
    if statistic.lower() in {"average", "avg", "mean", "sum"}:
        findings.append(
            Finding(
                "F605",
                "warning",
                f"the series carries the {statistic} statistic over {grid.period}s windows, "
                "so any burst shorter than that window is divided by the window before it "
                "reaches the series; every peak above is a lower bound on the "
                "instantaneous peak",
            )
        )
    else:
        findings.append(
            Finding(
                "F606",
                "note",
                f"the {statistic} statistic over {grid.period}s windows still cannot show a "
                "burst shorter than the collection resolution behind it, so the peak is a "
                "lower bound rather than a maximum",
            )
        )
    return applicability, findings


def _headroom(
    utilization: float,
    *,
    servers: int,
    service_time: float | None,
    cv_squared: float,
    elasticity_target: float,
    response_budget: float | None,
) -> tuple[Headroom, QueueModel | None, list[Finding]]:
    """Convert the operating point into the growth it leaves before a ceiling.

    The elasticity ceiling needs no service time: elasticity depends only on
    utilization, server count and service-time variability, so a resource with
    no latency instrumentation at all still gets a defensible ceiling. A
    response-time budget does need one, and is skipped with a finding when the
    service time is unknown rather than being invented.
    """
    findings: list[Finding] = []

    queue: QueueModel | None = None
    elasticity_now = math.inf
    try:
        queue = QueueModel(
            servers=servers,
            service_time=service_time if service_time is not None else 1.0,
            utilization=utilization,
            cv_squared=cv_squared,
        )
        elasticity_now = queue.elasticity
    except QueueingError as exc:
        findings.append(
            Finding(
                "F522",
                "warning",
                f"the operating point cannot be evaluated as a queue: {exc}",
            )
        )
        queue = None

    ceiling_elasticity = utilization_for_elasticity(
        elasticity_target, servers=servers, service_time=1.0, cv_squared=cv_squared
    )

    ceiling_response: float | None = None
    if response_budget is not None:
        if service_time is None:
            findings.append(
                Finding(
                    "F523",
                    "note",
                    "a response-time budget was given but the mean service time is "
                    "unknown, so only the elasticity ceiling is reported",
                )
            )
        else:
            ceiling_response = utilization_for_mean_response(
                response_budget,
                servers=servers,
                service_time=service_time,
                cv_squared=cv_squared,
            )
            if ceiling_response is None:
                findings.append(
                    Finding(
                        "F524",
                        "warning",
                        f"the response budget of {response_budget:.4g}s is below the mean "
                        f"service time of {service_time:.4g}s, so no amount of spare "
                        "capacity meets it: this is a sizing problem, not a loading one",
                    )
                )

    candidates = [
        value for value in (ceiling_elasticity, ceiling_response) if value is not None
    ]
    binding = min(candidates) if candidates else None

    multiplier: float | None = None
    overstatement: float | None = None
    naive = math.inf if utilization <= 0.0 else 1.0 / utilization
    past = False
    if binding is not None and utilization > 0.0:
        multiplier = binding / utilization
        past = multiplier <= 1.0
        if math.isfinite(naive) and multiplier > 0.0:
            overstatement = naive / multiplier

    if past:
        findings.append(
            Finding(
                "F520",
                "error",
                f"the operating point of {utilization:.1%} is already at or above the "
                f"{binding:.1%} ceiling implied by an elasticity target of "
                f"{elasticity_target:.3g}: there is no growth headroom to allocate, and "
                "the resource needs capacity before it needs a plan",
            )
        )
    elif multiplier is not None and overstatement is not None and overstatement > 1.1:
        findings.append(
            Finding(
                "F521",
                "warning",
                f"usable demand headroom is {multiplier:.2f}x, not the {naive:.2f}x that "
                f"reading {utilization:.1%} utilization as linear headroom implies: the "
                f"linear reading overstates available growth by {overstatement:.1f}x",
            )
        )

    headroom = Headroom(
        utilization=utilization,
        elasticity_target=elasticity_target,
        elasticity_now=elasticity_now,
        ceiling_from_elasticity=ceiling_elasticity,
        ceiling_from_response=ceiling_response,
        binding_ceiling=binding,
        demand_multiplier=multiplier,
        naive_demand_multiplier=naive,
        overstatement_factor=overstatement,
        past_ceiling=past,
    )
    return headroom, queue, findings


def analyse_saturation(
    series: MetricSeries,
    limit: CapacityLimit,
    *,
    planning_percentile: float = DEFAULT_PLANNING_PERCENTILE,
    exceedance_threshold: float = DEFAULT_EXCEEDANCE_THRESHOLD,
    servers: int = 1,
    service_time: float | None = None,
    service_cv_squared: float | None = None,
    elasticity_target: float = DEFAULT_ELASTICITY_TARGET,
    response_budget: float | None = None,
    allow_holes: bool = False,
) -> SaturationResult:
    """Analyse one series against a declared limit and report its headroom.

    Refuses, with a finding that says why, when the series is empty, too short
    to support a percentile, holed without ``allow_holes``, or exceeds the
    declared limit -- the last of which means the limit is wrong, the metric is
    not what it claims to be, or both, and no headroom statement is meaningful
    until that is resolved.
    """
    if not 0.0 < planning_percentile < 1.0:
        raise SaturationError(
            f"planning percentile must be in (0, 1), got {planning_percentile}"
        )
    if not 0.0 < exceedance_threshold <= 1.0:
        raise SaturationError(
            f"exceedance threshold must be in (0, 1], got {exceedance_threshold}"
        )
    if service_time is not None and service_time <= 0.0:
        raise SaturationError(f"service time must be positive, got {service_time}")
    if (
        servers > 1
        and service_cv_squared is not None
        and abs(service_cv_squared - 1.0) > 1e-9
    ):
        # No exact several-server result exists for general service times, and
        # the queueing layer refuses the combination rather than applying the
        # single-server variability factor to a several-server formula. Caught
        # here, with the other argument checks, because the alternative is a
        # QueueingError raised from the middle of an analysis that has already
        # recorded findings about a window it is then going to abandon.
        raise SaturationError(
            "a service-time variability of "
            f"{service_cv_squared:.6g} cannot be modelled across {servers} servers: "
            "pass servers=1 to use the general-service result, or omit the "
            "measured variability to assume exponential service"
        )

    findings: list[Finding] = []
    if not limit.is_a_real_limit:
        findings.append(
            Finding(
                "F505",
                "warning",
                f"the limit of {limit.value:.6g} {limit.unit} is an observed maximum, which "
                "bounds the real limit from below rather than stating it; every utilization "
                "below is therefore an upper bound and the true headroom is at least what "
                "is reported",
            )
        )
    elif limit.provenance == "quota":
        findings.append(
            Finding(
                "F506",
                "note",
                f"the limit of {limit.value:.6g} {limit.unit} is a service quota, so "
                "saturation against it is raisable by request rather than by engineering; "
                "confirm the next limit behind it before planning growth into the increase",
            )
        )

    if not series.points:
        findings.append(Finding("F500", "error", "series holds no observations"))
        return _refuse(series.key, series.period, limit, findings)

    if series.unit and limit.unit and series.unit.lower() != limit.unit.lower():
        findings.append(
            Finding(
                "F504",
                "warning",
                f"the series is in {series.unit} and the limit in {limit.unit}; the ratio "
                "below assumes they are the same scale, which nothing here can verify",
            )
        )

    coverage = series.coverage
    if coverage.has_hole:
        message = (
            f"coverage is {coverage.ratio:.0%} with a largest gap of "
            f"{coverage.largest_gap_seconds}s against a {coverage.period}s period: a "
            "saturation peak inside the gap is invisible, so the reported peak may not be "
            "the window's peak"
        )
        if allow_holes:
            findings.append(Finding("F502", "warning", message))
        else:
            findings.append(Finding("F502", "error", message))
            return _refuse(series.key, series.period, limit, findings)

    if len(series.points) < MIN_OBSERVATIONS:
        findings.append(
            Finding(
                "F501",
                "error",
                f"{len(series.points)} observations is fewer than the {MIN_OBSERVATIONS} "
                "needed before a percentile or a trend says anything about the resource",
            )
        )
        return _refuse(series.key, series.period, limit, findings)

    utilizations = [point.value / limit.value for point in series.points]
    over_limit = [value for value in utilizations if value > 1.0]
    if over_limit:
        findings.append(
            Finding(
                "F507",
                "error",
                f"{len(over_limit)} of {len(utilizations)} observations exceed the declared "
                f"limit, by up to {(max(over_limit) - 1.0):.0%}: either the limit is wrong "
                "or the metric does not measure what the limit bounds, and no headroom "
                "statement is meaningful until that is settled",
            )
        )
        return _refuse(series.key, series.period, limit, findings)

    profile, profile_findings = _profile(utilizations, planning_percentile)
    findings.extend(profile_findings)

    grid = to_grid(series)
    if grid.off_grid:
        findings.append(
            Finding(
                "F503",
                "warning",
                f"{grid.off_grid} observation(s) do not sit on the {series.period}s sampling "
                "grid, so the series mixes resolutions and the exceedance runs below are "
                "measured over the points that do",
            )
        )
    exceedance = _exceedance(grid, limit.value, exceedance_threshold)
    if exceedance.longest_run_points > 0:
        findings.append(
            Finding(
                "F513",
                "note",
                f"utilization held at or above {exceedance_threshold:.0%} for "
                f"{exceedance.longest_run_seconds}s at a stretch across "
                f"{exceedance.run_count} run(s), totalling "
                f"{exceedance.total_seconds_above}s: sustained runs are what build a queue, "
                "and isolated spikes of the same count are not the same risk",
            )
        )

    cv_squared = 1.0 if service_cv_squared is None else service_cv_squared
    applicability, applicability_findings = _applicability(
        utilizations,
        grid,
        profile,
        series.key.statistic,
        cv_squared_measured=service_cv_squared is not None,
    )
    findings.extend(applicability_findings)

    headroom, queue, headroom_findings = _headroom(
        profile.planning_value,
        servers=servers,
        service_time=service_time,
        cv_squared=cv_squared,
        elasticity_target=elasticity_target,
        response_budget=response_budget,
    )
    findings.extend(headroom_findings)

    _LOG.info(
        "saturation %s: p%.4g utilization %.3f, headroom %s",
        series.key.label(),
        profile.planning_percentile * 100,
        profile.planning_value,
        "none" if headroom.demand_multiplier is None else f"{headroom.demand_multiplier:.2f}x",
    )

    return SaturationResult(
        key=series.key,
        period=series.period,
        refused=False,
        limit=limit,
        profile=profile,
        exceedance=exceedance,
        applicability=applicability,
        headroom=headroom,
        queue=queue,
        findings=tuple(findings),
    )


def check_little_law(
    concurrency: float,
    throughput: float,
    latency: float,
    *,
    tolerance: float = LITTLE_LAW_TOLERANCE,
) -> list[Finding]:
    """Cross-check three measurements that an identity requires to agree.

    Little's law holds for every stable system regardless of arrival process or
    service distribution, so disagreement is never a property of the workload --
    it is a measurement fault. The usual causes are a latency percentile used
    where a mean belongs, three series averaged over different windows, or a
    concurrency gauge scoped to one component of a path the latency covers.
    Worth running before any capacity number is derived from the three.
    """
    residual = little_law_residual(concurrency, throughput, latency)
    if residual is None:
        return [
            Finding(
                "F530",
                "note",
                "concurrency is zero, so Little's law cannot be checked on this window",
            )
        ]
    if abs(residual) <= tolerance:
        return [
            Finding(
                "F531",
                "note",
                f"Little's law holds to {abs(residual):.1%}: concurrency, throughput and "
                "latency are mutually consistent over this window",
            )
        ]
    expected = throughput * latency
    return [
        Finding(
            "F532",
            "warning",
            f"Little's law is violated by {abs(residual):.0%}: measured concurrency "
            f"{concurrency:.4g} against the {expected:.4g} implied by throughput "
            f"{throughput:.4g} and latency {latency:.4g}. The identity cannot fail, so one "
            "of the three is averaged over a different window, scoped to a different "
            "component, or reported as a percentile where a mean is required",
        )
    ]


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m models.saturation",
        description=(
            "Analyse a stored series against a declared capacity limit and report "
            "the demand growth it leaves."
        ),
    )
    add_common_arguments(parser)
    parser.add_argument("--source", default="cloudwatch", help="observation source")
    parser.add_argument("--namespace", required=True, help="metric namespace")
    parser.add_argument("--metric", required=True, help="metric name")
    parser.add_argument("--statistic", default="Average", help="statistic to analyse")
    parser.add_argument(
        "--dimension",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="dimension of the series; repeat for each one",
    )
    parser.add_argument(
        "--limit",
        type=float,
        required=True,
        help="the capacity limit this metric is measured against",
    )
    parser.add_argument(
        "--limit-unit",
        default="",
        help="unit of the limit, compared against the series unit",
    )
    parser.add_argument(
        "--limit-provenance",
        required=True,
        choices=["hard", "quota", "configured", "observed"],
        help=(
            "where the limit came from; 'observed' is not a limit and is reported "
            "as an upper bound on utilization"
        ),
    )
    parser.add_argument(
        "--limit-note", default="", help="free-text provenance detail for the report"
    )
    parser.add_argument(
        "--planning-percentile",
        type=float,
        default=DEFAULT_PLANNING_PERCENTILE,
        help=(
            "operating percentile of utilization to plan against "
            f"(default: {DEFAULT_PLANNING_PERCENTILE})"
        ),
    )
    parser.add_argument(
        "--exceedance-threshold",
        type=float,
        default=DEFAULT_EXCEEDANCE_THRESHOLD,
        help=(
            "utilization at or above which a sample counts toward a sustained run "
            f"(default: {DEFAULT_EXCEEDANCE_THRESHOLD})"
        ),
    )
    parser.add_argument(
        "--servers",
        type=int,
        default=1,
        help="parallel servers sharing the load (default: 1)",
    )
    parser.add_argument(
        "--service-time",
        type=float,
        default=None,
        help=(
            "mean service time of one request in seconds; only needed for a "
            "response-time ceiling, since the elasticity ceiling is scale-free"
        ),
    )
    parser.add_argument(
        "--service-cv-squared",
        type=float,
        default=None,
        help=(
            "measured squared coefficient of variation of service time; omitted "
            "means exponential is assumed, and the assumption is reported"
        ),
    )
    parser.add_argument(
        "--elasticity-target",
        type=float,
        default=DEFAULT_ELASTICITY_TARGET,
        help=(
            "percent of latency a percent of demand growth may cost "
            f"(default: {DEFAULT_ELASTICITY_TARGET}, which is 80%% for one "
            "exponential server)"
        ),
    )
    parser.add_argument(
        "--response-budget",
        type=float,
        default=None,
        help="optional mean response-time budget in seconds, as a second ceiling",
    )
    parser.add_argument(
        "--lookback-hours",
        type=int,
        default=None,
        help="restrict the analysed window to the most recent N hours",
    )
    parser.add_argument(
        "--allow-holes",
        action="store_true",
        help="analyse despite a coverage gap, recording it as a finding",
    )
    parser.add_argument("--json", action="store_true", help="emit the result as JSON")
    return parser


def _parse_dimensions(pairs: Sequence[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs:
        name, separator, value = pair.partition("=")
        if not separator or not name:
            raise SaturationError(f"dimension must be NAME=VALUE, got {pair!r}")
        out[name] = value
    return out


def _render(result: SaturationResult) -> str:
    lines = [f"series: {result.key.label()}  period: {result.period}s"]
    if result.limit is not None:
        limit = result.limit
        lines.append(
            f"limit: {limit.value:.6g} {limit.unit or '(unit unstated)'} "
            f"[{limit.provenance}]"
            + (f" -- {limit.note}" if limit.note else "")
        )

    if result.profile is not None:
        profile = result.profile
        peak_to_mean = profile.peak_to_mean
        lines.append("")
        lines.append(
            f"utilization over {profile.observations} samples: "
            f"mean {profile.mean:.1%}  median {profile.median:.1%}  "
            f"p{profile.planning_percentile * 100:.4g} {profile.planning_value:.1%}  "
            f"max {profile.maximum:.1%}"
        )
        if peak_to_mean is not None:
            lines.append(
                f"peak-to-mean: {peak_to_mean:.2f}x "
                "(the factor by which planning to the mean under-provisions)"
            )

    if result.exceedance is not None:
        exceedance = result.exceedance
        lines.append(
            f"above {exceedance.threshold:.0%}: {exceedance.fraction_above:.1%} of samples, "
            f"longest sustained run {exceedance.longest_run_seconds}s "
            f"across {exceedance.run_count} run(s)"
        )

    if result.headroom is not None:
        headroom = result.headroom
        lines.append("")
        lines.append(
            f"elasticity here: {headroom.elasticity_now:.3g} percent latency per percent "
            "growth"
        )
        if headroom.binding_ceiling is not None:
            lines.append(
                f"planning ceiling: {headroom.binding_ceiling:.1%} utilization "
                f"(elasticity target {headroom.elasticity_target:.3g})"
            )
        if headroom.demand_multiplier is not None:
            lines.append(
                f"usable headroom: {headroom.demand_multiplier:.2f}x demand "
                f"({headroom.growth_headroom_percent:+.0f}% growth); "
                f"a linear reading of utilization would claim "
                f"{headroom.naive_demand_multiplier:.2f}x"
            )

    if result.applicability is not None and not result.applicability.assumptions_hold:
        lines.append("")
        lines.append(
            "the closed-form ceiling above rests on assumptions this series refutes; "
            "treat it as an upper bound on the safe operating point"
        )

    if result.findings:
        lines.append("")
        for finding in result.findings:
            lines.append(str(finding))
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Exit 0 on an analysis, 1 on a refusal, 2 on a request that cannot be run."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.log_level, args.log_format)

    try:
        dimensions = _parse_dimensions(args.dimension)
        limit = CapacityLimit(
            value=args.limit,
            unit=args.limit_unit,
            provenance=args.limit_provenance,
            note=args.limit_note,
        )
    except SaturationError as exc:
        _LOG.error("%s", exc)
        return 2

    key = SeriesKey(
        source=args.source,
        namespace=args.namespace,
        metric=args.metric,
        statistic=args.statistic,
        entity=canonical_entity(dimensions),
    )

    start: datetime | None = None
    if args.lookback_hours is not None:
        if args.lookback_hours <= 0:
            _LOG.error("--lookback-hours must be positive")
            return 2
        start = datetime.now(tz=timezone.utc) - timedelta(hours=args.lookback_hours)

    try:
        with ObservationStore(args.store, read_only=True) as store:
            series = store.read_series(key, start=start)
    except Exception as exc:
        _LOG.error("cannot read %s from %s: %s", key.label(), args.store, exc)
        return 2

    try:
        result = analyse_saturation(
            series,
            limit,
            planning_percentile=args.planning_percentile,
            exceedance_threshold=args.exceedance_threshold,
            servers=args.servers,
            service_time=args.service_time,
            service_cv_squared=args.service_cv_squared,
            elasticity_target=args.elasticity_target,
            response_budget=args.response_budget,
            allow_holes=args.allow_holes,
        )
    except (SaturationError, QueueingError) as exc:
        _LOG.error("%s", exc)
        return 2

    if args.json:
        print(json.dumps(result.as_dict(), indent=2, sort_keys=True, default=str))
    else:
        print(_render(result))
    return 1 if result.refused else 0


if __name__ == "__main__":
    sys.exit(main())
