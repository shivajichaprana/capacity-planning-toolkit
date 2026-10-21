"""Seasonal structure of a demand series.

Demand is rarely stationary and almost never aperiodic: a service has a
time-of-day shape and a day-of-week shape, and a forecast that ignores them
reports Tuesday's trend on Sunday's data. This module estimates that shape so
the forecaster can remove it, project the remainder, and put it back.

Four decisions here are load-bearing, and each of them is the opposite of the
obvious implementation.

**A phase comes from the clock, not from the index.** The naive implementation
buckets the ``i``-th sample into phase ``i % lag``. That is correct only while
the series has no holes: a single missing sample shifts every later sample into
the wrong phase and rotates the whole profile. Phase is therefore computed from
each point's own timestamp, so a hole removes one observation from one phase and
nothing else.

**A daily cycle is a cycle in local time, not in 86400 seconds.** Human demand
follows a wall clock. In a zone that observes daylight saving, two instants 24
hours apart are not at the same point in the demand cycle for half the year, so
the phase key is the local time of day rather than an offset from the epoch. The
cost is visible rather than hidden: on the spring transition one phase receives
no sample and on the autumn transition one receives two, which shows up directly
in the per-phase support counts this module reports.

**The profile is normalised, and a mode is not chosen by comparing strengths.**
An un-normalised seasonal profile carries part of the level, which the trend then
counts a second time. And because additive strength is a variance ratio on the
measured scale while multiplicative strength is a variance ratio on the ratio
scale, the two numbers are not comparable — so the mode is decided by whether
the seasonal amplitude actually scales with the level, which is a different
question with a different test.

**Two cycles estimate a profile; three are needed to choose a mode.** The
amplitude-versus-level correlation over two cycles is +1 or -1 whatever the
data, because any two points are perfectly correlated. Below three cycles the
test is unavailable and the mode defaults to additive, which is stated rather
than assumed.

**Seasonal strength has to be adjusted for what the profile cost, and then
tested.** The usual measure is the share of detrended variance the profile
explains, and on its own it measures the profile's flexibility rather than the
series' seasonality: a profile at lag ``L`` fitted to ``L`` detrended
observations reproduces every one of them, so that measure is exactly 1.0 for
any series whatsoever — pure noise included — at precisely two cycles of
history. Even with more history it rises with ``L/n``, so a 24-phase profile on
a week of hourly noise explains around a seventh of the variance by arithmetic
alone. Two gates are therefore applied rather than one: the explained share is
adjusted for the degrees of freedom the profile consumed, and the decomposition
is subjected to an F test against the null of no seasonality at all. A cycle has
to be both statistically present and practically large to be modelled.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final, Literal
from zoneinfo import ZoneInfo

from ingest.store import MetricSeries, SeriesPoint

_LOG = logging.getLogger(__name__)

Severity = Literal["error", "warning", "note"]
Mode = Literal["additive", "multiplicative"]

#: Candidate cycles, longest last so a weekly shape wins a tie on strength.
CANDIDATE_CYCLES: Final[tuple[tuple[str, int], ...]] = (
    ("daily", 86_400),
    ("weekly", 604_800),
)

#: A profile needs this many complete cycles before any phase has an estimate.
MIN_CYCLES_FOR_PROFILE: Final[int] = 2

#: Below this many cycles every phase rests on a single observation.
CYCLES_FOR_REPEATED_SUPPORT: Final[int] = 3

#: Correlating per-cycle amplitude against per-cycle level needs three points;
#: two are perfectly correlated by construction.
MIN_CYCLES_FOR_MODE_TEST: Final[int] = 3

#: Amplitude-versus-level correlation above which seasonality is called
#: multiplicative. Deliberately well clear of zero: the consequence of guessing
#: multiplicative on an additive series is a forecast whose seasonal swing grows
#: with the trend, which is the error that compounds.
MULTIPLICATIVE_CORRELATION: Final[float] = 0.5

#: Seasonal strength below which a cycle is not worth modelling. The remainder
#: carries almost all of the detrended variance, so a profile fitted here spends
#: degrees of freedom to describe noise.
MIN_USEFUL_STRENGTH: Final[float] = 0.1


#: Largest F-test p-value at which a decomposition counts as seasonal at all.
MAX_SEASONAL_P_VALUE: Final[float] = 0.05


class SeasonalityError(ValueError):
    """Raised when a request is malformed rather than merely unsupported."""


# ---------------------------------------------------------------------------
# Special functions
# ---------------------------------------------------------------------------
#
# The regularised incomplete beta lives in this module because it is the lower
# level of the two that need it: the F test below and the Student's t quantile
# in the forecaster are both built on it, and two copies of a continued fraction
# are two things to get wrong independently.


def _log_beta(a: float, b: float) -> float:
    return math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)


def _beta_continued_fraction(a: float, b: float, x: float) -> float:
    """Lentz evaluation of the continued fraction for the incomplete beta."""
    tiny = 1e-300
    epsilon = 1e-15
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for step in range(1, 300):
        double = 2 * step
        numerator = step * (b - step) * x / ((qam + double) * (a + double))
        d = 1.0 + numerator * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + numerator / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        numerator = -(a + step) * (qab + step) * x / ((a + double) * (qap + double))
        d = 1.0 + numerator * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + numerator / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < epsilon:
            break
    return h


def regularised_incomplete_beta(a: float, b: float, x: float) -> float:
    """``I_x(a, b)``, the regularised incomplete beta function."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(a * math.log(x) + b * math.log1p(-x) - _log_beta(a, b))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _beta_continued_fraction(a, b, x) / a
    return 1.0 - front * _beta_continued_fraction(b, a, 1.0 - x) / b


