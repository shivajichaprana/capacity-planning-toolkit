"""CloudWatch metric collection.

Reads one metric from ``GetMetricStatistics`` over a bounded window and writes
the result to the observation store. The collector deliberately does no
interpolation: a period CloudWatch has no datapoint for is simply absent, and
the store's coverage accounting reports it as a gap. A model that cannot
tolerate the gap must say so rather than be handed a fabricated value.

Run it as a module::

    python -m ingest.cloudwatch \
      --namespace AWS/ECS --metric CPUUtilization \
      --dimension ServiceName=checkout --dimension ClusterName=production \
      --statistic Average --period 3600 --lookback-hours 336
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Final

from ingest.runtime import add_common_arguments, build_session, configure_logging
from ingest.store import (
    Observation,
    ObservationStore,
    SeriesKey,
    canonical_entity,
    iter_windows,
    utc_now,
)

_LOG = logging.getLogger(__name__)

SOURCE: Final[str] = "cloudwatch"

#: Statistics ``GetMetricStatistics`` returns under their own response key.
BASE_STATISTICS: Final[frozenset[str]] = frozenset(
    {"Average", "Sum", "Minimum", "Maximum", "SampleCount"}
)

#: A single call returns at most 1440 datapoints, so the window is chunked to
#: stay under that regardless of the requested period.
MAX_DATAPOINTS_PER_CALL: Final[int] = 1440

#: CloudWatch retains sub-minute and one-minute resolution for a limited time;
#: beyond this, only five-minute-and-coarser periods return anything at all.
FINE_RESOLUTION_RETENTION: Final[timedelta] = timedelta(days=15)


class CollectionError(RuntimeError):
    """Raised when the request cannot be satisfied as asked."""


@dataclass(frozen=True, slots=True)
class MetricRequest:
    """One fully specified metric collection."""

    namespace: str
    metric_name: str
    statistic: str
    period: int
    dimensions: Mapping[str, str]
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.period <= 0 or self.period % 60 != 0:
            raise CollectionError(
                f"period must be a positive multiple of 60 seconds, got {self.period}"
            )
        if self.end <= self.start:
            raise CollectionError("end must be after start")
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise CollectionError("window bounds must be timezone-aware")
        if not self.statistic:
            raise CollectionError("a statistic is required")

    @property
    def is_extended(self) -> bool:
        """True for a percentile such as ``p99``, which is requested differently."""
        return self.statistic not in BASE_STATISTICS

    def series_key(self) -> SeriesKey:
        return SeriesKey(
            source=SOURCE,
            namespace=self.namespace,
            metric=self.metric_name,
            statistic=self.statistic,
            entity=canonical_entity(self.dimensions),
        )


def chunk_span(period: int) -> timedelta:
    """Largest window one call may cover without exceeding the datapoint cap."""
    return timedelta(seconds=period * MAX_DATAPOINTS_PER_CALL)


def warn_on_retention(request: MetricRequest, *, now: datetime | None = None) -> str | None:
    """Return a warning when the window asks for resolution that has aged out.

    CloudWatch does not error here — it returns fewer datapoints, or none — so
    the only way the operator learns about it is if the collector says so.
    """
    if request.period >= 300:
        return None
    reference = now or utc_now()
    age = reference - request.start
    if age > FINE_RESOLUTION_RETENTION:
        return (
            f"period {request.period}s was requested for a window starting "
            f"{age.days} days ago; fine-resolution datapoints are retained for "
            f"{FINE_RESOLUTION_RETENTION.days} days, so expect gaps or an empty result"
        )
    return None


def _as_dimension_list(dimensions: Mapping[str, str]) -> list[dict[str, str]]:
    return [{"Name": name, "Value": dimensions[name]} for name in sorted(dimensions)]


def _extract_value(point: Mapping[str, Any], statistic: str) -> float | None:
    """Pull the requested statistic out of a datapoint.

    Base statistics arrive under their own name; percentiles arrive nested under
    ``ExtendedStatistics``.
    """
    if statistic in point:
        return float(point[statistic])
    extended = point.get("ExtendedStatistics")
    if isinstance(extended, Mapping) and statistic in extended:
        return float(extended[statistic])
    return None


def collect(
    client: Any,
    request: MetricRequest,
) -> list[Observation]:
    """Fetch every datapoint for ``request``, chunking the window as needed."""
    warning = warn_on_retention(request)
    if warning:
        _LOG.warning("%s", warning)

    key = request.series_key()
    dimensions = _as_dimension_list(request.dimensions)
    call: dict[str, Any] = {
        "Namespace": request.namespace,
        "MetricName": request.metric_name,
        "Dimensions": dimensions,
        "Period": request.period,
    }
    if request.is_extended:
        call["ExtendedStatistics"] = [request.statistic]
    else:
        call["Statistics"] = [request.statistic]

    observations: list[Observation] = []
    seen: set[int] = set()
    span = chunk_span(request.period)

    for window_start, window_end in iter_windows(request.start, request.end, span=span):
        _LOG.debug(
            "requesting %s from %s to %s",
            key.label(),
            window_start.isoformat(),
            window_end.isoformat(),
        )
        response = client.get_metric_statistics(
            StartTime=window_start,
            EndTime=window_end,
            **call,
        )
        datapoints: Sequence[Mapping[str, Any]] = response.get("Datapoints", []) or []
        for point in datapoints:
            timestamp = point.get("Timestamp")
            if not isinstance(timestamp, datetime):
                _LOG.debug("skipping datapoint with no usable timestamp")
                continue
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            value = _extract_value(point, request.statistic)
            if value is None:
                _LOG.debug(
                    "datapoint at %s carries no %s value",
                    timestamp.isoformat(),
                    request.statistic,
                )
                continue
            epoch = int(timestamp.timestamp())
            if epoch in seen:
                # Chunk boundaries are half-open on the store side, but the API
                # may still repeat an edge datapoint; drop it here so the write
                # count reported is the true point count.
                continue
            seen.add(epoch)
            observations.append(
                Observation(
                    key=key,
                    timestamp=timestamp,
                    value=value,
                    unit=str(point.get("Unit", "None")),
                    period=request.period,
                    meta={"label": response.get("Label", request.metric_name)},
                )
            )

    observations.sort(key=lambda obs: obs.timestamp)
    _LOG.info(
        "collected %d datapoint(s) for %s over %s to %s",
        len(observations),
        key.label(),
        request.start.isoformat(),
        request.end.isoformat(),
    )
    return observations


def parse_dimension(raw: str) -> tuple[str, str]:
    """Parse a ``Name=Value`` dimension argument."""
    name, separator, value = raw.partition("=")
    if not separator or not name.strip() or not value.strip():
        raise argparse.ArgumentTypeError(
            f"dimension must be given as Name=Value, got {raw!r}"
        )
    return name.strip(), value.strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ingest.cloudwatch",
        description="Collect a CloudWatch metric into the observation store.",
    )
    parser.add_argument("--namespace", required=True, help="metric namespace, e.g. AWS/ECS")
    parser.add_argument("--metric", required=True, help="metric name, e.g. CPUUtilization")
    parser.add_argument(
        "--statistic",
        default="Average",
        help="statistic or percentile to collect (default: Average)",
    )
    parser.add_argument(
        "--period",
        type=int,
        default=3600,
        help="sampling period in seconds, a multiple of 60 (default: 3600)",
    )
    parser.add_argument(
        "--dimension",
        action="append",
        default=[],
        type=parse_dimension,
        metavar="NAME=VALUE",
        help="metric dimension; repeat for each one",
    )
    parser.add_argument(
        "--lookback-hours",
        type=int,
        default=336,
        help="how far back to collect from now, in hours (default: 336)",
    )
    add_common_arguments(parser)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level, args.log_format)

    if args.lookback_hours <= 0:
        _LOG.error("--lookback-hours must be positive")
        return 2

    end = utc_now().replace(second=0, microsecond=0)
    start = end - timedelta(hours=args.lookback_hours)

    try:
        request = MetricRequest(
            namespace=args.namespace,
            metric_name=args.metric,
            statistic=args.statistic,
            period=args.period,
            dimensions=dict(args.dimension),
            start=start,
            end=end,
        )
    except CollectionError as error:
        _LOG.error("%s", error)
        return 2

    session = build_session(args.region, args.profile)
    client = session.client("cloudwatch")

    try:
        observations = collect(client, request)
    except Exception:
        _LOG.exception("metric collection failed")
        return 1

    if not observations:
        _LOG.warning(
            "no datapoints returned; check the dimensions, the period and the window"
        )
        return 0

    if args.dry_run:
        _LOG.info("dry run: %d datapoint(s) not written", len(observations))
        return 0

    with ObservationStore(args.store) as store:
        store.write(observations)
        series = store.read_series(request.series_key(), start=start, end=end)

    _LOG.info(
        "series %s now holds %d point(s), coverage %.1f%%%s",
        series.key.label(),
        len(series),
        series.coverage.ratio * 100.0,
        " (gaps present)" if series.coverage.has_hole else "",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
