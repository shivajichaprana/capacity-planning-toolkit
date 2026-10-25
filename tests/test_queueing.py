"""Checks on the queueing engine.

The engine is pure arithmetic, so almost every check here is an identity rather
than a tolerance: a closed form the literature states, evaluated at several
operating points, compared against what the module returns. That is the only
honest way to test this layer. A regression in queueing arithmetic does not
raise and does not look wrong -- it returns a plausible number, and a plausible
wrong ceiling is what a capacity plan is then built on.

Two of the identities do more work than the rest. The M/M/2 closed forms pin
the ERLANG path and, because elasticity for several servers is computed by a
central difference in log space, the M/M/2 elasticity identity is what
establishes that the numerical derivative is right -- there is no other check
in the suite that would notice if it drifted. And the refusals matter as much
as the formulas: every place this module returns ``None`` instead of a number
is a place where returning a number would have been a fabrication.
"""

from __future__ import annotations

import json
import math

import pytest

from models.queueing import (
    MAX_UTILIZATION,
    MODEL_ASSUMPTIONS,
    QueueingError,
    QueueModel,
    erlang_b,
    erlang_c,
    little_law_residual,
    servers_for_wait_budget,
    utilization_for_elasticity,
    utilization_for_mean_response,
    utilization_for_mean_wait,
    utilization_for_wait_quantile,
)

LOADS = (0.1, 0.5, 0.9, 1.7, 4.3)
UTILIZATIONS = (0.1, 0.25, 0.5, 0.75, 0.9, 0.95)


# ---------------------------------------------------------------------------
# Erlang B and C
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("load", LOADS)
def test_erlang_b_single_server_closed_form(load: float) -> None:
    assert erlang_b(1, load) == pytest.approx(load / (1.0 + load))


@pytest.mark.parametrize("load", LOADS)
def test_erlang_b_two_server_closed_form(load: float) -> None:
    expected = load * load / (2.0 + 2.0 * load + load * load)
    assert erlang_b(2, load) == pytest.approx(expected)


def test_erlang_b_is_a_probability_and_decreases_with_servers() -> None:
    previous = 1.0
    for servers in range(1, 40):
        blocking = erlang_b(servers, 8.0)
        assert 0.0 <= blocking <= 1.0
        assert blocking < previous
        previous = blocking


def test_erlang_b_survives_a_server_count_a_factorial_would_not() -> None:
    # The recursion keeps every intermediate value a probability. The factorial
    # form overflows a float somewhere around 170 servers, so this is the check
    # that the stable route is the one in use.
    blocking = erlang_b(5_000, 4_500.0)
    assert math.isfinite(blocking)
    assert 0.0 < blocking < 1.0


def test_erlang_b_zero_load_blocks_nothing() -> None:
    assert erlang_b(8, 0.0) == 0.0


@pytest.mark.parametrize("utilization", UTILIZATIONS)
def test_erlang_c_single_server_equals_utilization(utilization: float) -> None:
    # With one server every arrival that finds it busy waits, and it is busy a
    # rho fraction of the time.
    assert erlang_c(1, utilization) == pytest.approx(utilization)


@pytest.mark.parametrize("utilization", UTILIZATIONS)
def test_erlang_c_two_server_closed_form(utilization: float) -> None:
    expected = 2.0 * utilization**2 / (1.0 + utilization)
    assert erlang_c(2, 2.0 * utilization) == pytest.approx(expected)


def test_erlang_c_refuses_a_saturated_system() -> None:
    with pytest.raises(QueueingError, match="no steady state"):
        erlang_c(2, 2.0)


def test_erlang_inputs_are_validated() -> None:
    with pytest.raises(QueueingError, match="servers must be at least 1"):
        erlang_b(0, 1.0)
    with pytest.raises(QueueingError, match="non-negative"):
        erlang_b(1, -0.5)
    with pytest.raises(QueueingError, match="servers must be at least 1"):
        erlang_c(0, 1.0)


