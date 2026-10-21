"""Demand forecasting with intervals that mean what they say.

A capacity forecast is only useful if its interval is honest, and the usual way
a forecast is wrong is not that the central line is off — it is that the band
around it is too narrow, so a plan is made with confidence the data does not
support. Everything below exists to make the band wide enough and then to
measure whether it actually was.

**The interval must widen with the horizon.** The common implementation takes
the spread of one-step-ahead residuals and applies it unchanged at every
horizon, which reports the same uncertainty twelve weeks out as tomorrow. The
two models here carry their own closed-form error growth instead:

* random walk with drift — ``sd(h) = sigma * sqrt(h * (1 + h/m))``. The first
  term accumulates ``h`` independent steps; the second is the drift estimate's
  own error ``sigma^2/m`` acting over ``h`` steps, where ``m`` is the number of
  differences the drift was estimated from.
* linear trend — ``sd(h) = sigma * sqrt(1 + 1/n + (x_h - xbar)^2 / Sxx)``, the
  textbook prediction-interval term: residual spread, plus the uncertainty in
  the fitted level, plus the uncertainty in the slope, which grows with distance
  from the centre of the fitted window.

**Degrees of freedom are charged for the seasonal profile.** A profile at lag
``L`` estimates ``L - 1`` free parameters, so dividing the residual sum of
squares by ``n - 2`` on a weekly profile over hourly data uses 502 degrees of
freedom where 335 were left. That understates sigma by about a fifth, and the
understatement goes straight into every interval. The denominator here is
``n - k`` with the profile counted.

**The interval is measured, not merely asserted.** An interval at the 90% level
is a claim about frequency, so the last stretch of history is held back, the
candidate models forecast it, and the proportion of held-back actuals that fell
inside the predicted band is reported next to the nominal level. A band that
covers 61% of a 90% holdout is reported as such rather than presented as 90%.

**The one-sided question is asked explicitly.** "When do we run out" needs an
upper bound, not an interval, and the upper end of a two-sided 90% interval is
the one-sided bound at 95%. Reading the one off the other is a silent change of
level in whichever direction the reader happens to assume, so both are reported.

Two further honesty notes travel with every result rather than living in a
document nobody opens: the seasonal profile is treated as known, so interval
width excludes the profile's own estimation error; and when residuals are
autocorrelated the independent-steps assumption behind the horizon scaling
fails, which makes the reported band a lower bound on the truth.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Final

from ingest.runtime import add_common_arguments, configure_logging
from ingest.store import (
    MetricSeries,
    ObservationStore,
    SeriesKey,
    SeriesPoint,
    canonical_entity,
)
from models.seasonality import (
    Finding,
    SeasonalModel,
    SeriesGrid,
    deseasonalise,
    detect_on_grid,
    grid_from_points,
    regularised_incomplete_beta,
    reseasonalise,
)

_LOG = logging.getLogger(__name__)

#: Fewest observations any model is fitted to. Below this the residual spread is
#: an estimate of nothing in particular, which makes the interval decorative.
MIN_OBSERVATIONS: Final[int] = 12

#: Forecasting further than this fraction of the fitted history is extrapolation
#: rather than projection: the slope is being carried well past the span that
#: evidenced it.
MAX_HORIZON_FRACTION: Final[float] = 0.5

#: Below this many residual degrees of freedom the sigma estimate is itself
#: unstable and the t multiplier is large enough to notice.
LOW_DEGREES_OF_FREEDOM: Final[int] = 10

#: A holdout shorter than this cannot distinguish two models from each other.
MIN_USEFUL_HOLDOUT: Final[int] = 8

#: Statistics whose newest value is biased when its period has not closed, with
#: the direction of the bias. ``Average`` is absent deliberately: an average
#: over fewer samples is noisier but not systematically displaced, while a sum
#: or a sample count is short, a maximum over fewer samples can only fall, and a
#: minimum over fewer samples can only rise.
PARTIAL_PERIOD_BIAS: Final[dict[str, str]] = {
    "Sum": "low",
    "SampleCount": "low",
    "Maximum": "low",
    "Minimum": "high",
}


class ForecastError(ValueError):
    """Raised when a forecast request is malformed."""


# ---------------------------------------------------------------------------
# Student's t quantile
# ---------------------------------------------------------------------------
#
# Built on the regularised incomplete beta in ``models.seasonality``, which is
# the lower level of the two modules that need it.


def student_t_cdf(value: float, df: float) -> float:
    """Cumulative distribution of Student's t with ``df`` degrees of freedom."""
    if df <= 0:
        raise ForecastError(f"degrees of freedom must be positive, got {df}")
    tail = 0.5 * regularised_incomplete_beta(df / 2.0, 0.5, df / (df + value * value))
    return 1.0 - tail if value >= 0.0 else tail


