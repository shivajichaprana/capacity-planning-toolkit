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

## What it does

| Capability | Where |
|---|---|
| Pull utilisation and throughput metrics from CloudWatch into a normalised series | `ingest/cloudwatch.py` |
| Pull daily amortised spend and usage quantities from Cost Explorer | `ingest/cost.py` |
| Persist and query observation history locally, de-duplicated and typed | `ingest/store.py` |
| Seasonal demand forecasting with confidence intervals | `models/` |
| Utilisation and queueing-theory saturation modelling | `models/` |
| Headroom recommendations and what-if simulation | `recommend/` |
| Rendered capacity reviews | `reports/` |

## Repository layout

```
ingest/      metric and cost collection, plus the local observation store
models/      forecasting and saturation models
recommend/   headroom arithmetic and what-if scenarios
reports/     generated capacity reviews
tests/       unit tests for the models and the ingestion layer
docs/        capacity model, stated assumptions, review runbook
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
5. **Local by default.** The store is a plain file. Nothing is written back to
   the account being measured, and read-only credentials are sufficient.

## Getting started

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
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

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `CAPACITY_STORE_PATH` | `./data/capacity.sqlite` | Observation store location |
| `CAPACITY_LOG_LEVEL` | `INFO` | Log verbosity for all entry points |
| `CAPACITY_LOG_FORMAT` | `text` | `text` for humans, `json` for log pipelines |
| `AWS_REGION` | — | Region for the metric and cost clients |
| `AWS_PROFILE` | — | Shared-config profile, when not using a role |

## Contributing

Open a Discussion in the repo or comment on the PR. Report a security concern
through a private advisory:
<https://github.com/shivajichaprana/capacity-planning-toolkit/security/advisories/new>

## License

MIT — see [LICENSE](LICENSE).
