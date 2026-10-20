"""Local observation store.

Every collector writes :class:`Observation` records here and every model reads
them back as a :class:`MetricSeries`. The store is a single SQLite file so that
a capacity review is reproducible from an artefact you can copy, diff and attach
to a ticket, with no service to stand up first.

Two properties are load-bearing:

* **Idempotent writes.** A series point is identified by
  ``(source, namespace, metric, statistic, entity, timestamp)``. Re-collecting an
  overlapping window updates those points in place, so a collector can be run on
  a schedule with a generous lookback and never double-count.
* **Honest coverage.** :meth:`ObservationStore.coverage` reports how many samples
  a window actually holds against how many the sampling period implies. Models
  consult it before projecting, because a forecast over a series with a silent
  hole in it is worse than no forecast.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import TracebackType
from typing import Final

_LOG = logging.getLogger(__name__)

SCHEMA_VERSION: Final[int] = 1

_SCHEMA: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS schema_version (
        version    INTEGER NOT NULL,
        applied_at TEXT    NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS observation (
        source      TEXT    NOT NULL,
        namespace   TEXT    NOT NULL,
        metric      TEXT    NOT NULL,
        statistic   TEXT    NOT NULL,
        entity      TEXT    NOT NULL,
        ts          INTEGER NOT NULL,
        value       REAL    NOT NULL,
        unit        TEXT    NOT NULL,
        period      INTEGER NOT NULL,
        meta        TEXT    NOT NULL DEFAULT '{}',
        ingested_at TEXT    NOT NULL
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS observation_point
        ON observation (source, namespace, metric, statistic, entity, ts)
    """,
    """
    CREATE INDEX IF NOT EXISTS observation_series_window
        ON observation (source, namespace, metric, statistic, entity, ts DESC)
    """,
)


class StoreError(RuntimeError):
    """Raised when the store cannot satisfy a request it was asked to satisfy."""


def utc_now() -> datetime:
    """Current time, timezone-aware, in UTC."""
    return datetime.now(tz=timezone.utc)


def canonical_entity(dimensions: Mapping[str, str] | None) -> str:
    """Render dimensions as a stable, sortable identity string.

    Sorting is what makes the identity stable: the same dimension set collected
    by two different call sites must produce the same string, or the unique index
    will not de-duplicate it.
    """
    if not dimensions:
        return "-"
    parts = [f"{name}={dimensions[name]}" for name in sorted(dimensions)]
    return ",".join(parts)


def parse_entity(entity: str) -> dict[str, str]:
    """Inverse of :func:`canonical_entity`, for reporting."""
    if entity in ("", "-"):
        return {}
    out: dict[str, str] = {}
    for part in entity.split(","):
        name, _, value = part.partition("=")
        if name:
            out[name] = value
    return out


@dataclass(frozen=True, slots=True)
class SeriesKey:
    """The identity of one time series."""

    source: str
    namespace: str
    metric: str
    statistic: str
    entity: str

    def as_tuple(self) -> tuple[str, str, str, str, str]:
        return (self.source, self.namespace, self.metric, self.statistic, self.entity)

    def label(self) -> str:
        return f"{self.namespace}/{self.metric}.{self.statistic}[{self.entity}]"


@dataclass(frozen=True, slots=True)
class Observation:
    """A single measured point.

    ``period`` is the sampling period in seconds that the source reported this
    value for. It is stored rather than inferred because coverage checks and
    every downstream model need to know the resolution of what they were given.
    """

    key: SeriesKey
    timestamp: datetime
    value: float
    unit: str
    period: int
    meta: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError("observation timestamps must be timezone-aware")
        if self.period <= 0:
            raise ValueError(f"period must be positive, got {self.period}")

    def epoch(self) -> int:
        return int(self.timestamp.astimezone(timezone.utc).timestamp())


@dataclass(frozen=True, slots=True)
class SeriesPoint:
    """One point of a series read back out of the store."""

    timestamp: datetime
    value: float


@dataclass(frozen=True, slots=True)
class Coverage:
    """How complete a window is, measured rather than assumed."""

    expected: int
    observed: int
    period: int
    largest_gap_seconds: int

    @property
    def ratio(self) -> float:
        if self.expected <= 0:
            return 0.0
        return min(1.0, self.observed / self.expected)

    @property
    def has_hole(self) -> bool:
        """True when any gap exceeds one sampling period.

        A gap of exactly one period is the normal spacing between consecutive
        samples, so the comparison is strictly greater than.
        """
        return self.largest_gap_seconds > self.period


