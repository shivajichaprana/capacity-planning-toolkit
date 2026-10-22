"""Queueing-theory primitives, and the arithmetic that makes utilization legible.

Capacity work goes wrong in a specific, repeatable way: utilization is treated
as if it were headroom. A resource reported at 70% is described as having 30%
left, the plan assumes demand can grow by a factor of 1/0.7, and the service
falls over somewhere short of that. The error is not in the measurement. It is
that response time is not linear in utilization, so the distance between "70%"
and "unusable" is not 30% of anything.

This module is the closed-form half of the answer. It holds no state, reads no
store and makes no claim about any particular service; it turns a declared
operating point into the numbers that point implies, and it is explicit about
which of those numbers are exact and which are approximations.

**Elasticity is the number worth carrying around.** For a single server with
exponential service, mean response time is ``E[S] / (1 - rho)``, and the
elasticity of response time with respect to demand -- the percentage change in
response time per percentage change in arrival rate -- is ``rho / (1 - rho)``.
It equals 1 at 50% utilization, 4 at 80%, 9 at 90% and 19 at 95%. So a service
at 90% pays nine percent in latency for every one percent of growth, which is
why the last tenth of a resource cannot be planned as if it were the first.
:func:`utilization_for_elasticity` inverts this: it answers "how loaded can
this run before growth gets expensive" rather than "how loaded is it".

**Service-time variability costs as much as load.** The Pollaczek-Khinchine
formula puts a factor of ``(1 + cv2) / 2`` on the queueing term, where ``cv2``
is the squared coefficient of variation of service time. Exponential service
has ``cv2 = 1``; a service whose work mixes cache hits with cold reads can sit
at ``cv2 = 10``, and then the queue at a given utilization is five and a half
times longer than the exponential formula predicts. A capacity model that
assumes ``cv2 = 1`` without measuring it is making a load-bearing assumption
silently, so ``cv2`` is a required field here rather than a default.

**Erlang C is computed by recursion, not by factorials.** The textbook form of
the Erlang C formula divides ``a**c / c!`` by a truncated series, which
overflows a float at a few hundred servers and loses precision well before
that. The implementation here runs the Erlang B recursion
``B(k) = a*B(k-1) / (k + a*B(k-1))``, which is bounded in ``[0, 1]`` at every
step, and converts to Erlang C at the end. It is stable for thousands of
servers, which matters because connection pools and thread pools are routinely
that size.

**Tail quantiles are reported only where they are exact.** For exponential
service and first-come-first-served, the waiting time above zero is itself
exponential, so :meth:`QueueModel.wait_quantile` is closed-form and exact. The
sojourn-time quantile is exact only for a single server; for several servers
the sojourn distribution is a mixture, and adding the mean service time to a
wait quantile is not the sojourn quantile in either direction. That calculation
is therefore refused rather than approximated, and
:data:`MODEL_ASSUMPTIONS` records why. For general service times no tail
formula exists at all, so only means are offered.

What none of this can do is decide whether the model applies. Every formula
below assumes Poisson arrivals, a stationary arrival rate and a queue allowed
to grow without bound. Measured demand is bursty, drifting and clipped by
timeouts, and under bursty arrivals these formulas understate delay. The
companion saturation module measures those departures from the assumptions on
the real series and reports them next to any number taken from here.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

_LOG = logging.getLogger(__name__)

Discipline = Literal["mm1", "mmc", "mg1"]

#: The largest utilization any solver will return. At ``rho = 1`` an unbounded
#: queue has no steady state, so the open interval is the domain and the bound
#: exists only to keep bisection from returning the singularity itself.
MAX_UTILIZATION: Final[float] = 1.0 - 1e-9

#: Bisection is used wherever a closed-form inverse does not exist. Every
#: objective below is continuous and strictly monotone in utilization over
#: ``(0, 1)``, so bisection converges and cannot land on a wrong root.
_BISECTION_STEPS: Final[int] = 200
_BISECTION_FLOOR: Final[float] = 1e-12

#: Relative step used for the numerical elasticity of the several-server model,
#: which has no convenient analytic derivative. Chosen as the cube root of the
#: double epsilon scale: large enough that catastrophic cancellation in the
#: central difference stays below the truncation error, small enough that the
#: curvature of the response curve does not dominate.
_ELASTICITY_STEP: Final[float] = 1e-5

#: Stated once, machine-readable, so a report can print the assumptions behind
#: a number instead of leaving a reader to assume there are none.
MODEL_ASSUMPTIONS: Final[dict[str, str]] = {
    "poisson_arrivals": (
        "Arrivals are a Poisson process: independent, memoryless, constant rate. "
        "Real demand arrives in bursts, and burstiness raises delay at a given "
        "utilization, so every wait below is a lower bound on measured wait."
    ),
    "stationarity": (
        "The arrival rate is constant over the window being modelled. A series "
        "with a trend or a strong cycle has no single operating point, and a "
        "model fitted to its mean describes a load the service never runs at."
    ),
    "unbounded_queue": (
        "The queue may grow without limit and no arrival is rejected or times "
        "out. A real queue is bounded by a timeout, a connection limit or a "
        "load shedder, which caps measured latency and hides the saturation "
        "this model would predict."
    ),
    "service_independence": (
        "Service times are independent of each other and of the queue length. "
        "Services that batch, or that slow down under memory pressure, violate "
        "this in the direction of worse delay."
    ),
    "sojourn_quantile_several_servers": (
        "For more than one server the sojourn-time distribution is a mixture, "
        "not an exponential, so its quantiles are not the wait quantiles plus "
        "the mean service time. They are refused rather than approximated."
    ),
    "general_service_tails": (
        "For general service-time distributions only the mean queueing delay "
        "has a closed form. No percentile of wait or response time is reported."
    ),
}


class QueueingError(ValueError):
    """Raised when a queueing calculation cannot be performed as asked."""


# ---------------------------------------------------------------------------
# Erlang functions
# ---------------------------------------------------------------------------


def erlang_b(servers: int, offered_load: float) -> float:
    """Blocking probability of a loss system with ``servers`` servers and no queue.

    This is the M/M/c/c model: a request that finds every server busy is
    rejected immediately rather than queued. It is the right model for a fixed
    connection pool with no wait list, or for a thread pool that sheds on
    exhaustion, and it is also the numerically stable route to Erlang C.

    The recursion ``B(0) = 1``, ``B(k) = a*B(k-1) / (k + a*B(k-1))`` is
    algebraically identical to the factorial form but every intermediate value
    is a probability, so nothing overflows and relative error stays near
    machine epsilon for any server count worth modelling.
    """
    if servers < 1:
        raise QueueingError(f"servers must be at least 1, got {servers}")
    if offered_load < 0.0:
        raise QueueingError(f"offered load must be non-negative, got {offered_load}")
    if offered_load == 0.0:
        return 0.0

    blocking = 1.0
    for index in range(1, servers + 1):
        scaled = offered_load * blocking
        blocking = scaled / (index + scaled)
    return blocking


def erlang_c(servers: int, offered_load: float) -> float:
    """Probability that an arrival has to wait in an M/M/c queue.

    ``offered_load`` is ``arrival_rate * mean_service_time``, in erlangs: the
    mean number of servers kept busy. Utilization is ``offered_load / servers``
    and must be below 1 for a steady state to exist.

    Derived from Erlang B by ``C = B / (1 - rho * (1 - B))``, which is exact.
    """
    if servers < 1:
        raise QueueingError(f"servers must be at least 1, got {servers}")
    utilization = offered_load / servers
    if utilization >= 1.0:
        raise QueueingError(
            f"offered load {offered_load:.6g} saturates {servers} server(s): "
            "an unbounded queue has no steady state at or above full utilization"
        )
    if offered_load == 0.0:
        return 0.0

    blocking = erlang_b(servers, offered_load)
    denominator = 1.0 - utilization * (1.0 - blocking)
    if denominator <= 0.0:  # pragma: no cover - guarded by the utilization check
        raise QueueingError("Erlang C denominator vanished; offered load is at capacity")
    return min(1.0, blocking / denominator)


# ---------------------------------------------------------------------------
# The operating point
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QueueModel:
    """One operating point, and everything that point implies.

    The point is given as a utilization rather than an arrival rate because
    utilization is what monitoring reports. Arrival rate is derived from it:
    ``arrival_rate = utilization * servers / service_time``.

    ``cv_squared`` is the squared coefficient of variation of service time.
    It is required, not defaulted, because assuming 1 is the most common silent
    error in this kind of model and the formulas scale linearly in
    ``(1 + cv_squared) / 2``. Pass 1.0 to state the exponential assumption
    deliberately.
    """

    servers: int
    service_time: float
    utilization: float
    cv_squared: float

    def __post_init__(self) -> None:
        if self.servers < 1:
            raise QueueingError(f"servers must be at least 1, got {self.servers}")
        if self.service_time <= 0.0:
            raise QueueingError(f"service time must be positive, got {self.service_time}")
        if not 0.0 <= self.utilization < 1.0:
            raise QueueingError(
                f"utilization must be in [0, 1), got {self.utilization}: "
                "at or above 1 an unbounded queue has no steady state"
            )
        if self.cv_squared < 0.0:
            raise QueueingError(f"cv_squared must be non-negative, got {self.cv_squared}")
        if self.servers > 1 and abs(self.cv_squared - 1.0) > 1e-9:
            raise QueueingError(
                "non-exponential service time is only modelled for a single server: "
                "no exact several-server result exists for general service times"
            )

    # -- identity ---------------------------------------------------------

    @property
    def discipline(self) -> Discipline:
        """Which model this operating point is being evaluated under."""
        if self.servers > 1:
            return "mmc"
        if abs(self.cv_squared - 1.0) <= 1e-9:
            return "mm1"
        return "mg1"

    @property
    def offered_load(self) -> float:
        """Mean number of servers kept busy, in erlangs."""
        return self.utilization * self.servers

    @property
    def arrival_rate(self) -> float:
        """Arrivals per unit time implied by the utilization and service time."""
        return self.offered_load / self.service_time

    @property
    def service_rate(self) -> float:
        """Completions per unit time per busy server."""
        return 1.0 / self.service_time

    @property
    def variability_factor(self) -> float:
        """The Pollaczek-Khinchine factor ``(1 + cv2) / 2``.

        1.0 for exponential service. Everything above 1 is queueing delay the
        exponential formula would not have predicted.
        """
        return (1.0 + self.cv_squared) / 2.0

    # -- means ------------------------------------------------------------

    @property
    def wait_probability(self) -> float:
        """Probability that an arrival waits rather than being served at once."""
        if self.discipline == "mmc":
            return erlang_c(self.servers, self.offered_load)
        return self.utilization

    @property
    def mean_wait(self) -> float:
        """Mean time spent queueing, excluding service.

        * M/M/1: ``E[S] * rho / (1 - rho)``.
        * M/G/1: the same, times ``(1 + cv2) / 2`` -- Pollaczek-Khinchine.
        * M/M/c: ``C(c, a) * E[S] / (c * (1 - rho))``.
        """
        if self.utilization == 0.0:
            return 0.0
        if self.discipline == "mmc":
            return (
                self.wait_probability
                * self.service_time
                / (self.servers * (1.0 - self.utilization))
            )
        return (
            self.service_time
            * self.utilization
            / (1.0 - self.utilization)
            * self.variability_factor
        )

    @property
    def mean_response(self) -> float:
        """Mean time from arrival to completion: queueing plus service."""
        return self.mean_wait + self.service_time

    @property
    def mean_queue_length(self) -> float:
        """Mean number of requests waiting, by Little's law on the queue."""
        return self.arrival_rate * self.mean_wait

    @property
    def mean_concurrency(self) -> float:
        """Mean number of requests in the system, by Little's law."""
        return self.arrival_rate * self.mean_response

    @property
    def response_inflation(self) -> float:
        """Mean response time as a multiple of its unloaded value.

        The number a reader actually wants when told a utilization: at 50% a
        request takes twice as long as it would on an idle system, at 90% ten
        times as long.
        """
        return self.mean_response / self.service_time

    # -- sensitivity ------------------------------------------------------

    @property
    def elasticity(self) -> float:
        """Percentage change in mean response time per percentage change in demand.

        The single most useful summary of an operating point, because it is the
        exchange rate between growth and latency. Exact for one server, where
        it reduces to ``rho / (1 - rho)`` under exponential service; computed by
        a central difference in log space for several servers, where the Erlang
        C term has no tidy derivative.
        """
        if self.utilization == 0.0:
            return 0.0
        if self.discipline != "mmc":
            factor = self.variability_factor
            numerator = self.utilization * factor
            denominator = (1.0 - self.utilization) * (
                1.0 - self.utilization + self.utilization * factor
            )
            return numerator / denominator

        step = _ELASTICITY_STEP
        low = self.utilization * (1.0 - step)
        high = min(MAX_UTILIZATION, self.utilization * (1.0 + step))
        if high <= low:  # pragma: no cover - only at the clamped boundary
            return math.inf
        below = self._with_utilization(low).mean_response
        above = self._with_utilization(high).mean_response
        return (math.log(above) - math.log(below)) / (math.log(high) - math.log(low))

    def _with_utilization(self, utilization: float) -> QueueModel:
        return QueueModel(
            servers=self.servers,
            service_time=self.service_time,
            utilization=utilization,
            cv_squared=self.cv_squared,
        )

    # -- tails ------------------------------------------------------------

    def wait_quantile(self, probability: float) -> float | None:
        """The ``probability`` quantile of queueing delay, or ``None`` if not exact.

        With exponential service and first-come-first-served, waiting time has
        an atom of size ``1 - P(wait)`` at zero and an exponential tail above
        it with rate ``c*mu - lambda``. So the quantile is zero whenever the
        requested probability sits inside the atom, and
        ``ln(P(wait) / (1 - p)) / (c*mu - lambda)`` above it.

        Returns ``None`` for general service times, where no tail formula exists.
        """
        if not 0.0 < probability < 1.0:
            raise QueueingError(f"probability must be in (0, 1), got {probability}")
        if self.discipline == "mg1":
            return None
        if self.utilization == 0.0:
            return 0.0

        wait_probability = self.wait_probability
        upper_tail = 1.0 - probability
        if upper_tail >= wait_probability:
            # The quantile falls inside the atom at zero: more than this
            # fraction of arrivals are served without queueing at all.
            return 0.0
        drain_rate = self.servers * (1.0 - self.utilization) / self.service_time
        return math.log(wait_probability / upper_tail) / drain_rate

    def response_quantile(self, probability: float) -> float | None:
        """The ``probability`` quantile of response time, or ``None`` if not exact.

        Exact only for a single server with exponential service, where sojourn
        time is exponential with rate ``mu - lambda``. For several servers the
        sojourn distribution is a mixture whose quantiles are not recoverable
        from the wait quantiles, so this returns ``None`` rather than adding a
        mean service time to a tail and calling the result a percentile.
        """
        if not 0.0 < probability < 1.0:
            raise QueueingError(f"probability must be in (0, 1), got {probability}")
        if self.discipline != "mm1":
            return None
        return -math.log1p(-probability) * self.service_time / (1.0 - self.utilization)

    # -- rendering --------------------------------------------------------

    def as_dict(self, *, quantiles: Sequence[float] = (0.95, 0.99)) -> dict[str, Any]:
        """JSON-ready form of the operating point."""
        return {
            "discipline": self.discipline,
            "servers": self.servers,
            "service_time": self.service_time,
            "utilization": self.utilization,
            "cv_squared": self.cv_squared,
            "offered_load": self.offered_load,
            "arrival_rate": self.arrival_rate,
            "variability_factor": self.variability_factor,
            "wait_probability": self.wait_probability,
            "mean_wait": self.mean_wait,
            "mean_response": self.mean_response,
            "mean_queue_length": self.mean_queue_length,
            "mean_concurrency": self.mean_concurrency,
            "response_inflation": self.response_inflation,
            "elasticity": self.elasticity,
            "wait_quantiles": {
                f"p{int(round(probability * 100))}": self.wait_quantile(probability)
                for probability in quantiles
            },
            "response_quantiles": {
                f"p{int(round(probability * 100))}": self.response_quantile(probability)
                for probability in quantiles
            },
        }


