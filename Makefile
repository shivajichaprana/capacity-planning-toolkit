# Every gate this project is judged by, as a target you can run before pushing.
#
# The point of this file is NOT convenience. It is that the commands here are
# the same strings the pipeline runs, character for character, so that a clean
# run locally means the same thing a green run means. tests/test_makefile.py
# compares the two files in both directions and fails when they drift: a target
# that quietly drops a flag fails, and so does a gate added to the pipeline
# with no target here and no stated exception.
#
# The analysis targets are the other half. A capacity review is a chain of four
# commands whose arguments have to agree with each other -- the same series, the
# same limit, the same fleet -- and getting one of them wrong produces a number
# rather than an error. Declaring them once as variables is how they stay
# consistent between the forecast you looked at and the recommendation you acted
# on.

SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c
.DELETE_ON_ERROR:
MAKEFLAGS += --warn-undefined-variables --no-builtin-rules

PYTHON ?= python

# -- what to analyse --------------------------------------------------------
# Override on the command line: make review METRIC=MemoryUtilization UNITS=8

STORE          ?= ./data/capacity.sqlite
SOURCE         ?= cloudwatch
NAMESPACE      ?= AWS/ECS
METRIC         ?= CPUUtilization
STATISTIC      ?= Average
# Repeatable: DIMENSIONS='--dimension ClusterName=prod --dimension ServiceName=api'
DIMENSIONS     ?=
PERIOD         ?= 3600
LOOKBACK_HOURS ?= 336

# -- the capacity limit, and where it came from -----------------------------
# An observed maximum is not a limit. Say so when that is all you have, and the
# analysis will keep saying so in every result derived from it.

LIMIT            ?= 100
LIMIT_UNIT       ?= Percent
LIMIT_PROVENANCE ?= hard
LIMIT_NOTE       ?=

# -- the model's declared parameters ----------------------------------------

PLANNING_PERCENTILE  ?= 0.99
EXCEEDANCE_THRESHOLD ?= 0.8
ELASTICITY_TARGET    ?= 4.0
SERVICE_TIME         ?=
SERVICE_CV_SQUARED   ?=
RESPONSE_BUDGET      ?=
HORIZON              ?= 168
LEVEL                ?= 0.9
TIMEZONE             ?= UTC

# -- the fleet being sized --------------------------------------------------

UNITS            ?= 1
UNIT_NAME        ?= unit
POOLING          ?= shared
MAX_UNITS        ?=
UNIT_MONTHLY_COST ?=
LEAD_TIME_HOURS  ?= 0

# -- where a review is written ----------------------------------------------

REVIEW_OUT   ?= reports
REVIEW_TITLE ?= Capacity review

# Assembled once so that every analysis target describes the same series, the
# same ceiling and the same operating assumptions. An empty override
# contributes nothing rather than an empty argument.

SERIES_ARGS = --store $(STORE) --source $(SOURCE) --namespace $(NAMESPACE) \
	--metric $(METRIC) --statistic $(STATISTIC) $(DIMENSIONS)

LIMIT_ARGS = --limit $(LIMIT) --limit-unit $(LIMIT_UNIT) \
	--limit-provenance $(LIMIT_PROVENANCE) \
	$(if $(LIMIT_NOTE),--limit-note '$(LIMIT_NOTE)',)

MODEL_ARGS = --planning-percentile $(PLANNING_PERCENTILE) \
	--exceedance-threshold $(EXCEEDANCE_THRESHOLD) \
	--elasticity-target $(ELASTICITY_TARGET) \
	$(if $(SERVICE_TIME),--service-time $(SERVICE_TIME),) \
	$(if $(SERVICE_CV_SQUARED),--service-cv-squared $(SERVICE_CV_SQUARED),) \
	$(if $(RESPONSE_BUDGET),--response-budget $(RESPONSE_BUDGET),)

FORECAST_ARGS = --horizon $(HORIZON) --level $(LEVEL) --timezone $(TIMEZONE)

FLEET_ARGS = --units $(UNITS) --unit-name $(UNIT_NAME) --pooling $(POOLING) \
	$(if $(MAX_UNITS),--max-units $(MAX_UNITS),) \
	$(if $(UNIT_MONTHLY_COST),--unit-monthly-cost $(UNIT_MONTHLY_COST),) \
	--lead-time-hours $(LEAD_TIME_HOURS)

.PHONY: help install check test typing lint compile analysis-imports \
	ingest-metrics ingest-cost forecast saturation recommend review clean

