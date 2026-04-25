"""Smoke tests for app.lib.klog (structured logging helper).

Run from the repo root:
    PYTHONPATH=. python app/lib/test_klog.py
"""
from __future__ import annotations

import importlib
import io
import json
import os
import sys
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def _capture(fn, *, json_mode: bool = False, level: str | None = None):
    """Run `fn()` and capture (stdout, stderr) lines emitted by the logger."""
    out, err = io.StringIO(), io.StringIO()
    # Re-import the module so env-var changes take effect (the module reads
    # LOG_AS_JSON / LOG_LEVEL at import time).
    if json_mode:
        os.environ["LOG_AS_JSON"] = "1"
    else:
        os.environ.pop("LOG_AS_JSON", None)
    if level is None:
        os.environ.pop("LOG_LEVEL", None)
    else:
        os.environ["LOG_LEVEL"] = level
    import app.lib.klog as klog  # noqa: E402
    klog = importlib.reload(klog)

    with redirect_stdout(out), redirect_stderr(err):
        fn(klog)
    return out.getvalue().strip().splitlines(), err.getvalue().strip().splitlines()


def test_human_format():
    def run(klog):
        klog.log("scrape.start", user_id="u-1", count=3)
        klog.log("scrape.error", level="error", error="boom")

    out, err = _capture(run)
    assert any("event=scrape.start" in l and "user_id=u-1" in l and "count=3" in l for l in out), out
    # Errors go to stderr.
    assert any("event=scrape.error" in l for l in err), err
    print("[OK] human format routes info→stdout, error→stderr")


def test_json_format():
    def run(klog):
        klog.log("scorer.done", score=8, model="groq")

    out, _ = _capture(run, json_mode=True)
    assert out, "expected at least one line"
    payload = json.loads(out[0])
    assert payload["event"] == "scorer.done"
    assert payload["score"] == 8
    assert payload["model"] == "groq"
    assert payload["level"] == "info"
    assert "ts" in payload
    print("[OK] json format")


def test_bind_preserves_context():
    def run(klog):
        logger = klog.bind(user_id="u-7", run_id="r-42")
        logger.info("scrape.begin", source="linkedin")
        sublogger = logger.bind(job_id="j-99")
        sublogger.warn("scrape.slow", ms=12500)

    out, err = _capture(run)
    line1 = next(l for l in out if "scrape.begin" in l)
    assert "user_id=u-7" in line1 and "run_id=r-42" in line1 and "source=linkedin" in line1, line1
    line2 = next(l for l in err if "scrape.slow" in l)
    assert "user_id=u-7" in line2 and "job_id=j-99" in line2 and "ms=12500" in line2, line2
    print("[OK] bind() preserves and extends context")


def test_quote_strings_with_spaces():
    def run(klog):
        klog.log("scorer.parse_failed", reason="bad json blob with spaces")

    out, _ = _capture(run)
    line = next(l for l in out if "parse_failed" in l)
    assert 'reason="bad json blob with spaces"' in line, line
    print("[OK] strings with spaces are JSON-quoted")


def test_level_filter():
    def run(klog):
        klog.log("noisy", level="debug")
        klog.log("kept", level="info")

    out, err = _capture(run, level="info")
    assert all("noisy" not in l for l in out), out
    assert any("kept" in l for l in out), out
    print("[OK] LOG_LEVEL=info hides debug")


def test_timed_block():
    def run(klog):
        with klog.timed("scorer.call", model="groq"):
            pass

    out, _ = _capture(run)
    line = next(l for l in out if "scorer.call" in l)
    assert "model=groq" in line and "ms=" in line, line
    print("[OK] timed() emits ms=…")


def test_timed_block_error():
    def run(klog):
        try:
            with klog.timed("scorer.call", model="groq"):
                raise ValueError("kaboom")
        except ValueError:
            pass

    _, err = _capture(run)
    line = next(l for l in err if "scorer.call" in l)
    assert "error=" in line and "ValueError" in line, line
    print("[OK] timed() upgrades level→error when wrapped block raises")


def test_log_never_raises():
    def run(klog):
        # Pass an un-stringable object; logger should not blow up.
        class _Boom:
            def __str__(self):
                raise RuntimeError("can't stringify me")

        klog.log("ok.event", trouble=_Boom())  # must not raise

    # Just verify no exception escapes.
    _capture(run)
    print("[OK] log() swallows internal errors")


if __name__ == "__main__":
    test_human_format()
    test_json_format()
    test_bind_preserves_context()
    test_quote_strings_with_spaces()
    test_level_filter()
    test_timed_block()
    test_timed_block_error()
    test_log_never_raises()
    print("\nAll logging tests passed.")