def student_t_ppf(probability: float, df: float) -> float:
    """Quantile of Student's t, by bisection on its own distribution function.

    Computed rather than tabulated because the confidence level is an input: a
    table forces the caller to one of a handful of levels, and an asymptotic
    expansion in ``1/df`` is least accurate exactly where a short history puts
    the degrees of freedom.
    """
    if not 0.0 < probability < 1.0:
        raise ForecastError(f"probability must be in (0, 1), got {probability}")
    if probability == 0.5:
        return 0.0
    target = probability if probability > 0.5 else 1.0 - probability

    low = 0.0
    high = 2.0
    while student_t_cdf(high, df) < target and high < 1e8:
        high *= 2.0
    for _ in range(200):
        middle = 0.5 * (low + high)
        if student_t_cdf(middle, df) < target:
            low = middle
        else:
            high = middle
        if high - low <= 1e-12 * max(1.0, high):
            break
    quantile = 0.5 * (low + high)
    return quantile if probability > 0.5 else -quantile


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ForecastPoint:
    """One forecast period: the central value and what is known around it."""

    timestamp: datetime
    horizon: int
    value: float
    lower: float
    upper: float
    upper_one_sided: float
    clipped: bool

    @property
    def width(self) -> float:
        return self.upper - self.lower


@dataclass(frozen=True, slots=True)
class Diagnostics:
    """Everything needed to judge a forecast without refitting it."""

    method: str
    level: float
    one_sided_level: float
    residual_sd: float
    degrees_of_freedom: int
    seasonal_parameters: int
    multiplier: float
    lag1_autocorrelation: float | None
    fitted_observations: int
    holdout_observations: int
    holdout_mae: float | None
    holdout_interval_coverage: float | None
    candidate_scores: tuple[tuple[str, float], ...]

    @property
    def sigma_understatement(self) -> float:
        """How far a naive ``n - 2`` denominator would have understated sigma.

        A factor of 1.0 means the seasonal profile cost nothing; 1.22 means the
        naive sigma — and therefore every interval derived from it — would have
        been a fifth too small.
        """
        naive = self.degrees_of_freedom + self.seasonal_parameters
        if self.degrees_of_freedom <= 0 or naive <= 0:
            return 1.0
        return math.sqrt(naive / self.degrees_of_freedom)


@dataclass(frozen=True, slots=True)
class ForecastResult:
    """A forecast, or an explicit refusal to produce one."""

    key: SeriesKey
    period: int
    refused: bool
    points: tuple[ForecastPoint, ...]
    seasonal: SeasonalModel | None
    diagnostics: Diagnostics | None
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
            "seasonal": (
                None
                if self.seasonal is None
                else {
                    "cycle": self.seasonal.cycle_name,
                    "lag": self.seasonal.lag,
                    "mode": self.seasonal.mode,
                    "timezone": self.seasonal.timezone_name,
                    "strength": round(self.seasonal.strength, 4),
                    "cycles_observed": round(self.seasonal.cycles_observed, 2),
                    "min_phase_support": self.seasonal.min_phase_support,
                }
            ),
            "diagnostics": (
                None
                if self.diagnostics is None
                else {
                    "method": self.diagnostics.method,
                    "level": self.diagnostics.level,
                    "one_sided_level": self.diagnostics.one_sided_level,
                    "residual_sd": self.diagnostics.residual_sd,
                    "degrees_of_freedom": self.diagnostics.degrees_of_freedom,
                    "seasonal_parameters": self.diagnostics.seasonal_parameters,
                    "sigma_understatement_if_naive": round(
                        self.diagnostics.sigma_understatement, 4
                    ),
                    "multiplier": round(self.diagnostics.multiplier, 4),
                    "lag1_autocorrelation": self.diagnostics.lag1_autocorrelation,
                    "fitted_observations": self.diagnostics.fitted_observations,
                    "holdout_observations": self.diagnostics.holdout_observations,
                    "holdout_mae": self.diagnostics.holdout_mae,
                    "holdout_interval_coverage": self.diagnostics.holdout_interval_coverage,
                    "candidate_scores": [
                        {"method": name, "holdout_mae": score}
                        for name, score in self.diagnostics.candidate_scores
                    ],
                }
            ),
            "points": [
                {
                    "timestamp": point.timestamp.isoformat(),
                    "horizon": point.horizon,
                    "value": point.value,
                    "lower": point.lower,
                    "upper": point.upper,
                    "upper_one_sided": point.upper_one_sided,
                    "clipped": point.clipped,
                }
                for point in self.points
            ],
            "findings": [
                {"code": f.code, "severity": f.severity, "message": f.message}
                for f in self.findings
            ],
        }