# ---------------------------------------------------------------------------
# The operating point
# ---------------------------------------------------------------------------


def test_operating_point_is_validated() -> None:
    with pytest.raises(QueueingError, match="servers must be at least 1"):
        QueueModel(servers=0, service_time=1.0, utilization=0.5, cv_squared=1.0)
    with pytest.raises(QueueingError, match="service time must be positive"):
        QueueModel(servers=1, service_time=0.0, utilization=0.5, cv_squared=1.0)
    with pytest.raises(QueueingError, match="no steady state"):
        QueueModel(servers=1, service_time=1.0, utilization=1.0, cv_squared=1.0)
    with pytest.raises(QueueingError, match="cv_squared must be non-negative"):
        QueueModel(servers=1, service_time=1.0, utilization=0.5, cv_squared=-1.0)


def test_general_service_time_is_refused_for_several_servers() -> None:
    # There is no exact M/G/c result, and the quiet alternative -- applying the
    # single-server variability factor to a several-server formula -- is wrong
    # in the direction that under-states delay.
    with pytest.raises(QueueingError, match="single server"):
        QueueModel(servers=4, service_time=1.0, utilization=0.5, cv_squared=4.0)


def test_discipline_routing() -> None:
    assert QueueModel(servers=1, service_time=1.0, utilization=0.5,
                      cv_squared=1.0).discipline == "mm1"
    assert QueueModel(servers=1, service_time=1.0, utilization=0.5,
                      cv_squared=0.0).discipline == "mg1"
    assert QueueModel(servers=3, service_time=1.0, utilization=0.5,
                      cv_squared=1.0).discipline == "mmc"


@pytest.mark.parametrize("utilization", UTILIZATIONS)
def test_single_server_means(utilization: float) -> None:
    model = QueueModel(servers=1, service_time=0.2, utilization=utilization, cv_squared=1.0)
    assert model.mean_wait == pytest.approx(0.2 * utilization / (1.0 - utilization))
    assert model.mean_response == pytest.approx(0.2 / (1.0 - utilization))
    assert model.response_inflation == pytest.approx(1.0 / (1.0 - utilization))
    assert model.arrival_rate == pytest.approx(utilization / 0.2)
    assert model.offered_load == pytest.approx(utilization)


@pytest.mark.parametrize("utilization", UTILIZATIONS)
def test_single_server_queue_length_follows_little(utilization: float) -> None:
    model = QueueModel(servers=1, service_time=0.5, utilization=utilization, cv_squared=1.0)
    assert model.mean_queue_length == pytest.approx(utilization**2 / (1.0 - utilization))
    assert model.mean_concurrency == pytest.approx(utilization / (1.0 - utilization))


@pytest.mark.parametrize("utilization", UTILIZATIONS)
def test_two_server_response_closed_form(utilization: float) -> None:
    model = QueueModel(servers=2, service_time=1.5, utilization=utilization, cv_squared=1.0)
    assert model.mean_response == pytest.approx(1.5 / (1.0 - utilization**2))
    assert model.mean_wait == pytest.approx(1.5 * utilization**2 / (1.0 - utilization**2))


@pytest.mark.parametrize("utilization", UTILIZATIONS)
def test_single_server_elasticity_closed_form(utilization: float) -> None:
    model = QueueModel(servers=1, service_time=1.0, utilization=utilization, cv_squared=1.0)
    assert model.elasticity == pytest.approx(utilization / (1.0 - utilization))


@pytest.mark.parametrize("utilization", UTILIZATIONS)
def test_two_server_elasticity_matches_its_closed_form(utilization: float) -> None:
    # This is the check that validates the numerical derivative: for M/M/c the
    # module differentiates mean response in log space, and M/M/2 is the only
    # several-server case with a closed form to compare it against.
    model = QueueModel(servers=2, service_time=1.0, utilization=utilization, cv_squared=1.0)
    expected = 2.0 * utilization**2 / (1.0 - utilization**2)
    assert model.elasticity == pytest.approx(expected, rel=1e-6)


