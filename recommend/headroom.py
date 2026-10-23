"""Headroom recommendations: a run-out date, a decide-by date, and a size.

This module joins the two halves of the toolkit. The forecast says where demand
is going and how uncertain that is; the saturation analysis says how much more
demand the current configuration can absorb before a stated latency sensitivity
is exceeded. Neither is a decision. The decision is the smallest integer fleet
whose absorbable demand covers the forecast's upper bound at the planning
horizon, together with the date by which somebody has to order it.

Four corrections are applied on the way, in the same spirit as the saturation
module's four.

**The run-out date comes from the upper bound.** The central line crosses the
ceiling later than the upper bound does, always, because the interval widens
with horizon. Planning to the central line is planning to a coin flip. The
earlier of the two dates is the one reported, the later one is reported beside
it, and the gap between them is the margin the interval bought.

**The demand multiplier and the operating point are not measured in the same
place.** The saturation ceiling is expressed as a multiplier on the operating
point -- an upper percentile of the observed utilization. The forecast projects
the series' own statistic, which is usually a mean. Comparing them assumes the
distribution keeps its shape as the level moves: that the peak-to-mean ratio is
scale-invariant. That assumption is recorded rather than buried, and its
direction is known. Independent demand sources superpose, so a larger aggregate
is relatively smoother and the real peak-to-mean falls as volume rises. Holding
it fixed therefore over-provisions slightly, which is the safe direction, but
only for aggregate demand -- a fleet whose growth is one large tenant rather
than many small ones gets no smoothing and the assumption is neutral at best.

**Rounding up is a cost, so it is reported as one.** The required size is an
integer. The achieved headroom at that integer generally exceeds the required
headroom, and on a three-unit fleet the forced overshoot can be larger than the
growth being planned for. Both numbers are reported so that the share of the
decision forced by granularity is visible.

**A pooled fleet scales better than a sharded one.** For servers sharing a
queue, the utilization sustainable at a fixed elasticity rises with the server
count, so the size needed to absorb a given growth is smaller than the linear
reading says. For a sharded fleet -- one queue per unit, traffic hashed or
round-robined to a fixed shard -- there is no pooling and the linear reading is
exactly right. Which one a fleet is cannot be read off a utilization metric, so
it is declared on the fleet, and the pooling gain is only claimed where the
declaration says it exists.

The result is deliberately a bound rather than an answer wherever the inputs
make it one: an observed-maximum limit makes the recommended size an upper
bound, refuted queueing assumptions make it a lower bound, and both are stated
as findings on the recommendation rather than left for the reader to work out.
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
from ingest.store import MetricSeries, ObservationStore, SeriesKey, canonical_entity
from models.forecast import ForecastError, ForecastPoint, ForecastResult, forecast_series
from models.queueing import (
    QueueingError,
    utilization_for_elasticity,
    utilization_for_mean_response,
)
from models.saturation import (
    DEFAULT_ELASTICITY_TARGET,
    DEFAULT_EXCEEDANCE_THRESHOLD,
    DEFAULT_PLANNING_PERCENTILE,
    CapacityLimit,
    Provenance,
    SaturationError,
    SaturationResult,
    analyse_saturation,
)
from models.seasonality import Finding

_LOG = logging.getLogger(__name__)

#: How a fleet's units relate to each other.
#:
#: ``shared``
#:     The units serve one queue: a load balancer in front of interchangeable
#:     workers, a connection pool, a thread pool. Adding a unit raises the
#:     utilization the fleet can sustain at a fixed delay, because a request
#:     arriving at a busy unit can be served by an idle one.
#: ``sharded``
#:     Each unit has its own queue and its own share of the keyspace: a hashed
#:     partition, a per-tenant instance, a stateful node. An idle unit cannot
#:     help a busy one, so there is no pooling gain and capacity is linear in
#:     the unit count.
Pooling = Literal["shared", "sharded"]

#: What the recommendation asks for.
Action = Literal[
    "none",
    "scale-out",
    "raise-quota",
    "reduce-demand",
    "investigate",
    "refused",
]

#: Largest fleet the sizing search will consider before giving up. A search
#: that runs past this is not sizing, it is diverging, and the right output is
#: a refusal rather than a number with four digits in it.
MAX_SIZING_SEARCH: Final[int] = 100_000

#: Quantisation overshoot worth reporting, as a fraction above the requirement.
#: Below this the rounding is noise next to the forecast interval.
OVERSHOOT_WORTH_REPORTING: Final[float] = 0.10

#: Ratio of upper-bound run-out to central run-out below which the decision is
#: reported as interval-driven rather than growth-driven. At 0.5 the upper
#: bound arrives in half the time the central line would take, which means
#: uncertainty, not demand, is what is buying the hardware.
INTERVAL_DRIVEN_RATIO: Final[float] = 0.5

#: Utilization above which every intervention looks dramatic, because
#: elasticity ``rho/(1-rho)`` is steep there. Reported so a reviewer knows the
#: improvement is arithmetic before it is engineering.
STEEP_REGION_UTILIZATION: Final[float] = 0.85

#: Every finding this module can emit. ``F70x`` concern the inputs and the
#: ceiling's provenance, ``F71x`` the timing, ``F72x`` the size.
FINDING_CODES: Final[dict[str, str]] = {
    "F700": "the saturation analysis produced no ceiling to plan against",
    "F701": "the forecast produced no projection to plan with",
    "F702": "peak-to-mean held fixed while the level moves",
    "F703": "the ceiling rests on an observed maximum, not a limit",
    "F704": "the binding limit is a quota, so the fix is a request, not a deploy",
    "F705": "the operating point already sits at or above the ceiling",
    "F706": "the queueing assumptions behind the ceiling are refuted",
    "F707": "the forecast interval is understated, so run-out is earlier",
    "F710": "run-out does not occur within the forecast horizon",
    "F711": "the central line never reaches the ceiling: the decision is interval-driven",
    "F712": "the decide-by date has already passed",
    "F713": "the planning margin between the upper and central crossings",
    "F720": "no change is needed within the horizon",
    "F721": "rounding to whole units forces headroom past the requirement",
    "F722": "the pooling gain reduces the required size below the linear reading",
    "F723": "the fleet is sharded, so no pooling gain was claimed",
    "F724": "the required size exceeds the declared maximum",
    "F725": "the sizing search did not converge",
    "F726": "the fleet size disagrees with the server count the analysis used",
}


class RecommendationError(ValueError):
    """Raised when a recommendation cannot be produced as asked."""


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Fleet:
    """What is deployed now, and what one more of it would mean.

    ``pooling`` is the property that decides whether scaling out is cheaper
    than linear, and it is declared rather than inferred because no utilization
    metric distinguishes a load-balanced pool from a hashed shard set.
    """

    units: int
    unit_name: str = "unit"
    pooling: Pooling = "shared"
    min_units: int = 1
    max_units: int | None = None
    monthly_cost_per_unit: float | None = None

    def __post_init__(self) -> None:
        if self.units < 1:
            raise RecommendationError(f"fleet must hold at least one unit, got {self.units}")
        if self.min_units < 1:
            raise RecommendationError(f"minimum fleet size must be positive, got {self.min_units}")
        if self.max_units is not None and self.max_units < self.units:
            raise RecommendationError(
                f"maximum fleet size {self.max_units} is below the current size {self.units}"
            )
        if self.monthly_cost_per_unit is not None and self.monthly_cost_per_unit < 0.0:
            raise RecommendationError(
                f"unit cost cannot be negative, got {self.monthly_cost_per_unit}"
            )

    @property
    def monthly_cost(self) -> float | None:
        """Current monthly cost, or ``None`` when no unit price was supplied."""
        if self.monthly_cost_per_unit is None:
            return None
        return self.monthly_cost_per_unit * self.units

    def cost_of(self, units: int) -> float | None:
        """Monthly cost of a hypothetical fleet of ``units``."""
        if self.monthly_cost_per_unit is None:
            return None
        return self.monthly_cost_per_unit * units

    def label(self) -> str:
        plural = self.unit_name if self.units == 1 else f"{self.unit_name}s"
        return f"{self.units} {plural} ({self.pooling})"


def demand_reference(series: MetricSeries) -> float:
    """The demand level the saturation operating point was measured against.

    The mean of the analysed window. A forecast value divided by this is a
    demand multiplier on the same basis the saturation ceiling uses, which is
    the only form in which the two can be compared.
    """
    values = series.values
    if not values:
        raise RecommendationError("cannot take a demand reference from an empty series")
    total = math.fsum(values)
    if total <= 0.0:
        raise RecommendationError(
            "the analysed window has a non-positive mean, so demand cannot be "
            "expressed as a multiplier of it"
        )
    return total / len(values)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunOut:
    """When demand reaches the ceiling, read two ways."""

    ceiling_multiplier: float
    reference_level: float
    horizon_end: datetime
    upper_crossing: datetime | None
    central_crossing: datetime | None
    upper_multiplier_at_horizon: float
    central_multiplier_at_horizon: float
    already_past: bool

    @property
    def margin(self) -> timedelta | None:
        """How much earlier the upper bound runs out than the central line.

        This is the planning margin the interval buys. It is ``None`` when
        either crossing lies beyond the horizon, because a margin measured
        against a date that was never computed is not a margin.
        """
        if self.upper_crossing is None or self.central_crossing is None:
            return None
        return self.central_crossing - self.upper_crossing

    def time_remaining(self, now: datetime) -> timedelta | None:
        """Time from ``now`` until the upper bound reaches the ceiling."""
        if self.upper_crossing is None:
            return None
        return self.upper_crossing - now


@dataclass(frozen=True, slots=True)
class Sizing:
    """The smallest fleet that covers the planning target, and what it costs."""

    units_now: int
    units_required: int
    units_required_linear: int
    target_multiplier: float
    absorbable_now: float
    absorbable_after: float
    overshoot_factor: float
    utilization_now: float
    utilization_after: float
    pooling: Pooling
    capped_by_max: bool
    converged: bool
    monthly_cost_now: float | None
    monthly_cost_after: float | None

    @property
    def units_added(self) -> int:
        return self.units_required - self.units_now

    @property
    def pooling_units_saved(self) -> int:
        """Units the pooling gain removed from the linear answer."""
        return max(0, self.units_required_linear - self.units_required)

    @property
    def monthly_cost_delta(self) -> float | None:
        if self.monthly_cost_now is None or self.monthly_cost_after is None:
            return None
        return self.monthly_cost_after - self.monthly_cost_now


@dataclass(frozen=True, slots=True)
class Recommendation:
    """A sizing decision, or an explicit refusal to make one."""

    series: str
    action: Action
    refused: bool
    fleet: Fleet
    limit_provenance: Provenance | None
    run_out: RunOut | None
    sizing: Sizing | None
    lead_time: timedelta
    decide_by: datetime | None
    now: datetime
    findings: tuple[Finding, ...]

    @property
    def errors(self) -> tuple[Finding, ...]:
        return tuple(finding for finding in self.findings if finding.severity == "error")

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(finding for finding in self.findings if finding.severity == "warning")

    @property
    def late(self) -> bool:
        """True when the decide-by date is already behind us."""
        return self.decide_by is not None and self.decide_by <= self.now

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready form, for a report generator or a pipeline step."""
        return {
            "series": self.series,
            "action": self.action,
            "refused": self.refused,
            "generated_at": self.now.isoformat(),
            "fleet": {
                "units": self.fleet.units,
                "unit_name": self.fleet.unit_name,
                "pooling": self.fleet.pooling,
                "max_units": self.fleet.max_units,
                "monthly_cost": self.fleet.monthly_cost,
            },
            "limit_provenance": self.limit_provenance,
            "lead_time_seconds": int(self.lead_time.total_seconds()),
            "decide_by": None if self.decide_by is None else self.decide_by.isoformat(),
            "late": self.late,
            "run_out": (
                None
                if self.run_out is None
                else {
                    "ceiling_multiplier": self.run_out.ceiling_multiplier,
                    "reference_level": self.run_out.reference_level,
                    "horizon_end": self.run_out.horizon_end.isoformat(),
                    "upper_crossing": (
                        None
                        if self.run_out.upper_crossing is None
                        else self.run_out.upper_crossing.isoformat()
                    ),
                    "central_crossing": (
                        None
                        if self.run_out.central_crossing is None
                        else self.run_out.central_crossing.isoformat()
                    ),
                    "margin_seconds": (
                        None
                        if self.run_out.margin is None
                        else int(self.run_out.margin.total_seconds())
                    ),
                    "upper_multiplier_at_horizon": self.run_out.upper_multiplier_at_horizon,
                    "central_multiplier_at_horizon": self.run_out.central_multiplier_at_horizon,
                    "already_past": self.run_out.already_past,
                }
            ),
            "sizing": (
                None
                if self.sizing is None
                else {
                    "units_now": self.sizing.units_now,
                    "units_required": self.sizing.units_required,
                    "units_added": self.sizing.units_added,
                    "units_required_linear": self.sizing.units_required_linear,
                    "pooling_units_saved": self.sizing.pooling_units_saved,
                    "target_multiplier": self.sizing.target_multiplier,
                    "absorbable_now": self.sizing.absorbable_now,
                    "absorbable_after": self.sizing.absorbable_after,
                    "overshoot_factor": self.sizing.overshoot_factor,
                    "utilization_now": self.sizing.utilization_now,
                    "utilization_after": self.sizing.utilization_after,
                    "capped_by_max": self.sizing.capped_by_max,
                    "converged": self.sizing.converged,
                    "monthly_cost_now": self.sizing.monthly_cost_now,
                    "monthly_cost_after": self.sizing.monthly_cost_after,
                    "monthly_cost_delta": self.sizing.monthly_cost_delta,
                }
            ),
            "findings": [
                {"code": f.code, "severity": f.severity, "message": f.message}
                for f in self.findings
            ],
        }


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------