@dataclass(frozen=True, slots=True)
class MetricSeries:
    """An ordered, de-duplicated series plus the coverage it was read with."""

    key: SeriesKey
    unit: str
    period: int
    points: tuple[SeriesPoint, ...]
    coverage: Coverage

    def __len__(self) -> int:
        return len(self.points)

    @property
    def values(self) -> tuple[float, ...]:
        return tuple(point.value for point in self.points)

    @property
    def start(self) -> datetime | None:
        return self.points[0].timestamp if self.points else None

    @property
    def end(self) -> datetime | None:
        return self.points[-1].timestamp if self.points else None


class ObservationStore:
    """SQLite-backed observation store.

    Usable as a context manager, which is the recommended form — it commits on a
    clean exit and rolls back on an exception, so a collector that fails halfway
    through a window leaves no partial write behind.
    """

    def __init__(self, path: str, *, read_only: bool = False) -> None:
        self._path = path
        self._read_only = read_only
        self._conn: sqlite3.Connection | None = None

    # -- lifecycle ---------------------------------------------------------

    def connect(self) -> ObservationStore:
        if self._conn is not None:
            return self

        if self._path != ":memory:":
            parent = os.path.dirname(os.path.abspath(self._path))
            if parent and not os.path.isdir(parent):
                if self._read_only:
                    raise StoreError(f"store directory does not exist: {parent}")
                os.makedirs(parent, exist_ok=True)
                _LOG.debug("created store directory %s", parent)

        self._conn = sqlite3.connect(self._path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self._path != ":memory:":
            # WAL keeps a long read (a model) from blocking a write (a collector).
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        if not self._read_only:
            self._migrate()
        return self

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> ObservationStore:
        return self.connect()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StoreError(
                "store is not connected; call connect() or use it as a context manager"
            )
        return self._conn

    def _migrate(self) -> None:
        db = self._db
        db.execute("BEGIN IMMEDIATE")
        try:
            for statement in _SCHEMA:
                db.execute(statement)
            row = db.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
            current = row["v"] if row and row["v"] is not None else 0
            if current < SCHEMA_VERSION:
                db.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (SCHEMA_VERSION, utc_now().isoformat()),
                )
                _LOG.info("store schema migrated to version %d", SCHEMA_VERSION)
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise

    # -- writes ------------------------------------------------------------

    def write(self, observations: Iterable[Observation]) -> int:
        """Upsert observations; returns the number of points written.

        The whole batch is one transaction. Conflicting points are updated rather
        than rejected, which is what makes a re-run over an overlapping window
        safe.
        """
        if self._read_only:
            raise StoreError("store was opened read-only")

        now = utc_now().isoformat()
        rows = [
            (
                obs.key.source,
                obs.key.namespace,
                obs.key.metric,
                obs.key.statistic,
                obs.key.entity,
                obs.epoch(),
                float(obs.value),
                obs.unit,
                int(obs.period),
                json.dumps(dict(obs.meta), separators=(",", ":"), sort_keys=True, default=str),
                now,
            )
            for obs in observations
        ]
        if not rows:
            _LOG.info("no observations to write")
            return 0

        db = self._db
        db.execute("BEGIN IMMEDIATE")
        try:
            db.executemany(
                """
                INSERT INTO observation
                    (source, namespace, metric, statistic, entity, ts,
                     value, unit, period, meta, ingested_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (source, namespace, metric, statistic, entity, ts)
                DO UPDATE SET
                    value       = excluded.value,
                    unit        = excluded.unit,
                    period      = excluded.period,
                    meta        = excluded.meta,
                    ingested_at = excluded.ingested_at
                """,
                rows,
            )
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise

        _LOG.info("wrote %d observation(s)", len(rows))
        return len(rows)

    # -- reads -------------------------------------------------------------

    def series_keys(self, *, source: str | None = None) -> list[SeriesKey]:
        """Every series held, optionally narrowed to one source."""
        sql = (
            "SELECT DISTINCT source, namespace, metric, statistic, entity "
            "FROM observation"
        )
        params: list[object] = []
        if source is not None:
            sql += " WHERE source = ?"
            params.append(source)
        sql += " ORDER BY source, namespace, metric, statistic, entity"
        return [
            SeriesKey(
                source=row["source"],
                namespace=row["namespace"],
                metric=row["metric"],
                statistic=row["statistic"],
                entity=row["entity"],
            )
            for row in self._db.execute(sql, params)
        ]

    def read_series(
        self,
        key: SeriesKey,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> MetricSeries:
        """Read one series, ascending by timestamp, with its coverage."""
        sql = (
            "SELECT ts, value, unit, period FROM observation "
            "WHERE source = ? AND namespace = ? AND metric = ? "
            "AND statistic = ? AND entity = ?"
        )
        params: list[object] = list(key.as_tuple())
        if start is not None:
            sql += " AND ts >= ?"
            params.append(int(start.astimezone(timezone.utc).timestamp()))
        if end is not None:
            sql += " AND ts <= ?"
            params.append(int(end.astimezone(timezone.utc).timestamp()))
        sql += " ORDER BY ts ASC"

        rows = self._db.execute(sql, params).fetchall()
        if not rows:
            _LOG.debug("series %s is empty for the requested window", key.label())
            return MetricSeries(
                key=key,
                unit="None",
                period=0,
                points=(),
                coverage=Coverage(expected=0, observed=0, period=0, largest_gap_seconds=0),
            )

        points = tuple(
            SeriesPoint(
                timestamp=datetime.fromtimestamp(row["ts"], tz=timezone.utc),
                value=float(row["value"]),
            )
            for row in rows
        )
        # The dominant period is the right one to report: a single re-collection
        # at a finer resolution should not redefine the series.
        periods = [int(row["period"]) for row in rows]
        period = max(set(periods), key=periods.count)
        unit = rows[-1]["unit"]

        return MetricSeries(
            key=key,
            unit=unit,
            period=period,
            points=points,
            coverage=_coverage_of(points, period),
        )

    def coverage(
        self,
        key: SeriesKey,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> Coverage:
        """Coverage of a window without materialising its values."""
        return self.read_series(key, start=start, end=end).coverage

    def latest_timestamp(self, key: SeriesKey) -> datetime | None:
        """Newest point held for a series, used to pick a collection start."""
        row = self._db.execute(
            "SELECT MAX(ts) AS ts FROM observation "
            "WHERE source = ? AND namespace = ? AND metric = ? "
            "AND statistic = ? AND entity = ?",
            key.as_tuple(),
        ).fetchone()
        if row is None or row["ts"] is None:
            return None
        return datetime.fromtimestamp(int(row["ts"]), tz=timezone.utc)

    def prune(self, *, older_than: datetime) -> int:
        """Delete points older than a cutoff; returns rows removed."""
        if self._read_only:
            raise StoreError("store was opened read-only")
        cutoff = int(older_than.astimezone(timezone.utc).timestamp())
        db = self._db
        db.execute("BEGIN IMMEDIATE")
        try:
            cursor = db.execute("DELETE FROM observation WHERE ts < ?", (cutoff,))
            removed = cursor.rowcount or 0
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
        if removed:
            _LOG.info("pruned %d observation(s) older than %s", removed, older_than.isoformat())
        return removed


def _coverage_of(points: Sequence[SeriesPoint], period: int) -> Coverage:
    """Expected-versus-observed sample count and the largest gap found."""
    if not points or period <= 0:
        return Coverage(
            expected=0,
            observed=len(points),
            period=max(period, 0),
            largest_gap_seconds=0,
        )

    span = (points[-1].timestamp - points[0].timestamp).total_seconds()
    expected = int(span // period) + 1

    largest = 0
    for previous, current in zip(points, points[1:]):
        gap = int((current.timestamp - previous.timestamp).total_seconds())
        largest = max(largest, gap)

    return Coverage(
        expected=expected,
        observed=len(points),
        period=period,
        largest_gap_seconds=largest,
    )


def iter_windows(
    start: datetime,
    end: datetime,
    *,
    span: timedelta,
) -> Iterator[tuple[datetime, datetime]]:
    """Split a range into consecutive windows no longer than ``span``.

    Both metric and cost APIs cap how much a single call may return, so every
    collector paginates by time as well as by response token.
    """
    if span <= timedelta(0):
        raise ValueError("window span must be positive")
    cursor = start
    while cursor < end:
        nxt = min(cursor + span, end)
        yield cursor, nxt
        cursor = nxt