# ---------------------------------------------------------------------------
# Inverse problems
# ---------------------------------------------------------------------------


def _bisect_utilization(
    objective: Callable[[QueueModel], float],
    *,
    servers: int,
    service_time: float,
    cv_squared: float,
) -> float | None:
    """Largest utilization at which ``objective(model)`` is still non-positive.

    Every objective passed here is continuous and strictly increasing in
    utilization, so a sign change brackets exactly one root. ``None`` means the
    constraint is already violated at an arbitrarily small load, which is the
    honest answer when a budget is below the unloaded service time itself.
    """

    def evaluate(utilization: float) -> float:
        model = QueueModel(
            servers=servers,
            service_time=service_time,
            utilization=utilization,
            cv_squared=cv_squared,
        )
        return float(objective(model))

    low = _BISECTION_FLOOR
    if evaluate(low) > 0.0:
        return None
    high = MAX_UTILIZATION
    if evaluate(high) <= 0.0:
        return high

    for _ in range(_BISECTION_STEPS):
        middle = 0.5 * (low + high)
        if evaluate(middle) <= 0.0:
            low = middle
        else:
            high = middle
    return low


def utilization_for_mean_wait(
    budget: float,
    *,
    servers: int = 1,
    service_time: float = 1.0,
    cv_squared: float = 1.0,
) -> float | None:
    """Highest utilization whose mean queueing delay stays within ``budget``."""
    if budget < 0.0:
        raise QueueingError(f"wait budget must be non-negative, got {budget}")
    return _bisect_utilization(
        lambda model: model.mean_wait - budget,
        servers=servers,
        service_time=service_time,
        cv_squared=cv_squared,
    )