def _crossing(
    points: Sequence[ForecastPoint],
    reference_level: float,
    ceiling: float,
    *,
    upper: bool,
) -> datetime | None:
    """The first instant a projected track reaches ``ceiling``, interpolated.

    The first crossing, not a sustained one. A seasonal projection oscillates,
    so the upper bound may touch the ceiling at one afternoon peak and fall
    back overnight; for a resource whose SLO is about its peak, that afternoon
    is the run-out and averaging it away would hide the event the plan exists
    to prevent.

    Linear interpolation between the bracketing projections places the crossing
    inside a sampling period rather than at its boundary. That is more precise
    than the projection deserves in absolute terms, but it keeps the run-out
    from quantising to the forecast's step size, which otherwise makes a
    lead-time comparison lurch by a whole period at a time.
    """
    previous: tuple[datetime, float] | None = None
    for point in points:
        level = point.upper_one_sided if upper else point.value
        multiplier = level / reference_level
        if multiplier >= ceiling:
            if previous is None:
                return point.timestamp
            before_at, before_multiplier = previous
            span = multiplier - before_multiplier
            if span <= 0.0:
                return point.timestamp
            fraction = (ceiling - before_multiplier) / span
            fraction = min(1.0, max(0.0, fraction))
            return before_at + (point.timestamp - before_at) * fraction
        previous = (point.timestamp, multiplier)
    return None