## help: list every target with what it does
help:
	@printf 'targets:\n\n'
	@grep -E '^## [a-z-]+:' $(MAKEFILE_LIST) \
		| sed -e 's/^## //' -e 's/:/\t/' \
		| awk -F'\t' '{ printf "  %-18s %s\n", $$1, $$2 }'
	@printf '\nanalysis targets read the variables at the top of the Makefile;\n'
	@printf 'override them on the command line, e.g. make review UNITS=12.\n'

## install: install the project and its check dependencies into the active environment
install:
	$(PYTHON) -m pip install --disable-pip-version-check -e ".[dev]"

# ---------------------------------------------------------------------------
# The gates. Each command below is byte-identical to the one the pipeline runs;
# tests/test_makefile.py is what keeps it that way.
# ---------------------------------------------------------------------------

## check: run every gate the pipeline runs, in the order it reports them
check: test typing lint compile analysis-imports
	@printf 'every gate passed\n'

## test: run the model suite
test:
	$(PYTHON) -m pytest

## typing: type check every declared package under the strict settings
typing:
	$(PYTHON) -m mypy ingest models recommend reports tests conftest.py

## lint: check the style contract declared in setup.cfg
lint:
	$(PYTHON) -m flake8

## compile: confirm every committed module compiles
compile:
	git ls-files '*.py' | xargs -r $(PYTHON) -m py_compile
	@printf 'compiled: %s file(s)\n' "$$(git ls-files '*.py' | wc -l)"

## analysis-imports: confirm the analysis layer imports without the cloud SDK
# Deliberately installs nothing: the gate is that these modules need nothing
# but the standard library, and a target that installed the SDK first would
# prove the opposite of what it claims. Run it in an environment where the SDK
# is absent, which is the condition the pipeline job asserts before importing.
#
# The module list is the contract, and it is the same list the pipeline job
# carries. tests/test_makefile.py compares the two as sets in both directions,
# so a module added to one and not the other fails rather than going unchecked.
analysis-imports:
	$(PYTHON) -c 'import importlib, sys; [importlib.import_module(name) and None for name in ["ingest.store", "ingest.runtime", "ingest.cloudwatch", "ingest.cost", "models.seasonality", "models.forecast", "models.queueing", "models.saturation", "recommend.headroom", "recommend.whatif", "reports.review"]]; sys.exit("the SDK was imported at module scope by one of the above" if "boto3" in sys.modules else 0)'

# ---------------------------------------------------------------------------
# The analysis chain. Collection reaches an API and needs read-only
# credentials; everything after it reads the store and reaches nothing.
# ---------------------------------------------------------------------------

## ingest-metrics: collect a utilisation or throughput series into the store
ingest-metrics:
	$(PYTHON) -m ingest.cloudwatch --store $(STORE) --namespace $(NAMESPACE) \
		--metric $(METRIC) --statistic $(STATISTIC) $(DIMENSIONS) \
		--period $(PERIOD) --lookback-hours $(LOOKBACK_HOURS)

## ingest-cost: collect amortised spend and usage quantities into the store
ingest-cost:
	$(PYTHON) -m ingest.cost --store $(STORE) --lookback-days 90 \
		--granularity DAILY --metric AmortizedCost --group-by SERVICE

## forecast: project the series, with an interval that widens with the horizon
forecast:
	$(PYTHON) -m models.forecast $(SERIES_ARGS) $(FORECAST_ARGS)

## saturation: report the operating point, the ceiling and the usable headroom
saturation:
	$(PYTHON) -m models.saturation $(SERIES_ARGS) $(LIMIT_ARGS) $(MODEL_ARGS) \
		--servers $(UNITS)

## recommend: size the fleet and report the run-out and decide-by dates
recommend:
	$(PYTHON) -m recommend.headroom $(SERIES_ARGS) $(LIMIT_ARGS) $(MODEL_ARGS) \
		$(FORECAST_ARGS) $(FLEET_ARGS)

## review: render the whole review -- decision, evidence, options, findings
review:
	$(PYTHON) -m reports.review $(SERIES_ARGS) $(LIMIT_ARGS) $(MODEL_ARGS) \
		$(FORECAST_ARGS) $(FLEET_ARGS) \
		--out $(REVIEW_OUT) --title '$(REVIEW_TITLE)'

## clean: remove tooling caches and compiled bytecode, never the store
clean:
	rm -rf .mypy_cache .pytest_cache
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	find . -name '*.py[co]' -delete
	@printf 'caches removed; the observation store and generated reviews are untouched\n'
