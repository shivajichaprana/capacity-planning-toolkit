"""Render a capacity review: the decision first, the evidence under it.

A capacity review is read by somebody deciding whether to spend money, usually
in the twenty minutes before the meeting it is discussed in. So the decision,
the date it has to be made by, and the size being asked for come first, and
everything that qualifies them follows in the order a sceptic would ask for it:
what the limit was and where it came from, what the operating point actually
is, what the forecast method was and how well it scored, and then every
objection the analysis recorded.

Two rules govern the output, both inherited from :mod:`reports`.

**Nothing is computed here.** Every figure is a field of a result object,
formatted. A reviewer who doubts a number can find the object it came from and
re-derive it from the observation store, and no arithmetic is hidden in a
template.

**Every finding is rendered.** Errors first, then warnings, then notes, each
with its code, because a review that quietly drops the warnings is worse than
no review: it launders uncertainty into confidence. A short glossary resolves
the codes whose modules publish one, and the finding's own message is printed
whether or not the glossary knows it.

The closing section is the one reviewers tend to use most. It lists, for the
codes actually present, the specific thing that would change the conclusion --
document the limit, lengthen the window, measure the service time -- so that
the meeting ends with work items rather than with a feeling.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from ingest.runtime import configure_logging
from models.forecast import ForecastError, ForecastResult
from models.queueing import QueueingError
from models.saturation import FINDING_CODES as SATURATION_CODES
from models.saturation import SaturationError, SaturationResult
from models.seasonality import Finding
from recommend import headroom as headroom_module
from recommend.headroom import FINDING_CODES as HEADROOM_CODES
from recommend.headroom import Recommendation, RecommendationError
from recommend.whatif import FINDING_CODES as WHATIF_CODES
from recommend.whatif import (
    Comparison,
    DemandChange,
    RaiseLimit,
    ScaleOut,
    ScaleUp,
    Scenario,
    compare,
    situation_from,
)

_LOG = logging.getLogger(__name__)

#: One-line meanings for the codes whose modules publish a registry. The
#: seasonality and forecast namespaces document their codes in prose rather
#: than in a dictionary, so a review prints those findings with their own
#: message and no glossary line, which is honest about what is resolvable.
GLOSSARY: Final[dict[str, str]] = {**SATURATION_CODES, **HEADROOM_CODES, **WHATIF_CODES}

#: For each code, the thing that would change the conclusion if it were done.
#: Only codes present in a review are listed, so the section is a work list
#: rather than a checklist of everything that could ever be wrong.
REMEDIES: Final[dict[str, str]] = {
    "F502": "backfill or explain the coverage gap; a peak inside a hole is invisible",
    "F505": "find and document the real limit; an observed maximum is not one",
    "F506": "confirm the quota ceiling with the provider before sizing against it",
    "F510": "collect a longer window, or plan to a percentile the sample can resolve",
    "F511": "collect a longer window; two samples currently decide the operating point",
    "F523": "measure the mean service time so the response budget can bind",
    "F603": "shorten the analysis window to one the series is stationary over",
    "F604": "measure service-time variability instead of assuming an exponential",
    "F605": "collect a maximum statistic alongside the average, or sample finer",
    "F703": "document the real limit before provisioning against this one",
    "F706": "treat the ceiling as optimistic and verify it with a load test",
    "F707": "lengthen the history; the interval is understated and run-out is earlier",
    "F711": "narrow the interval with more history before buying on it",
    "F726": "reconcile the declared fleet with the server count the analysis used",
    "F801": "load-test at the assumed demand; it is beyond anything observed",
    "F814": "identify the next limit downstream before raising this one",
}

_SEVERITY_ORDER: Final[dict[str, int]] = {"error": 0, "warning": 1, "note": 2}
_SEVERITY_TITLE: Final[dict[str, str]] = {
    "error": "Errors",
    "warning": "Warnings",
    "note": "Notes",
}


@dataclass(frozen=True, slots=True)
class Review:
    """Everything a capacity review renders, and nothing derived from it."""

    recommendation: Recommendation
    saturation: SaturationResult
    forecast: ForecastResult
    comparison: Comparison | None = None
    title: str = "Capacity review"
    generated_at: datetime | None = None

    @property
    def moment(self) -> datetime:
        return self.generated_at or self.recommendation.now

    def findings(self) -> tuple[Finding, ...]:
        """Every finding, de-duplicated by code and message, severity first.

        De-duplicated because the recommendation carries the model findings
        forward, so rendering both would print each one twice; by code *and*
        message because one code can legitimately fire more than once with
        different detail.
        """
        seen: set[tuple[str, str]] = set()
        collected: list[Finding] = []
        sources: Iterable[Iterable[Finding]] = (
            self.recommendation.findings,
            self.saturation.findings,
            self.forecast.findings,
            () if self.comparison is None else self.comparison.findings,
        )
        for group in sources:
            for finding in group:
                marker = (finding.code, finding.message)
                if marker in seen:
                    continue
                seen.add(marker)
                collected.append(finding)
        return tuple(
            sorted(collected, key=lambda f: (_SEVERITY_ORDER.get(f.severity, 3), f.code))
        )

    def as_dict(self) -> dict[str, Any]:
        """The whole review as JSON, for a pipeline that wants the numbers."""
        return {
            "title": self.title,
            "generated_at": self.moment.isoformat(),
            "recommendation": self.recommendation.as_dict(),
            "saturation": self.saturation.as_dict(),
            "forecast": self.forecast.as_dict(),
            "comparison": None if self.comparison is None else self.comparison.as_dict(),
            "findings": [
                {
                    "code": f.code,
                    "severity": f.severity,
                    "message": f.message,
                    "meaning": GLOSSARY.get(f.code),
                }
                for f in self.findings()
            ],
        }


# ---------------------------------------------------------------------------
# Formatting helpers. These format; they do not compute.
# ---------------------------------------------------------------------------


def _or_dash(value: float | None, spec: str) -> str:
    return "--" if value is None else format(value, spec)


def _moment(value: datetime | None) -> str:
    return "--" if value is None else value.isoformat()


def _table(rows: Sequence[tuple[str, str]]) -> list[str]:
    return ["| | |", "| --- | --- |", *[f"| {name} | {value} |" for name, value in rows]]


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def _decision_section(review: Review) -> list[str]:
    recommendation = review.recommendation
    lines = [
        "## Decision",
        "",
        f"**{headroom_module.ACTION_HEADLINE.get(recommendation.action, recommendation.action)}**",
        "",
    ]

    rows: list[tuple[str, str]] = [
        ("Series", f"`{recommendation.series}`"),
        ("Fleet today", recommendation.fleet.label()),
    ]
    sizing = recommendation.sizing
    if sizing is not None:
        rows.append(
            (
                "Fleet required",
                f"{sizing.units_required} {recommendation.fleet.unit_name}(s) "
                f"({sizing.units_added:+d})",
            )
        )
        rows.append(
            (
                "Utilization",
                f"{sizing.utilization_now:.1%} now, {sizing.utilization_after:.1%} "
                "after, at today's demand",
            )
        )
        rows.append(
            (
                "Absorbable demand",
                f"{sizing.absorbable_now:.2f}x now, {sizing.absorbable_after:.2f}x after "
                f"(target {sizing.target_multiplier:.2f}x)",
            )
        )
        if sizing.units_required_linear != sizing.units_required:
            rows.append(
                (
                    "Linear reading",
                    f"{sizing.units_required_linear} {recommendation.fleet.unit_name}(s), "
                    f"{sizing.pooling_units_saved} more than the pooled answer",
                )
            )
        delta = sizing.monthly_cost_delta
        if delta is not None:
            rows.append(("Monthly cost change", f"{delta:+,.2f}"))

    run_out = recommendation.run_out
    if run_out is not None:
        # The peak is what the fleet was sized against; the value at the
        # horizon's last period is reported beside it because the two differ by
        # a lot on a cyclical series and a reader comparing a review against a
        # graph needs to know which one they are looking at.
        rows.append(
            (
                "Projected demand (peak)",
                f"{run_out.upper_multiplier_peak:.2f}x upper, "
                f"{run_out.central_multiplier_peak:.2f}x central",
            )
        )
        rows.append(
            (
                "Projected demand (horizon end)",
                f"{run_out.upper_multiplier_at_horizon:.2f}x upper, "
                f"{run_out.central_multiplier_at_horizon:.2f}x central",
            )
        )
        rows.append(("Run-out (upper bound)", _moment(run_out.upper_crossing)))
        rows.append(("Run-out (central)", _moment(run_out.central_crossing)))
        rows.append(("Forecast horizon ends", _moment(run_out.horizon_end)))
    rows.append(
        (
            "Decide by",
            _moment(recommendation.decide_by)
            + (" **(already passed)**" if recommendation.late else ""),
        )
    )
    rows.append(("Lead time", headroom_module.format_duration(recommendation.lead_time)))
    rows.append(("Generated", review.moment.isoformat()))

    lines.extend(_table(rows))
    lines.append("")
    return lines


def _evidence_section(review: Review) -> list[str]:
    saturation = review.saturation
    lines = ["## Evidence", ""]

    limit = saturation.limit
    if limit is not None:
        lines.append("### The limit")
        lines.append("")
        lines.extend(
            _table(
                [
                    ("Value", f"{limit.value:,.4g} {limit.unit}".strip()),
                    ("Provenance", limit.provenance),
                    ("Is a real limit", "yes" if limit.is_a_real_limit else "no"),
                    ("Note", limit.note or "--"),
                ]
            )
        )
        lines.append("")

    profile = saturation.profile
    if profile is not None:
        lines.append("### The operating point")
        lines.append("")
        lines.extend(
            _table(
                [
                    ("Observations", f"{profile.observations:,}"),
                    ("Planning percentile", f"p{profile.planning_percentile * 100:g}"),
                    ("Planning value", f"{profile.planning_value:.1%}"),
                    ("Mean", f"{profile.mean:.1%}"),
                    ("Median", f"{profile.median:.1%}"),
                    ("Maximum", f"{profile.maximum:.1%}"),
                    (
                        "Highest resolvable percentile",
                        f"p{profile.resolvable_percentile * 100:g}",
                    ),
                    (
                        "Rests on the top two samples",
                        "yes" if profile.determined_by_top_two else "no",
                    ),
                    ("Peak to mean", _or_dash(profile.peak_to_mean, ".2f")),
                ]
            )
        )
        lines.append("")

    exceedance = saturation.exceedance
    if exceedance is not None:
        lines.append("### Time above the threshold")
        lines.append("")
        lines.extend(
            _table(
                [
                    ("Threshold", f"{exceedance.threshold:.1%}"),
                    (
                        "Points above",
                        f"{exceedance.points_above:,} ({exceedance.fraction_above:.1%})",
                    ),
                    ("Runs", f"{exceedance.run_count:,}"),
                    (
                        "Longest run",
                        f"{exceedance.longest_run_seconds:,}s "
                        f"({exceedance.longest_run_points:,} points)",
                    ),
                    ("Total time above", f"{exceedance.total_seconds_above:,}s"),
                    ("Runs ended by a hole", f"{exceedance.runs_ended_by_hole:,}"),
                ]
            )
        )
        lines.append("")

    headroom = saturation.headroom
    if headroom is not None:
        lines.append("### Headroom")
        lines.append("")
        lines.extend(
            _table(
                [
                    ("Utilization", f"{headroom.utilization:.1%}"),
                    ("Elasticity now", f"{headroom.elasticity_now:.3g}"),
                    ("Elasticity target", f"{headroom.elasticity_target:.3g}"),
                    ("Binding ceiling", _or_dash(headroom.binding_ceiling, ".1%")),
                    (
                        "From elasticity",
                        _or_dash(headroom.ceiling_from_elasticity, ".1%"),
                    ),
                    ("From response budget", _or_dash(headroom.ceiling_from_response, ".1%")),
                    ("Absorbable demand", _or_dash(headroom.demand_multiplier, ".2f")),
                    (
                        "Linear reading of the same number",
                        f"{headroom.naive_demand_multiplier:.2f}",
                    ),
                    ("Overstatement factor", _or_dash(headroom.overstatement_factor, ".2f")),
                    ("Past the ceiling", "yes" if headroom.past_ceiling else "no"),
                ]
            )
        )
        lines.append("")

    applicability = saturation.applicability
    if applicability is not None:
        lines.append("### Do the queueing assumptions hold")
        lines.append("")
        lines.extend(
            _table(
                [
                    (
                        "Overall",
                        "yes"
                        if applicability.assumptions_hold
                        else "no -- the ceiling is optimistic",
                    ),
                    ("Source statistic", applicability.source_statistic),
                    ("Sampling period", f"{applicability.period_seconds:,}s"),
                    ("Peak to mean", _or_dash(applicability.peak_to_mean, ".2f")),
                    (
                        "Coefficient of variation",
                        _or_dash(applicability.coefficient_of_variation, ".2f"),
                    ),
                    (
                        "Lag-one autocorrelation",
                        _or_dash(applicability.lag1_autocorrelation, ".2f"),
                    ),
                    (
                        "Trend over the window",
                        _or_dash(applicability.trend_fraction_of_mean, ".1%"),
                    ),
                    (
                        "Service variability measured",
                        "yes" if applicability.service_cv_squared_measured else "no, assumed",
                    ),
                ]
            )
        )
        lines.append("")
    return lines


def _forecast_section(review: Review) -> list[str]:
    forecast = review.forecast
    lines = ["### The forecast", ""]
    diagnostics = forecast.diagnostics
    if diagnostics is None:
        lines.append("The forecast produced no diagnostics.")
        lines.append("")
        return lines
    lines.extend(
        _table(
            [
                ("Method", diagnostics.method),
                ("Interval level", f"{diagnostics.level:.0%} two-sided"),
                ("One-sided level", f"{diagnostics.one_sided_level:.0%}"),
                ("Fitted observations", f"{diagnostics.fitted_observations:,}"),
                ("Holdout observations", f"{diagnostics.holdout_observations:,}"),
                ("Holdout mean absolute error", _or_dash(diagnostics.holdout_mae, ",.4g")),
                (
                    "Holdout interval coverage",
                    _or_dash(diagnostics.holdout_interval_coverage, ".0%"),
                ),
                ("Residual standard deviation", f"{diagnostics.residual_sd:,.4g}"),
                ("Degrees of freedom", f"{diagnostics.degrees_of_freedom:,}"),
                ("Seasonal parameters", f"{diagnostics.seasonal_parameters:,}"),
                (
                    "Interval understatement if read naively",
                    f"{diagnostics.sigma_understatement:.2f}x",
                ),
                (
                    "Candidates scored",
                    ", ".join(
                        f"{name} {score:,.4g}" for name, score in diagnostics.candidate_scores
                    )
                    or "--",
                ),
            ]
        )
    )
    lines.append("")

    seasonal = forecast.seasonal
    if seasonal is not None:
        lines.append("Seasonal profile:")
        lines.append("")
        lines.extend(
            _table(
                [
                    ("Cycle", f"{seasonal.cycle_name} ({seasonal.lag} phases)"),
                    ("Mode", seasonal.mode),
                    ("Timezone", seasonal.timezone_name),
                    ("Strength", f"{seasonal.strength:.3f}"),
                    ("Strength before the parameter charge", f"{seasonal.raw_strength:.3f}"),
                    ("p-value", _or_dash(seasonal.p_value, ".4f")),
                    ("Cycles observed", f"{seasonal.cycles_observed:.1f}"),
                ]
            )
        )
        lines.append("")
    return lines


def _options_section(review: Review) -> list[str]:
    comparison = review.comparison
    if comparison is None:
        return []
    lines = [
        "## Options",
        "",
        "Alternatives, each against the counterfactual of doing nothing. They do "
        "not add: the gain from one depends on the utilization it starts from.",
        "",
        "| Option | Units | Utilization | Absorbs | Elasticity | Monthly cost | vs nothing |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for outcome in comparison.outcomes:
        gain = comparison.gain_over_baseline(outcome)
        lines.append(
            f"| {outcome.label} | {outcome.units} | {outcome.utilization:.1%} | "
            f"{_or_dash(outcome.absorbable_multiplier, '.2f')}x | "
            f"{_or_dash(outcome.elasticity, '.2f')} | "
            f"{_or_dash(outcome.monthly_cost, ',.2f')} | "
            f"{'--' if gain is None else format(gain, '+.2f')} |"
        )
    lines.append("")
    lines.append("What each option holds fixed:")
    lines.append("")
    for outcome in comparison.outcomes:
        lines.append(f"- **{outcome.label}**: {', '.join(outcome.holds_fixed)}")
    lines.append("")
    return lines


def _findings_section(findings: Sequence[Finding]) -> list[str]:
    lines = ["## Findings", ""]
    if not findings:
        lines.append("None recorded.")
        lines.append("")
        return lines
    for severity in ("error", "warning", "note"):
        group = [finding for finding in findings if finding.severity == severity]
        if not group:
            continue
        lines.append(f"### {_SEVERITY_TITLE[severity]}")
        lines.append("")
        for finding in group:
            meaning = GLOSSARY.get(finding.code)
            suffix = f" _({meaning})_" if meaning else ""
            lines.append(f"- **{finding.code}**{suffix} -- {finding.message}")
        lines.append("")
    return lines


def _remedies_section(findings: Sequence[Finding]) -> list[str]:
    present = sorted({finding.code for finding in findings} & set(REMEDIES))
    if not present:
        return []
    lines = [
        "## What would change this conclusion",
        "",
        "Each item below corresponds to a finding above. Doing it changes the "
        "analysis rather than merely documenting it.",
        "",
    ]
    for code in present:
        lines.append(f"- **{code}**: {REMEDIES[code]}")
    lines.append("")
    return lines


def render_markdown(review: Review) -> str:
    """The whole review as Markdown."""
    findings = review.findings()
    lines = [f"# {review.title}", ""]
    lines.extend(_decision_section(review))
    lines.extend(_evidence_section(review))
    lines.extend(_forecast_section(review))
    lines.extend(_options_section(review))
    lines.extend(_findings_section(findings))
    lines.extend(_remedies_section(findings))
    return "\n".join(lines).rstrip() + "\n"


def write_review(review: Review, directory: str | Path, *, stem: str) -> tuple[Path, Path]:
    """Write the Markdown and JSON forms side by side, returning both paths.

    Both are written because they answer different questions: the Markdown is
    read in the meeting, and the JSON is what a later run diffs against to see
    what moved.
    """
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    markdown_path = target / f"{stem}.md"
    json_path = target / f"{stem}.json"
    markdown_path.write_text(render_markdown(review), encoding="utf-8")
    json_path.write_text(
        json.dumps(review.as_dict(), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return markdown_path, json_path


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def _slug(label: str) -> str:
    """A filesystem-safe stem from a series label."""
    cleaned = [character if character.isalnum() else "-" for character in label.lower()]
    collapsed = re.sub(r"-+", "-", "".join(cleaned)).strip("-")
    return collapsed or "series"


def build_parser() -> argparse.ArgumentParser:
    """The analysis parser, extended with the options a review adds.

    Built from :func:`recommend.headroom.build_parser` rather than redeclared,
    so a review is always rendered from exactly the analysis the equivalent
    recommendation command would print.
    """
    parser = headroom_module.build_parser()
    parser.prog = "reports.review"
    parser.description = (
        "Render a capacity review: the recommendation, the evidence behind it, "
        "the options, and every finding."
    )
    parser.epilog = (
        "Markdown and JSON are always written to the output directory; --json "
        "additionally prints the JSON form to standard output."
    )

    review = parser.add_argument_group("review")
    review.add_argument(
        "--out", default="reports", help="directory to write the review into"
    )
    review.add_argument(
        "--stem", default=None, help="file stem; defaults to the series and a timestamp"
    )
    review.add_argument("--title", default="Capacity review", help="review title")
    review.add_argument(
        "--option-scale-out", type=int, action="append", default=[], metavar="DELTA",
        help="include a scale-out option in the comparison; repeatable",
    )
    review.add_argument(
        "--option-scale-up", type=float, action="append", default=[], metavar="FACTOR",
        help="include a faster-unit option in the comparison; repeatable",
    )
    review.add_argument(
        "--option-raise-limit", type=float, action="append", default=[], metavar="FACTOR",
        help="include a raised-limit option in the comparison; repeatable",
    )
    review.add_argument(
        "--option-demand", type=float, action="append", default=[], metavar="MULTIPLIER",
        help="include a demand-change option in the comparison; repeatable",
    )
    return parser


def _comparison_for(
    args: argparse.Namespace,
    saturation: SaturationResult,
    recommendation: Recommendation,
) -> Comparison | None:
    """Build the options table, or ``None`` when no options were asked for."""
    scenarios: list[Scenario] = []
    scenarios.extend(ScaleOut(delta) for delta in args.option_scale_out)
    scenarios.extend(ScaleUp(factor) for factor in args.option_scale_up)
    scenarios.extend(RaiseLimit(factor) for factor in args.option_raise_limit)
    scenarios.extend(DemandChange(multiplier) for multiplier in args.option_demand)
    if not scenarios:
        return None

    situation = situation_from(
        saturation,
        recommendation.fleet,
        service_time=args.service_time,
        response_budget=args.response_budget,
    )
    target = None if recommendation.sizing is None else recommendation.sizing.target_multiplier
    return compare(situation, scenarios, target_multiplier=target)


def main(argv: Sequence[str] | None = None) -> int:
    """Exit 0 on a review, 1 when the recommendation was refused, 2 on bad input."""
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.log_level, args.log_format)

    try:
        _series, saturation, forecast, recommendation = headroom_module.run_from_args(args)
    except (RecommendationError, SaturationError, ForecastError, QueueingError) as exc:
        _LOG.error("%s", exc)
        return 2

    comparison: Comparison | None = None
    try:
        comparison = _comparison_for(args, saturation, recommendation)
    except RecommendationError as exc:
        # A comparison that cannot be built must not cost the review: the
        # recommendation and its evidence stand on their own.
        _LOG.warning("options omitted from the review: %s", exc)

    review = Review(
        recommendation=recommendation,
        saturation=saturation,
        forecast=forecast,
        comparison=comparison,
        title=args.title,
    )

    stamp = review.moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stem = args.stem or f"{_slug(recommendation.series)}-{stamp}"
    try:
        markdown_path, json_path = write_review(review, args.out, stem=stem)
    except OSError as exc:
        _LOG.error("cannot write the review into %s: %s", args.out, exc)
        return 2

    if args.json:
        print(json.dumps(review.as_dict(), indent=2, sort_keys=True, default=str))
    _LOG.info("wrote %s and %s", markdown_path, json_path)
    print(markdown_path)
    print(json_path)
    return 1 if recommendation.refused else 0


if __name__ == "__main__":
    sys.exit(main())