def _run_out(
    forecast: ForecastResult,
    reference_level: float,
    ceiling: float,
    *,
    already_past: bool,
) -> RunOut:
    """Both crossings and the horizon the projection actually covers."""
    points = forecast.points
    last = points[-1]
    return RunOut(
        ceiling_multiplier=ceiling,
        reference_level=reference_level,
        horizon_end=last.timestamp,
        upper_crossing=_crossing(points, reference_level, ceiling, upper=True),
        central_crossing=_crossing(points, reference_level, ceiling, upper=False),
        upper_multiplier_at_horizon=last.upper_one_sided / reference_level,
        central_multiplier_at_horizon=last.value / reference_level,
        already_past=already_past,
    )


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------


def binding_ceiling(
    servers: int,
    *,
    cv_squared: float,
    elasticity_target: float,
    service_time: float | None,
    response_budget: float | None,
) -> float | None:
    """The binding utilization ceiling for a queue of ``servers``.

    The elasticity ceiling always exists; the response-time ceiling exists only
    when a budget and a mean service time were both supplied. The binding one
    is the smaller, exactly as the saturation module computes it, so that a
    sizing search evaluated at the current fleet size reproduces the ceiling
    the analysis already reported instead of a near-miss of it.
    """
    try:
        elasticity = utilization_for_elasticity(
            elasticity_target, servers=servers, service_time=1.0, cv_squared=cv_squared
        )
    except QueueingError:
        return None
    if elasticity is None:
        return None
    candidates: list[float] = [elasticity]
    if response_budget is not None and service_time is not None:
        try:
            response = utilization_for_mean_response(
                response_budget,
                servers=servers,
                service_time=service_time,
                cv_squared=cv_squared,
            )
        except QueueingError:
            response = None
        if response is not None:
            candidates.append(response)
    return min(candidates)