def f_survival(statistic: float, df_numerator: int, df_denominator: int) -> float:
    """``P(F > statistic)`` for an F distribution: the test's p-value."""
    if df_numerator < 1 or df_denominator < 1:
        raise SeasonalityError("F test needs at least one degree of freedom on each side")
    if statistic <= 0.0:
        return 1.0
    scaled = df_numerator * statistic
    x = scaled / (scaled + df_denominator)
    below = regularised_incomplete_beta(df_numerator / 2.0, df_denominator / 2.0, x)
    return max(0.0, min(1.0, 1.0 - below))


@dataclass(frozen=True, slots=True)
class Finding:
    """One thing worth saying about a model's input or output."""

    code: str
    severity: Severity
    message: str

    def __str__(self) -> str:
        return f"[{self.code}] {self.severity.upper()}: {self.message}"


@dataclass(frozen=True, slots=True)
class SeasonalModel:
    """An estimated seasonal profile and everything needed to judge it.

    ``profile`` has one entry per phase of the cycle. Additive profiles sum to
    zero and multiplicative profiles average one, so applying a profile changes
    the shape of a series without moving its level.
    """

    cycle_name: str
    cycle_seconds: int
    lag: int
    mode: Mode
    timezone_name: str
    profile: tuple[float, ...]
    phase_support: tuple[int, ...]
    strength: float
    raw_strength: float
    p_value: float | None
    detrended_observations: int
    cycles_observed: float

    @property
    def is_saturated(self) -> bool:
        """True when the profile has no more observations than free parameters.

        A saturated profile reproduces its input exactly, so ``raw_strength`` is
        1.0 regardless of the series and no test of it is possible.
        """
        return self.detrended_observations <= self.lag

    @property
    def min_phase_support(self) -> int:
        return min(self.phase_support) if self.phase_support else 0

    @property
    def amplitude(self) -> float:
        """Peak-to-trough size of the profile, on whichever scale it lives."""
        if not self.profile:
            return 0.0
        return max(self.profile) - min(self.profile)

    def factor_at(self, timestamp: datetime, period: int) -> float:
        """Seasonal term for an instant: subtracted when additive, divided when not."""
        phase = phase_of(timestamp, period, self.cycle_seconds, self.timezone_name)
        return self.profile[phase]


@dataclass(frozen=True, slots=True)
class SeasonalAnalysis:
    """Every cycle that was considered, and the one that was chosen."""

    chosen: SeasonalModel | None
    candidates: tuple[SeasonalModel, ...]
    findings: tuple[Finding, ...]

    @property
    def is_seasonal(self) -> bool:
        return self.chosen is not None


# ---------------------------------------------------------------------------
# Phase
# ---------------------------------------------------------------------------