# ---------------------------------------------------------------------------
# Candidate models
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Candidate:
    """A fitted model on the deseasonalised scale."""

    name: str
    parameters: int
    seasonal: SeasonalModel | None
    predict: Callable[[float], float]
    variance_scale: Callable[[float], float]
    residuals: tuple[float, ...]

    def sigma(self) -> float | None:
        """Residual standard deviation with the right denominator, or ``None``."""
        df = self.degrees_of_freedom()
        if df < 1:
            return None
        total = math.fsum(value * value for value in self.residuals)
        return math.sqrt(total / df)

    def degrees_of_freedom(self) -> int:
        seasonal_parameters = 0 if self.seasonal is None else self.seasonal.lag - 1
        return len(self.residuals) - self.parameters - seasonal_parameters


def _fit_trend(
    slots: Sequence[int],
    values: Sequence[float],
    seasonal: SeasonalModel | None,
) -> _Candidate | None:
    """Ordinary least squares on slot index, with the prediction-interval term."""
    count = len(slots)
    if count < 3:
        return None
    mean_x = math.fsum(float(slot) for slot in slots) / count
    mean_y = math.fsum(values) / count
    sxx = math.fsum((slot - mean_x) ** 2 for slot in slots)
    if sxx <= 0.0:
        return None
    sxy = math.fsum((slot - mean_x) * (value - mean_y) for slot, value in zip(slots, values))
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x

    def predict(slot: float) -> float:
        return intercept + slope * slot

    def variance_scale(slot: float) -> float:
        return 1.0 + 1.0 / count + ((slot - mean_x) ** 2) / sxx

    residuals = tuple(value - predict(float(slot)) for slot, value in zip(slots, values))
    name = "trend" if seasonal is None else "trend+seasonal"
    return _Candidate(
        name=name,
        parameters=2,
        seasonal=seasonal,
        predict=predict,
        variance_scale=variance_scale,
        residuals=residuals,
    )


def _fit_drift(
    slots: Sequence[int],
    values: Sequence[float],
    seasonal: SeasonalModel | None,
) -> _Candidate | None:
    """Random walk with drift, anchored on the last observation.

    One-step residuals are divided by the square root of their slot gap, because
    a residual spanning two periods has twice the variance of one spanning a
    single period. Normalising makes every residual an estimate of the same
    sigma, which is what the interval arithmetic assumes.
    """
    count = len(slots)
    if count < 3:
        return None
    span = float(slots[-1] - slots[0])
    if span <= 0.0:
        return None
    drift = (values[-1] - values[0]) / span
    last_slot = float(slots[-1])
    last_value = values[-1]

    residuals: list[float] = []
    for index in range(1, count):
        gap = float(slots[index] - slots[index - 1])
        if gap <= 0.0:
            return None
        expected = values[index - 1] + drift * gap
        residuals.append((values[index] - expected) / math.sqrt(gap))

    def predict(slot: float) -> float:
        return last_value + drift * (slot - last_slot)

    def variance_scale(slot: float) -> float:
        steps = slot - last_slot
        if steps <= 0.0:
            return 0.0
        return steps * (1.0 + steps / span)

    name = "drift" if seasonal is None else "drift+seasonal"
    return _Candidate(
        name=name,
        parameters=1,
        seasonal=seasonal,
        predict=predict,
        variance_scale=variance_scale,
        residuals=tuple(residuals),
    )


def _build_candidates(grid: SeriesGrid, seasonal: SeasonalModel | None) -> list[_Candidate]:
    """Every model worth fitting to this grid, with and without seasonality.

    Both plain models are kept even when a seasonal profile exists, so the
    holdout decides whether removing the seasonality actually helped rather than
    the decision being made by whoever wrote the detector.
    """
    slots = grid.observed_slots()
    raw = [grid.values[slot] for slot in slots]
    values = [value for value in raw if value is not None]
    if len(values) != len(slots):
        return []

    candidates: list[_Candidate] = []
    for fit in (_fit_trend, _fit_drift):
        built = fit(slots, values, None)
        if built is not None:
            candidates.append(built)

    if seasonal is not None:
        stamps = [grid.timestamps[slot] for slot in slots]
        adjusted = deseasonalise(stamps, values, seasonal, grid.period)
        for fit in (_fit_trend, _fit_drift):
            built = fit(slots, adjusted, seasonal)
            if built is not None:
                candidates.append(built)
    return candidates


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