def _smallest_fleet(
    *,
    units_now: int,
    utilization_now: float,
    target_multiplier: float,
    pooling: Pooling,
    cv_squared: float,
    elasticity_target: float,
    service_time: float | None,
    response_budget: float | None,
    max_units: int | None,
) -> tuple[int, bool, bool]:
    """Smallest unit count whose ceiling covers the target demand.

    Returns the size, whether the search converged, and whether the declared
    maximum truncated it.

    Demand is assumed to spread evenly across the fleet, so ``n`` units at a
    demand multiplier ``M`` each run at ``utilization_now * M * units_now / n``.
    What differs between the two pooling models is the ceiling that utilization
    is judged against: a shared pool is one queue of ``n`` servers and its
    sustainable utilization rises with ``n``, while a sharded fleet is ``n``
    independent single-server queues whose ceiling never moves.
    """
    ceiling_servers = 1 if pooling == "sharded" else None
    offered = utilization_now * target_multiplier * units_now
    limit = max_units if max_units is not None else MAX_SIZING_SEARCH
    limit = min(limit, MAX_SIZING_SEARCH)

    units = units_now
    while units <= limit:
        ceiling = binding_ceiling(
            ceiling_servers if ceiling_servers is not None else units,
            cv_squared=cv_squared,
            elasticity_target=elasticity_target,
            service_time=service_time,
            response_budget=response_budget,
        )
        if ceiling is None:
            return units_now, False, False
        if offered / units <= ceiling:
            return units, True, False
        units += 1

    capped = max_units is not None and max_units <= MAX_SIZING_SEARCH
    return limit, False, capped


def _absorbable(
    units: int,
    *,
    units_now: int,
    utilization_now: float,
    pooling: Pooling,
    cv_squared: float,
    elasticity_target: float,
    service_time: float | None,
    response_budget: float | None,
) -> tuple[float, float]:
    """Demand multiplier a fleet of ``units`` absorbs, and its utilization now.

    The utilization returned is what today's demand would produce on that
    fleet, which is the number a reviewer checks the recommendation against:
    scaling out to a utilization of four percent is a different conversation
    from scaling out to sixty.
    """
    utilization_after = utilization_now * units_now / units
    ceiling = binding_ceiling(
        1 if pooling == "sharded" else units,
        cv_squared=cv_squared,
        elasticity_target=elasticity_target,
        service_time=service_time,
        response_budget=response_budget,
    )
    if ceiling is None or utilization_after <= 0.0:
        return math.inf, utilization_after
    return ceiling / utilization_after, utilization_after


# ---------------------------------------------------------------------------
# The recommendation
# ---------------------------------------------------------------------------


def _refused(
    *,
    series: str,
    fleet: Fleet,
    now: datetime,
    lead_time: timedelta,
    provenance: Provenance | None,
    findings: list[Finding],
) -> Recommendation:
    """A recommendation that declines to recommend, carrying the reason."""
    return Recommendation(
        series=series,
        action="refused",
        refused=True,
        fleet=fleet,
        limit_provenance=provenance,
        run_out=None,
        sizing=None,
        lead_time=lead_time,
        decide_by=None,
        now=now,
        findings=tuple(findings),
    )