def test_idle_operating_point_has_no_delay_and_no_elasticity() -> None:
    model = QueueModel(servers=4, service_time=1.0, utilization=0.0, cv_squared=1.0)
    assert model.mean_wait == 0.0
    assert model.elasticity == 0.0
    assert model.wait_quantile(0.99) == 0.0


@pytest.mark.parametrize("utilization", UTILIZATIONS)
def test_deterministic_service_halves_the_queue(utilization: float) -> None:
    exponential = QueueModel(servers=1, service_time=1.0, utilization=utilization,
                             cv_squared=1.0)
    deterministic = QueueModel(servers=1, service_time=1.0, utilization=utilization,
                               cv_squared=0.0)
    assert deterministic.variability_factor == pytest.approx(0.5)
    assert deterministic.mean_wait == pytest.approx(0.5 * exponential.mean_wait)


@pytest.mark.parametrize("utilization", UTILIZATIONS)
def test_high_variability_multiplies_the_queue(utilization: float) -> None:
    # cv2 = 9 is a heavy-tailed but entirely ordinary service distribution, and
    # it quintuples queueing delay at every load. Defaulting cv2 to 1 is the
    # most expensive silent assumption this module could make.
    exponential = QueueModel(servers=1, service_time=1.0, utilization=utilization,
                             cv_squared=1.0)
    variable = QueueModel(servers=1, service_time=1.0, utilization=utilization,
                          cv_squared=9.0)
    assert variable.variability_factor == pytest.approx(5.0)
    assert variable.mean_wait == pytest.approx(5.0 * exponential.mean_wait)


# ---------------------------------------------------------------------------
# Tails
# ---------------------------------------------------------------------------


def test_wait_quantile_is_zero_inside_the_atom() -> None:
    # At 30% utilization 70% of arrivals never queue, so every quantile up to
    # p70 is exactly zero -- not a small positive number.
    model = QueueModel(servers=1, service_time=1.0, utilization=0.3, cv_squared=1.0)
    assert model.wait_quantile(0.5) == 0.0
    assert model.wait_quantile(0.7) == 0.0
    above_the_atom = model.wait_quantile(0.71)
    assert above_the_atom is not None and above_the_atom > 0.0


@pytest.mark.parametrize("probability", (0.95, 0.99, 0.999))
def test_wait_quantile_tail_closed_form(probability: float) -> None:
    model = QueueModel(servers=1, service_time=0.4, utilization=0.8, cv_squared=1.0)
    drain = (1.0 - 0.8) / 0.4
    expected = math.log(0.8 / (1.0 - probability)) / drain
    assert model.wait_quantile(probability) == pytest.approx(expected)


@pytest.mark.parametrize("probability", (0.5, 0.9, 0.99))
def test_response_quantile_closed_form_for_one_server(probability: float) -> None:
    model = QueueModel(servers=1, service_time=0.25, utilization=0.6, cv_squared=1.0)
    expected = -math.log1p(-probability) * 0.25 / (1.0 - 0.6)
    assert model.response_quantile(probability) == pytest.approx(expected)


def test_tails_are_refused_where_no_formula_exists() -> None:
    general = QueueModel(servers=1, service_time=1.0, utilization=0.5, cv_squared=4.0)
    assert general.wait_quantile(0.99) is None
    assert general.response_quantile(0.99) is None

    # For several servers the sojourn distribution is a mixture, and adding a
    # mean service time to a wait quantile does not produce its percentile.
    pooled = QueueModel(servers=3, service_time=1.0, utilization=0.5, cv_squared=1.0)
    assert pooled.wait_quantile(0.99) is not None
    assert pooled.response_quantile(0.99) is None