def utilization_for_mean_response(
    budget: float,
    *,
    servers: int = 1,
    service_time: float = 1.0,
    cv_squared: float = 1.0,
) -> float | None:
    """Highest utilization whose mean response time stays within ``budget``.

    ``None`` when the budget is below the mean service time: no amount of spare
    capacity makes a request finish faster than the work it contains, and that
    is a sizing problem rather than a loading problem.
    """
    if budget <= 0.0:
        raise QueueingError(f"response budget must be positive, got {budget}")
    return _bisect_utilization(
        lambda model: model.mean_response - budget,
        servers=servers,
        service_time=service_time,
        cv_squared=cv_squared,
    )


def utilization_for_wait_quantile(
    budget: float,
    probability: float,
    *,
    servers: int = 1,
    service_time: float = 1.0,
) -> float | None:
    """Highest utilization whose ``probability`` wait quantile stays within ``budget``.

    Exponential service only -- the quantile has no closed form otherwise, and
    this refuses rather than silently falling back to the mean.
    """
    if budget < 0.0:
        raise QueueingError(f"wait budget must be non-negative, got {budget}")
    if not 0.0 < probability < 1.0:
        raise QueueingError(f"probability must be in (0, 1), got {probability}")

    def objective(model: QueueModel) -> float:
        quantile = model.wait_quantile(probability)
        if quantile is None:  # pragma: no cover - cv_squared is fixed at 1 here
            raise QueueingError("wait quantile is not available for this model")
        return quantile - budget

    return _bisect_utilization(
        objective, servers=servers, service_time=service_time, cv_squared=1.0
    )


