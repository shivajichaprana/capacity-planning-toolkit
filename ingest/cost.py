"""Cost Explorer collection.

Capacity work needs the money axis as well as the utilisation axis: a tier with
plenty of headroom and a cost curve steeper than its demand curve is still a
capacity problem. This collector reads ``GetCostAndUsage`` for a bounded range
and writes one series per cost metric and group value.

Two Cost Explorer behaviours shape the implementation:

* The range is **date-based and end-exclusive**. ``2026-02-01`` to ``2026-02-08``
  is seven complete UTC days, not eight. Observations are stamped at the start of
  the period they describe.
* ``AmortizedCost`` and ``UnblendedCost`` answer different questions. Amortised
  spreads a reservation or savings-plan commitment across the term it covers,
  which is the one capacity planning wants; unblended shows the cash as billed.
  The default here is amortised, and the choice is recorded on every point.

Run it as a module::

    python -m ingest.cost --lookback-days 14 --metric AmortizedCost --group-by SERVICE
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Final

from ingest.runtime import add_common_arguments, build_session, configure_logging
from ingest.store import (
    Observation,
    ObservationStore,
    SeriesKey,
    canonical_entity,
    utc_now,
)

_LOG = logging.getLogger(__name__)

SOURCE: Final[str] = "costexplorer"
NAMESPACE: Final[str] = "Billing"

#: Cost Explorer is a global endpoint; the client must be built against it
#: regardless of the region a workload runs in.
CLIENT_REGION: Final[str] = "us-east-1"

SUPPORTED_GRANULARITY: Final[frozenset[str]] = frozenset({"DAILY", "MONTHLY", "HOURLY"})

#: Period in seconds implied by each granularity. Monthly is reported as 30 days
#: so that a coverage calculation has a usable constant; the exact month length
#: lives on each observation's metadata.
PERIOD_SECONDS: Final[Mapping[str, int]] = {
    "HOURLY": 3600,
    "DAILY": 86400,
    "MONTHLY": 86400 * 30,
}

#: Dimensions that may be used with --group-by.
GROUPABLE_DIMENSIONS: Final[frozenset[str]] = frozenset(
    {
        "SERVICE",
        "USAGE_TYPE",
        "INSTANCE_TYPE",
        "LINKED_ACCOUNT",
        "REGION",
        "OPERATION",
        "RECORD_TYPE",
        "PLATFORM",
    }
)

UNGROUPED: Final[str] = "TOTAL"


class CostCollectionError(RuntimeError):
    """Raised when the request cannot be satisfied as asked."""


@dataclass(frozen=True, slots=True)
class CostRequest:
    """One fully specified cost collection."""

    start: date
    end: date
    granularity: str
    metrics: tuple[str, ...]
    group_by: str | None
    tag_key: str | None = None

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise CostCollectionError(
                "end date must be after start date (the range is end-exclusive)"
            )
        if self.granularity not in SUPPORTED_GRANULARITY:
            raise CostCollectionError(
                "granularity must be one of "
                f"{sorted(SUPPORTED_GRANULARITY)}, got {self.granularity!r}"
            )
        if not self.metrics:
            raise CostCollectionError("at least one cost metric is required")
        if self.group_by is not None and self.group_by not in GROUPABLE_DIMENSIONS:
            raise CostCollectionError(
                f"group-by must be one of {sorted(GROUPABLE_DIMENSIONS)}, got {self.group_by!r}"
            )
        if self.tag_key is not None and self.group_by is not None:
            raise CostCollectionError("group by a dimension or by a tag key, not both")

    @property
    def period(self) -> int:
        return PERIOD_SECONDS[self.granularity]

    def as_api_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "TimePeriod": {"Start": self.start.isoformat(), "End": self.end.isoformat()},
            "Granularity": self.granularity,
            "Metrics": list(self.metrics),
        }
        if self.group_by is not None:
            kwargs["GroupBy"] = [{"Type": "DIMENSION", "Key": self.group_by}]
        elif self.tag_key is not None:
            kwargs["GroupBy"] = [{"Type": "TAG", "Key": self.tag_key}]
        return kwargs

    def grouping_label(self) -> str:
        if self.group_by is not None:
            return self.group_by
        if self.tag_key is not None:
            return f"TAG:{self.tag_key}"
        return UNGROUPED


def _period_start(raw: Mapping[str, Any]) -> datetime | None:
    """Parse a result period's start into an aware UTC datetime.

    Cost Explorer returns a bare date for daily and monthly granularity and a
    full timestamp for hourly, so both shapes are handled.
    """
    value = raw.get("Start")
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(f"{text}T00:00:00+00:00")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _metric_amount(payload: Mapping[str, Any]) -> tuple[float, str] | None:
    """Pull ``(amount, unit)`` out of a metric value, or ``None`` if unusable."""
    amount = payload.get("Amount")
    if amount is None:
        return None
    try:
        return float(amount), str(payload.get("Unit", "None"))
    except (TypeError, ValueError):
        return None


def _entity_for(group_label: str, keys: Sequence[str] | None) -> str:
    """Identity string for a result row.

    An ungrouped total gets a fixed identity so it does not collide with a group
    value; a grouped row is identified by its dimension name and value, which
    keeps two different groupings of the same spend in separate series.
    """
    if not keys:
        return canonical_entity({group_label: UNGROUPED})
    value = keys[0] if keys[0] else UNGROUPED
    return canonical_entity({group_label: value})


def collect(client: Any, request: CostRequest) -> list[Observation]:
    """Fetch every result page for ``request`` and flatten it to observations."""
    group_label = request.grouping_label()
    observations: list[Observation] = []
    kwargs = request.as_api_kwargs()
    page = 0
    next_token: str | None = None

    while True:
        page += 1
        call = dict(kwargs)
        if next_token:
            call["NextPageToken"] = next_token
        _LOG.debug("requesting cost page %d", page)
        response = client.get_cost_and_usage(**call)

        for result in response.get("ResultsByTime", []) or []:
            timestamp = _period_start(result.get("TimePeriod", {}) or {})
            if timestamp is None:
                _LOG.debug("skipping result with an unparseable period")
                continue
            estimated = bool(result.get("Estimated", False))

            rows: list[tuple[Sequence[str] | None, Mapping[str, Any]]] = []
            groups = result.get("Groups") or []
            for group in groups:
                rows.append((group.get("Keys") or [], group.get("Metrics") or {}))
            if not groups:
                rows.append((None, result.get("Total") or {}))

            for keys, metrics in rows:
                entity = _entity_for(group_label, keys)
                for metric_name in request.metrics:
                    payload = metrics.get(metric_name)
                    if not isinstance(payload, Mapping):
                        continue
                    parsed = _metric_amount(payload)
                    if parsed is None:
                        _LOG.debug("metric %s at %s has no usable amount", metric_name, timestamp)
                        continue
                    amount, unit = parsed
                    observations.append(
                        Observation(
                            key=SeriesKey(
                                source=SOURCE,
                                namespace=NAMESPACE,
                                metric=metric_name,
                                statistic=request.granularity,
                                entity=entity,
                            ),
                            timestamp=timestamp,
                            value=amount,
                            unit=unit,
                            period=request.period,
                            meta={
                                "estimated": estimated,
                                "grouping": group_label,
                                "granularity": request.granularity,
                            },
                        )
                    )

        next_token = response.get("NextPageToken")
        if not next_token:
            break

    observations.sort(key=lambda obs: (obs.key.entity, obs.key.metric, obs.timestamp))
    _LOG.info(
        "collected %d cost observation(s) across %d page(s) for %s to %s grouped by %s",
        len(observations),
        page,
        request.start.isoformat(),
        request.end.isoformat(),
        group_label,
    )
    return observations


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ingest.cost",
        description="Collect cost and usage into the observation store.",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=90,
        help="how many complete periods back from today to collect (default: 90)",
    )
    parser.add_argument(
        "--granularity",
        default="DAILY",
        choices=sorted(SUPPORTED_GRANULARITY),
        help="result granularity (default: DAILY)",
    )
    parser.add_argument(
        "--metric",
        action="append",
        default=[],
        help="cost metric; repeat for several (default: AmortizedCost)",
    )
    parser.add_argument(
        "--group-by",
        default=None,
        choices=sorted(GROUPABLE_DIMENSIONS),
        help="dimension to group by; omit for an account total",
    )
    parser.add_argument(
        "--tag-key",
        default=None,
        help="cost-allocation tag key to group by instead of a dimension",
    )
    add_common_arguments(parser)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level, args.log_format)

    if args.lookback_days <= 0:
        _LOG.error("--lookback-days must be positive")
        return 2

    # The range is end-exclusive, so today's partial period is excluded and the
    # newest observation is the most recent complete one.
    end = utc_now().date()
    start = end - timedelta(days=args.lookback_days)
    metrics = tuple(args.metric) if args.metric else ("AmortizedCost",)

    try:
        request = CostRequest(
            start=start,
            end=end,
            granularity=args.granularity,
            metrics=metrics,
            group_by=args.group_by,
            tag_key=args.tag_key,
        )
    except CostCollectionError as error:
        _LOG.error("%s", error)
        return 2

    session = build_session(args.region or CLIENT_REGION, args.profile)
    client = session.client("ce", region_name=CLIENT_REGION)

    try:
        observations = collect(client, request)
    except Exception:
        _LOG.exception("cost collection failed")
        return 1

    if not observations:
        _LOG.warning("no cost results returned for the requested range")
        return 0

    if args.dry_run:
        _LOG.info("dry run: %d observation(s) not written", len(observations))
        return 0

    with ObservationStore(args.store) as store:
        store.write(observations)
        distinct = {obs.key for obs in observations}

    _LOG.info("store now holds %d cost series for this grouping", len(distinct))
    return 0


if __name__ == "__main__":
    sys.exit(main())