def _project(
    candidate: _Candidate,
    grid: SeriesGrid,
    target_slots: Sequence[int],
    *,
    sigma: float,
    multiplier: float,
    one_sided_multiplier: float,
    non_negative: bool,
) -> list[ForecastPoint]:
    """Turn a fitted candidate into forecast points with intervals.

    For a multiplicative profile the interval is computed on the deseasonalised
    scale and then multiplied by the phase's own factor, so a busy phase gets a
    proportionally wider band. Scaling the error on the measured scale instead
    would report one width for every phase, which is too wide at the trough and
    too narrow at the peak — and the peak is the one the plan is made against.
    """
    last_slot = grid.observed_slots()[-1]
    points: list[ForecastPoint] = []
    for slot in target_slots:
        stamp = grid.timestamps[0] + timedelta(seconds=slot * grid.period)
        level = candidate.predict(float(slot))
        spread = sigma * math.sqrt(max(0.0, candidate.variance_scale(float(slot))))

        if candidate.seasonal is None:
            value = level
        else:
            value = reseasonalise([stamp], [level], candidate.seasonal, grid.period)[0]
            if candidate.seasonal.mode == "multiplicative":
                spread *= abs(candidate.seasonal.factor_at(stamp, grid.period))

        lower = value - multiplier * spread
        upper = value + multiplier * spread
        one_sided = value + one_sided_multiplier * spread
        clipped = False
        if non_negative and lower < 0.0:
            lower = 0.0
            clipped = True
        points.append(
            ForecastPoint(
                timestamp=stamp,
                horizon=slot - last_slot,
                value=value,
                lower=lower,
                upper=upper,
                upper_one_sided=one_sided,
                clipped=clipped,
            )
        )
    return points


def _lag1_autocorrelation(residuals: Sequence[float]) -> float | None:
    """Lag-1 autocorrelation of residuals, or ``None`` when undefined."""
    count = len(residuals)
    if count < 3:
        return None
    mean = math.fsum(residuals) / count
    centred = [value - mean for value in residuals]
    denominator = math.fsum(value * value for value in centred)
    if denominator <= 0.0:
        return None
    numerator = math.fsum(a * b for a, b in zip(centred, centred[1:]))
    return numerator / denominator


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Score:
    name: str
    mae: float
    interval_coverage: float
    scored: int


def _score_on_holdout(
    grid: SeriesGrid,
    split: int,
    *,
    level: float,
    timezone_name: str,
    non_negative: bool,
) -> tuple[list[_Score], int]:
    """Fit on ``grid[:split]`` and score every candidate on the remainder.

    Seasonality is re-estimated from the training slice alone. Reusing the
    profile fitted to the whole series would let every seasonal candidate score
    against data it had already seen, which reliably picks the seasonal model
    whether or not seasonality is real.
    """
    train = grid.slice(0, split)
    test = grid.slice(split, grid.slots)
    test_slots = [split + index for index in test.observed_slots()]
    if not test_slots:
        return [], 0

    analysis = detect_on_grid(train, timezone_name=timezone_name)
    candidates = _build_candidates(train, analysis.chosen)

    actuals = [grid.values[slot] for slot in test_slots]
    scores: list[_Score] = []
    for candidate in candidates:
        sigma = candidate.sigma()
        if sigma is None:
            continue
        df = candidate.degrees_of_freedom()
        multiplier = student_t_ppf(1.0 - (1.0 - level) / 2.0, float(df))
        predicted = _project(
            candidate,
            train,
            test_slots,
            sigma=sigma,
            multiplier=multiplier,
            one_sided_multiplier=multiplier,
            non_negative=non_negative,
        )
        errors: list[float] = []
        inside = 0
        for point, actual in zip(predicted, actuals):
            if actual is None:
                continue
            errors.append(abs(point.value - actual))
            if point.lower <= actual <= point.upper:
                inside += 1
        if not errors:
            continue
        scores.append(
            _Score(
                name=candidate.name,
                mae=math.fsum(errors) / len(errors),
                interval_coverage=inside / len(errors),
                scored=len(errors),
            )
        )
    return scores, len(test_slots)


