"""Models that turn observations into statements about the future.

Each model reads a :class:`ingest.store.MetricSeries` and returns a typed
result carrying its own findings. No model reaches an API, and none of them
mutate the store: a model is a pure function of the observations it was given,
so a capacity review can be re-derived from the store file alone.

Finding codes are namespaced by concern, and the namespace is reserved here so
that later modules cannot quietly reuse a prefix:

``F1xx``
    Seasonal structure — whether a cycle is representable, estimable and real.
``F2xx``
    Input adequacy — history length, coverage, grid regularity.
``F3xx``
    Model selection — which candidate was chosen and how weak the comparison was.
``F4xx``
    Interval honesty — every reason a reported interval is narrower than the
    truth, measured where it can be measured.
"""

__all__ = ["forecast", "seasonality"]
