"""Collection of capacity observations from metric and billing sources.

The ingestion layer is deliberately thin. It reads from an upstream API,
normalises the result into :class:`ingest.store.Observation` records, and writes
them to the local store. It performs no smoothing, no gap filling and no
modelling: those belong downstream, where they can be inspected and tested.
"""

__all__ = ["cloudwatch", "cost", "store"]