def _choose_split(grid: SeriesGrid, seasonal: SeasonalModel | None) -> int | None:
    """Where to cut the holdout, or ``None`` when no honest cut exists.

    The holdout is one cycle when there is a cycle, because a shorter window
    cannot see a seasonal model fail. It is only taken if what remains still
    satisfies the training requirement of the models being compared — a
    comparison run on a training window too short to fit the candidates is worse
    than no comparison.
    """
    observed = grid.observed
    desired = seasonal.lag if seasonal is not None else max(MIN_USEFUL_HOLDOUT, observed // 4)
    desired = min(desired, observed // 3)
    if desired < 1:
        return None

    need = MIN_OBSERVATIONS
    if seasonal is not None:
        # A seasonal profile needs two cycles of training data to exist at all,
        # so a split that leaves fewer is not a split, it is a refusal.
        need = max(need, 2 * seasonal.lag)

    # Shrink the holdout until the training window can carry the candidates, and
    # stop one slot short of the whole grid: a split that leaves nothing held
    # back is the no-comparison case, which is reported rather than faked.
    for split in range(max(1, grid.slots - desired), grid.slots):
        if grid.slice(0, split).observed >= need:
            return split
    return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _trim_partial_period(
    series: MetricSeries,
    now: datetime,
) -> tuple[tuple[SeriesPoint, ...], list[Finding]]:
    """Drop a trailing observation whose period has not closed, when it is biased.

    Observations are stamped at the start of the period they describe, so the
    newest point covers ``[t, t + period)``. If that interval has not ended, an
    extensive statistic is short through no fault of demand — and a trend fitted
    through it is dragged toward the dip, while a model anchored on the last
    observation inherits it outright.
    """
    findings: list[Finding] = []
    if not series.points or series.period <= 0:
        return series.points, findings

    end = series.points[-1].timestamp + timedelta(seconds=series.period)
    if end <= now:
        return series.points, findings

    statistic = series.key.statistic
    direction = PARTIAL_PERIOD_BIAS.get(statistic)
    if direction is None:
        findings.append(
            Finding(
                "F206",
                "note",
                f"the newest observation covers a period ending {end.isoformat()}, which "
                f"has not closed; {statistic} over a part-period is noisier but not "
                "systematically displaced, so it was kept",
            )
        )
        return series.points, findings

    findings.append(
        Finding(
            "F205",
            "note",
            f"dropped the newest observation: its period ends {end.isoformat()} and has "
            f"not closed, so a {statistic} over it reads {direction} for a reason that "
            "has nothing to do with demand",
        )
    )
    return series.points[:-1], findings


def forecast_series(
    series: MetricSeries,
    *,
    horizon: int,
    level: float = 0.9,
    timezone_name: str = "UTC",
    now: datetime | None = None,
    non_negative: bool = True,
    allow_holes: bool = False,
    min_observations: int = MIN_OBSERVATIONS,
) -> ForecastResult:
    """Forecast ``horizon`` periods ahead, or refuse and say why.

    ``level`` is the two-sided interval level. The one-sided upper bound that
    answers "how high could this get" is reported at ``(1 + level) / 2``, which
    is the level that upper end actually carries.
    """
    if horizon < 1:
        raise ForecastError(f"horizon must be at least 1 period, got {horizon}")
    if not 0.5 <= level < 1.0:
        raise ForecastError(f"level must be in [0.5, 1), got {level}")

    findings: list[Finding] = []
    refusal = ForecastResult(
        key=series.key,
        period=series.period,
        refused=True,
        points=(),
        seasonal=None,
        diagnostics=None,
        findings=(),
    )

    if not series.points:
        return _refuse(refusal, findings, "F200", "series holds no observations")
    if series.period <= 0:
        return _refuse(
            refusal, findings, "F207", f"series period is {series.period}, so it has no grid"
        )

    # The coverage gate runs against the window that was actually read, before
    # anything is dropped from it: coverage is a measurement of that window, and
    # a shortened copy of it would be an estimate dressed as one.
    coverage = series.coverage
    if coverage.has_hole and not allow_holes:
        return _refuse(
            refusal,
            findings,
            "F201",
            f"coverage has a gap of {coverage.largest_gap_seconds}s against a "
            f"{coverage.period}s sampling period "
            f"({coverage.observed}/{coverage.expected} samples); a forecast over a series "
            "with a hole in it is worse than no forecast",
        )
    if coverage.has_hole:
        findings.append(
            Finding(
                "F202",
                "warning",
                f"forecasting over a gap of {coverage.largest_gap_seconds}s because holes "
                "were explicitly allowed; the trend rests on a window that is not fully "
                "measured",
            )
        )

    observations, trim_findings = _trim_partial_period(
        series, now or datetime.now(tz=timezone.utc)
    )
    findings.extend(trim_findings)
    if not observations:
        return _refuse(
            refusal, findings, "F200", "series holds no observations once the open period is out"
        )
    if len(observations) < min_observations:
        return _refuse(
            refusal,
            findings,
            "F203",
            f"{len(observations)} observation(s) is below the {min_observations} a model "
            "is fitted to",
        )

    grid = grid_from_points(observations, series.period)
    analysis = detect_on_grid(grid, timezone_name=timezone_name)
    findings.extend(analysis.findings)
    seasonal = analysis.chosen

    candidates = _build_candidates(grid, seasonal)
    usable = [candidate for candidate in candidates if candidate.sigma() is not None]
    for candidate in candidates:
        if candidate.sigma() is None:
            estimated = candidate.parameters
            if candidate.seasonal is not None:
                estimated += candidate.seasonal.lag - 1
            findings.append(
                Finding(
                    "F303",
                    "note",
                    f"{candidate.name} not estimable: {len(candidate.residuals)} "
                    f"residual(s) against {estimated} estimated parameter(s)",
                )
            )
    if not usable:
        return _refuse(
            refusal,
            findings,
            "F204",
            "no candidate model is estimable from this history",
        )

    split = _choose_split(grid, seasonal)
    scores: list[_Score] = []
    holdout_observations = 0
    if split is not None:
        scores, holdout_observations = _score_on_holdout(
            grid,
            split,
            level=level,
            timezone_name=timezone_name,
            non_negative=non_negative,
        )

    # Selection matches on candidate NAME, so what the holdout chooses is the
    # model's FORM — trend against drift, seasonal against not — while the
    # shipped fit uses the profile estimated from the whole series. A training
    # slice that found a daily cycle where the full series finds a weekly one
    # still votes for "use a seasonal model", which is the question it can
    # answer honestly.
    by_name = {candidate.name: candidate for candidate in usable}
    chosen: _Candidate
    chosen_score: _Score | None = None
    if scores:
        ranked = sorted(scores, key=lambda score: (score.mae, score.name))
        for score in ranked:
            if score.name in by_name:
                chosen = by_name[score.name]
                chosen_score = score
                break
        else:  # pragma: no cover - a scored name always comes from the same builder
            chosen = min(usable, key=lambda candidate: candidate.parameters)
        findings.append(
            Finding(
                "F300",
                "note",
                "selected "
                + ranked[0].name
                + " on held-back history: "
                + ", ".join(f"{score.name} MAE {score.mae:.4g}" for score in ranked),
            )
        )
        if holdout_observations < MIN_USEFUL_HOLDOUT or (
            seasonal is not None and holdout_observations < seasonal.lag
        ):
            findings.append(
                Finding(
                    "F302",
                    "warning",
                    f"the model comparison rests on {holdout_observations} held-back "
                    "period(s), too few to separate the candidates with confidence",
                )
            )
    else:
        chosen = min(usable, key=lambda candidate: candidate.parameters)
        findings.append(
            Finding(
                "F301",
                "warning",
                "no history could be held back, so the model was not compared out of "
                f"sample; {chosen.name} was taken as the fewest-parameter candidate rather "
                "than by in-sample fit, which always prefers the most flexible model",
            )
        )

    sigma = chosen.sigma()
    assert sigma is not None  # usable candidates have a sigma by construction
    df = chosen.degrees_of_freedom()
    one_sided_level = 0.5 * (1.0 + level)
    multiplier = student_t_ppf(one_sided_level, float(df))

    last_slot = grid.observed_slots()[-1]
    target_slots = [last_slot + step for step in range(1, horizon + 1)]
    points = _project(
        chosen,
        grid,
        target_slots,
        sigma=sigma,
        multiplier=multiplier,
        one_sided_multiplier=student_t_ppf(level, float(df)),
        non_negative=non_negative,
    )

    findings.extend(
        _interval_findings(
            chosen,
            points,
            sigma=sigma,
            df=df,
            level=level,
            one_sided_level=one_sided_level,
            multiplier=multiplier,
            score=chosen_score,
            series_values=[point.value for point in observations],
        )
    )
    findings.extend(_horizon_findings(horizon, grid.observed))

    diagnostics = Diagnostics(
        method=chosen.name,
        level=level,
        one_sided_level=one_sided_level,
        residual_sd=sigma,
        degrees_of_freedom=df,
        seasonal_parameters=0 if chosen.seasonal is None else chosen.seasonal.lag - 1,
        multiplier=multiplier,
        lag1_autocorrelation=_lag1_autocorrelation(chosen.residuals),
        fitted_observations=grid.observed,
        holdout_observations=holdout_observations,
        holdout_mae=None if chosen_score is None else chosen_score.mae,
        holdout_interval_coverage=(
            None if chosen_score is None else chosen_score.interval_coverage
        ),
        candidate_scores=tuple((score.name, score.mae) for score in scores),
    )

    _LOG.info(
        "forecast %s: %s, %d period(s), sigma=%.4g, df=%d",
        series.key.label(),
        chosen.name,
        horizon,
        sigma,
        df,
    )
    return ForecastResult(
        key=series.key,
        period=series.period,
        refused=False,
        points=tuple(points),
        seasonal=chosen.seasonal,
        diagnostics=diagnostics,
        findings=tuple(findings),
    )


def _refuse(
    template: ForecastResult,
    findings: list[Finding],
    code: str,
    message: str,
) -> ForecastResult:
    findings.append(Finding(code, "error", message))
    _LOG.warning("refusing to forecast %s: %s", template.key.label(), message)
    return ForecastResult(
        key=template.key,
        period=template.period,
        refused=True,
        points=(),
        seasonal=None,
        diagnostics=None,
        findings=tuple(findings),
    )


def _interval_findings(
    candidate: _Candidate,
    points: Sequence[ForecastPoint],
    *,
    sigma: float,
    df: int,
    level: float,
    one_sided_level: float,
    multiplier: float,
    score: _Score | None,
    series_values: Sequence[float],
) -> list[Finding]:
    """Every reason the reported band is not exactly what it appears to be."""
    findings: list[Finding] = [
        Finding(
            "F400",
            "note",
            f"interval is the {level:.0%} two-sided prediction interval: t({df}) = "
            f"{multiplier:.3f} times a residual spread of {sigma:.4g} scaled by horizon. "
            f"Its upper end is the one-sided {one_sided_level:.1%} bound, which is the "
            "figure a 'how high could this get' question is asking for",
        )
    ]

    seasonal_parameters = 0 if candidate.seasonal is None else candidate.seasonal.lag - 1
    if seasonal_parameters:
        naive = math.sqrt((df + seasonal_parameters) / df) if df > 0 else 1.0
        findings.append(
            Finding(
                "F407",
                "note",
                f"the seasonal profile estimates {seasonal_parameters} free parameter(s), "
                f"leaving {df} residual degrees of freedom; charging only the trend's "
                f"would have understated the residual spread by a factor of {naive:.3f} "
                "and narrowed every interval by the same amount",
            )
        )
        findings.append(
            Finding(
                "F406",
                "note",
                "the seasonal profile is treated as known, so interval width excludes the "
                "profile's own estimation error; the reported band is a lower bound on "
                "that account",
            )
        )
    if candidate.seasonal is not None and candidate.seasonal.mode == "multiplicative":
        findings.append(
            Finding(
                "F405",
                "note",
                "seasonality is multiplicative, so interval width scales with the "
                "phase factor: a busy phase carries a proportionally wider band",
            )
        )

    if df < LOW_DEGREES_OF_FREEDOM:
        findings.append(
            Finding(
                "F304",
                "warning",
                f"{df} residual degree(s) of freedom: the residual spread is itself a "
                f"weak estimate and the t multiplier is {multiplier:.2f} rather than the "
                "large-sample value",
            )
        )

    spread = max(series_values) - min(series_values) if series_values else 0.0
    if sigma <= 0.0 or (spread > 0.0 and sigma < 1e-9 * spread):
        findings.append(
            Finding(
                "F404",
                "warning",
                "residual spread is effectively zero, so the interval collapses onto the "
                "central line; on measured data that means the model reproduced the "
                "history, not that the future is certain",
            )
        )

    autocorrelation = _lag1_autocorrelation(candidate.residuals)
    if autocorrelation is not None:
        threshold = 2.0 / math.sqrt(len(candidate.residuals))
        if abs(autocorrelation) > threshold:
            findings.append(
                Finding(
                    "F402",
                    "warning",
                    f"residual lag-1 autocorrelation is {autocorrelation:+.2f} against a "
                    f"white-noise band of +/-{threshold:.2f}; the horizon scaling assumes "
                    "independent steps, so the reported interval is a lower bound",
                )
            )

    clipped = sum(1 for point in points if point.clipped)
    if clipped:
        findings.append(
            Finding(
                "F403",
                "note",
                f"the lower bound was clipped at zero on {clipped} of {len(points)} "
                f"period(s); the stated {level:.0%} coverage does not hold on the lower "
                "side there, because probability was removed rather than redistributed",
            )
        )

    if score is not None and score.scored > 0:
        standard_error = math.sqrt(level * (1.0 - level) / score.scored)
        if score.interval_coverage < level - 2.0 * standard_error:
            findings.append(
                Finding(
                    "F401",
                    "warning",
                    f"measured interval coverage on held-back history was "
                    f"{score.interval_coverage:.0%} over {score.scored} period(s) against "
                    f"a nominal {level:.0%} (two standard errors is "
                    f"{2.0 * standard_error:.0%}); the band is narrower than it claims",
                )
            )
        else:
            findings.append(
                Finding(
                    "F408",
                    "note",
                    f"measured interval coverage on held-back history was "
                    f"{score.interval_coverage:.0%} over {score.scored} period(s) against "
                    f"a nominal {level:.0%}",
                )
            )
    return findings


def _horizon_findings(horizon: int, fitted: int) -> list[Finding]:
    limit = int(fitted * MAX_HORIZON_FRACTION)
    if horizon <= limit:
        return []
    return [
        Finding(
            "F305",
            "warning",
            f"a {horizon}-period horizon is more than {MAX_HORIZON_FRACTION:.0%} of the "
            f"{fitted} period(s) fitted; the slope is being carried well past the span "
            "that evidenced it and the interval does not account for the model itself "
            "being wrong",
        )
    ]


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m models.forecast",
        description="Forecast a stored demand series with an explicit interval.",
    )
    add_common_arguments(parser)
    parser.add_argument("--source", default="cloudwatch", help="observation source")
    parser.add_argument("--namespace", required=True, help="metric namespace")
    parser.add_argument("--metric", required=True, help="metric name")
    parser.add_argument("--statistic", default="Average", help="statistic to forecast")
    parser.add_argument(
        "--dimension",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="dimension of the series; repeat for each one",
    )
    parser.add_argument("--horizon", type=int, required=True, help="periods to forecast")
    parser.add_argument(
        "--level",
        type=float,
        default=0.9,
        help="two-sided interval level (default: 0.9)",
    )
    parser.add_argument(
        "--timezone",
        default="UTC",
        help="zone whose wall clock the daily and weekly cycles follow (default: UTC)",
    )
    parser.add_argument(
        "--lookback-hours",
        type=int,
        default=None,
        help="restrict the fitted window to the most recent N hours",
    )
    parser.add_argument(
        "--allow-negative",
        action="store_true",
        help="do not clip the lower bound at zero",
    )
    parser.add_argument(
        "--allow-holes",
        action="store_true",
        help="forecast despite a coverage gap, recording it as a finding",
    )
    parser.add_argument("--json", action="store_true", help="emit the result as JSON")
    return parser


def _parse_dimensions(pairs: Sequence[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs:
        name, separator, value = pair.partition("=")
        if not separator or not name:
            raise ForecastError(f"dimension must be NAME=VALUE, got {pair!r}")
        out[name] = value
    return out


def _render(result: ForecastResult) -> str:
    lines: list[str] = [f"series: {result.key.label()}  period: {result.period}s"]
    if result.diagnostics is not None:
        diagnostics = result.diagnostics
        lines.append(
            f"method: {diagnostics.method}  sigma: {diagnostics.residual_sd:.4g}  "
            f"df: {diagnostics.degrees_of_freedom}  "
            f"interval: {diagnostics.level:.0%} two-sided "
            f"({diagnostics.one_sided_level:.1%} one-sided upper)"
        )
        if diagnostics.holdout_interval_coverage is not None:
            lines.append(
                f"held-back check: MAE {diagnostics.holdout_mae:.4g} over "
                f"{diagnostics.holdout_observations} period(s), measured interval "
                f"coverage {diagnostics.holdout_interval_coverage:.0%}"
            )
    if result.seasonal is not None:
        seasonal = result.seasonal
        lines.append(
            f"seasonality: {seasonal.cycle_name} {seasonal.mode} lag {seasonal.lag} "
            f"strength {seasonal.strength:.2f} ({seasonal.timezone_name})"
        )
    if result.points:
        lines.append("")
        lines.append(f"{'timestamp':<26}{'h':>5}{'forecast':>14}{'lower':>14}{'upper':>14}")
        for point in result.points:
            lines.append(
                f"{point.timestamp.isoformat():<26}{point.horizon:>5}"
                f"{point.value:>14.4g}{point.lower:>14.4g}{point.upper:>14.4g}"
            )
    if result.findings:
        lines.append("")
        for finding in result.findings:
            lines.append(str(finding))
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Exit 0 on a forecast, 1 on a refusal, 2 on a request that cannot be run."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.log_level, args.log_format)

    try:
        dimensions = _parse_dimensions(args.dimension)
    except ForecastError as exc:
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
        result = forecast_series(
            series,
            horizon=args.horizon,
            level=args.level,
            timezone_name=args.timezone,
            non_negative=not args.allow_negative,
            allow_holes=args.allow_holes,
        )
    except ForecastError as exc:
        _LOG.error("%s", exc)
        return 2

    if args.json:
        print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    else:
        print(_render(result))
    return 1 if result.refused else 0


if __name__ == "__main__":
    sys.exit(main())