def recommend_headroom(
    *,
    forecast: ForecastResult,
    saturation: SaturationResult,
    reference_level: float,
    fleet: Fleet,
    lead_time: timedelta = timedelta(0),
    service_time: float | None = None,
    service_cv_squared: float | None = None,
    response_budget: float | None = None,
    now: datetime | None = None,
    carry_model_findings: bool = True,
) -> Recommendation:
    """Size a fleet against a forecast and a saturation ceiling.

    ``reference_level`` is the demand level the saturation operating point was
    measured at -- :func:`demand_reference` of the same series -- and it is
    required rather than derived, because a recommendation built from a
    forecast of one series and a ceiling measured on another is a unit error
    that no amount of internal consistency will catch.

    ``service_time``, ``service_cv_squared`` and ``response_budget`` should be
    the values handed to :func:`models.saturation.analyse_saturation`. The
    variability defaults to whatever the analysis used; the service time does
    not, because the analysis substitutes a placeholder of one second when it
    has none and that placeholder must not silently become an input to a
    response-time ceiling here.

    Model findings are carried forward unchanged by default. A sizing decision
    inherits every objection recorded by the analysis it rests on, and a report
    that shows the recommendation without them is more confident than the
    evidence.
    """
    if reference_level <= 0.0:
        raise RecommendationError(
            f"demand reference must be positive, got {reference_level}"
        )
    if lead_time < timedelta(0):
        raise RecommendationError(f"lead time cannot be negative, got {lead_time}")

    moment = now or datetime.now(tz=timezone.utc)
    series_label = saturation.key.label()
    provenance = None if saturation.limit is None else saturation.limit.provenance

    findings: list[Finding] = []
    if carry_model_findings:
        findings.extend(saturation.findings)
        findings.extend(forecast.findings)

    headroom = saturation.headroom
    if saturation.refused or headroom is None or headroom.demand_multiplier is None:
        findings.append(
            Finding(
                "F700",
                "error",
                "the saturation analysis produced no absorbable demand multiplier, so "
                "there is no ceiling to plan against: resolve its findings before "
                "asking what size the fleet should be",
            )
        )
        return _refused(
            series=series_label,
            fleet=fleet,
            now=moment,
            lead_time=lead_time,
            provenance=provenance,
            findings=findings,
        )

    if forecast.refused or not forecast.points:
        findings.append(
            Finding(
                "F701",
                "error",
                "the forecast produced no projection, so demand cannot be compared "
                f"against the {headroom.demand_multiplier:.2f}x the current fleet "
                "absorbs: the ceiling is known and the growth is not",
            )
        )
        return _refused(
            series=series_label,
            fleet=fleet,
            now=moment,
            lead_time=lead_time,
            provenance=provenance,
            findings=findings,
        )

    queue = saturation.queue
    cv_squared = (
        service_cv_squared
        if service_cv_squared is not None
        else (queue.cv_squared if queue is not None else 1.0)
    )
    elasticity_target = headroom.elasticity_target
    utilization_now = headroom.utilization
    absorbable_now = headroom.demand_multiplier

    expected_servers = 1 if fleet.pooling == "sharded" else fleet.units
    if queue is not None and queue.servers != expected_servers:
        findings.append(
            Finding(
                "F726",
                "warning",
                f"the saturation analysis modelled {queue.servers} server(s) but the "
                f"fleet declares {fleet.units} {fleet.pooling} unit(s), which implies "
                f"{expected_servers}: the ceiling and the sizing search disagree about "
                "what one unit is, and the recommended size inherits that disagreement",
            )
        )

    profile = saturation.profile
    peak_to_mean = None if profile is None else profile.peak_to_mean
    findings.append(
        Finding(
            "F702",
            "note",
            "the ceiling is a multiplier on the operating point and the forecast "
            "projects the series statistic, so comparing them holds the peak-to-mean "
            f"ratio{'' if peak_to_mean is None else f' of {peak_to_mean:.2f}'} fixed "
            "as the level moves: independent demand superposes and smooths, so for "
            "aggregate growth this over-provisions slightly, but growth concentrated "
            "in one large consumer gets no smoothing and the assumption is then neutral",
        )
    )

    if saturation.limit is not None and not saturation.limit.is_a_real_limit:
        findings.append(
            Finding(
                "F703",
                "warning",
                "the ceiling is computed against an observed maximum, which bounds the "
                "real limit from below: measured utilization is therefore an upper "
                "bound and true headroom is at least what is reported, so a conclusion "
                "of no change is safe while a conclusion to scale out may not be needed",
            )
        )
    if provenance == "quota":
        findings.append(
            Finding(
                "F704",
                "note",
                "the binding limit is a service quota, so the remedy is a quota "
                "increase rather than a deployment, and the lead time to plan against "
                "is the provider's approval time, not the provisioning time",
            )
        )
    if headroom.past_ceiling:
        findings.append(
            Finding(
                "F705",
                "error",
                f"the operating point of {utilization_now:.1%} already sits at or above "
                "the ceiling, so the run-out date is in the past and the sizing below "
                "describes a fleet that should already exist",
            )
        )

    applicability = saturation.applicability
    if applicability is not None and not applicability.assumptions_hold:
        findings.append(
            Finding(
                "F706",
                "warning",
                "the queueing assumptions behind the ceiling are refuted by the series "
                "itself, and every such departure fails toward optimism: the absorbable "
                f"{absorbable_now:.2f}x is an upper bound, so the recommended size is a "
                "lower bound on what the fleet actually needs",
            )
        )

    diagnostics = forecast.diagnostics
    if diagnostics is not None:
        understatement = diagnostics.sigma_understatement
        if understatement > 1.05:
            findings.append(
                Finding(
                    "F707",
                    "warning",
                    f"the forecast interval is understated by about {understatement:.2f}x, "
                    "so the true upper bound reaches the ceiling earlier than the "
                    "run-out date below: treat that date as the optimistic end of a "
                    "range rather than as the date",
                )
            )

    run_out = _run_out(
        forecast,
        reference_level,
        absorbable_now,
        already_past=headroom.past_ceiling,
    )

    decide_by: datetime | None = None
    if run_out.upper_crossing is not None:
        decide_by = run_out.upper_crossing - lead_time
        if decide_by <= moment:
            overdue = moment - decide_by
            findings.append(
                Finding(
                    "F712",
                    "error",
                    f"the decide-by date passed {format_duration(overdue)} ago: with a lead "
                    f"time of {format_duration(lead_time)} and run-out at "
                    f"{run_out.upper_crossing.isoformat()}, ordering capacity today "
                    "arrives after the ceiling is reached",
                )
            )
    else:
        findings.append(
            Finding(
                "F710",
                "note",
                "the upper bound does not reach the ceiling within the forecast "
                f"horizon ending {run_out.horizon_end.isoformat()}: the statement "
                "available is that run-out is not within the horizon, which is not "
                "the same as there being capacity beyond it",
            )
        )

    if run_out.upper_crossing is not None and run_out.central_crossing is None:
        findings.append(
            Finding(
                "F711",
                "warning",
                "the central projection never reaches the ceiling within the horizon "
                "but its upper bound does, so this recommendation is driven entirely "
                "by forecast uncertainty: narrowing the interval, by collecting more "
                "history or a finer series, may remove the need to buy anything",
            )
        )
    elif run_out.upper_crossing is not None and run_out.central_crossing is not None:
        margin = run_out.margin
        upper_remaining = (run_out.upper_crossing - moment).total_seconds()
        central_remaining = (run_out.central_crossing - moment).total_seconds()
        ratio = (
            upper_remaining / central_remaining if central_remaining > 0.0 else 1.0
        )
        if ratio < INTERVAL_DRIVEN_RATIO:
            findings.append(
                Finding(
                    "F711",
                    "warning",
                    f"the upper bound runs out in {ratio:.0%} of the time the central "
                    "projection takes, so most of the urgency is interval width rather "
                    "than measured growth",
                )
            )
        elif margin is not None:
            findings.append(
                Finding(
                    "F713",
                    "note",
                    f"planning to the upper bound rather than the central projection "
                    f"brings the run-out forward by {format_duration(margin)}, which is the "
                    "margin the interval buys",
                )
            )

    target_multiplier = max(1.0, run_out.upper_multiplier_at_horizon)
    sizing = _build_sizing(
        fleet=fleet,
        utilization_now=utilization_now,
        absorbable_now=absorbable_now,
        target_multiplier=target_multiplier,
        cv_squared=cv_squared,
        elasticity_target=elasticity_target,
        service_time=service_time,
        response_budget=response_budget,
        findings=findings,
        past_ceiling=headroom.past_ceiling,
    )

    action = _choose_action(
        sizing=sizing,
        provenance=provenance,
        past_ceiling=headroom.past_ceiling,
    )

    return Recommendation(
        series=series_label,
        action=action,
        refused=False,
        fleet=fleet,
        limit_provenance=provenance,
        run_out=run_out,
        sizing=sizing,
        lead_time=lead_time,
        decide_by=decide_by,
        now=moment,
        findings=tuple(findings),
    )


