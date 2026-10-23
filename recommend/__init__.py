"""Recommendations: turning a forecast and a ceiling into a sizing decision.

A model says what will happen. A recommendation says what to do about it, by
when, and how large the thing you buy has to be. The gap between those two
statements is where a capacity review stops being an analysis and starts being
a decision, and it is made of four things the models deliberately leave open.

**A date, not a verdict.** "There is 1.4x of headroom" is not actionable; "the
upper bound of the forecast reaches the ceiling in eleven days, and provisioning
takes four" is. Every recommendation here resolves to a run-out date and a
decide-by date, and a decide-by date already in the past is an error rather than
a warning.

**The upper bound decides, not the central line.** Sizing to the median
forecast is under-provisioned half the time by construction. The run-out date
is therefore read off the forecast's one-sided upper bound, and the distance
between that date and the central crossing is reported as the margin the
interval bought -- because when the two are far apart, the decision is being
driven by uncertainty rather than by growth, and that is worth knowing before
spending money on it.

**Capacity is an integer.** Nobody provisions 1.37 of anything. Rounding up
forces the achieved headroom past the required headroom, and on a small fleet
the forced overshoot can exceed the growth being planned for. The quantisation
is reported as its own number so that a reviewer can see how much of the bill
is demand and how much is arithmetic.

**Scaling out is not linear, and the non-linearity has a sign.** Pooled servers
get cheaper per unit of work as the pool grows: an M/M/c queue sustains a higher
utilization at the same delay as ``c`` rises, so the naive ``ceil(demand /
per-unit capacity)`` over-provisions a large shared fleet. The same arithmetic
gives nothing at all to a sharded fleet, where each unit has its own queue and
pooling gains do not exist. Which of those two a fleet is cannot be inferred
from a metric, so it is a declared property, and the pooling gain is claimed
only where it was declared.

Finding codes are namespaced here the same way the models namespace theirs, so
that a report can resolve any code in this toolkit against exactly one registry:

``F7xx``
    Sizing and timing -- the ceiling's provenance, the run-out and decide-by
    dates, the quantisation overshoot, the pooling assumption, and every reason
    the recommended size is a bound rather than an answer.
``F8xx``
    Scenario applicability -- for each simulated intervention, what it holds
    fixed, whether the data supports the demand it assumes, and whether the
    baseline sits in the steep region where every intervention looks good.

The models' ``F1xx``--``F6xx`` namespaces are reserved in :mod:`models`, and a
recommendation carries the model findings forward unchanged rather than
re-deriving or summarising them: a sizing decision inherits every objection the
analysis it rests on recorded.
"""

__all__ = ["headroom", "whatif"]
