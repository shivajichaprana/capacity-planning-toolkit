"""What-if simulation: comparing interventions, and naming what each holds fixed.

A what-if answer is only as good as its invariants. "Doubling the fleet halves
utilization" is true exactly when demand, service time, variability and the
pooling model all stay where they were, and the interesting failures of
capacity planning are the ones where a scenario quietly moved one of them. So
every scenario here declares what it holds fixed, that declaration travels with
the outcome, and the outcome is invalid the moment one of the listed invariants
is not.

Three further properties of this arithmetic are worth stating before any
number is read off it.

**Extrapolation beyond observed support is a different kind of claim.** A
series whose largest observation is 1.4 times its mean has nothing to say about
behaviour at 4 times its mean. The scenario can still be evaluated -- the
queueing model is defined everywhere below saturation -- but it is then a
statement about a model rather than about a system, and it is marked as one.

**Near saturation, every intervention looks spectacular.** Elasticity is
``rho/(1-rho)``, so at 95% utilization a modest capacity addition produces an
enormous proportional improvement, and a comparison run from that baseline will
rank anything above nothing by a wide margin. That ranking is arithmetic before
it is engineering, and a comparison whose baseline sits in the steep region
says so, because the ordering it produces is much less informative than the
size of the numbers suggests.

**Scenarios are alternatives, not a plan.** Two interventions do not add. The
gain from adding capacity depends on the utilization it starts from, so
applying a second intervention after a first yields less than the first one
did, and a reader summing a column of improvements will overcount. Comparisons
therefore always include doing nothing, as the only honest baseline against
which an intervention can be judged, and report each outcome against that
baseline rather than against each other.

Finding codes are ``F8xx``, reserved in :mod:`recommend`.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Final, Protocol, runtime_checkable

from models.queueing import QueueModel, QueueingError
from models.saturation import SaturationResult
from models.seasonality import Finding
from recommend.headroom import (
    STEEP_REGION_UTILIZATION,
    Fleet,
    RecommendationError,
    binding_ceiling,
)

#: Demand beyond this multiple of the largest observed level is reported as
#: unsupported extrapolation even before the observed-support check, because a
#: queueing model fitted to one regime says little about a tenfold one.
HARD_EXTRAPOLATION_MULTIPLE: Final[float] = 10.0

#: Every finding this module can emit. ``F80x`` are about the comparison as a
#: whole, ``F81x`` about one scenario's mechanics.
FINDING_CODES: Final[dict[str, str]] = {
    "F800": "the scenario leaves the system unstable: no finite queue exists",
    "F801": "the scenario assumes demand the observations never reached",
    "F802": "the baseline sits in the steep region, so every option looks good",
    "F803": "outcomes are alternatives and do not add",
    "F804": "no scenario reaches the target headroom",
    "F810": "the fleet is sharded, so scaling out buys no pooling gain",
    "F811": "the scenario exceeds the declared maximum fleet size",
    "F812": "a larger unit rarely divides service time by its size factor",
    "F813": "fewer, larger units concentrate the failure domain",
    "F814": "raising a limit moves the bottleneck rather than removing it",
    "F815": "a changed target is a policy change and adds no capacity",
    "F816": "the scenario removes capacity",
}


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Situation:
    """The operating point a scenario perturbs.

    ``observed_support`` is the largest demand multiplier the observations
    actually reached -- the window maximum over its mean. It is carried here so
    that a scenario asking about demand the system has never seen can be marked
    as extrapolation rather than quietly evaluated at the same confidence as an
    interpolation.
    """

    utilization: float
    fleet: Fleet
    elasticity_target: float
    cv_squared: float = 1.0
    service_time: float | None = None
    response_budget: float | None = None
    observed_support: float = 1.0
    demand_multiplier: float = 1.0

    def __post_init__(self) -> None:
        if self.utilization <= 0.0:
            raise RecommendationError(
                f"utilization must be positive, got {self.utilization}"
            )
        if self.elasticity_target <= 0.0:
            raise RecommendationError(
                f"elasticity target must be positive, got {self.elasticity_target}"
            )
        if self.cv_squared < 0.0:
            raise RecommendationError(
                f"service-time variability cannot be negative, got {self.cv_squared}"
            )
        if self.observed_support <= 0.0:
            raise RecommendationError(
                f"observed support must be positive, got {self.observed_support}"
            )

    @property
    def is_stable(self) -> bool:
        """False once offered load reaches capacity: no finite queue exists.

        A perturbed situation is allowed to be unstable. Clamping it just below
        one would turn "this falls over" into "this gets slow", which is the
        single most expensive substitution a what-if tool can make.
        """
        return self.utilization < 1.0

    @property
    def queue_servers(self) -> int:
        """Servers in the queue being modelled: the pool, or one shard."""
        return 1 if self.fleet.pooling == "sharded" else self.fleet.units


def situation_from(
    saturation: SaturationResult,
    fleet: Fleet,
    *,
    service_time: float | None = None,
    response_budget: float | None = None,
) -> Situation:
    """Build a baseline from a completed saturation analysis.

    Refuses on a refused analysis rather than inventing an operating point: a
    what-if built on an analysis that declined to produce one is a simulation
    of an assumption.
    """
    if saturation.refused or saturation.headroom is None or saturation.profile is None:
        raise RecommendationError(
            "cannot build a baseline from a refused saturation analysis"
        )
    profile = saturation.profile
    support = profile.max_to_mean if profile.max_to_mean is not None else 1.0
    queue = saturation.queue
    return Situation(
        utilization=saturation.headroom.utilization,
        fleet=fleet,
        elasticity_target=saturation.headroom.elasticity_target,
        cv_squared=queue.cv_squared if queue is not None else 1.0,
        service_time=service_time,
        response_budget=response_budget,
        observed_support=max(1.0, support),
    )


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


@runtime_checkable
class Scenario(Protocol):
    """One intervention, and the invariants that make its arithmetic valid."""

    @property
    def label(self) -> str:
        """A short human name for the report."""

    @property
    def holds_fixed(self) -> tuple[str, ...]:
        """What must not move for this outcome to be meaningful."""

    def apply(self, situation: Situation) -> tuple[Situation, list[Finding]]:
        """The perturbed situation, and what is worth saying about it."""


@dataclass(frozen=True, slots=True)
class DoNothing:
    """The counterfactual. Always present in a comparison."""

    @property
    def label(self) -> str:
        return "do nothing"

    @property
    def holds_fixed(self) -> tuple[str, ...]:
        return ("demand", "fleet", "service time", "variability")

    def apply(self, situation: Situation) -> tuple[Situation, list[Finding]]:
        return situation, []


@dataclass(frozen=True, slots=True)
class ScaleOut:
    """Add or remove units, splitting the same demand across them."""

    units_delta: int

    @property
    def label(self) -> str:
        sign = "+" if self.units_delta >= 0 else ""
        return f"scale out {sign}{self.units_delta}"

    @property
    def holds_fixed(self) -> tuple[str, ...]:
        return (
            "demand",
            "service time per unit",
            "variability",
            "even distribution across units",
            "the pooling model",
        )

    def apply(self, situation: Situation) -> tuple[Situation, list[Finding]]:
        findings: list[Finding] = []
        units = situation.fleet.units + self.units_delta
        if units < situation.fleet.min_units:
            raise RecommendationError(
                f"scaling to {units} unit(s) is below the declared minimum of "
                f"{situation.fleet.min_units}"
            )
        maximum = situation.fleet.max_units
        if maximum is not None and units > maximum:
            findings.append(
                Finding(
                    "F811",
                    "error",
                    f"the scenario needs {units} {situation.fleet.unit_name}(s) but the "
                    f"declared maximum is {maximum}: the outcome below describes a "
                    "fleet that is not currently allowed to exist",
                )
            )
        if self.units_delta < 0:
            findings.append(
                Finding(
                    "F816",
                    "note",
                    "the scenario removes capacity, so its outcome is the cost of a "
                    "saving rather than the benefit of a spend",
                )
            )
        if situation.fleet.pooling == "sharded" and self.units_delta > 0:
            findings.append(
                Finding(
                    "F810",
                    "note",
                    "the fleet is sharded, so each added unit only takes its own share "
                    "of the keyspace: utilization falls in proportion and the ceiling "
                    "does not move, which is why the gain here is smaller than the "
                    "same addition would give a pooled fleet",
                )
            )
        # A fleet above its own declared maximum is a state the policy forbids
        # but the arithmetic must still be able to describe, so the hypothetical
        # carries a raised cap and the F811 finding above carries the real one.
        hypothetical_max = None if maximum is None else max(maximum, units)
        moved = replace(
            situation,
            fleet=replace(situation.fleet, units=units, max_units=hypothetical_max),
            utilization=situation.utilization * situation.fleet.units / units,
        )
        return moved, findings


@dataclass(frozen=True, slots=True)
class ScaleUp:
    """Replace each unit with a faster one, dividing service time by a factor."""

    speed_factor: float

    def __post_init__(self) -> None:
        if self.speed_factor <= 0.0:
            raise RecommendationError(
                f"speed factor must be positive, got {self.speed_factor}"
            )

    @property
    def label(self) -> str:
        return f"scale up x{self.speed_factor:g}"

    @property
    def holds_fixed(self) -> tuple[str, ...]:
        return ("demand", "unit count", "variability", "the workload's shape")

    def apply(self, situation: Situation) -> tuple[Situation, list[Finding]]:
        findings: list[Finding] = [
            Finding(
                "F812",
                "warning",
                f"the scenario divides service time by {self.speed_factor:g}, which is "
                "the resource ratio rather than a measured speedup: shared memory "
                "bandwidth, lock contention and any serial section put a floor under "
                "the real service time, so this outcome is the optimistic end",
            )
        ]
        if self.speed_factor > 1.0:
            findings.append(
                Finding(
                    "F813",
                    "note",
                    f"the same work now sits on {situation.fleet.units} larger "
                    f"{situation.fleet.unit_name}(s), so one failure removes the same "
                    "share of a more concentrated fleet: the capacity question "
                    "improves and the availability question does not",
                )
            )
        service_time = (
            None if situation.service_time is None else situation.service_time / self.speed_factor
        )
        moved = replace(
            situation,
            utilization=situation.utilization / self.speed_factor,
            service_time=service_time,
        )
        return moved, findings


@dataclass(frozen=True, slots=True)
class RaiseLimit:
    """Raise the denominator of the utilization ratio: a quota or a cap."""

    factor: float

    def __post_init__(self) -> None:
        if self.factor <= 0.0:
            raise RecommendationError(f"limit factor must be positive, got {self.factor}")

    @property
    def label(self) -> str:
        return f"raise the limit x{self.factor:g}"

    @property
    def holds_fixed(self) -> tuple[str, ...]:
        return ("demand", "fleet", "service time", "every downstream limit")

    def apply(self, situation: Situation) -> tuple[Situation, list[Finding]]:
        findings = [
            Finding(
                "F814",
                "warning",
                "raising a limit moves the bottleneck rather than removing it: the "
                "next constraint -- a downstream quota, a connection pool, a database "
                "-- is not measured by this series and does not appear in this outcome",
            )
        ]
        moved = replace(situation, utilization=situation.utilization / self.factor)
        return moved, findings


@dataclass(frozen=True, slots=True)
class DemandChange:
    """Multiply demand: a launch, a migration in, a campaign, a shed."""

    multiplier: float

    def __post_init__(self) -> None:
        if self.multiplier <= 0.0:
            raise RecommendationError(
                f"demand multiplier must be positive, got {self.multiplier}"
            )

    @property
    def label(self) -> str:
        return f"demand x{self.multiplier:g}"

    @property
    def holds_fixed(self) -> tuple[str, ...]:
        return ("fleet", "service time", "variability", "the shape of demand")

    def apply(self, situation: Situation) -> tuple[Situation, list[Finding]]:
        findings: list[Finding] = []
        reached = situation.demand_multiplier * self.multiplier
        if reached > situation.observed_support:
            findings.append(
                Finding(
                    "F801",
                    "warning",
                    f"the scenario evaluates demand at {reached:.2f}x the measured "
                    f"level, above the {situation.observed_support:.2f}x the window "
                    "ever reached: beyond observed support this is a statement about "
                    "the model, not about the system",
                )
            )
        if reached > situation.observed_support * HARD_EXTRAPOLATION_MULTIPLE:
            findings.append(
                Finding(
                    "F801",
                    "error",
                    f"{reached:.2f}x is more than {HARD_EXTRAPOLATION_MULTIPLE:g} times "
                    "beyond anything observed; at that distance the service time, the "
                    "variability and the shape of demand are all unknown, and the "
                    "arithmetic below should not be quoted",
                )
            )
        moved = replace(
            situation,
            utilization=situation.utilization * self.multiplier,
            demand_multiplier=reached,
        )
        return moved, findings


@dataclass(frozen=True, slots=True)
class RetargetElasticity:
    """Accept more or less latency growth per unit of demand growth."""

    elasticity_target: float

    def __post_init__(self) -> None:
        if self.elasticity_target <= 0.0:
            raise RecommendationError(
                f"elasticity target must be positive, got {self.elasticity_target}"
            )

    @property
    def label(self) -> str:
        return f"retarget elasticity to {self.elasticity_target:g}"

    @property
    def holds_fixed(self) -> tuple[str, ...]:
        return ("demand", "fleet", "service time", "variability", "the physics")

    def apply(self, situation: Situation) -> tuple[Situation, list[Finding]]:
        direction = (
            "loosens" if self.elasticity_target > situation.elasticity_target else "tightens"
        )
        findings = [
            Finding(
                "F815",
                "warning",
                f"this {direction} the target rather than changing the system: no "
                "capacity is added, the measured operating point is unchanged, and "
                "the headroom it reports is bought entirely by agreeing to a "
                "different latency sensitivity",
            )
        ]
        return replace(situation, elasticity_target=self.elasticity_target), findings


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Outcome:
    """What one scenario leaves behind, with the invariants it assumed."""

    label: str
    holds_fixed: tuple[str, ...]
    feasible: bool
    units: int
    utilization: float
    demand_multiplier: float
    elasticity: float | None
    absorbable_multiplier: float | None
    response_inflation: float | None
    mean_response: float | None
    monthly_cost: float | None
    findings: tuple[Finding, ...]

    @property
    def errors(self) -> tuple[Finding, ...]:
        return tuple(finding for finding in self.findings if finding.severity == "error")

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "holds_fixed": list(self.holds_fixed),
            "feasible": self.feasible,
            "units": self.units,
            "utilization": self.utilization,
            "demand_multiplier": self.demand_multiplier,
            "elasticity": self.elasticity,
            "absorbable_multiplier": self.absorbable_multiplier,
            "response_inflation": self.response_inflation,
            "mean_response": self.mean_response,
            "monthly_cost": self.monthly_cost,
            "findings": [
                {"code": f.code, "severity": f.severity, "message": f.message}
                for f in self.findings
            ],
        }


def simulate(situation: Situation, scenario: Scenario) -> Outcome:
    """Apply one scenario to a baseline and evaluate the result.

    The evaluation is the same arithmetic the saturation module uses, run at
    the perturbed operating point, so a scenario that changes nothing returns
    the analysis's own numbers rather than an approximation of them.
    """
    moved, findings = scenario.apply(situation)

    servers = moved.queue_servers
    ceiling = binding_ceiling(
        servers,
        cv_squared=moved.cv_squared,
        elasticity_target=moved.elasticity_target,
        service_time=moved.service_time,
        response_budget=moved.response_budget,
    )

    model: QueueModel | None
    if not moved.is_stable:
        findings.append(
            Finding(
                "F800",
                "error",
                f"the scenario drives offered load to {moved.utilization:.1%} of "
                "capacity, at or beyond saturation: the queue has no steady state, so "
                "the question is not how slow it gets but how fast the backlog grows",
            )
        )
    try:
        model = QueueModel(
            servers=servers,
            service_time=moved.service_time if moved.service_time is not None else 1.0,
            utilization=moved.utilization,
            cv_squared=moved.cv_squared,
        )
    except QueueingError as exc:
        model = None
        if moved.is_stable:
            findings.append(
                Finding(
                    "F800",
                    "error",
                    f"the operating point cannot be evaluated as a queue: {exc}",
                )
            )

    absorbable = (
        ceiling / moved.utilization
        if ceiling is not None and moved.utilization > 0.0
        else None
    )
    return Outcome(
        label=scenario.label,
        holds_fixed=scenario.holds_fixed,
        feasible=model is not None,
        units=moved.fleet.units,
        utilization=moved.utilization,
        demand_multiplier=moved.demand_multiplier,
        elasticity=None if model is None else model.elasticity,
        absorbable_multiplier=absorbable,
        response_inflation=None if model is None else model.response_inflation,
        mean_response=(
            None if model is None or moved.service_time is None else model.mean_response
        ),
        monthly_cost=moved.fleet.monthly_cost,
        findings=tuple(findings),
    )


@dataclass(frozen=True, slots=True)
class Comparison:
    """Several alternatives against the counterfactual of doing nothing."""

    baseline: Outcome
    outcomes: tuple[Outcome, ...]
    target_multiplier: float | None
    findings: tuple[Finding, ...]

    def gain_over_baseline(self, outcome: Outcome) -> float | None:
        """Extra absorbable demand this option buys, against doing nothing."""
        if outcome.absorbable_multiplier is None or self.baseline.absorbable_multiplier is None:
            return None
        return outcome.absorbable_multiplier - self.baseline.absorbable_multiplier

    def gain_per_monthly_cost(self, outcome: Outcome) -> float | None:
        """Absorbable demand bought per unit of extra monthly spend.

        ``None`` when no unit price was declared, and ``None`` rather than
        infinity when an option costs nothing extra: a free improvement has no
        meaningful ratio, and printing infinity in a cost column invites it to
        be read as a number.
        """
        gain = self.gain_over_baseline(outcome)
        if gain is None or outcome.monthly_cost is None or self.baseline.monthly_cost is None:
            return None
        delta = outcome.monthly_cost - self.baseline.monthly_cost
        if delta <= 0.0:
            return None
        return gain / delta

    def as_dict(self) -> dict[str, Any]:
        return {
            "target_multiplier": self.target_multiplier,
            "baseline": self.baseline.as_dict(),
            "outcomes": [
                {
                    **outcome.as_dict(),
                    "gain_over_baseline": self.gain_over_baseline(outcome),
                    "gain_per_monthly_cost": self.gain_per_monthly_cost(outcome),
                }
                for outcome in self.outcomes
            ],
            "findings": [
                {"code": f.code, "severity": f.severity, "message": f.message}
                for f in self.findings
            ],
        }


def compare(
    situation: Situation,
    scenarios: Sequence[Scenario],
    *,
    target_multiplier: float | None = None,
) -> Comparison:
    """Evaluate several alternatives against the baseline of doing nothing.

    Doing nothing is prepended when the caller did not include it, because an
    intervention can only be judged against the counterfactual and a list
    without one makes the cheapest option on it look necessary. Outcomes are
    ordered by absorbable demand, then by cost, and every one of them is
    measured against the baseline rather than against its neighbour.
    """
    if not situation.is_stable:
        raise RecommendationError(
            f"the baseline is already at or beyond saturation ({situation.utilization:.1%}), "
            "so there is no operating point to perturb"
        )

    findings: list[Finding] = []

    listed = list(scenarios)
    if not any(isinstance(scenario, DoNothing) for scenario in listed):
        listed.insert(0, DoNothing())

    baseline = simulate(situation, DoNothing())
    outcomes = [simulate(situation, scenario) for scenario in listed]

    if situation.utilization >= STEEP_REGION_UTILIZATION:
        findings.append(
            Finding(
                "F802",
                "warning",
                f"the baseline sits at {situation.utilization:.1%}, inside the steep "
                "region where elasticity rises sharply: every option below will look "
                "dramatic against it, and the ordering is driven by the shape of "
                "rho/(1-rho) more than by the merits of the interventions",
            )
        )
    if len(listed) > 2:
        findings.append(
            Finding(
                "F803",
                "note",
                "these are alternatives, not a plan: the gain from one intervention "
                "depends on the utilization it starts from, so applying two yields "
                "less than the sum of the two rows",
            )
        )
    if target_multiplier is not None:
        reached = [
            outcome
            for outcome in outcomes
            if outcome.absorbable_multiplier is not None
            and outcome.absorbable_multiplier >= target_multiplier
        ]
        if not reached:
            findings.append(
                Finding(
                    "F804",
                    "error",
                    f"no scenario reaches the {target_multiplier:.2f}x the plan needs: "
                    "the options on the table are insufficient, which is a result "
                    "rather than a reason to pick the best of them",
                )
            )

    ordered = sorted(
        outcomes,
        key=lambda outcome: (
            -(outcome.absorbable_multiplier or 0.0),
            outcome.monthly_cost if outcome.monthly_cost is not None else 0.0,
            outcome.label,
        ),
    )
    return Comparison(
        baseline=baseline,
        outcomes=tuple(ordered),
        target_multiplier=target_multiplier,
        findings=tuple(findings),
    )


def render(comparison: Comparison) -> str:
    """A terminal table, with the invariants under each row rather than in a footnote."""
    lines: list[str] = [
        f"{'option':<28} {'units':>6} {'util':>8} {'absorbs':>9} {'elast':>8} {'cost':>12}",
        "-" * 76,
    ]
    for outcome in comparison.outcomes:
        absorbs = (
            "n/a"
            if outcome.absorbable_multiplier is None
            else f"{outcome.absorbable_multiplier:.2f}x"
        )
        elasticity = (
            "n/a"
            if outcome.elasticity is None or not math.isfinite(outcome.elasticity)
            else f"{outcome.elasticity:.2f}"
        )
        cost = "n/a" if outcome.monthly_cost is None else f"{outcome.monthly_cost:,.2f}"
        lines.append(
            f"{outcome.label:<28} {outcome.units:>6} {outcome.utilization:>7.1%} "
            f"{absorbs:>9} {elasticity:>8} {cost:>12}"
        )
        lines.append(f"{'':<4}holds fixed: {', '.join(outcome.holds_fixed)}")
        gain = comparison.gain_over_baseline(outcome)
        per_cost = comparison.gain_per_monthly_cost(outcome)
        if gain is not None and abs(gain) > 1e-9:
            detail = f"{'':<4}vs doing nothing: {gain:+.2f}x absorbable"
            if per_cost is not None:
                detail += f", {per_cost:.4g}x per unit of monthly spend"
            lines.append(detail)
        for finding in outcome.findings:
            lines.append(f"{'':<4}{finding}")
        lines.append("")

    if comparison.findings:
        lines.append("about this comparison:")
        for finding in comparison.findings:
            lines.append(f"  {finding}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="recommend.whatif",
        description=(
            "Compare capacity interventions against doing nothing, from a stated "
            "operating point."
        ),
        epilog=(
            "The operating point is given rather than measured so that a reviewer can "
            "re-run a published analysis by hand; take the numbers from the saturation "
            "report's utilization, servers and elasticity target."
        ),
    )
    base = parser.add_argument_group("baseline")
    base.add_argument(
        "--utilization", type=float, required=True, help="operating point, 0 to 1"
    )
    base.add_argument("--units", type=int, required=True, help="units deployed now")
    base.add_argument("--unit-name", default="unit", help="what one unit is called")
    base.add_argument(
        "--pooling", choices=["shared", "sharded"], default="shared",
        help="whether the units serve one queue or one queue each",
    )
    base.add_argument("--max-units", type=int, default=None, help="largest allowed fleet")
    base.add_argument(
        "--unit-monthly-cost", type=float, default=None, help="monthly cost of one unit"
    )
    base.add_argument(
        "--elasticity-target", type=float, default=4.0,
        help="percent of latency a percent of demand growth may cost",
    )
    base.add_argument("--service-time", type=float, default=None, help="mean service time, seconds")
    base.add_argument(
        "--service-cv-squared", type=float, default=1.0,
        help="squared coefficient of variation of the service time",
    )
    base.add_argument(
        "--response-budget", type=float, default=None, help="mean response budget, seconds"
    )
    base.add_argument(
        "--observed-support", type=float, default=1.0,
        help="largest demand multiple the observations reached (window max over mean)",
    )

    options = parser.add_argument_group("scenarios (repeatable)")
    options.add_argument(
        "--scale-out", type=int, action="append", default=[], metavar="DELTA",
        help="add (or, negative, remove) this many units",
    )
    options.add_argument(
        "--scale-up", type=float, action="append", default=[], metavar="FACTOR",
        help="divide service time by this factor",
    )
    options.add_argument(
        "--raise-limit", type=float, action="append", default=[], metavar="FACTOR",
        help="multiply the capacity limit by this factor",
    )
    options.add_argument(
        "--demand", type=float, action="append", default=[], metavar="MULTIPLIER",
        help="multiply demand by this factor",
    )
    options.add_argument(
        "--retarget", type=float, action="append", default=[], metavar="ELASTICITY",
        help="evaluate against a different elasticity target",
    )
    parser.add_argument(
        "--target-multiplier", type=float, default=None,
        help="absorbable demand the plan needs; reported as unmet when no option reaches it",
    )
    parser.add_argument("--json", action="store_true", help="emit the comparison as JSON")
    return parser


def _scenarios_from(args: argparse.Namespace) -> list[Scenario]:
    scenarios: list[Scenario] = []
    scenarios.extend(ScaleOut(delta) for delta in args.scale_out)
    scenarios.extend(ScaleUp(factor) for factor in args.scale_up)
    scenarios.extend(RaiseLimit(factor) for factor in args.raise_limit)
    scenarios.extend(DemandChange(multiplier) for multiplier in args.demand)
    scenarios.extend(RetargetElasticity(target) for target in args.retarget)
    return scenarios


def main(argv: Sequence[str] | None = None) -> int:
    """Exit 0 on a comparison, 1 when no option meets the target, 2 on bad input."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        situation = Situation(
            utilization=args.utilization,
            fleet=Fleet(
                units=args.units,
                unit_name=args.unit_name,
                pooling=args.pooling,
                max_units=args.max_units,
                monthly_cost_per_unit=args.unit_monthly_cost,
            ),
            elasticity_target=args.elasticity_target,
            cv_squared=args.service_cv_squared,
            service_time=args.service_time,
            response_budget=args.response_budget,
            observed_support=args.observed_support,
        )
        comparison = compare(
            situation, _scenarios_from(args), target_multiplier=args.target_multiplier
        )
    except RecommendationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(comparison.as_dict(), indent=2, sort_keys=True, default=str))
    else:
        print(render(comparison))
    return 1 if any(f.code == "F804" for f in comparison.findings) else 0


if __name__ == "__main__":
    sys.exit(main())
