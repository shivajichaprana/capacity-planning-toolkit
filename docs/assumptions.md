# Stated assumptions

A capacity plan is a chain of assumptions, and the dangerous ones are the
unwritten ones. This is the complete list: what is assumed, where it is
declared, what it defaults to, which direction the answer moves when the
assumption is wrong, and how to find out whether it holds.

Three conventions hold throughout.

1. **An assumption with a default is a declared parameter**, not lore. It
   appears as a flag, it is echoed in the output, and changing it changes the
   answer visibly.
2. **An assumption that can be measured is measured**, not assumed. Burstiness,
   autocorrelation, trend, interval coverage and seasonal strength are all
   computed from the series and reported, including when they refute the model
   that uses them.
3. **Where an assumption fails, it fails toward optimism.** This is not a
   coincidence — queueing results are derived under idealisations that real
   demand violates in the direction of *more* delay. So every ceiling here is
   an upper bound on the safe operating point, and every recommended size is a
   lower bound on what is needed.

## Contents

- [Collection](#collection)
- [The observation store](#the-observation-store)
- [Seasonality](#seasonality)
- [Forecasting](#forecasting)
- [The capacity limit](#the-capacity-limit)
- [The queueing model](#the-queueing-model)
- [Sizing and timing](#sizing-and-timing)
- [Scenarios](#scenarios)
- [Assumptions with no parameter](#assumptions-with-no-parameter)
- [How to audit a review](#how-to-audit-a-review)

## Collection

| Assumption | Declared | Default | If wrong | How to tell |
|---|---|---|---|---|
| The sampling period is what the series is on | `--period` | `3600` | A period finer than the source's retention silently returns fewer points or none — the API does not error | The collector warns when a sub-300s period is asked of a window older than fine-resolution retention |
| The statistic represents the quantity being planned | `--statistic` | `Average` | An average divides every burst shorter than the window; peaks read lower than they are | `F605`: the source statistic and period are reported with every analysis |
| The lookback window is representative | `--lookback-hours` | `336` | A window containing an incident plans for the incident; one containing a quiet fortnight plans for the quiet | Compare the operating point against a longer window; `F603` fires when the window is not stationary |
| Cost Explorer's range is end-exclusive | not a parameter | — | Reading the newest cost point as a dip; it is a partial period | The collector drops today's partial period deliberately |
| Ingestion does not interpolate | not a parameter | — | — | By design: a collector that filled gaps would destroy the only evidence the data was incomplete |

Collection reaches an API and needs read-only credentials. Nothing after it
reaches anything: every later stage is a pure function of the store, which is
what makes a review re-derivable on a laptop with no access to the account.

## The observation store

| Assumption | Declared | Default | If wrong | How to tell |
|---|---|---|---|---|
| A point is identified by source, namespace, metric, statistic, entity and timestamp | not a parameter | — | Two call sites would write the same observation twice as separate series | The identity is a unique index with upsert-on-conflict, so re-collection over an overlapping window is free |
| A gap wider than one sampling period is a hole | not a parameter | — | One period *is* the normal spacing between consecutive samples; calling that a hole would flag every healthy series | `Coverage` reports expected and observed counts and the largest gap found |
| An ungrouped cost total is a distinct entity | not a parameter | — | A group genuinely named `TOTAL` would collide with the total | The ungrouped identity is the fixed pair `TOTAL=TOTAL` |

## Seasonality

| Assumption | Declared | Default | If wrong | How to tell |
|---|---|---|---|---|
| Demand cycles daily or weekly | not a parameter | both tried | A monthly or quarterly cycle is not modelled and lands in the residual, widening every interval | Residual autocorrelation (`F402`) rises |
| The cycle is a whole number of sampling periods | — | — | A cycle that is not representable on the grid is skipped | `F101` |
| Phase comes from the timestamp, not the index | not a parameter | — | Bucketing the `i`-th sample into `i % lag` rotates the entire profile the moment one sample is missing | By construction; `F109`/`F110` report thin phases |
| A daily cycle is a cycle in **local** time | `--timezone` | `UTC` | Two instants 24 hours apart are not at the same point in a human demand cycle across a clock change | `F113` fires when the window crosses a UTC-offset change |
| Seasonality is additive unless amplitude tracks level | not a parameter | decided | Additive and multiplicative residuals live on different scales and cannot be ranked against each other, so the mode is decided by correlating per-cycle range with per-cycle mean | `F106`/`F107` report the correlation; `F104` says the test was unavailable below three cycles |
| A profile below 10% strength is noise | not a parameter | `0.1` | Fitting it spends degrees of freedom describing noise | `F111`/`F112` report the strength and the decision |
| The profile is treated as known | not a parameter | — | Interval width excludes the profile's own estimation error, so the band is a lower bound | `F406` |

The strength figure is adjusted for the parameters the profile spends and
confirmed by an F test, because the unadjusted share of detrended variance
measures the profile's *flexibility* rather than the series' seasonality: at
exactly two cycles of history it reads 1.0 for any series at all, pure noise
included.

## Forecasting

| Assumption | Declared | Default | If wrong | How to tell |
|---|---|---|---|---|
| Growth continues as observed | not a parameter | — | A launch, campaign or retry storm is a different series and the forecast knows nothing about it | Out of scope by design; stated in [capacity-model.md](capacity-model.md#what-the-model-does-not-tell-you) |
| Residuals are independent across steps | not a parameter | — | Autocorrelated residuals mean the horizon scaling understates the band, so run-out is earlier than reported | `F402` reports the measured lag-one autocorrelation and the white-noise band; `F707` carries the understatement factor into the recommendation |
| The interval level is the level wanted | `--level` | `0.9` | A 90% band is not a worst case; one observation in ten is expected outside it | `F400` reports the multiplier and degrees of freedom; `F401`/`F408` report measured coverage |
| The horizon is within what the history supports | `--horizon` | `168` | A horizon beyond half the fitted history is extrapolation | `F305` |
| A hole invalidates a forecast | `--allow-holes` | refuse | Forecasting across a gap means a peak inside the gap was invisible to the fit | `F201` refuses; `--allow-holes` converts it to `F202` |
| Demand cannot be negative | `--allow-negative` | clip at zero | Clipping removes probability rather than redistributing it, so stated coverage fails on the lower side | `F403` reports how many periods were clipped |

Degrees of freedom are charged for the seasonal profile rather than assumed to
be `n - 2`; `F407` publishes the factor a naive count would have missed, and on
a weekly profile over hourly data it is around 1.10.

## The capacity limit

This is the single most consequential input, and the one most often wrong.

| Provenance | What it means | What the analysis does with it |
|---|---|---|
| `hard` | Architecture or hardware: core count, provisioned IOPS, memory | Nothing extra; this is the case the arithmetic assumes |
| `quota` | A provider limit, raisable by asking | `F506`/`F704`: the remedy is a request, not a deploy |
| `configured` | A limit this service set on itself: pool size, thread count, concurrency cap | Raisable by deploying, often into a downstream limit nobody has measured |
| `observed` | The largest value ever seen | **Not a limit.** `F505`/`F703`: it is a lower bound on one, so utilisation computed against it is an upper bound and real headroom is at least what is reported |

| Assumption | Declared | Default | If wrong | How to tell |
|---|---|---|---|---|
| The limit's unit matches the series' unit | `--limit-unit` | `""` | A ratio of two different units is not a utilisation | `F504` |
| Observations do not exceed the limit | — | — | They can, and when they do the limit is wrong rather than the data | `F507` |
| The limit is the binding limit | not a parameter | — | A fleet sized for one resource can still exhaust another | Out of scope; one series, one ceiling |

## The queueing model

The ceiling comes from a closed-form result with four idealisations. Each is
measured on the real series rather than assumed, and each failure is optimistic.

| Assumption | Measured as | Threshold | Finding | Direction of error |
|---|---|---|---|---|
| Arrivals are Poisson | peak-to-mean | above `2.0` | `F601` | Bursts raise delay at a given utilisation, so the reported wait is a lower bound |
| Arrivals are independent | lag-one autocorrelation | above `0.5` | `F602` | Clustered load builds queues further than the model predicts |
| The arrival rate is stationary | trend as a fraction of the mean | above `0.25` | `F603` | There is no single operating point; the analysis describes a load the resource is leaving |
| Service times are exponential | `--service-cv-squared` | unmeasured by default | `F604` | Delay scales with `(1 + cv²) / 2`, so a mixed fast/slow service queues proportionally longer |
| The source statistic records peaks | the statistic itself | `Average` | `F605` | Any burst shorter than the sampling window is divided by it |
| The queue is unbounded | not measurable | — | documented | A real queue is bounded by a timeout, connection limit or load shedder, which caps measured latency and **hides** the saturation this model predicts |

When any of these fires, `F706` is raised on the recommendation: the absorbable
multiplier is an upper bound and the recommended fleet size is a lower bound.

Two results are refused rather than approximated:

- **Sojourn-time quantiles for more than one server.** The distribution is a
  mixture, not an exponential, so its quantiles are not the wait quantiles plus
  the mean service time.
- **Any percentile under general service times.** Only the mean queueing delay
  has a closed form; a non-exponential service time is modelled for a single
  server only, and a several-server request with measured variability is
  refused at the argument check rather than part-way through an analysis.

| Parameter | Declared | Default | What it means |
|---|---|---|---|
| Latency sensitivity | `--elasticity-target` | `4.0` | A percent of demand growth may cost up to four percent of latency. This is where the business judgement lives; the toolkit has no basis for choosing it |
| Operating percentile | `--planning-percentile` | `0.99` | Which point of the utilisation distribution is "the operating point". Refused and downgraded when the sample cannot resolve it (`F510`) |
| Exceedance threshold | `--exceedance-threshold` | `0.8` | Utilisation above which a sample counts toward a sustained run (`F513`) |
| Mean service time | `--service-time` | none | Required for a response-time ceiling. Without it, only the elasticity ceiling exists (`F523` if a budget was given without one) |
| Response budget | `--response-budget` | none | A mean response-time ceiling in seconds. Below the mean service time it is unsatisfiable (`F524`) |
| Servers sharing the queue | `--servers` / `--units` | `1` | Drives the ceiling. `F726` fires when the declared fleet and the server count disagree |

## Sizing and timing

| Assumption | Declared | Default | If wrong | How to tell |
|---|---|---|---|---|
| Demand spreads evenly across the fleet | not a parameter | — | A hot shard saturates while the fleet average looks healthy | Not detectable from a fleet-level series; size per shard instead |
| Units pool into one queue | `--pooling` | `shared` | A sharded fleet gets no pooling gain, and claiming one under-provisions it | `F722` reports a claimed gain, `F723` reports that none was claimed |
| The declared maximum is reachable | `--max-units` | none | A requirement past the maximum is truncated rather than met | `F724` |
| Capacity is an integer | not a parameter | — | — | By design; `F721` reports the forced overshoot when it exceeds 10% |
| Provisioning takes the stated lead time | `--lead-time-hours` | `0` | A decide-by date computed from the wrong lead time is the wrong date | `F712` is an **error** when the decide-by date has already passed |
| The ceiling multiplier and the projection share a basis | **derived, not declared** | — | Using the window mean instead of the operating level inflates every projected multiplier by the peak-to-mean ratio, which on a seasonal series means an immediate false run-out | `F702` states the level the multipliers are relative to; an explicitly supplied reference that disagrees with the derived one is refused |
| Peak-to-mean holds as the level moves | not a parameter | — | Independent demand superposes and smooths, so aggregate growth is slightly over-provisioned; growth concentrated in one consumer gets no smoothing | `F702` reports the margin between the window's maximum and its operating percentile |

The run-out date is read off the forecast's **one-sided upper bound**, not its
central line: sizing to the median is under-provisioned half the time by
construction. When the central line never reaches the ceiling and only the upper
bound does, `F711` says the decision is driven by interval width rather than by
measured growth — and more history may remove the need to spend anything.

## Scenarios

Each option in the what-if table holds something fixed, and the list is printed
beside it. The assumptions worth stating:

| Scenario | Holds fixed | What it over-promises |
|---|---|---|
| Scale out | Demand, per-unit service time, even distribution, the pooling model | Nothing, if `--pooling` is right |
| Scale up | Demand, unit count, variability, the workload's shape | A larger unit rarely divides service time by its size factor (`F812`); fewer, larger units concentrate the failure domain (`F813`) |
| Raise limit | Demand, fleet, service time | Raising a limit usually relocates the bottleneck rather than removing it (`F814`) |
| Demand change | Fleet, service time, the shape of demand | A multiplier beyond anything observed is extrapolation (`F801`) |
| Retarget elasticity | Everything | It adds no capacity at all; it is a policy change (`F815`) |

Two structural warnings: outcomes are alternatives and **do not add** (`F803`),
because each one's gain depends on the utilisation it starts from; and a
baseline already in the steep region makes every option look good (`F802`),
which is a property of `rho / (1 - rho)` rather than evidence about any option.

A scenario that leaves the system unstable is reported as unstable (`F800`)
rather than as a very slow queue. The question then is how fast the backlog
grows, not how long a request waits.

## Assumptions with no parameter

These cannot be turned off, so they are stated instead.

- **The store is the record.** Every figure in a review is a field of a result
  object, formatted — the renderer computes nothing. If a number in a review is
  wrong, either an input was wrong or a model is wrong; the report cannot have
  introduced it.
- **A model is a pure function of its observations.** No model reaches an API
  or mutates the store, which is what lets a review be re-derived from a store
  file alone.
- **A refusal is a result.** Too little history, a gap wider than the sampling
  period, or a percentile the sample cannot resolve produces an explicit
  finding and a non-zero exit, never a weaker answer wearing the stronger
  answer's label.
- **Findings are carried forward unchanged.** A recommendation inherits every
  objection the analysis it rests on recorded. A report that shows a conclusion
  without them is more confident than its evidence.
- **Read-only credentials are sufficient.** Nothing is written back to the
  account being measured.

## How to audit a review

In order, because each step makes the next one meaningful.

1. **Check the limit's provenance.** `observed` means there is no limit in this
   review, only a lower bound on one. Everything downstream inherits that.
2. **Check the resolvable percentile against the requested one.** If the window
   cannot resolve the operating percentile, the operating point is the largest
   few samples and the planning figure is noise.
3. **Check the stationarity finding.** `F603` means the window has no single
   operating point. Shorten it and re-run before reading anything else.
4. **Check measured interval coverage against the nominal level.** `F401` means
   the band does not do what it claims on this series' own recent history.
5. **Check whether the decision is interval-driven.** `F711` means more history
   may be cheaper than more capacity.
6. **Check the decide-by date against today.** `F712` is the one error that is
   about the calendar rather than the data, and it does not go away by
   re-running.
7. **Then read the headline.** It is the least informative line in the
   document, and it is the first one most readers stop at.