def test_quantile_probabilities_are_validated() -> None:
    model = QueueModel(servers=1, service_time=1.0, utilization=0.5, cv_squared=1.0)
    for probability in (0.0, 1.0, -0.1, 1.2):
        with pytest.raises(QueueingError, match="probability must be in"):
            model.wait_quantile(probability)
        with pytest.raises(QueueingError, match="probability must be in"):
            model.response_quantile(probability)


# ---------------------------------------------------------------------------
# Inverses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target", (0.5, 1.0, 4.0, 9.0, 19.0))
def test_elasticity_inverse_closed_form_for_one_server(target: float) -> None:
    # target / (1 + target): elasticity 1 at 50%, 4 at 80%, 9 at 90%. The
    # planning rules of thumb are this identity and nothing more.
    found = utilization_for_elasticity(target)
    assert found is not None
    assert found == pytest.approx(target / (1.0 + target), abs=1e-9)


@pytest.mark.parametrize("target", (1.0, 4.0))
@pytest.mark.parametrize("servers", (2, 8, 32))
def test_pooling_raises_the_elasticity_ceiling(target: float, servers: int) -> None:
    # The whole argument for a shared fleet: at a fixed exchange rate between
    # growth and latency, more servers may be run hotter.
    single = utilization_for_elasticity(target)
    pooled = utilization_for_elasticity(target, servers=servers)
    assert single is not None and pooled is not None
    assert pooled > single


@pytest.mark.parametrize("budget", (0.05, 0.5, 5.0))
def test_mean_wait_inverse_is_consistent_with_the_forward_formula(budget: float) -> None:
    found = utilization_for_mean_wait(budget, service_time=0.2)
    assert found is not None
    model = QueueModel(servers=1, service_time=0.2, utilization=found, cv_squared=1.0)
    assert model.mean_wait == pytest.approx(budget, rel=1e-6)


def test_response_inverse_refuses_a_budget_below_the_service_time() -> None:
    # No amount of spare capacity makes a request faster than its own service
    # time, so the honest answer is that this is a sizing problem.
    assert utilization_for_mean_response(0.1, service_time=0.5) is None
    found = utilization_for_mean_response(1.0, service_time=0.5)
    assert found is not None
    model = QueueModel(servers=1, service_time=0.5, utilization=found, cv_squared=1.0)
    assert model.mean_response == pytest.approx(1.0, rel=1e-6)


def test_wait_quantile_inverse_is_consistent_with_the_forward_formula() -> None:
    found = utilization_for_wait_quantile(0.75, probability=0.99, service_time=0.1)
    assert found is not None
    model = QueueModel(servers=1, service_time=0.1, utilization=found, cv_squared=1.0)
    measured = model.wait_quantile(0.99)
    assert measured is not None
    assert measured == pytest.approx(0.75, rel=1e-6)


def test_a_budget_nothing_can_violate_returns_the_ceiling() -> None:
    found = utilization_for_mean_wait(1e12, service_time=1.0)
    assert found == pytest.approx(MAX_UTILIZATION)


def test_inverse_inputs_are_validated() -> None:
    with pytest.raises(QueueingError, match="non-negative"):
        utilization_for_mean_wait(-1.0)
    with pytest.raises(QueueingError, match="must be positive"):
        utilization_for_elasticity(0.0)


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("budget", (0.01, 0.1, 1.0))
def test_server_count_is_the_fewest_that_meets_the_budget(budget: float) -> None:
    arrival_rate = 9.0
    service_time = 1.0
    servers = servers_for_wait_budget(arrival_rate, service_time, budget)
    assert servers is not None

    offered_load = arrival_rate * service_time
    chosen = QueueModel(
        servers=servers,
        service_time=service_time,
        utilization=offered_load / servers,
        cv_squared=1.0,
    )
    assert chosen.mean_wait <= budget

    # Minimality, proved rather than asserted: one fewer server either
    # saturates or misses the budget.
    fewer = servers - 1
    assert fewer >= 1
    if offered_load / fewer >= 1.0:
        return
    worse = QueueModel(
        servers=fewer,
        service_time=service_time,
        utilization=offered_load / fewer,
        cv_squared=1.0,
    )
    assert worse.mean_wait > budget