def utilization_for_elasticity(
    target: float,
    *,
    servers: int = 1,
    service_time: float = 1.0,
    cv_squared: float = 1.0,
) -> float | None:
    """Highest utilization whose response-time elasticity stays within ``target``.

    The planning question this answers is "where does growth stop being cheap",
    and it needs no latency budget at all -- only a decision about how much
    latency a percent of growth may cost. For a single exponential server the
    answer is ``target / (1 + target)``: elasticity 1 at 50%, 4 at 80%, 9 at 90%.
    """
    if target <= 0.0:
        raise QueueingError(f"elasticity target must be positive, got {target}")
    return _bisect_utilization(
        lambda model: model.elasticity - target,
        servers=servers,
        service_time=service_time,
        cv_squared=cv_squared,
    )


def servers_for_wait_budget(
    arrival_rate: float,
    service_time: float,
    budget: float,
    *,
    probability: float | None = None,
    max_servers: int = 100_000,
) -> int | None:
    """Fewest servers that keep queueing delay within ``budget`` at this demand.

    With ``probability`` set, the budget applies to that quantile of wait;
    otherwise to the mean. Returns ``None`` if ``max_servers`` is not enough,
    which is a real outcome when the budget is tighter than the granularity a
    single server provides.

    Search starts at the smallest server count that is stable at all -- below
    ``ceil(offered_load)`` there is no steady state to evaluate -- and walks up.
    Mean wait is decreasing in server count at fixed demand, so the first
    feasible count is the fewest.
    """
    if arrival_rate <= 0.0:
        raise QueueingError(f"arrival rate must be positive, got {arrival_rate}")
    if service_time <= 0.0:
        raise QueueingError(f"service time must be positive, got {service_time}")
    if budget < 0.0:
        raise QueueingError(f"budget must be non-negative, got {budget}")

    offered_load = arrival_rate * service_time
    start = max(1, math.floor(offered_load) + 1)
    for servers in range(start, max_servers + 1):
        model = QueueModel(
            servers=servers,
            service_time=service_time,
            utilization=offered_load / servers,
            cv_squared=1.0,
        )
        measured = model.mean_wait if probability is None else model.wait_quantile(probability)
        if measured is None:  # pragma: no cover - exponential service throughout
            raise QueueingError("wait quantile is not available for this model")
        if measured <= budget:
            return servers
    return None