def resolve_zone(timezone_name: str) -> ZoneInfo | timezone:
    """Resolve a zone name, accepting ``UTC`` without requiring tz data."""
    if timezone_name.upper() == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(timezone_name)
    except Exception as exc:  # pragma: no cover - depends on host tz database
        raise SeasonalityError(f"unknown timezone {timezone_name!r}: {exc}") from exc


def lag_for(cycle_seconds: int, period: int) -> int:
    """Number of samples in one cycle.

    Raises when the cycle is not a whole number of sampling periods. Rounding
    here would be the quietest possible bug: a profile at the wrong lag drifts
    one phase per cycle and reads as a weak, slowly rotating seasonality rather
    than as a misconfiguration.
    """
    if period <= 0:
        raise SeasonalityError(f"period must be positive, got {period}")
    if cycle_seconds % period != 0:
        raise SeasonalityError(
            f"cycle of {cycle_seconds}s is not a whole number of {period}s periods"
        )
    return cycle_seconds // period


def phase_of(timestamp: datetime, period: int, cycle_seconds: int, timezone_name: str) -> int:
    """Phase of an instant within a cycle, keyed on local wall-clock time.

    The daily cycle is keyed on local time of day and the weekly cycle on local
    weekday plus time of day, which is what makes the profile survive a
    daylight-saving transition as a gap in one phase rather than as a one-hour
    rotation of every phase.
    """
    lag = lag_for(cycle_seconds, period)
    local = timestamp.astimezone(resolve_zone(timezone_name))
    into_day = local.hour * 3600 + local.minute * 60 + local.second
    if cycle_seconds == 86_400:
        offset = into_day
    elif cycle_seconds == 604_800:
        offset = local.weekday() * 86_400 + into_day
    else:
        # A cycle with no wall-clock meaning is keyed on the epoch, which is
        # exact for machine-driven periodicity and wrong for human periodicity.
        offset = int(timestamp.timestamp()) % cycle_seconds
    return (offset // period) % lag


# ---------------------------------------------------------------------------
# Grid
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SeriesGrid:
    """A series laid out on its own sampling grid, holes left as ``None``.

    Laying the series out this way is what lets the trend estimate require a
    complete window instead of silently averaging over a hole.
    """

    start: datetime
    period: int
    values: tuple[float | None, ...]
    timestamps: tuple[datetime, ...]
    off_grid: int

    @property
    def slots(self) -> int:
        return len(self.values)

    @property
    def observed(self) -> int:
        return sum(1 for value in self.values if value is not None)

    def observed_slots(self) -> list[int]:
        return [index for index, value in enumerate(self.values) if value is not None]

    def slice(self, begin: int, end: int) -> SeriesGrid:
        """A contiguous sub-grid, keeping absolute timestamps and the period.

        Slicing the grid rather than the series is what lets a holdout be cut
        without re-deriving coverage: coverage is computed once, where the
        series is read, and a model never recomputes it.
        """
        if begin < 0 or end > self.slots or begin >= end:
            raise SeasonalityError(f"invalid grid slice [{begin}, {end}) of {self.slots} slots")
        return SeriesGrid(
            start=self.timestamps[begin],
            period=self.period,
            values=self.values[begin:end],
            timestamps=self.timestamps[begin:end],
            off_grid=0,
        )


def grid_from_points(points: Sequence[SeriesPoint], period: int) -> SeriesGrid:
    """Place measured points on their sampling grid.

    A point whose offset from the first sample is not a whole number of periods
    cannot be placed and is counted rather than rounded into a neighbour: an
    off-grid point means the series mixes resolutions, and the caller needs to
    know that before a model reads it.

    This takes points rather than a series so that a caller which has dropped an
    observation — an open period, say — can grid what remains without having to
    invent a :class:`ingest.store.Coverage` for the shortened window. Coverage is
    measured once, where the series is read, and never recomputed here.
    """
    if not points:
        raise SeasonalityError("cannot grid an empty series")
    if period <= 0:
        raise SeasonalityError(f"series period must be positive, got {period}")

    start = points[0].timestamp
    span = int((points[-1].timestamp - start).total_seconds())
    slots = span // period + 1

    values: list[float | None] = [None] * slots
    timestamps = [start + timedelta(seconds=index * period) for index in range(slots)]
    off_grid = 0
    for point in points:
        offset = int((point.timestamp - start).total_seconds())
        if offset % period != 0:
            off_grid += 1
            continue
        values[offset // period] = point.value

    return SeriesGrid(
        start=start,
        period=period,
        values=tuple(values),
        timestamps=tuple(timestamps),
        off_grid=off_grid,
    )


def to_grid(series: MetricSeries) -> SeriesGrid:
    """Place a series on its sampling grid."""
    return grid_from_points(series.points, series.period)


# ---------------------------------------------------------------------------
# Decomposition
# ---------------------------------------------------------------------------


def centred_moving_average(values: Sequence[float | None], lag: int) -> list[float | None]:
    """Trend estimate of a seasonal series, one value per slot where defined.

    For an odd lag this is the plain ``lag``-point average. For an even lag it is
    the ``2 x lag`` average — half weight on each end point — because a window of
    even width has no centre sample, and using the plain average instead shifts
    the trend by half a period and leaks the shift into the seasonal profile.

    A window containing a hole yields no trend value. Averaging over the hole
    would produce a trend that is biased toward whatever the surviving phases
    were, which is exactly the kind of invented number this project refuses.
    """
    if lag <= 0:
        raise SeasonalityError(f"lag must be positive, got {lag}")

    count = len(values)
    out: list[float | None] = [None] * count
    half = lag // 2

    if lag % 2 == 1:
        for index in range(half, count - half):
            window = values[index - half: index + half + 1]
            if any(value is None for value in window):
                continue
            out[index] = math.fsum(value for value in window if value is not None) / lag
        return out

    for index in range(half, count - half):
        window = values[index - half: index + half + 1]
        if any(value is None for value in window):
            continue
        inner = [value for value in window[1:-1] if value is not None]
        first = window[0]
        last = window[-1]
        assert first is not None and last is not None  # guarded above
        out[index] = (math.fsum(inner) + 0.5 * first + 0.5 * last) / lag
    return out


@dataclass(frozen=True, slots=True)
class _Decomposition:
    profile: tuple[float, ...]
    support: tuple[int, ...]
    strength: float
    raw_strength: float
    p_value: float | None
    detrended_count: int


def _decompose(
    grid: SeriesGrid,
    lag: int,
    phases: Sequence[int],
    mode: Mode,
) -> _Decomposition | None:
    """Classical decomposition at a known lag, or ``None`` if not estimable."""
    trend = centred_moving_average(grid.values, lag)

    detrended: list[tuple[int, float]] = []
    for index, (value, level) in enumerate(zip(grid.values, trend)):
        if value is None or level is None:
            continue
        if mode == "multiplicative":
            if level <= 0:
                continue
            detrended.append((index, value / level))
        else:
            detrended.append((index, value - level))

    if not detrended:
        return None

    buckets: list[list[float]] = [[] for _ in range(lag)]
    for index, residual in detrended:
        buckets[phases[index]].append(residual)
    if any(not bucket for bucket in buckets):
        # A phase with no detrended observation has no estimate, and filling it
        # with the series mean is the same mistake as interpolating a hole.
        return None

    means = [math.fsum(bucket) / len(bucket) for bucket in buckets]
    if mode == "multiplicative":
        mean = math.fsum(means) / lag
        if mean <= 0:
            return None
        profile = [factor / mean for factor in means]
    else:
        mean = math.fsum(means) / lag
        profile = [factor - mean for factor in means]

    remainder: list[float] = []
    seasonal_plus_remainder: list[float] = []
    for index, residual in detrended:
        term = profile[phases[index]]
        seasonal_plus_remainder.append(residual)
        if mode == "multiplicative":
            if term <= 0:
                return None
            remainder.append(residual / term)
        else:
            remainder.append(residual - term)

    total = _variance(seasonal_plus_remainder)
    count = len(detrended)
    ratio = 1.0 if total <= 0 else _variance(remainder) / total
    explained = max(0.0, min(1.0, 1.0 - ratio))

    # Adjust for the degrees of freedom the profile spent, and test what is
    # left. ``lag - 1`` parameters are free because the profile is normalised.
    df_numerator = lag - 1
    df_denominator = count - lag
    if df_numerator < 1 or df_denominator < 1:
        strength = 0.0
        p_value: float | None = None
    else:
        strength = max(0.0, min(1.0, 1.0 - ratio * (count - 1) / df_denominator))
        if ratio <= 0.0:
            p_value = 0.0
        else:
            statistic = (explained / df_numerator) / (ratio / df_denominator)
            p_value = f_survival(statistic, df_numerator, df_denominator)

    return _Decomposition(
        profile=tuple(profile),
        support=tuple(len(bucket) for bucket in buckets),
        strength=strength,
        raw_strength=explained,
        p_value=p_value,
        detrended_count=count,
    )


def _variance(values: Sequence[float]) -> float:
    """Population variance; zero for fewer than two values."""
    count = len(values)
    if count < 2:
        return 0.0
    mean = math.fsum(values) / count
    return math.fsum((value - mean) ** 2 for value in values) / count


def _correlation(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Pearson correlation, or ``None`` when either side has no spread."""
    count = len(xs)
    if count < 2 or count != len(ys):
        return None
    mean_x = math.fsum(xs) / count
    mean_y = math.fsum(ys) / count
    dx = [value - mean_x for value in xs]
    dy = [value - mean_y for value in ys]
    denominator = math.sqrt(math.fsum(v * v for v in dx) * math.fsum(v * v for v in dy))
    if denominator <= 0:
        return None
    return math.fsum(a * b for a, b in zip(dx, dy)) / denominator


def choose_mode(grid: SeriesGrid, lag: int, cycle_name: str) -> tuple[Mode, Finding]:
    """Decide additive versus multiplicative, and say why.

    The cycle's name travels into the message because the decision is made per
    candidate: a result can carry one mode finding for the daily cycle and
    another for the weekly one, and a message that named neither would leave a
    reader unable to tell which cycle it described.

    The test is whether the seasonal swing grows with the level, measured as the
    correlation between each complete cycle's mean and its peak-to-trough range.
    That is the definition of multiplicative seasonality; a lower residual
    variance is not, because the two modes' residuals do not live on the same
    scale and so cannot be ranked against each other.
    """
    observed = [value for value in grid.values if value is not None]
    if any(value <= 0 for value in observed):
        return "additive", Finding(
            "F103",
            "note",
            f"{cycle_name} cycle, additive: the series contains a non-positive value, so "
            "a multiplicative factor has no meaning at that point",
        )

    levels: list[float] = []
    ranges: list[float] = []
    for begin in range(0, grid.slots - lag + 1, lag):
        window = [value for value in grid.values[begin: begin + lag] if value is not None]
        if len(window) < lag:
            continue
        levels.append(math.fsum(window) / lag)
        ranges.append(max(window) - min(window))

    if len(levels) < MIN_CYCLES_FOR_MODE_TEST:
        return "additive", Finding(
            "F104",
            "note",
            f"{cycle_name} cycle, additive assumed: the amplitude-versus-level test needs "
            f"{MIN_CYCLES_FOR_MODE_TEST} complete cycles of it and this series holds "
            f"{len(levels)}; a correlation over two cycles is +/-1 whatever the data",
        )

    correlation = _correlation(levels, ranges)
    if correlation is None:
        return "additive", Finding(
            "F105",
            "note",
            f"{cycle_name} cycle, additive: either the per-cycle level or the per-cycle "
            "amplitude is constant, so no scaling relationship is measurable",
        )
    if correlation >= MULTIPLICATIVE_CORRELATION:
        return "multiplicative", Finding(
            "F106",
            "note",
            f"{cycle_name} cycle, multiplicative: per-cycle amplitude tracks per-cycle "
            f"level (r={correlation:.2f} over {len(levels)} cycles)",
        )
    return "additive", Finding(
        "F107",
        "note",
        f"{cycle_name} cycle, additive: per-cycle amplitude does not track per-cycle "
        f"level (r={correlation:.2f} over {len(levels)} cycles)",
    )


def detect_seasonality(
    series: MetricSeries,
    *,
    timezone_name: str = "UTC",
    cycles: Sequence[tuple[str, int]] = CANDIDATE_CYCLES,
    min_strength: float = MIN_USEFUL_STRENGTH,
    max_p_value: float = MAX_SEASONAL_P_VALUE,
) -> SeasonalAnalysis:
    """Estimate the strongest representable seasonal cycle of a series."""
    if not series.points:
        return SeasonalAnalysis(
            chosen=None,
            candidates=(),
            findings=(Finding("F100", "error", "series is empty"),),
        )
    return detect_on_grid(
        to_grid(series),
        timezone_name=timezone_name,
        cycles=cycles,
        min_strength=min_strength,
        max_p_value=max_p_value,
    )


def detect_on_grid(
    grid: SeriesGrid,
    *,
    timezone_name: str = "UTC",
    cycles: Sequence[tuple[str, int]] = CANDIDATE_CYCLES,
    min_strength: float = MIN_USEFUL_STRENGTH,
    max_p_value: float = MAX_SEASONAL_P_VALUE,
) -> SeasonalAnalysis:
    """Estimate the strongest representable seasonal cycle of a gridded series.

    Returns every candidate that could be estimated together with the chosen
    one, so a reviewer can see that a weekly shape was considered and rejected
    rather than never looked for. Taking a grid rather than a series is what
    lets a caller estimate seasonality from a training window alone — which is
    the difference between an honest model comparison and one that has already
    seen the data it is scored on.
    """
    findings: list[Finding] = []
    if grid.off_grid:
        findings.append(
            Finding(
                "F108",
                "warning",
                f"{grid.off_grid} point(s) do not sit on the {grid.period}s sampling "
                "grid and were excluded; the series mixes resolutions",
            )
        )

    findings.extend(_zone_findings(grid, timezone_name))

    candidates: list[SeasonalModel] = []
    for name, cycle_seconds in cycles:
        try:
            lag = lag_for(cycle_seconds, grid.period)
        except SeasonalityError:
            findings.append(
                Finding(
                    "F101",
                    "note",
                    f"{name} cycle not representable: {cycle_seconds}s is not a whole "
                    f"number of {grid.period}s sampling periods",
                )
            )
            continue

        if lag < 2:
            findings.append(
                Finding(
                    "F101",
                    "note",
                    f"{name} cycle not representable: the sampling period is the whole "
                    "cycle, so there is no within-cycle shape to estimate",
                )
            )
            continue

        required = MIN_CYCLES_FOR_PROFILE * lag
        if grid.slots < required:
            findings.append(
                Finding(
                    "F102",
                    "note",
                    f"{name} cycle not estimable: a profile at lag {lag} needs "
                    f"{required} slots of history and the series spans {grid.slots}",
                )
            )
            continue

        phases = [
            phase_of(stamp, grid.period, cycle_seconds, timezone_name)
            for stamp in grid.timestamps
        ]
        mode, mode_finding = choose_mode(grid, lag, name)
        decomposition = _decompose(grid, lag, phases, mode)
        if decomposition is None:
            findings.append(
                Finding(
                    "F109",
                    "note",
                    f"{name} cycle not estimable: at least one phase has no detrended "
                    "observation, usually a hole wider than the trend window",
                )
            )
            continue

        findings.append(mode_finding)
        model = SeasonalModel(
            cycle_name=name,
            cycle_seconds=cycle_seconds,
            lag=lag,
            mode=mode,
            timezone_name=timezone_name,
            profile=decomposition.profile,
            phase_support=decomposition.support,
            strength=decomposition.strength,
            raw_strength=decomposition.raw_strength,
            p_value=decomposition.p_value,
            detrended_observations=decomposition.detrended_count,
            cycles_observed=grid.slots / lag,
        )
        candidates.append(model)

        if model.is_saturated:
            findings.append(
                Finding(
                    "F114",
                    "warning",
                    f"{name} profile is saturated: {model.detrended_observations} detrended "
                    f"observation(s) against {lag} phases, so it reproduces its input and "
                    "its explained share is 1.0 for any series at all; no seasonal strength "
                    "can be demonstrated from this much history",
                )
            )

        if model.min_phase_support < 2:
            singletons = sum(1 for support in model.phase_support if support < 2)
            findings.append(
                Finding(
                    "F110",
                    "warning",
                    f"{name} profile is not used: {singletons} of {lag} phases rest on a "
                    f"single observation ({model.cycles_observed:.1f} cycles of history), so "
                    "those phases contribute no residual variance and the F test has "
                    f"nothing to weigh them against; {CYCLES_FOR_REPEATED_SUPPORT} cycles "
                    "give every phase a repeat",
                )
            )

    # Two gates, because they answer different questions: the F test asks
    # whether any seasonality is present, and the adjusted share asks whether it
    # is large enough to be worth spending the profile's parameters on.
    usable = [
        model
        for model in candidates
        if model.p_value is not None
        and model.p_value <= max_p_value
        and model.strength >= min_strength
        and model.min_phase_support >= 2
    ]
    chosen: SeasonalModel | None = None
    if usable:
        # Longest cycle among those within a whisker of the best strength: a
        # weekly shape subsumes a daily one, and picking the daily cycle because
        # it scored marginally higher throws away the day-of-week effect.
        best = max(model.strength for model in usable)
        chosen = max(
            (model for model in usable if model.strength >= best - 0.05),
            key=lambda model: model.cycle_seconds,
        )
        findings.append(
            Finding(
                "F111",
                "note",
                f"{chosen.cycle_name} seasonality selected, adjusted strength "
                f"{chosen.strength:.2f} (unadjusted {chosen.raw_strength:.2f}), "
                f"p={_format_p(chosen.p_value)}, {chosen.mode}, lag {chosen.lag}",
            )
        )
    elif candidates:
        strongest = max(candidates, key=lambda model: model.strength)
        if strongest.min_phase_support < 2:
            reason = (
                f"at {strongest.cycles_observed:.1f} cycles of history at least one phase "
                "rests on a single observation, so the profile is fitted to points it "
                "cannot also be judged against"
            )
        elif strongest.p_value is None:
            reason = "no F test is possible on a saturated profile"
        elif strongest.p_value > max_p_value:
            reason = (
                f"the decomposition is indistinguishable from no seasonality "
                f"(p={_format_p(strongest.p_value)} against a {max_p_value:.2f} threshold)"
            )
        else:
            reason = (
                f"it explains only {strongest.strength:.0%} of the detrended variance once "
                f"its {strongest.lag - 1} free parameters are charged for "
                f"({strongest.raw_strength:.0%} before), below the {min_strength:.0%} floor"
            )
        findings.append(
            Finding(
                "F112",
                "note",
                f"no seasonality modelled: for the strongest representable cycle "
                f"({strongest.cycle_name}), {reason}",
            )
        )

    _LOG.debug(
        "seasonality: %d candidate(s), chosen=%s",
        len(candidates),
        chosen.cycle_name if chosen else "none",
    )
    return SeasonalAnalysis(
        chosen=chosen,
        candidates=tuple(candidates),
        findings=tuple(findings),
    )


def _format_p(p_value: float | None) -> str:
    if p_value is None:
        return "untestable"
    return "<1e-6" if p_value < 1e-6 else f"{p_value:.3g}"


def _zone_findings(grid: SeriesGrid, timezone_name: str) -> list[Finding]:
    """Report a daylight-saving transition inside the observed span."""
    zone = resolve_zone(timezone_name)
    first = grid.timestamps[0].astimezone(zone).utcoffset()
    last = grid.timestamps[-1].astimezone(zone).utcoffset()
    if first is None or last is None or first == last:
        return []
    return [
        Finding(
            "F113",
            "note",
            f"the observed span crosses a UTC-offset change in {timezone_name} "
            f"({first} to {last}); phases are keyed on local time of day, so one "
            "phase loses a sample at the transition and another gains one, which "
            "the per-phase support counts show directly",
        )
    ]


# ---------------------------------------------------------------------------
# Applying a profile
# ---------------------------------------------------------------------------


def deseasonalise(
    timestamps: Sequence[datetime],
    values: Sequence[float],
    model: SeasonalModel,
    period: int,
) -> list[float]:
    """Remove the seasonal term, leaving level plus trend plus noise."""
    out: list[float] = []
    for stamp, value in zip(timestamps, values):
        factor = model.factor_at(stamp, period)
        if model.mode == "multiplicative":
            if factor <= 0:
                raise SeasonalityError(
                    "multiplicative profile holds a non-positive factor; it cannot divide"
                )
            out.append(value / factor)
        else:
            out.append(value - factor)
    return out


def reseasonalise(
    timestamps: Sequence[datetime],
    values: Sequence[float],
    model: SeasonalModel,
    period: int,
) -> list[float]:
    """Inverse of :func:`deseasonalise`, exact on the same timestamps."""
    out: list[float] = []
    for stamp, value in zip(timestamps, values):
        factor = model.factor_at(stamp, period)
        out.append(value * factor if model.mode == "multiplicative" else value + factor)
    return out
