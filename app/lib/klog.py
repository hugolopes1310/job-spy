"""Structured-ish logging helper for Kairo.

We don't want a heavy dependency. The print-based output we already use
is fine — but a) it's hard to grep with structured fields, and b) the
scraper / scorer / jobs_store all use slightly different prefixes.

This module gives us:

  - `log(event, level="info", **fields)` → emits a single line that's both
    human-readable and trivially `grep | jq`-able.
  - `bind(**ctx)` → returns a logger with `user_id=…`, `job_id=…`, etc.
    pre-filled, so call-sites only pass the event-specific bits.
  - `LOG_AS_JSON=1` env var to switch to pure JSON output (for log
    aggregators on GitHub Actions).

Output formats:
  Human:   [INFO] event=scrape.start user=u-abc src=linkedin q='Quant'
  JSON:    {"ts":"...","level":"info","event":"scrape.start","user_id":"u-abc",...}

Intentionally tiny — no rotation, no formatter classes, no colors.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any


_LEVELS = ("debug", "info", "warn", "error")
_DEFAULT_LEVEL = (os.environ.get("LOG_LEVEL") or "info").lower()
_LOG_AS_JSON = os.environ.get("LOG_AS_JSON", "").lower() in ("1", "true", "yes")


def _level_idx(name: str) -> int:
    try:
        return _LEVELS.index(name.lower())
    except ValueError:
        return 1  # info


def _enabled(level: str) -> bool:
    return _level_idx(level) >= _level_idx(_DEFAULT_LEVEL)


def _format_value(v: Any) -> str:
    """Quote strings that contain whitespace; everything else passed through."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if any(c in s for c in (" ", "\t", "=", '"')):
        return json.dumps(s, ensure_ascii=False)
    return s


def _emit(line: str, level: str) -> None:
    stream = sys.stderr if level in ("warn", "error") else sys.stdout
    print(line, file=stream, flush=True)


def log(
    event: str,
    *,
    level: str = "info",
    _ctx: dict[str, Any] | None = None,
    **fields: Any,
) -> None:
    """Emit one structured log line.

    Args:
        event:  short dotted name, e.g. ``scrape.start`` or ``scorer.parse_failed``.
        level:  one of debug / info / warn / error.
        _ctx:   internal — injected by ``bind``.
        **fields: extra k/v context (user_id, job_id, count, error, ...).

    Never raises — logging must not break the call site.
    """
    if not _enabled(level):
        return
    try:
        merged: dict[str, Any] = {}
        if _ctx:
            merged.update(_ctx)
        merged.update(fields)

        if _LOG_AS_JSON:
            payload = {
                "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "level": level,
                "event": event,
                **merged,
            }
            _emit(json.dumps(payload, ensure_ascii=False, default=str), level)
            return

        parts = [f"[{level.upper()}]", f"event={event}"]
        for k, v in merged.items():
            parts.append(f"{k}={_format_value(v)}")
        _emit(" ".join(parts), level)
    except Exception:  # noqa: BLE001
        # Logging must never break the caller. Swallow.
        try:
            print(f"[ERROR] event=log.failed event_attempted={event}", file=sys.stderr)
        except Exception:  # noqa: BLE001
            pass


class _BoundLogger:
    """Lightweight logger that carries a context dict (user_id, run_id, ...)."""

    __slots__ = ("_ctx",)

    def __init__(self, ctx: dict[str, Any]):
        self._ctx = dict(ctx)

    def bind(self, **more: Any) -> "_BoundLogger":
        merged = dict(self._ctx)
        merged.update(more)
        return _BoundLogger(merged)

    def log(self, event: str, *, level: str = "info", **fields: Any) -> None:
        log(event, level=level, _ctx=self._ctx, **fields)

    def debug(self, event: str, **fields: Any) -> None:
        log(event, level="debug", _ctx=self._ctx, **fields)

    def info(self, event: str, **fields: Any) -> None:
        log(event, level="info", _ctx=self._ctx, **fields)

    def warn(self, event: str, **fields: Any) -> None:
        log(event, level="warn", _ctx=self._ctx, **fields)

    def error(self, event: str, **fields: Any) -> None:
        log(event, level="error", _ctx=self._ctx, **fields)


def bind(**ctx: Any) -> _BoundLogger:
    """Build a logger pre-filled with context (user_id, job_id, run_id, ...).

    Usage:
        logger = bind(user_id="u-1", run_id="r-7")
        logger.info("scrape.done", count=42)
    """
    return _BoundLogger(ctx)


# ---------------------------------------------------------------------------
# Tiny convenience: time a block.
# ---------------------------------------------------------------------------
class timed:
    """Context manager that emits ``event=<name> ms=<duration>`` on exit.

    Usage:
        with timed("scorer.call", logger=logger, model="groq"):
            result = call_groq(...)
    """

    def __init__(
        self,
        event: str,
        *,
        logger: _BoundLogger | None = None,
        level: str = "info",
        **fields: Any,
    ):
        self._event = event
        self._logger = logger
        self._level = level
        self._fields = fields
        self._t0 = 0.0

    def __enter__(self) -> "timed":
        self._t0 = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        ms = int((time.monotonic() - self._t0) * 1000)
        f = dict(self._fields)
        f["ms"] = ms
        if exc is not None:
            f["error"] = f"{exc_type.__name__}: {exc}"
            level = "error"
        else:
            level = self._level
        if self._logger is not None:
            self._logger.log(self._event, level=level, **f)
        else:
            log(self._event, level=level, **f)
        # Don't swallow exceptions.
        return None