def little_law_residual(
    concurrency: float, throughput: float, latency: float
) -> float | None:
    """Relative disagreement between three measurements that must satisfy ``L = lambda*W``.

    Little's law is an identity, not a model: it holds for any stable system
    regardless of arrival process or service distribution. So when measured
    concurrency, throughput and latency do not satisfy it, the conclusion is not
    that the system is unusual -- it is that at least one of the three is
    measured over the wrong window, scoped to the wrong component, or reported
    in the wrong unit. Returns the signed relative residual
    ``(L - lambda*W) / L``, or ``None`` when concurrency is zero and the ratio
    is undefined.
    """
    if throughput < 0.0 or latency < 0.0 or concurrency < 0.0:
        raise QueueingError("Little's law inputs must be non-negative")
    if concurrency == 0.0:
        return None
    return (concurrency - throughput * latency) / concurrency


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="queueing",
        description=(
            "Evaluate one operating point: what a utilization implies for "
            "latency, and how much growth it leaves."
        ),
    )
    parser.add_argument(
        "--servers", type=int, default=1, help="number of parallel servers (default: 1)"
    )
    parser.add_argument(
        "--service-time",
        type=float,
        required=True,
        help="mean service time of one request, in seconds",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--utilization", type=float, help="operating point, in [0, 1)")
    group.add_argument("--arrival-rate", type=float, help="arrivals per second")
    parser.add_argument(
        "--cv-squared",
        type=float,
        default=1.0,
        help=(
            "squared coefficient of variation of service time; 1.0 is "
            "exponential, and higher values queue proportionally longer"
        ),
    )
    parser.add_argument(
        "--elasticity-target",
        type=float,
        default=4.0,
        help=(
            "how many percent of latency a percent of growth may cost, used to "
            "report a planning ceiling (default: 4.0, which is 80%% for one "
            "exponential server)"
        ),
    )
    parser.add_argument(
        "--response-budget",
        type=float,
        default=None,
        help="optional mean response-time budget in seconds, for a second ceiling",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="log verbosity (default: INFO)",
    )
    return parser


def _ceilings(model: QueueModel, args: argparse.Namespace) -> dict[str, Any]:
    """Planning ceilings for this configuration, and the growth each leaves."""
    out: dict[str, Any] = {}
    elasticity_ceiling = utilization_for_elasticity(
        args.elasticity_target,
        servers=model.servers,
        service_time=model.service_time,
        cv_squared=model.cv_squared,
    )
    out["elasticity_target"] = args.elasticity_target
    out["utilization_ceiling_elasticity"] = elasticity_ceiling
    if args.response_budget is not None:
        out["response_budget"] = args.response_budget
        out["utilization_ceiling_response"] = utilization_for_mean_response(
            args.response_budget,
            servers=model.servers,
            service_time=model.service_time,
            cv_squared=model.cv_squared,
        )

    ceilings = [
        value
        for key, value in out.items()
        if key.startswith("utilization_ceiling") and isinstance(value, float)
    ]
    binding = min(ceilings) if ceilings else None
    out["binding_ceiling"] = binding
    if binding is not None and model.utilization > 0.0:
        out["demand_multiplier_to_ceiling"] = binding / model.utilization
        out["demand_multiplier_naive"] = 1.0 / model.utilization
    return out


def _render(model: QueueModel, ceilings: dict[str, Any]) -> str:
    lines = [
        f"model: {model.discipline}  servers: {model.servers}  "
        f"service time: {model.service_time:.4g}s  cv2: {model.cv_squared:.3g}",
        f"utilization: {model.utilization:.2%}  arrivals: {model.arrival_rate:.4g}/s  "
        f"offered load: {model.offered_load:.4g} erlang",
        "",
        f"mean wait:        {model.mean_wait:.4g}s",
        f"mean response:    {model.mean_response:.4g}s "
        f"({model.response_inflation:.2f}x unloaded)",
        f"mean concurrency: {model.mean_concurrency:.4g} "
        f"(queue {model.mean_queue_length:.4g})",
        f"wait probability: {model.wait_probability:.2%}",
        f"elasticity:       {model.elasticity:.3g} "
        "(percent latency per percent growth)",
    ]
    for probability in (0.95, 0.99):
        wait = model.wait_quantile(probability)
        response = model.response_quantile(probability)
        label = f"p{int(probability * 100)}"
        wait_text = "not exact for this model" if wait is None else f"{wait:.4g}s"
        response_text = (
            "not exact for this model" if response is None else f"{response:.4g}s"
        )
        lines.append(f"{label} wait:          {wait_text}")
        lines.append(f"{label} response:      {response_text}")

    lines.append("")
    binding = ceilings.get("binding_ceiling")
    if isinstance(binding, float):
        lines.append(
            f"planning ceiling: {binding:.2%} utilization "
            f"(elasticity target {ceilings['elasticity_target']:.3g}"
            + (
                f", response budget {ceilings['response_budget']:.4g}s"
                if "response_budget" in ceilings
                else ""
            )
            + ")"
        )
        multiplier = ceilings.get("demand_multiplier_to_ceiling")
        naive = ceilings.get("demand_multiplier_naive")
        if isinstance(multiplier, float) and isinstance(naive, float):
            lines.append(
                f"demand headroom: {multiplier:.2f}x before the ceiling is reached; "
                f"reading utilization as linear headroom would claim {naive:.2f}x"
            )
    else:
        lines.append(
            "planning ceiling: none -- the stated target is unreachable at any load"
        )

    lines.append("")
    lines.append("assumptions carried by every number above:")
    for name, text in MODEL_ASSUMPTIONS.items():
        lines.append(f"  - {name}: {text}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Exit 0 on a result, 2 on a request that cannot be evaluated."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        stream=sys.stderr,
    )

    try:
        if args.utilization is not None:
            utilization = args.utilization
        else:
            utilization = args.arrival_rate * args.service_time / args.servers
            _LOG.debug(
                "derived utilization %.6f from arrival rate %.6g", utilization, args.arrival_rate
            )
        model = QueueModel(
            servers=args.servers,
            service_time=args.service_time,
            utilization=utilization,
            cv_squared=args.cv_squared,
        )
        ceilings = _ceilings(model, args)
    except QueueingError as exc:
        _LOG.error("%s", exc)
        return 2

    if args.json:
        payload = model.as_dict()
        payload["ceilings"] = ceilings
        payload["assumptions"] = MODEL_ASSUMPTIONS
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print(_render(model, ceilings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
