"""Shared runtime concerns for the collectors: logging, environment defaults.

Kept separate so that both collectors configure themselves identically and so a
change to log formatting or to a default is made in one place.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any, Final

DEFAULT_STORE_PATH: Final[str] = "./data/capacity.sqlite"

_ENV_STORE: Final[str] = "CAPACITY_STORE_PATH"
_ENV_LEVEL: Final[str] = "CAPACITY_LOG_LEVEL"
_ENV_FORMAT: Final[str] = "CAPACITY_LOG_FORMAT"

_LOG = logging.getLogger(__name__)


class JsonLogFormatter(logging.Formatter):
    """Render records as single-line JSON for ingestion by a log pipeline."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        return json.dumps(payload, separators=(",", ":"), default=str)


def configure_logging(level: str | None = None, fmt: str | None = None) -> None:
    """Install a single stderr handler on the root logger.

    Idempotent: repeated calls replace the handler rather than stacking copies,
    which matters when a collector is imported as a module and also run as a
    script in the same process.
    """
    resolved_level = (level or os.environ.get(_ENV_LEVEL) or "INFO").upper()
    resolved_fmt = (fmt or os.environ.get(_ENV_FORMAT) or "text").lower()

    handler = logging.StreamHandler(stream=sys.stderr)
    if resolved_fmt == "json":
        handler.setFormatter(JsonLogFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S%z",
            )
        )

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(getattr(logging, resolved_level, logging.INFO))

    # Collector output is the signal; the SDK's wire logging is not.
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("boto3").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def default_store_path() -> str:
    """Store location from the environment, falling back to the documented default."""
    return os.environ.get(_ENV_STORE) or DEFAULT_STORE_PATH


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    """Attach the arguments every collector accepts."""
    parser.add_argument(
        "--store",
        default=default_store_path(),
        help=f"path to the observation store (default: {default_store_path()})",
    )
    parser.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION") or None,
        help="region for the API client (default: the provider chain's region)",
    )
    parser.add_argument(
        "--profile",
        default=os.environ.get("AWS_PROFILE") or None,
        help="shared-config profile to use instead of ambient credentials",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="log verbosity (default: CAPACITY_LOG_LEVEL, else INFO)",
    )
    parser.add_argument(
        "--log-format",
        default=None,
        choices=["text", "json"],
        help="log rendering (default: CAPACITY_LOG_FORMAT, else text)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="collect and report, but do not write to the store",
    )


def build_session(region: str | None, profile: str | None) -> Any:
    """Create a boto3 session.

    Imported lazily so that the pure-Python parts of this package — the store,
    the models — are importable without the SDK installed.
    """
    import boto3  # noqa: PLC0415  (deliberate: keeps the SDK an optional import)

    _LOG.debug("building session region=%s profile=%s", region, profile)
    return boto3.Session(region_name=region, profile_name=profile)
