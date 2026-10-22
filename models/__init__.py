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
``F5xx``
    Saturation evidence — the limit and its provenance, the utilization
    distribution, the resolution a percentile claim needs, sustained time above
    a threshold, and the headroom conversion.
``F6xx``
    Queueing applicability — every measured departure from the assumptions a
    closed-form ceiling rests on. These all fail toward optimism, so a ceiling
    carrying an F6xx finding is an upper bound on the safe operating point.

The queueing module holds no finding codes of its own: it is pure arithmetic on
a stated operating point and raises rather than reports, leaving every judgement
about whether its assumptions hold to the saturation module that measures them.
"""

__all__ = ["forecast", "queueing", "saturation", "seasonality"]