def test_server_count_can_be_out_of_reach() -> None:
    # A budget tighter than the granularity one more server provides is a real
    # outcome, and None says so rather than returning the search ceiling.
    assert servers_for_wait_budget(1.0, 1.0, 1e-12, max_servers=3) is None


def test_server_count_can_be_driven_by_a_quantile_rather_than_a_mean() -> None:
    mean_sized = servers_for_wait_budget(9.0, 1.0, 0.5)
    tail_sized = servers_for_wait_budget(9.0, 1.0, 0.5, probability=0.99)
    assert mean_sized is not None and tail_sized is not None
    assert tail_sized >= mean_sized


def test_sizing_inputs_are_validated() -> None:
    with pytest.raises(QueueingError, match="arrival rate must be positive"):
        servers_for_wait_budget(0.0, 1.0, 1.0)
    with pytest.raises(QueueingError, match="service time must be positive"):
        servers_for_wait_budget(1.0, 0.0, 1.0)
    with pytest.raises(QueueingError, match="budget must be non-negative"):
        servers_for_wait_budget(1.0, 1.0, -1.0)


# ---------------------------------------------------------------------------
# Little's law
# ---------------------------------------------------------------------------


def test_little_law_residual_is_zero_when_the_identity_holds() -> None:
    assert little_law_residual(10.0, 2.0, 5.0) == pytest.approx(0.0)


def test_little_law_residual_is_signed() -> None:
    # Positive means measured concurrency exceeds what throughput and latency
    # imply; the sign is what points at which of the three is wrong.
    assert little_law_residual(10.0, 2.0, 4.0) == pytest.approx(0.2)
    assert little_law_residual(10.0, 2.0, 6.0) == pytest.approx(-0.2)


def test_little_law_residual_is_undefined_at_zero_concurrency() -> None:
    assert little_law_residual(0.0, 2.0, 5.0) is None


def test_little_law_inputs_are_validated() -> None:
    with pytest.raises(QueueingError, match="non-negative"):
        little_law_residual(-1.0, 1.0, 1.0)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_operating_point_renders_as_json() -> None:
    model = QueueModel(servers=2, service_time=0.3, utilization=0.7, cv_squared=1.0)
    rendered = model.as_dict(quantiles=(0.9, 0.99))
    json.dumps(rendered)
    assert rendered["discipline"] == "mmc"
    assert rendered["utilization"] == pytest.approx(0.7)
    assert rendered["mean_response"] == pytest.approx(model.mean_response)
    # The sojourn mixture has no quantiles for several servers, and the
    # rendered form says so with a null rather than omitting the key.
    assert rendered["response_quantiles"] == {"p90": None, "p99": None}


def test_every_assumption_is_stated_in_prose() -> None:
    assert MODEL_ASSUMPTIONS
    for name, text in MODEL_ASSUMPTIONS.items():
        assert name.islower() and " " not in name
        assert isinstance(text, str) and len(text) > 40


def test_both_refusals_carry_a_stated_assumption() -> None:
    # Each place this module returns None instead of a number is a modelling
    # boundary, and a boundary with no written reason is one a reader will
    # eventually route around. These two keys are those two refusals.
    assert "sojourn_quantile_several_servers" in MODEL_ASSUMPTIONS
    assert "general_service_tails" in MODEL_ASSUMPTIONS

    pooled = QueueModel(servers=3, service_time=1.0, utilization=0.5, cv_squared=1.0)
    general = QueueModel(servers=1, service_time=1.0, utilization=0.5, cv_squared=4.0)
    assert pooled.response_quantile(0.99) is None
    assert general.wait_quantile(0.99) is None