def format_duration(span: timedelta) -> str:
    """A duration in one unit, chosen for readability.

    Deliberately never compounded. A single unit with one decimal place is
    easier to compare across two recommendations than a mixed expression, and
    the precision beyond that is spurious next to a forecast interval.
    """
    seconds = abs(span.total_seconds())
    if seconds >= 86_400:
        return f"{seconds / 86_400:.1f} days"
    if seconds >= 3_600:
        return f"{seconds / 3_600:.1f} hours"
    if seconds >= 60:
        return f"{seconds / 60:.0f} minutes"
    return f"{seconds:.0f} seconds"


def _build_sizing(
    *,
    fleet: Fleet,
    utilization_now: float,
    absorbable_now: float,
    target_multiplier: float,
    cv_squared: float,
    elasticity_target: float,
    service_time: float | None,
    response_budget: float | None,
    findings: list[Finding],
    past_ceiling: bool,
) -> Sizing:
    """Find the smallest fleet that covers the target and explain the number."""
    units_now = fleet.units

    linear = units_now
    if absorbable_now > 0.0:
        linear = math.ceil(units_now * target_multiplier / absorbable_now)
    linear = max(units_now, min(linear, MAX_SIZING_SEARCH))

    required = units_now
    converged = True
    capped = False
    if target_multiplier > absorbable_now or past_ceiling:
        required, converged, capped = _smallest_fleet(
            units_now=units_now,
            utilization_now=utilization_now,
            target_multiplier=target_multiplier,
            pooling=fleet.pooling,
            cv_squared=cv_squared,
            elasticity_target=elasticity_target,
            service_time=service_time,
            response_budget=response_budget,
            max_units=fleet.max_units,
        )
    else:
        findings.append(
            Finding(
                "F720",
                "note",
                f"the fleet absorbs {absorbable_now:.2f}x and the upper bound reaches "
                f"{target_multiplier:.2f}x at the horizon, so no change is needed "
                "within it: this is a statement about the horizon, and it expires "
                "with the forecast that produced it",
            )
        )

    absorbable_after, utilization_after = _absorbable(
        required,
        units_now=units_now,
        utilization_now=utilization_now,
        pooling=fleet.pooling,
        cv_squared=cv_squared,
        elasticity_target=elasticity_target,
        service_time=service_time,
        response_budget=response_budget,
    )
    overshoot = (
        absorbable_after / target_multiplier if target_multiplier > 0.0 else math.inf
    )

    added = required - units_now
    if added > 0 and math.isfinite(overshoot) and overshoot - 1.0 > OVERSHOOT_WORTH_REPORTING:
        findings.append(
            Finding(
                "F721",
                "note",
                f"whole units force the fleet past the requirement: {required} "
                f"{fleet.unit_name}(s) absorb {absorbable_after:.2f}x where "
                f"{target_multiplier:.2f}x was needed, an overshoot of "
                f"{(overshoot - 1.0):.0%} that is granularity rather than demand",
            )
        )

    if added > 0 and fleet.pooling == "shared" and linear > required:
        findings.append(
            Finding(
                "F722",
                "note",
                f"pooling removes {linear - required} {fleet.unit_name}(s) from the "
                f"linear answer of {linear}: a shared queue of {required} servers "
                "sustains a higher utilization at the same elasticity than one of "
                f"{units_now} does, and that gain is real only because the fleet is "
                "declared shared",
            )
        )
    if added > 0 and fleet.pooling == "sharded":
        findings.append(
            Finding(
                "F723",
                "note",
                "the fleet is declared sharded, so an idle unit cannot absorb a busy "
                "unit's work and no pooling gain was claimed: capacity here is linear "
                "in the unit count, and the way to make it cheaper is to share the "
                "queue, not to add units",
            )
        )
    if capped:
        findings.append(
            Finding(
                "F724",
                "error",
                f"the requirement exceeds the declared maximum of {fleet.max_units} "
                f"{fleet.unit_name}(s), so supply cannot meet the projected demand: "
                "the remaining levers are reducing demand, raising the maximum, or "
                "accepting the elasticity above target",
            )
        )
    if not converged and not capped:
        findings.append(
            Finding(
                "F725",
                "error",
                f"the sizing search reached {MAX_SIZING_SEARCH} {fleet.unit_name}(s) "
                "without covering the target, which means the ceiling arithmetic is "
                "not converging rather than that the fleet should be that large",
            )
        )

    return Sizing(
        units_now=units_now,
        units_required=required,
        units_required_linear=linear,
        target_multiplier=target_multiplier,
        absorbable_now=absorbable_now,
        absorbable_after=absorbable_after,
        overshoot_factor=overshoot,
        utilization_now=utilization_now,
        utilization_after=utilization_after,
        pooling=fleet.pooling,
        capped_by_max=capped,
        converged=converged,
        monthly_cost_now=fleet.monthly_cost,
        monthly_cost_after=fleet.cost_of(required),
    )


