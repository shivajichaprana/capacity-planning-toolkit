# The capacity model

This document is the reasoning behind the numbers. The README says what the
commands do; this says why each one computes what it computes, and — more
usefully — what the obvious alternative would have got wrong.

The whole toolkit exists to answer one question without lying about it:

> We are at *X*% utilisation. How much growth is left, when do we run out, and
> how much bigger does the fleet have to be?

Every step below is a place where a convenient reading of the data answers that
question with a number that is wrong in a direction nobody notices.

## Contents

- [The chain](#the-chain)
- [1. Utilisation is not headroom](#1-utilisation-is-not-headroom)
- [2. The operating point](#2-the-operating-point)
- [3. The interval](#3-the-interval)
- [4. The basis rule](#4-the-basis-rule)
- [5. Sizing](#5-sizing)
- [6. Options are alternatives](#6-options-are-alternatives)
- [A worked example](#a-worked-example)
- [What the model does not tell you](#what-the-model-does-not-tell-you)

## The chain

```
observations                 ingest/        writes what the API returned, nothing else
    |
    v
series + coverage            ingest/store   expected vs observed samples, largest gap
    |
    +--> seasonal shape      models/seasonality   cycle, mode, strength, phase support
    |        |
    |        v
    +--> forecast            models/forecast      interval that widens with the horizon
    |
    +--> operating point     models/saturation    planning percentile, limit, provenance
    |        |
    |        v
    |    ceiling             models/queueing      utilisation a latency target allows
    |
    v
recommendation               recommend/headroom   run-out date, decide-by date, fleet size
    |
    +--> options             recommend/whatif     alternatives against doing nothing
    |
    v
review                       reports/review       every number above, formatted, with findings
```

Each stage can refuse. A refusal is a result: it carries findings that say what
was insufficient, and it exits 1 rather than 0. The one thing no stage does is
substitute a weaker answer for the one it was asked for while keeping the label
of the stronger one.

## 1. Utilisation is not headroom

A resource at 80% utilisation does not have 25% growth left. It has almost
none, because queueing delay does not rise linearly with load — it rises with
`rho / (1 - rho)`, and that quantity is the *elasticity* of latency with
respect to demand: the percentage by which response time grows per percentage
of demand growth.

For a single server:

| Utilisation | Elasticity | Absorbable demand (ceiling 80%) | Linear reading |
|---:|---:|---:|---:|
| 30% | 0.43 | 2.67x | 3.33x |
| 50% | 1.00 | 1.60x | 2.00x |
| 70% | 2.33 | 1.14x | 1.43x |
| 80% | 4.00 | 1.00x | 1.25x |
| 90% | 9.00 | — past the ceiling | 1.11x |

So the model does not report "headroom" as `1 - rho`. It takes a declared
latency sensitivity — `--elasticity-target`, 4 by default, meaning *a percent of
demand growth may cost up to four percent of latency* — inverts it into the
utilisation that sensitivity permits, and reports the demand multiplier from the
operating point to that ceiling. The linear reading is printed next to it,
because the ratio between them is the size of the error.

That ratio is exactly the reciprocal of the ceiling:

```
linear / absorbable = (1 / rho) / (ceiling / rho) = 1 / ceiling
```

which is worth knowing because it means the overstatement does not depend on
where you are operating. It depends on how much latency sensitivity you
declared and on how many servers share the queue — and nothing else. A fleet
that reads utilisation as linear headroom is making the same proportional
mistake at 40% as at 85%; only the consequences differ.

### Pooling moves the ceiling

More servers sharing one queue sustain a higher utilisation at the same latency
sensitivity, because an arrival has more chances to find a free server:

| Servers | Ceiling at target 4 | Overstatement of the linear reading | Elasticity at 80% |
|---:|---:|---:|---:|
| 1 | 80.0% | 1.250x | 4.00 |
| 2 | 81.6% | 1.225x | 3.56 |
| 4 | 84.3% | 1.187x | 2.74 |
| 8 | 87.3% | 1.145x | 1.73 |
| 12 | 89.0% | 1.123x | 1.19 |
| 24 | 91.7% | 1.091x | 0.52 |
| 100 | 95.6% | 1.046x | 0.03 |

This is the entire economic argument for a shared pool, and it is also why
`--pooling` is a declared property rather than something inferred. A sharded
fleet is `n` independent single-server queues; its ceiling never moves off 80%
however many shards there are. No metric distinguishes the two, so the gain is
claimed only where it was declared, and a sharded fleet gets a finding
(`F723`) saying it was not claimed.

## 2. The operating point

Utilisation is a distribution, not a number, and four distinct corrections are
needed before a single number off it can be planned against.

**The mean is the wrong number.** A resource averaging 36% may spend an hour
each afternoon at 74%, and it is the hour that decides whether the service
stays up. The operating point is therefore an upper percentile of observed
utilisation (`--planning-percentile`, p99 by default). The mean is reported
beside it and the ratio — the peak-to-mean — is reported as the factor by which
planning to the mean under-provisions. *Provisioning is a statement about the
peak; only cost is a statement about the mean.*

**A percentile the sample cannot resolve is not a percentile.** Order
statistics resolve a percentile `p` only when the sample holds more than
`1 / (1 - p)` points: a p99 needs more than 100 samples, and the p99 of 50
samples is simply the largest of 50 samples. Rather than quietly substituting
it, the analysis reports the highest resolvable percentile and flags the
request (`F510`), and separately flags the case where the reported figure rests
on only the top two samples (`F511`).

**An averaging window hides the burst.** A series of hourly averages cannot
show a thirty-second spike; the spike is divided by the window before it
reaches the series. Every observed peak is therefore a *lower bound* on the
instantaneous peak, weaker the coarser the series. This is reported with the
period that caused it (`F605`), not treated as a record of peaks.

**A limit needs provenance.** Utilisation is a ratio and the denominator is an
assumption. `--limit-provenance` takes one of four values and they are not
interchangeable:

| Provenance | What it is | What it changes |
|---|---|---|
| `hard` | An architectural or hardware limit: core count, provisioned IOPS | Nothing; this is the case the arithmetic assumes |
| `quota` | A provider limit, raisable by asking | The remedy is a request, not a deploy (`F704`) |
| `configured` | A limit this service set on itself: pool size, thread count | Raisable by deploying, often into a downstream limit that is harder to see |
| `observed` | The largest value ever seen | **Not a limit.** It is a lower bound on one, so every utilisation computed against it is an upper bound (`F505`, `F703`) |

### Where the queueing model stops applying

The ceiling comes from a closed-form queueing result that assumes Poisson
arrivals, a stationary rate, an unbounded queue and independent service times.
A measured series satisfies none of those exactly. Rather than assume them, the
analysis *measures* the departures and reports each one, because all of them
fail in the same direction:

| Finding | Measured | Why it makes the ceiling optimistic |
|---|---|---|
| `F601` | Peak-to-mean above 2 | Burstier than Poisson; a wait computed from the mean rate is a lower bound |
| `F602` | Lag-one autocorrelation above 0.5 | Busy predicts busy, so load arrives in clusters and queues build further |
| `F603` | Trend above 25% of the mean across the window | No single stationary operating point; the analysis describes a load the resource is leaving |
| `F604` | No measured service-time variability | Delay scales with `(1 + cv²) / 2`; an assumed exponential understates a mixed-path service |
| `F605` | An averaging source statistic | The peak is diluted by the sampling window |

So a ceiling carrying any `F6xx` finding is an **upper bound on the safe
operating point**, and the recommendation says so in `F706`: the recommended
fleet size is then a *lower* bound on what is actually needed.

## 3. The interval

A capacity forecast is only as good as its interval, and the usual way one is
wrong is not that the central line is off — it is that the band around it is too
narrow, so a plan gets made with confidence the data does not support. Three
mechanisms keep it honest.

**It widens with the horizon.** Each model carries its own closed-form error
growth rather than reusing one-step residual spread at every horizon:

```
random walk with drift   sd(h) = sigma * sqrt( h * (1 + h/m) )
linear trend             sd(h) = sigma * sqrt( 1 + 1/n + (x_h - xbar)^2 / Sxx )
```

For the drift model the first term accumulates `h` independent steps and the
second is the drift estimate's own error acting over `h` of them, where `m` is
the number of differences it was estimated from. For the trend model the three
terms are residual spread, uncertainty in the fitted level, and uncertainty in
the slope — the last growing with distance from the centre of the fitted
window.

**Degrees of freedom are charged for the seasonal profile.** A profile at lag
`L` estimates `L - 1` free parameters. On three weeks of hourly data under a
weekly profile, dividing the residual sum of squares by `n - 2` uses 502
degrees of freedom where 335 remain — which understates `sigma`, and therefore
every interval, by about a fifth. The factor a naive count would have missed is
published as `sigma_understatement_if_naive` and reported in `F407`.

**Coverage is measured, not asserted.** An interval at the 90% level is a claim
about frequency, so the claim is tested: the last stretch of history is held
back, every candidate forecasts it, and the share of held-back actuals that
landed inside the band is reported next to the nominal level (`F401` below it,
`F408` at or above it). Seasonality is re-estimated from the training window
alone, so a seasonal candidate is never scored against data it has already
seen.

One-sided and two-sided are kept apart. "How high could this get" wants a
bound, and the upper end of a two-sided 90% interval *is* the one-sided 95%
bound — so both `upper` and `upper_one_sided` are published rather than left
for a reader to conflate. The run-out date is read off the one-sided bound,
because that is the question being asked.

## 4. The basis rule

This is the step that decides a fleet size, and the one where the error is
hardest to see.

The absorbable multiplier the saturation analysis reports — 1.21x, say — is a
multiplier **on the operating point**. The recommendation has to compare
projected demand against it, so it divides each projected value by a reference
level to get a multiplier on the same basis. Both quantities are bare numbers.
Neither carries its basis with it.

If the reference is the *window mean* rather than the level at the operating
point, every projected multiplier is inflated by exactly the peak-to-mean
ratio:

```
level at the operating point = planning percentile x limit
                             = window mean x peak-to-mean
```

On a series peaking at twice its mean, a projection that is really 1.0x of
today's demand reads as 2.05x. It crosses an absorbable 1.21x at the first
daily peak, every time, on every seasonal series — so the run-out date lands
within hours, the decide-by date is always already in the past, and the
recommendation is to buy capacity that is not needed. Nothing in the output
looks wrong; the answer is simply a different question's answer.

So the reference is **derived from the analysis** rather than passed alongside
it, and a reference supplied explicitly is checked against the derived one and
refused when the two disagree. An override may restate the basis; it cannot
change it. `F702` states in every result which level the multipliers are
relative to.

The residual imprecision is disclosed rather than engineered away: the crossing
is taken at the projected track's own peak, while the operating point is a
percentile of the observed window. The gap between a window's maximum and its
operating percentile — about 1.03x in the worked example below — is the margin
by which the run-out date comes out early. That is the conservative direction,
and `F702` reports the factor.

## 5. Sizing

Two properties of real fleets, both of which the obvious arithmetic gets wrong.

**Capacity is an integer.** Nobody provisions 1.37 of anything. Rounding up
forces achieved headroom past required headroom, and on a small fleet the
forced overshoot can exceed the growth being planned for. The quantisation is
reported as its own number (`F721`) so a reviewer can see how much of the bill
is demand and how much is arithmetic.

**Scaling out is not linear, and the non-linearity has a sign.** Because the
ceiling rises with the number of servers sharing a queue, the naive
`ceil(demand / per-unit capacity)` *over*-provisions a shared pool — the sizing
search reports the linear reading beside its own answer and the gap as `F722`.
The same arithmetic gives nothing to a sharded fleet, which gets `F723`
instead. The search itself is a scan from the current size upward, evaluating
the real ceiling at each candidate size, rather than a closed form — because
the ceiling is a function of the size being tested.

**The peak over the horizon, not the value at its end.** A projection is a
track, and a cyclical track ends wherever the horizon happens to stop. Sizing
against the last projected period makes the recommended fleet a function of that
accident: on the series below, the upper bound peaks at 1.15x but its last
period — a Sunday night in the weekly trough — reads 0.35x, a factor of 3.3
apart. Sizing to the horizon's end would recommend no change at all while the
same result reported a run-out date three days away, which is a review
contradicting itself in adjacent lines. So the planning target is
`max` over the horizon of the one-sided upper track, measured on the operating
point's basis — the same track the crossing is read off, which makes the two
statements consistent by construction: a crossing inside the horizon now
*implies* a size increase. Both figures are reported, because a reader
comparing a review against a graph needs to know which one they are looking at.

**A date, not a verdict.** "There is 1.10x of headroom" is not actionable.
"The upper bound reaches the ceiling in eleven days and provisioning takes
four" is. Every recommendation resolves to a run-out date and a decide-by date
(`run-out minus --lead-time-hours`), and a decide-by date already in the past is
an **error**, not a warning (`F712`). When the upper bound runs out in much less
than the time the central projection takes — or the central line never reaches
the ceiling at all — `F711` says so: the decision is being driven by interval
width rather than by measured growth, and more history may remove the need to
spend anything.

## 6. Options are alternatives

The options table compares each intervention against the counterfactual of
doing nothing. Three things about it are easy to get wrong:

- **They do not add** (`F803`). The gain from one option depends on the
  utilisation it starts from, so two options worth +0.4x each are not worth
  +0.8x together.
- **Each holds something fixed**, and the list is printed. A faster unit holds
  demand and unit count fixed; a demand change holds the fleet fixed. An option
  whose held-fixed assumption is the thing you are actually unsure about is not
  an option.
- **A scenario can exceed the evidence** (`F801`). A demand multiplier beyond
  anything the observations reached is an extrapolation, and the model says so
  rather than reporting a confident number about a load the service has never
  carried.

A few results are deliberately unflattering: a larger unit rarely divides
service time by its size factor (`F812`), fewer larger units concentrate the
failure domain (`F813`), raising a limit usually moves the bottleneck rather
than removing it (`F814`), and changing the elasticity target adds no capacity
at all — it is a policy change (`F815`).

## A worked example

The figures below are a real run, not an illustration. The series is synthetic
so that the example is reproducible without an account: six weeks of hourly CPU
utilisation for a service with a weekday business shape, a weekend trough, and a
drift that multiplies the underlying level by 1.25 across the window.

Generate it with the collector's own store API:

```python
import math, random
from datetime import datetime, timedelta, timezone
from ingest.store import ObservationStore, Observation, SeriesKey, canonical_entity

random.seed(20261026)
PERIOD, N = 3600, 6 * 7 * 24
end = datetime(2026, 10, 26, tzinfo=timezone.utc)
start = end - timedelta(seconds=PERIOD * N)
key = SeriesKey(
    source="cloudwatch", namespace="AWS/ECS", metric="CPUUtilization",
    statistic="Average",
    entity=canonical_entity({"ClusterName": "production", "ServiceName": "checkout"}),
)
points = []
for i in range(N):
    ts = start + timedelta(seconds=PERIOD * i)
    daily = (0.55
             + 0.45 * math.exp(-((ts.hour - 11) ** 2) / 18.0)
             + 0.50 * math.exp(-((ts.hour - 16) ** 2) / 12.0))
    weekly = 1.0 if ts.weekday() < 5 else 0.62
    level = 56.0 * daily * weekly * (1.0 + 0.00025 * i)
    points.append(Observation(key=key, timestamp=ts, unit="Percent", period=PERIOD,
                              value=max(1.0, level + random.gauss(0.0, 1.2))))

with ObservationStore("./data/capacity.sqlite") as store:
    print(store.write(points), "written")
```

Then render the review:

```bash
make review \
  NAMESPACE=AWS/ECS METRIC=CPUUtilization \
  DIMENSIONS='--dimension ClusterName=production --dimension ServiceName=checkout' \
  LIMIT=100 LIMIT_UNIT=Percent LIMIT_PROVENANCE=hard \
  SERVICE_TIME=0.08 RESPONSE_BUDGET=0.25 \
  UNITS=12 UNIT_NAME=task POOLING=shared UNIT_MONTHLY_COST=42 \
  LEAD_TIME_HOURS=48 HORIZON=336 TIMEZONE=Europe/Amsterdam \
  REVIEW_TITLE='Checkout capacity review'
```

### What it reports

| | |
|---|---|
| Observations | 1,008 hourly samples |
| Utilisation | mean 45.9%, median 42.0%, **p99 80.9%**, max 82.9% |
| Peak-to-mean | 1.76 |
| Highest resolvable percentile | p99.9 — so the p99 request is supportable |
| Time at or above 80% | 15 samples in 7 runs, longest 14,400s, 54,000s in total |
| Elasticity at the operating point | 1.32 |
| Binding ceiling | **89.0%** (elasticity 89.0%, response budget 96.6% — elasticity binds) |
| Absorbable demand | **1.10x**; the linear reading would claim 1.24x (overstatement 1.12x = 1/0.89) |
| Forecast | `trend+seasonal`, weekly multiplicative profile, strength 0.99 |
| Interval | 90% two-sided, `t(839) = 1.647` on a residual spread of 1.601 |
| Measured coverage on held-back history | 84% against a nominal 90% — reported as `F401` |
| Naive degrees of freedom would have understated the interval by | 1.10x |
| Projected demand | peak **1.15x** upper, 1.11x central; at the horizon's last period 0.35x |
| Run-out (upper bound) | 2026-10-29, in 3.4 days |
| Run-out (central) | 2026-11-05 — so the interval buys 7.0 days of margin |
| Decide by | 2026-10-27, with a 48-hour lead time |
| **Decision** | **scale out**: 12 tasks to **13**, utilisation 80.9% to 74.6%, +42.00 per month |

### What the two basis rules are worth, in tasks

The same inputs, with the reference level or the planning target taken from the
wrong place. These are not hypotheticals about a different model: they are the
two most natural implementations of the same comparison.

| | Reference | Target | Run-out | Fleet | Monthly cost |
|---|---|---|---|---|---|
| **Operating point, peak over the horizon** | 80.9 | 1.15x | 2026-10-29 | **13 tasks** | 546.00 |
| Window mean, peak over the horizon | 45.9 | 2.02x | 2026-10-26, in hours | 22 tasks | 924.00 |
| Operating point, value at the horizon's end | 80.9 | 1.00x | 2026-10-29 | 12 tasks | 504.00 |

The two mistakes fail in opposite directions and both look entirely ordinary in
the output. Dividing by the window mean inflates every multiplier by the
peak-to-mean ratio of 1.76 and buys nine tasks nobody needs. Reading the
projection at the horizon's last period instead of at its peak recommends no
change at all — in the same document that says the ceiling is reached in three
days. Each is one line of arithmetic.

### What it objects to

Nothing in this run is an error, and the eight warnings are the useful part:

- `F401` — measured coverage on held-back history was 84% against a nominal
  90%, with a two-standard-error band of 5%. The interval is narrower than it
  claims **on this series' own recent history**, which is the only test of an
  interval that means anything.
- `F711` — the upper bound runs out in 33% of the time the central projection
  takes, so most of the urgency is interval width. Combined with `F401`, the
  cheapest next step is more history rather than more capacity.
- `F707` — residual autocorrelation means the interval is understated by about
  1.10x, so the true run-out is earlier than 2026-10-29.
- `F521` — the linear reading of 80.9% utilisation would have claimed 1.24x of
  growth against an actual 1.10x.
- `F602`, `F605`, `F706` — arrivals cluster, the source statistic is an average
  over an hour, and so the ceiling is optimistic: 1.10x is an upper bound and
  13 tasks is a lower bound on what is needed.
- `F512`, `F513` — planning to the window mean would have under-provisioned the
  operating point by 1.76x, and utilisation sat at or above 80% for four hours
  at a stretch. Sustained runs build queues; isolated spikes of the same count
  do not.

A review whose findings are all notes is either a very well-instrumented
service or a window too short to have found anything.

## What the model does not tell you

Stated plainly, because a model's silences are where it gets misused.

- **Whether the limit is the right limit.** It checks provenance, not reality.
  A configured pool size may sit well below a downstream limit nobody has
  measured, and raising it just relocates the bottleneck.
- **What the next bottleneck is.** One series, one ceiling. A fleet sized for
  CPU can still exhaust connections, file descriptors or a database's write
  capacity, and nothing here will mention it.
- **Whether demand will keep its shape.** The forecast assumes the seasonal
  profile persists and that growth continues as observed. A product launch, a
  marketing campaign or a retry storm is a different series.
- **Anything about correctness under load.** A fleet with ample headroom can
  still fail on a lock, a cache stampede or a retry amplification the queueing
  model cannot see.
- **Whether the service degrades gracefully.** The model assumes an unbounded
  queue. A real system sheds load, times out or rejects — which caps measured
  latency and *hides* the saturation this model predicts.
- **The cost of being wrong.** It reports what a fleet costs, not what an
  outage costs, and the elasticity target is where that judgement is supposed
  to live. It is a declared parameter precisely because the toolkit has no
  business choosing it.

See [assumptions.md](assumptions.md) for every assumption this model makes,
where it is declared, and which direction it fails in.
