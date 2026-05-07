"""Pluggable extractors for known JSON access-log shapes.

Each :class:`LogShape` describes how to read four fields out of a JSON
payload: HTTP method, request path, response status, and response time
(plus the unit the time is in). :func:`extract_request` tries every
registered shape in order and returns the first match.

To add support for a new log format, append a new ``LogShape`` to the
``SHAPES`` list at the bottom of this module. If your format has a
unique top-level key, name it as ``time_field`` and you're done.
"""

from __future__ import annotations

from dataclasses import dataclass

# Multiplier from each supported unit to microseconds.
_TO_US = {"us": 1, "ms": 1_000, "s": 1_000_000}


@dataclass(frozen=True)
class LogShape:
    """Description of one JSON access-log shape.

    A shape "matches" a payload when ``time_field`` is present at the
    top level. The four field names point to the JSON keys to read;
    ``time_unit`` selects the multiplier used to convert the raw time
    value to microseconds.
    """

    name: str
    method_field: str
    path_field: str
    status_field: str
    time_field: str
    time_unit: str  # "us" | "ms" | "s"

    def extract(self, payload: dict) -> tuple[str, str, int, int] | None:
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
    """Try each registered :class:`LogShape` until one matches.

    Returns ``(method, path, response_time_us, status)`` or ``None`` if
    no shape recognised the payload.
    """
    for shape in SHAPES:
        result = shape.extract(payload)
        if result is not None:
            return result
    return None
