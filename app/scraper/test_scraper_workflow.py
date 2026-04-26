"""Smoke test for .github/workflows/scraper.yml (Phase 5).

This file is small but it's the load-bearing piece of "the scraper actually
runs in production". Cheap regression guards :

  - Valid YAML (catches accidental indentation breaks on edit).
  - Hourly cron + workflow_dispatch trigger present.
  - All four required secrets are referenced (and spelled correctly — typos
    silently inject empty strings into env without failing the workflow).
  - The python module path is `app.scraper.run` (not e.g. `scraper.run`).
  - Sane timeout-minutes so a hung run can't burn GHA minutes forever.

Run from the repo root :
    PYTHONPATH=. python app/scraper/test_scraper_workflow.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


WORKFLOW = ROOT / ".github" / "workflows" / "scraper.yml"


def _load_yaml() -> dict:
    """yaml is in requirements.txt (`pyyaml>=6.0`) so safe_load is available."""
    import yaml  # noqa: WPS433
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_workflow_file_exists():
    assert WORKFLOW.exists(), f"missing workflow file: {WORKFLOW}"
    print(f"[OK] {WORKFLOW.relative_to(ROOT)} exists")


def test_workflow_is_valid_yaml():
    doc = _load_yaml()
    assert isinstance(doc, dict), "workflow root must be a mapping"
    print("[OK] scraper.yml is valid YAML (root is a mapping)")


def test_has_hourly_cron_trigger():
    """PyYAML maps the bare `on:` key to True (YAML 1.1 boolean coercion).
    Either spelling — the boolean-True key OR the literal 'on' string — is
    acceptable."""
    doc = _load_yaml()
    triggers = doc.get(True, doc.get("on"))
    assert triggers is not None, f"no triggers section, top keys={list(doc)}"
    schedule = triggers.get("schedule") or []
    assert any(item.get("cron") == "0 * * * *" for item in schedule), schedule
    print("[OK] hourly cron '0 * * * *' configured")


def test_has_manual_trigger():
    doc = _load_yaml()
    triggers = doc.get(True, doc.get("on"))
    assert "workflow_dispatch" in triggers, list(triggers)
    print("[OK] workflow_dispatch (manual trigger) enabled")


def test_references_all_four_secrets():
    """A typo in a secret name silently injects "" into env — guard against it."""
    text = WORKFLOW.read_text(encoding="utf-8")
    for secret in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "GROQ_API_KEY", "GEMINI_API_KEY"):
        assert f"secrets.{secret}" in text, f"secret reference missing: {secret}"
    print("[OK] all 4 required secrets referenced (SUPABASE_URL/SERVICE_KEY/GROQ/GEMINI)")


def test_invokes_scraper_module():
    """Catches module-path drift if someone refactors the package layout."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "python -m app.scraper.run" in text, (
        "workflow must invoke `python -m app.scraper.run`"
    )
    print("[OK] workflow invokes `python -m app.scraper.run`")


def test_has_sane_timeout():
    """timeout-minutes must exist and be in a reasonable bracket — not too
    short (kills healthy runs) nor too long (runaway burns minutes)."""
    doc = _load_yaml()
    job = next(iter(doc["jobs"].values()))
    timeout = job.get("timeout-minutes")
    assert timeout is not None, "job must declare timeout-minutes"
    assert 10 <= timeout <= 60, f"timeout-minutes {timeout} outside sane range"
    print(f"[OK] timeout-minutes={timeout} (within 10..60)")


def test_concurrency_cancels_in_progress():
    """Two cron ticks while one is mid-run would duplicate work + burn LLM
    quota — the workflow MUST cancel the older run."""
    doc = _load_yaml()
    cc = doc.get("concurrency")
    assert cc, "missing concurrency block"
    assert cc.get("cancel-in-progress") is True, cc
    print("[OK] concurrency.cancel-in-progress=true (no duplicated runs)")


def test_uses_supported_python_version():
    """The codebase uses 3.10+ syntax (PEP 604 union types) so 3.11 is required."""
    doc = _load_yaml()
    job = next(iter(doc["jobs"].values()))
    setup = next(
        (s for s in job["steps"] if isinstance(s.get("uses"), str)
         and s["uses"].startswith("actions/setup-python")),
        None,
    )
    assert setup is not None, "no actions/setup-python step"
    py_version = str(setup["with"]["python-version"])
    assert py_version.startswith("3."), py_version
    major, minor = py_version.split(".")[:2]
    assert int(minor) >= 10, f"Python {py_version} too old for app/ (uses PEP 604)"
    print(f"[OK] uses Python {py_version} (>=3.10 required for type unions)")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    test_workflow_file_exists()
    test_workflow_is_valid_yaml()
    test_has_hourly_cron_trigger()
    test_has_manual_trigger()
    test_references_all_four_secrets()
    test_invokes_scraper_module()
    test_has_sane_timeout()
    test_concurrency_cancels_in_progress()
    test_uses_supported_python_version()
    print("\nAll scraper workflow smoke tests passed.")
