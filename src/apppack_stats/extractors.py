"""
Recognize supported JSON access-log formats and normalize them for ingestion.

This module isolates all knowledge of concrete log payload shapes from the rest
of the application. Each supported format is described declaratively as a
``LogShape``, and the caller only sees a canonical
``(method, path, response_time_us, status)`` tuple or ``None``.

The matching model is intentionally simple: the first shape whose trigger field
is present claims the payload. That keeps the hot path fast and makes it easy to
extend, but it also means new shapes should use distinctive trigger fields to
avoid accidental collisions.
"""

from __future__ import annotations

from dataclasses import dataclass

# Multiplier from each supported unit to microseconds.
_TO_US = {"us": 1, "ms": 1_000, "s": 1_000_000}


@dataclass(frozen=True)
class LogShape:
    """
    Describe how to extract one supported access-log payload shape.

    A ``LogShape`` is a lightweight schema adapter. It says which top-level JSON
    keys hold the method, path, status, and timing values, and how to convert
    the raw timing unit into microseconds so the rest of the application can
    compare all requests on the same scale.

    The dataclass is frozen because shapes are effectively configuration. They
    are defined once at import time and should not drift at runtime.
    """

    name: str
    method_field: str
    path_field: str
    status_field: str
    time_field: str
    time_unit: str  # "us" | "ms" | "s"

    def extract(self, payload: dict) -> tuple[str, str, int, int] | None:
        """
        Attempt to read this shape out of a payload and normalize its timing.

        A payload only counts as a match when the designated trigger field is
        present. Missing keys, bad numeric values, or incompatible types all
        cause this to return ``None`` rather than raising, because malformed or
        irrelevant log lines are expected noise in the input stream.
        """
        if self.time_field not in payload:
            return None
        try:
            method = payload[self.method_field]
            path = payload[self.path_field]
            status = int(payload[self.status_field])
            time_us = int(
                float(payload[self.time_field]) * _TO_US[self.time_unit]
            )
        except (KeyError, ValueError, TypeError):
            return None
        return method, path, time_us, status


# Registered shapes, tried in order. The first whose ``time_field`` is
# present in the payload claims it — when adding a new shape, pick a
# trigger that doesn't collide with an existing one.
SHAPES: list[LogShape] = [
    # AppPack default access log.
    LogShape(
        name="apppack-default",
        method_field="method",
        path_field="path",
        status_field="status",
        time_field="response_time_us",
        time_unit="us",
    ),
    # gunicorn structlog access log.
    LogShape(
        name="gunicorn-structlog",
        method_field="request_method",
        path_field="request_path",
        status_field="response_status",
        time_field="response_time",
        time_unit="s",
    ),
]


def extract_request(payload: dict) -> tuple[str, str, int, int] | None:
    """
    Convert a raw JSON payload into the canonical request tuple if possible.

    The registry order matters. The first shape that recognizes the payload wins,
    which keeps extraction cheap and predictable as long as each shape uses a
    distinctive trigger field. Payloads that do not resemble any known access
    log format return ``None`` so callers can skip them quietly.
    """
    for shape in SHAPES:
        result = shape.extract(payload)
        if result is not None:
            return result
    return None