def _choose_action(
    *,
    sizing: Sizing,
    provenance: Provenance | None,
    past_ceiling: bool,
) -> Action:
    """Which lever the recommendation asks for.

    An observed-maximum limit sends a scale-out to investigation rather than to
    procurement, because a denominator nobody documented is the thing to fix
    first and the headroom computed against it is only a bound. The same limit
    leaves a no-change conclusion alone: that conclusion is conservative
    whichever way the real limit moves.
    """
    if not sizing.converged and not sizing.capped_by_max:
        return "investigate"
    if sizing.capped_by_max:
        return "reduce-demand"
    if sizing.units_added <= 0 and not past_ceiling:
        return "none"
    if provenance == "observed":
        return "investigate"
    if provenance == "quota":
        return "raise-quota"
    return "scale-out"


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


#: Headline wording per action, shared with the report renderer so the review
#: and the terminal output cannot drift apart.
ACTION_HEADLINE: Final[dict[str, str]] = {
    "none": "NO CHANGE within the horizon",
    "scale-out": "SCALE OUT",
    "raise-quota": "RAISE THE QUOTA",
    "reduce-demand": "REDUCE DEMAND -- supply cannot meet it",
    "investigate": "INVESTIGATE before provisioning",
    "refused": "REFUSED -- the inputs do not support a recommendation",
}


def _render(recommendation: Recommendation) -> str:
    """A terminal rendering that puts the decision first and the caveats after."""
    lines: list[str] = [
        f"series: {recommendation.series}",
        f"fleet:  {recommendation.fleet.label()}",
        "",
        ACTION_HEADLINE.get(recommendation.action, recommendation.action),
    ]

    run_out = recommendation.run_out
    if run_out is not None:
        lines.append("")
        lines.append(f"  absorbable demand     {run_out.ceiling_multiplier:.2f}x")
        lines.append(
            f"  projected at horizon  {run_out.upper_multiplier_at_horizon:.2f}x upper, "
            f"{run_out.central_multiplier_at_horizon:.2f}x central"
        )
        if run_out.upper_crossing is None:
            lines.append(
                f"  run-out               none within the horizon ending "
                f"{run_out.horizon_end.isoformat()}"
            )
        else:
            remaining = run_out.time_remaining(recommendation.now)
            suffix = "" if remaining is None else f"  (in {format_duration(remaining)})"
            lines.append(
                f"  run-out (upper)       {run_out.upper_crossing.isoformat()}{suffix}"
            )
            if run_out.central_crossing is not None:
                lines.append(
                    f"  run-out (central)     {run_out.central_crossing.isoformat()}"
                )
        if recommendation.decide_by is not None:
            late = " -- ALREADY PASSED" if recommendation.late else ""
            lines.append(
                f"  decide by             {recommendation.decide_by.isoformat()}"
                f"  (lead time {format_duration(recommendation.lead_time)}){late}"
            )

    sizing = recommendation.sizing
    if sizing is not None:
        lines.append("")
        lines.append(
            f"  size                  {sizing.units_now} -> {sizing.units_required} "
            f"{recommendation.fleet.unit_name}(s)"
        )
        if sizing.units_required_linear != sizing.units_required:
            lines.append(
                f"  linear reading        {sizing.units_required_linear} "
                f"{recommendation.fleet.unit_name}(s)"
            )
        lines.append(
            f"  utilization           {sizing.utilization_now:.1%} -> "
            f"{sizing.utilization_after:.1%} at today's demand"
        )
        lines.append(
            f"  absorbs after         {sizing.absorbable_after:.2f}x "
            f"(target {sizing.target_multiplier:.2f}x)"
        )
        delta = sizing.monthly_cost_delta
        if delta is not None:
            lines.append(f"  monthly cost delta    {delta:+,.2f}")

    if recommendation.findings:
        lines.append("")
        lines.append("findings:")
        order = {"error": 0, "warning": 1, "note": 2}
        for finding in sorted(
            recommendation.findings, key=lambda f: (order.get(f.severity, 3), f.code)
        ):
            lines.append(f"  {finding}")
    return "\n".join(lines)


