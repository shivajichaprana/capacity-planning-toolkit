# Checks

Run the suite from the repository root:

```bash
pip install -e ".[dev]"
python -m pytest
```

`pytest` reads its settings from `pyproject.toml`: `tests/` is the only
collected directory, markers are strict, and a `DeprecationWarning` is an
error. The root `conftest.py` is what makes `import models` work from a plain
checkout without installing the project first.

| File | What a failure there means |
|---|---|
| `test_queueing.py` | The closed-form queueing arithmetic changed, or a refusal became an approximation |
| `test_seasonality.py` | Phase, the trend window, or one of the three gates that decide what is seasonal changed |
| `test_forecast.py` | The central line, the interval's growth with the horizon, or a refusal changed |
| `test_saturation.py` | The limit's handling, the percentile convention, sustained-time measurement, or the headroom conversion changed |
| `test_finding_codes.py` | The finding vocabulary drifted apart from its registries, the README table, or the remedy map |
| `support.py` | Not a check: the series every other file is run against |

## How these checks are chosen

The modules under test do not often crash. What they do, when something is
wrong, is return a plausible number -- and a plausible wrong ceiling is what a
capacity plan then gets built on. So the suite is weighted toward three things
that a reading of the code would not catch.

**Closed-form identities rather than tolerances.** Where the literature states
a result, it is asserted exactly at several operating points: Erlang B's
single- and two-server forms, Erlang C derived from it, the M/M/2 response and
elasticity identities, the drift and trend interval scalings, `target /
(1 + target)` for the elasticity ceiling, and the published Student-t critical
values. Two of these carry more weight than the rest. The M/M/2 elasticity
identity is the only check that would notice if the numerical derivative behind
several-server elasticity drifted, and the two interval scalings are the only
checks that would notice if one model's error growth were replaced by the
other's shape.

**Discrimination, not just refusal.** A gate that rejects a bad input is only
useful if it also admits a good one, so the gates are checked in both
directions: stationary noise is refused at five seeds while a real weekly step
through comparable noise is still found; a profile with as many phases as
observations is refused while the same cycle with a cycle more of history is
accepted. A one-directional check would pass with the gate wired shut.

**Every refusal is a check.** Each place these modules return `None`, or refuse
with an error finding, is a place where returning a number would have been a
fabrication: a percentile finer than the sample resolves, a sojourn quantile
for a server pool, a response budget below the service time, a series that
exceeds its own declared limit. Those paths are exercised deliberately, because
the pressure over time is always to replace them with a default.

## Determinism

Every noisy series comes from a seeded generator, and no check depends on wall
clock time or on the host's locale. A check that fails one run in twenty is
worse than no check: it teaches a reader to re-run the suite instead of reading
it. The one conditional skip is the time-zone check, which needs a time-zone
database and says so when it is absent.

## Fixtures carry real coverage

`support.py` builds series through the store's own coverage helper rather than
writing a `Coverage` by hand. Coverage is the first gate every model applies,
so a fixture whose coverage was derived differently would exercise a series
shape that production never produces -- and the refusal paths, which are the
most valuable thing these models have, would be tested against a fiction.
