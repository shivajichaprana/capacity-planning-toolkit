# capacity-planning-toolkit

Capacity and headroom analysis: demand forecasting, saturation modelling, scaling
recommendations.

Most capacity decisions are made from a dashboard glance and a gut feeling about
next quarter. This toolkit replaces that with an auditable pipeline: measured
demand goes in, a forecast with an explicit confidence interval comes out, the
forecast is pushed through a saturation model of the serving system, and the
result is a headroom statement you can defend in a review — "this tier runs out
of headroom in about eleven weeks, and here is the arithmetic".

Nothing here guesses at your infrastructure. Every input is a metric you already
emit, every assumption is written down in the model that uses it, and every
recommendation carries the numbers it was derived from.

## Documentation

| Read this | For |
|---|---|
| This file | What the commands are, how to run them, and how to read a review |
| [docs/capacity-model.md](docs/capacity-model.md) | Why each number is computed the way it is, and what the obvious alternative gets wrong — with a reproducible worked example |
| [docs/assumptions.md](docs/assumptions.md) | Every assumption, where it is declared, what it defaults to, which direction it fails in, and how to check it |
| `make help` | Every target, with the variables the analysis targets read |

If you are evaluating a single review someone handed you, start with
[how to audit a review](docs/assumptions.md#how-to-audit-a-review).

## What it does

| Capability | Where |
|---|---|
| Pull utilisation and throughput metrics from CloudWatch into a normalised series | `ingest/cloudwatch.py` |
| Pull daily amortised spend and usage quantities from Cost Explorer | `ingest/cost.py` |
| Persist and query observation history locally, de-duplicated and typed | `ingest/store.py` |
| Seasonal shape estimation, with the mode chosen rather than assumed | `models/seasonality.py` |
| Demand forecasting with intervals that widen with the horizon | `models/forecast.py` |
| Closed-form queueing arithmetic: ceilings, elasticity, server sizing | `models/queueing.py` |
| Utilisation distribution, limit provenance, and the headroom conversion | `models/saturation.py` |
| Run-out dates, decide-by dates and fleet sizing | `recommend/headroom.py` |
| What-if comparison of interventions against doing nothing | `recommend/whatif.py` |
| Rendered capacity reviews, in Markdown and JSON | `reports/review.py` |

## Repository layout

```
ingest/      metric and cost collection, plus the local observation store
models/      seasonality, forecasting, queueing arithmetic, saturation analysis
recommend/   headroom arithmetic, fleet sizing, and what-if scenarios
reports/     the review generator; generated reviews are written here and ignored
tests/       checks for the models, the finding vocabulary, and the build
docs/        the capacity model and the stated assumptions
Makefile     every gate the pipeline runs, plus the analysis chain
```

## Design rules

These hold across every module and are worth stating once, because they are the
reason the output is trustworthy rather than merely plausible.

1. **Measurement and modelling are separate.** Ingestion writes observations and
   nothing else — no smoothing, no gap filling, no unit conversion beyond what
   the source API itself reports. Models read observations and may transform
   freely, because a model's transformations are inspectable and a collector's
   are not.
2. **An interval, never a point.** A single forecast number invites false
   precision. Every projection carries a lower and upper bound, and a
   recommendation derived from the upper bound says so.
3. **Assumptions are code, not lore.** Service time, arrival distribution,
   target utilisation and safety margin are declared parameters with documented
   defaults. If a number has to be assumed, the assumption is named in the
   output.
4. **Refuse rather than extrapolate.** Too few observations, too short a
   history, or a gap wider than the sampling period produces an explicit
   insufficient-data result. Silence about data quality is how capacity plans
   go wrong.
5. **A multiplier is reported with its basis.** "1.2x of headroom" means
   nothing until you know what it is 1.2x *of*. Every multiplier here is
   relative to the operating point, that basis is derived from the analysis
   rather than supplied beside it, and a basis that disagrees is refused rather
   than used. On a series that peaks at twice its mean, getting this wrong
   doubles every projected multiplier and nothing in the output looks odd.
6. **Local by default.** The store is a plain file. Nothing is written back to
   the account being measured, and read-only credentials are sufficient.

## Getting started

```bash
python3 -m venv .venv && . .venv/bin/activate
make install          # or: pip install -e '.[dev]'
make check            # every gate the pipeline runs
```

Collection needs read-only access to CloudWatch and Cost Explorer. Credentials
are resolved by the standard provider chain — environment, shared config file,
or an instance or task role — and are never read from this repository.

```bash
# Collect two weeks of CPU utilisation for a service into the local store
python -m ingest.cloudwatch \
  --namespace AWS/ECS \
  --metric CPUUtilization \
  --dimension ServiceName=checkout \
  --dimension ClusterName=production \
  --statistic Average \
  --period 3600 \
  --lookback-hours 336 \
  --store ./data/capacity.sqlite

# Collect amortised daily spend for the same window, grouped by service
python -m ingest.cost \
  --lookback-days 14 \
  --granularity DAILY \
  --metric AmortizedCost \
  --group-by SERVICE \
  --store ./data/capacity.sqlite
```

Both commands are idempotent: re-running over an overlapping window updates the
observations it already holds rather than duplicating them.

## Make targets

The gate targets run the same command strings the pipeline runs, character for
character, so a clean `make check` means what a green run means.
`tests/test_makefile.py` compares the two files in both directions and fails
when they drift — including when a gate is added to the pipeline with no target
here.

| Target | Does |
|---|---|
| `make help` | List every target and the variables the analysis targets read |
| `make install` | Install the project and its check dependencies into the active environment |
| `make check` | Run every gate: tests, typing, lint, compile, the import boundary |
| `make test` | The model suite |
| `make typing` | `mypy` over every declared package, under the strict settings |
| `make lint` | The style contract declared in `setup.cfg` |
| `make compile` | Confirm every committed module compiles |
| `make analysis-imports` | Confirm the analysis layer imports with no cloud SDK present |
| `make ingest-metrics` | Collect a utilisation or throughput series into the store |
| `make ingest-cost` | Collect amortised spend and usage quantities into the store |
| `make forecast` | Project the series, with an interval that widens with the horizon |
| `make saturation` | The operating point, the ceiling, and the usable headroom |
| `make recommend` | Fleet size, run-out date and decide-by date |
| `make review` | The whole review: decision, evidence, options, findings |
| `make clean` | Remove tooling caches; never the store or generated reviews |

The analysis targets read the variables declared at the top of the `Makefile`
and are meant to be overridden on the command line, which is how the series,
the limit and the fleet stay consistent between the forecast you looked at and
the recommendation you acted on:

```bash
make review \
  NAMESPACE=AWS/ECS METRIC=CPUUtilization \
  DIMENSIONS='--dimension ClusterName=production --dimension ServiceName=checkout' \
  LIMIT=100 LIMIT_UNIT=Percent LIMIT_PROVENANCE=hard \
  SERVICE_TIME=0.08 RESPONSE_BUDGET=0.25 \
  UNITS=12 UNIT_NAME=task POOLING=shared UNIT_MONTHLY_COST=42 \
  LEAD_TIME_HOURS=48 HORIZON=336 TIMEZONE=Europe/Amsterdam
```

`make analysis-imports` installs nothing on purpose: the gate is that the
analysis layer needs only the standard library, and a target that installed the
SDK first would prove the opposite of what it claims.

## Forecasting demand

Demand is seasonal and a forecast that ignores the shape reports Tuesday's
trend on Sunday's data, so `models/seasonality.py` estimates the shape and
`models/forecast.py` projects what is left. Both are pure functions of the
observations they are given: nothing reaches an API, nothing is written back.

```bash
python -m models.forecast \
  --namespace AWS/ECS \
  --metric CPUUtilization \
  --dimension ServiceName=checkout \
  --dimension ClusterName=production \
  --statistic Average \
  --horizon 168 \
  --level 0.9 \
  --timezone Europe/Amsterdam
```

Exit status is `0` for a forecast, `1` for a refusal with the reason on stderr,
and `2` for a request that could not be run at all. A refusal is a result, not a
failure: too little history, or a gap wider than the sampling period, produces
an explicit `F2xx` finding rather than a number nobody should rely on.

### The seasonal model

| Decision | What this does | Why not the obvious thing |
|---|---|---|
| Phase comes from the clock | A point's phase is derived from its own timestamp | Bucketing the `i`-th sample into `i % lag` is correct only while the series has no holes; one missing sample rotates the whole profile |
| A daily cycle follows local time | Phase is local time of day, weekly is local weekday plus time of day | Two instants 24 hours apart are not at the same point in a human demand cycle across a daylight-saving change |
| The profile is normalised | Additive profiles sum to zero, multiplicative profiles average one | An un-normalised profile carries part of the level, which the trend then counts a second time |
| Mode is chosen by amplitude against level | Correlation of each cycle's range with its mean | Additive and multiplicative residuals live on different scales, so their variances cannot be ranked against each other |
| Three cycles to choose a mode | Below three, additive is assumed and stated | A correlation over two cycles is ±1 whatever the data |

Seasonal strength is the share of detrended variance the profile explains. Below
10% no profile is fitted: it would spend degrees of freedom describing noise.

### Intervals

A forecast interval is a claim about frequency, and the usual way one is wrong is
that it is too narrow. Three things keep it honest.

**It widens with the horizon.** Each model carries its own closed-form error
growth rather than reusing the spread of one-step residuals at every horizon:

```
random walk with drift   sd(h) = sigma * sqrt( h * (1 + h/m) )
linear trend             sd(h) = sigma * sqrt( 1 + 1/n + (x_h - xbar)^2 / Sxx )
```

For the drift model the first term accumulates `h` independent steps and the
second is the drift estimate's own error acting over `h` of them, where `m` is
the number of differences it was estimated from. For the trend model the terms
are residual spread, uncertainty in the fitted level, and uncertainty in the
slope — which grows with distance from the centre of the fitted window.

**Degrees of freedom are charged for the seasonal profile.** A profile at lag
`L` estimates `L - 1` free parameters. Dividing the residual sum of squares by
`n - 2` on a weekly profile over hourly data uses 502 degrees of freedom where
335 were left, which understates `sigma` — and therefore every interval — by
about a fifth. `sigma_understatement_if_naive` in the JSON output reports that
factor for the model actually fitted.

**Coverage is measured, not asserted.** The last stretch of history is held
back, every candidate forecasts it, and the proportion of held-back actuals that
landed inside the predicted band is reported next to the nominal level. A band
covering 61% of a 90% holdout is reported as such. Seasonality is re-estimated
from the training window alone, so a seasonal candidate is never scored against
data it has already seen.

The one-sided question is reported separately. "How high could this get" wants a
bound, not an interval, and the upper end of a two-sided 90% interval is the
one-sided bound at 95% — so `upper` and `upper_one_sided` are both published
rather than left for a reader to conflate.

### Findings

Every result carries its findings. `error` means no forecast was produced.

| Code | Severity | Meaning |
|---|---|---|
| F100 | error | Series is empty |
| F101 | note | Cycle is not a whole number of sampling periods, so it is not representable |
| F102 | note | Too little history for a profile at this lag |
| F103 | note | Additive assumed: a non-positive value makes a multiplicative factor meaningless |
| F104 | note | Additive assumed: fewer than three complete cycles, so the mode test is unavailable |
| F105 | note | Additive assumed: per-cycle level or amplitude is constant |
| F106 | note | Multiplicative: amplitude tracks level |
| F107 | note | Additive: amplitude does not track level |
| F108 | warning | Points off the sampling grid were excluded; the series mixes resolutions |
| F109 | note | A phase has no detrended observation, usually a hole wider than the trend window |
| F110 | warning | At least one phase rests on a single observation |
| F111 | note | Seasonality selected, with cycle, strength and mode |
| F112 | note | No seasonality modelled; the strongest cycle is below the strength floor |
| F113 | note | The span crosses a UTC-offset change in the stated zone |
| F114 | warning | The profile is saturated: as many phases as observations, so no strength can be demonstrated |
| F200 | error | No observations to fit |
| F201 | error | Coverage gap wider than the sampling period |
| F202 | warning | Forecasting over a gap because holes were explicitly allowed |
| F203 | error | Fewer observations than any model is fitted to |
| F204 | error | No candidate model is estimable from this history |
| F205 | note | Newest observation dropped: its period has not closed and the statistic is biased |
| F206 | note | Newest observation kept: its period has not closed but the statistic is not displaced |
| F207 | error | Series has no sampling period, so it has no grid |
| F300 | note | Model selected on held-back history, with every candidate's error |
| F301 | warning | No history could be held back, so the model was not compared out of sample |
| F302 | warning | The held-back window is too short to separate the candidates |
| F303 | note | A candidate was not estimable and was dropped |
| F304 | warning | Few residual degrees of freedom, so `sigma` is itself a weak estimate |
| F305 | warning | Horizon exceeds half the fitted history |
| F400 | note | Interval basis: multiplier, degrees of freedom, and the one-sided level |
| F401 | warning | Measured coverage on held-back history is below the nominal level |
| F402 | warning | Residuals are autocorrelated, so the interval is a lower bound |
| F403 | note | The lower bound was clipped at zero, so stated coverage fails on that side |
| F404 | warning | Residual spread is effectively zero: the model reproduced the history |
| F405 | note | Multiplicative seasonality, so interval width scales with the phase factor |
| F406 | note | The seasonal profile is treated as known, so profile error is excluded |
| F407 | note | Degrees of freedom charged for the profile, with the factor a naive count would miss |
| F408 | note | Measured coverage on held-back history, at or above the nominal level |

The table above is the forecasting vocabulary, and it is tabulated here because
nothing else describes it. The other four ranges are registered in code and
rendered into every review beside the finding itself, so a reader never has to
resolve one against a document:

| Range | Concern | Where it is described |
|---|---|---|
| `F1xx` | Seasonal structure — whether a cycle is representable, estimable and real | the table above |
| `F2xx` | Input adequacy — history length, coverage, grid regularity | the table above |
| `F3xx` | Model selection — which candidate won and how weak the comparison was | the table above |
| `F4xx` | Interval honesty — every reason a reported interval is narrower than the truth | the table above |
| `F5xx` | Saturation evidence — the limit, the percentiles, the dilution, the exceedance runs | `models.saturation.FINDING_CODES`, rendered with each finding |
| `F6xx` | Queueing applicability — every measured departure from the assumptions the ceiling rests on | `models.saturation.FINDING_CODES`, rendered with each finding |
| `F7xx` | Sizing and timing — provenance, run-out, decide-by, quantisation, pooling | `recommend.headroom.FINDING_CODES`, rendered with each finding |
| `F8xx` | Scenario applicability — what each intervention holds fixed and over-promises | `recommend.whatif.FINDING_CODES`, rendered with each finding |

The prefixes are reserved per concern so that a later module cannot take a range
already in use, and `tests/test_finding_codes.py` enforces the reservations,
the registries, this table and the renderer's remedy map against each other in
both directions.

## Reading a review

`reports/review.py` renders the whole analysis — decision, evidence, options and
findings — as Markdown and JSON side by side. The renderer computes nothing:
every number in it is a field of a result object, formatted. So if a figure in a
review is wrong, either an input was wrong or a model is wrong; the report
cannot have introduced it.

```bash
make review UNITS=12 LIMIT=100 LIMIT_PROVENANCE=hard \
  NAMESPACE=AWS/ECS METRIC=CPUUtilization REVIEW_OUT=reports
```

The sections, and what each is for:

| Section | What to take from it |
|---|---|
| **Decision** | The action, the fleet size, the run-out date and the decide-by date. The least informative section, and the one most readers stop at |
| **Evidence — the limit** | The denominator of every utilisation figure, and where it came from. `observed` provenance means there is no limit here, only a lower bound on one |
| **Evidence — the operating point** | The utilisation distribution, the percentile planned to, and whether the sample can resolve it |
| **Evidence — time above the threshold** | Sustained runs, not just a count of samples. One long run and many short ones are different problems |
| **Evidence — headroom** | The ceiling, the absorbable demand, and the linear reading beside it |
| **Evidence — do the queueing assumptions hold** | Measured burstiness, clustering, trend and variability. A "no" here makes the ceiling optimistic and the fleet size a lower bound |
| **Evidence — the forecast** | Method, interval basis, measured coverage on held-back history, and every candidate's score |
| **Options** | Alternatives against doing nothing, each with what it holds fixed. They do not add |
| **Findings** | Grouped by severity, errors first, each with the remedy where one exists |

### Audit it in this order

Each step makes the next one meaningful, and the headline is last on purpose:

1. **The limit's provenance.** `observed` invalidates everything downstream as a
   bound rather than a figure.
2. **The resolvable percentile against the requested one.** If the window cannot
   resolve the operating percentile, the operating point is the largest few
   samples.
3. **Stationarity.** `F603` means the window has no single operating point.
   Shorten it and re-run before reading anything else.
4. **Measured interval coverage against the nominal level.** `F401` means the
   band does not do what it claims on this series' own recent history.
5. **Whether the decision is interval-driven.** `F711` means more history may be
   cheaper than more capacity.
6. **The decide-by date against today.** `F712` is the one error about the
   calendar rather than the data, and re-running does not clear it.
7. **Then the headline.**

### Exit status

Every entry point uses the same three-value vocabulary, so a pipeline step can
branch on it:

| Status | Means | Typical cause |
|---|---|---|
| `0` | A result was produced | — |
| `1` | A refusal, with findings on stdout explaining it | Too little history, a coverage gap, a percentile the sample cannot resolve, no ceiling to plan against |
| `2` | The request could not be run at all | A series not in the store, an unreadable store, a contradictory argument |

A refusal is a result, not a failure: `1` means the toolkit declined to produce
a number nobody should rely on, and said why. Treat `1` as "read the findings"
and `2` as "fix the command".

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `CAPACITY_STORE_PATH` | `./data/capacity.sqlite` | Observation store location |
| `CAPACITY_LOG_LEVEL` | `INFO` | Log verbosity for all entry points |
| `CAPACITY_LOG_FORMAT` | `text` | `text` for humans, `json` for log pipelines |
| `AWS_REGION` | — | Region for the metric and cost clients |
| `AWS_PROFILE` | — | Shared-config profile, when not using a role |

## Contributing

Run `make check` before opening a pull request; it is every gate the pipeline
runs. Open a Discussion in the repo or comment on the PR. Report a security
concern through a private advisory:
<https://github.com/shivajichaprana/capacity-planning-toolkit/security/advisories/new>

## License

MIT — see [LICENSE](LICENSE).