def _parse_dimensions(pairs: Sequence[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise RecommendationError(f"dimension must be key=value, got {pair!r}")
        key, value = pair.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="recommend.headroom",
        description=(
            "Forecast a series, measure its saturation, and report the fleet size "
            "and decide-by date that follow."
        ),
    )
    add_common_arguments(parser)

    series = parser.add_argument_group("series")
    series.add_argument("--source", default="cloudwatch", help="observation source")
    series.add_argument("--namespace", required=True, help="metric namespace")
    series.add_argument("--metric", required=True, help="metric name")
    series.add_argument("--statistic", default="Average", help="statistic to analyse")
    series.add_argument(
        "--dimension",
        action="append",
        default=[],
        help="dimension as key=value; repeatable",
    )
    series.add_argument(
        "--lookback-hours",
        type=int,
        default=None,
        help="restrict the analysed window to the most recent N hours",
    )

    ceiling = parser.add_argument_group("capacity limit")
    ceiling.add_argument("--limit", type=float, required=True, help="the limit value")
    ceiling.add_argument("--limit-unit", default="", help="unit of the limit")
    ceiling.add_argument(
        "--limit-provenance",
        required=True,
        choices=["hard", "quota", "configured", "observed"],
        help="where the limit came from; an observed maximum is not a limit",
    )
    ceiling.add_argument("--limit-note", default="", help="free-text provenance detail")

    model = parser.add_argument_group("model parameters")
    model.add_argument(
        "--planning-percentile",
        type=float,
        default=DEFAULT_PLANNING_PERCENTILE,
        help="operating percentile of the utilization distribution",
    )
    model.add_argument(
        "--exceedance-threshold",
        type=float,
        default=DEFAULT_EXCEEDANCE_THRESHOLD,
        help="utilization above which a point counts toward an exceedance run",
    )
    model.add_argument(
        "--elasticity-target",
        type=float,
        default=DEFAULT_ELASTICITY_TARGET,
        help="percent of latency a percent of demand growth may cost",
    )
    model.add_argument(
        "--service-time", type=float, default=None, help="mean service time in seconds"
    )
    model.add_argument(
        "--service-cv-squared",
        type=float,
        default=None,
        help="squared coefficient of variation of the service time",
    )
    model.add_argument(
        "--response-budget",
        type=float,
        default=None,
        help="mean response-time budget in seconds",
    )
    model.add_argument(
        "--horizon", type=int, default=168, help="forecast horizon in sampling periods"
    )
    model.add_argument(
        "--level", type=float, default=0.9, help="forecast interval level"
    )
    model.add_argument(
        "--timezone", default="UTC", help="timezone for the seasonal profile"
    )
    model.add_argument(
        "--allow-holes",
        action="store_true",
        help="analyse despite coverage gaps, recording them as findings",
    )

    fleet = parser.add_argument_group("fleet")
    fleet.add_argument("--units", type=int, required=True, help="units deployed now")
    fleet.add_argument("--unit-name", default="unit", help="what one unit is called")
    fleet.add_argument(
        "--pooling",
        choices=["shared", "sharded"],
        default="shared",
        help="whether the units serve one queue or one queue each",
    )
    fleet.add_argument(
        "--max-units", type=int, default=None, help="largest fleet that may be deployed"
    )
    fleet.add_argument(
        "--unit-monthly-cost",
        type=float,
        default=None,
        help="monthly cost of one unit, for the cost delta",
    )
    fleet.add_argument(
        "--lead-time-hours",
        type=float,
        default=0.0,
        help="hours between deciding and the capacity being in service",
    )

    parser.add_argument("--json", action="store_true", help="emit the result as JSON")
    return parser


def run_from_args(
    args: argparse.Namespace,
) -> tuple[MetricSeries, SaturationResult, ForecastResult, Recommendation]:
    """Run the whole pipeline for a parsed command line.

    Public so that a report generator can reuse the parser and the pipeline
    rather than reimplementing either: the recommendation a report renders has
    to be the one the command line produces, and the only way to guarantee
    that is for there to be one code path.

    Every failure is raised as a :class:`RecommendationError` or one of the
    model errors, so the caller decides what an exit code is.
    """
    dimensions = _parse_dimensions(args.dimension)
    limit = CapacityLimit(
        value=args.limit,
        unit=args.limit_unit,
        provenance=args.limit_provenance,
        note=args.limit_note,
    )
    fleet = Fleet(
        units=args.units,
        unit_name=args.unit_name,
        pooling=args.pooling,
        max_units=args.max_units,
        monthly_cost_per_unit=args.unit_monthly_cost,
    )
    if args.lead_time_hours < 0.0:
        raise RecommendationError("lead time cannot be negative")
    lead_time = timedelta(hours=args.lead_time_hours)

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
            raise RecommendationError("lookback must be a positive number of hours")
        start = datetime.now(tz=timezone.utc) - timedelta(hours=args.lookback_hours)

    try:
        with ObservationStore(args.store, read_only=True) as store:
            series = store.read_series(key, start=start)
    except Exception as exc:  # the store raises several unrelated types
        raise RecommendationError(
            f"cannot read {key.label()} from {args.store}: {exc}"
        ) from exc

    saturation = analyse_saturation(
        series,
        limit,
        planning_percentile=args.planning_percentile,
        exceedance_threshold=args.exceedance_threshold,
        servers=1 if args.pooling == "sharded" else args.units,
        service_time=args.service_time,
        service_cv_squared=args.service_cv_squared,
        elasticity_target=args.elasticity_target,
        response_budget=args.response_budget,
        allow_holes=args.allow_holes,
    )
    forecast = forecast_series(
        series,
        horizon=args.horizon,
        level=args.level,
        timezone_name=args.timezone,
        allow_holes=args.allow_holes,
    )
    recommendation = recommend_headroom(
        forecast=forecast,
        saturation=saturation,
        reference_level=demand_reference(series),
        fleet=fleet,
        lead_time=lead_time,
        service_time=args.service_time,
        service_cv_squared=args.service_cv_squared,
        response_budget=args.response_budget,
    )
    return series, saturation, forecast, recommendation


def main(argv: Sequence[str] | None = None) -> int:
    """Exit 0 on a recommendation, 1 on a refusal, 2 on a request that cannot run."""
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.log_level, args.log_format)

    try:
        _series, _saturation, _forecast, recommendation = run_from_args(args)
    except (RecommendationError, SaturationError, ForecastError, QueueingError) as exc:
        _LOG.error("%s", exc)
        return 2

    if args.json:
        print(json.dumps(recommendation.as_dict(), indent=2, sort_keys=True, default=str))
    else:
        print(_render(recommendation))
    return 1 if recommendation.refused else 0


if __name__ == "__main__":
    sys.exit(main())
