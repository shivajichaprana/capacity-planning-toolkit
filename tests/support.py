"""Series the model checks are run against.

Two rules govern everything here.

**Fixtures carry the store's own coverage.** A model's first gate is the
coverage of the window it was handed, so coverage is computed with the store's
helper rather than written by hand: a fixture whose coverage was derived
differently would exercise a series shape that production never produces, and
the refusal paths -- which are the most valuable thing these models do -- would
be tested against a fiction.

**Generators are deterministic.** Every noisy series is drawn from a seeded
generator, because a check that fails one run in twenty is worse than no check:
it trains a reader to re-run the suite instead of reading it.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Final

from ingest.store import MetricSeries, SeriesKey, SeriesPoint, _coverage_of

HOUR: Final[int] = 3_600
DAY: Final[int] = 86_400
WEEK: Final[int] = 604_800

#: Midnight UTC on a Monday, so that an hourly grid starting here puts slot
#: zero at phase zero of both the daily and the weekly cycle. A check asserts
#: the weekday, because the whole phase story rests on it.
EPOCH: Final[datetime] = datetime(2026, 3, 2, tzinfo=timezone.utc)


def make_key(*, statistic: str = "Average", metric: str = "CPUUtilization") -> SeriesKey:
    """A series key with the dimension ordering the store would have produced."""
    return SeriesKey(
        source="cloudwatch",
        namespace="AWS/EC2",
        metric=metric,
        statistic=statistic,
        entity="AutoScalingGroupName=web",
    )


def make_points(
    values: Sequence[float],
    *,
    period: int = HOUR,
    start: datetime = EPOCH,
    skip: Sequence[int] = (),
) -> tuple[SeriesPoint, ...]:
    """Lay ``values`` on a regular grid, omitting the slots named in ``skip``.

    Omitting a slot is how a hole is built: the point is absent rather than
    zero, which is the distinction every coverage gate turns on.
    """
    omit = set(skip)
    return tuple(
        SeriesPoint(timestamp=start + timedelta(seconds=period * index), value=float(value))
        for index, value in enumerate(values)
        if index not in omit
    )


def make_series(
    values: Sequence[float],
    *,
    period: int = HOUR,
    start: datetime = EPOCH,
    unit: str = "Percent",
    statistic: str = "Average",
    metric: str = "CPUUtilization",
    skip: Sequence[int] = (),
) -> MetricSeries:
    """A series shaped exactly as :meth:`ingest.store.ObservationStore.read_series` returns."""
    points = make_points(values, period=period, start=start, skip=skip)
    return MetricSeries(
        key=make_key(statistic=statistic, metric=metric),
        unit=unit,
        period=period,
        points=points,
        coverage=_coverage_of(points, period),
    )


def series_from_points(
    points: Sequence[SeriesPoint],
    *,
    period: int = HOUR,
    unit: str = "Percent",
    statistic: str = "Average",
) -> MetricSeries:
    """A series built from points that may not sit on the grid."""
    ordered = tuple(sorted(points, key=lambda point: point.timestamp))
    return MetricSeries(
        key=make_key(statistic=statistic),
        unit=unit,
        period=period,
        points=ordered,
        coverage=_coverage_of(ordered, period),
    )


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


def ramp(count: int, *, base: float = 10.0, slope: float = 1.0) -> list[float]:
    """A noiseless straight line: the one series every model should fit exactly."""
    return [base + slope * index for index in range(count)]


def noise(count: int, *, base: float = 50.0, sd: float = 1.0, seed: int = 1) -> list[float]:
    """Stationary Gaussian noise about a level, with no structure to find."""
    generator = random.Random(seed)
    return [base + generator.gauss(0.0, sd) for _ in range(count)]


def noisy_ramp(
    count: int,
    *,
    base: float = 10.0,
    slope: float = 0.5,
    sd: float = 1.0,
    seed: int = 7,
) -> list[float]:
    """A trend with residual spread, so an interval has something to be made of."""
    generator = random.Random(seed)
    return [base + slope * index + generator.gauss(0.0, sd) for index in range(count)]


def daily_wave(
    count: int,
    *,
    base: float = 50.0,
    amplitude: float = 10.0,
    slope: float = 0.0,
    period: int = HOUR,
    sd: float = 0.0,
    seed: int = 3,
    multiplicative: bool = False,
) -> list[float]:
    """A daily cycle on an hourly grid, optionally riding a trend.

    The cycle is keyed on the slot's position within the day, which is the same
    thing the phase function derives from the timestamp as long as the series
    starts at local midnight -- so a profile recovered from this series can be
    compared against the shape that generated it.
    """
    lag = DAY // period
    generator = random.Random(seed)
    out: list[float] = []
    for index in range(count):
        level = base + slope * index
        swing = amplitude * math.sin(2.0 * math.pi * (index % lag) / lag)
        value = level * (1.0 + swing / base) if multiplicative else level + swing
        if sd:
            value += generator.gauss(0.0, sd)
        out.append(value)
    return out


def weekend_step(
    count: int,
    *,
    base: float = 100.0,
    step: float = 20.0,
    sd: float = 0.0,
    seed: int = 5,
    period: int = HOUR,
    start: datetime = EPOCH,
) -> list[float]:
    """A weekly cycle shaped as a weekend level change rather than a sine.

    A step is the harder case: it is not what a sine-shaped alternative would
    fit, so finding it is evidence the profile is estimated from the data and
    not assumed.
    """
    generator = random.Random(seed)
    out: list[float] = []
    for index in range(count):
        stamp = start + timedelta(seconds=period * index)
        value = base + (step if stamp.weekday() >= 5 else 0.0)
        if sd:
            value += generator.gauss(0.0, sd)
        out.append(value)
    return out
