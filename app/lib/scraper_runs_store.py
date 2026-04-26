"""CRUD for the `scraper_runs` table — telemetry only (Phase 5).

A row is inserted at the start of each scraper pass (cron OR manual button)
and updated to a terminal status (`ok` / `failed` / `partial`) when the pass
finishes. The data fuels the future admin panel — users don't read it.

Design constraints :
  - Service-role only (the scraper runs in GitHub Actions with the service
    key, the dashboard "Lancer recherche" button also has access via the
    streamlit session). Admins read via RLS policy.
  - Telemetry MUST NEVER raise. If Supabase is down, we want the actual
    scrape work to keep going — every helper here swallows exceptions and
    returns sentinels (None / False).
  - The `errors` JSONB array is bounded at `_MAX_ERRORS_PER_RUN` to keep
    the row payload sane. The scraper currently logs ~1 error per failed
    user, so 50 leaves headroom.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.lib.klog import log
from app.lib.supabase_client import get_service_client


# Hard cap on per-run errors. The scraper would have to be VERY broken to
# hit this — at that point a stack trace per user blows up the JSONB anyway.
_MAX_ERRORS_PER_RUN = 50

# Any `running` row older than this is presumed dead (the GHA workflow killed
# it via cancel-in-progress, the Streamlit session died mid-spinner, the dyno
# crashed, …). A healthy run never crosses ~10 min ; 30 leaves comfortable
# headroom while keeping the admin panel free of zombie rows.
_STALE_RUNNING_MINUTES = 30


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sweep_stale_running_runs() -> None:
    """Mark `running` rows older than _STALE_RUNNING_MINUTES as 'failed'.

    Called from start_run() on every new pass — the cheapest place to catch
    zombies (no extra cron job needed). Failures are swallowed : a sweep that
    doesn't run is harmless, but raising here would block real telemetry.

    The cutoff is computed in Python (not SQL `now()`) so we never need to
    worry about Supabase clock skew vs. the runner.
    """
    try:
        from datetime import timedelta  # local — keeps the module hot path lean

        cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=_STALE_RUNNING_MINUTES)
        ).isoformat()
        # We can't append to the JSONB array via the REST API in one round-trip,
        # so we just flip the status + finished_at + a generic note. The errors
        # column stays as whatever was there at start_run time (typically []).
        get_service_client().table("scraper_runs").update(
            {
                "status": "failed",
                "finished_at": _now_iso(),
                "notes": "auto-marked failed: stale running row (see _sweep_stale_running_runs)",
            }
        ).eq("status", "running").lt("started_at", cutoff).execute()
    except Exception as e:  # noqa: BLE001
        log("scraper_runs.sweep_failed", level="warn",
            error=f"{type(e).__name__}: {e}")


def start_run(
    *,
    runner: str = "cron",
    triggered_by_user_id: str | None = None,
    notes: str | None = None,
) -> str | None:
    """Insert a `scraper_runs` row in status='running'. Returns its id, or
    None if the insert failed (telemetry never blocks the actual scrape).

    Args:
        runner: 'cron' for GHA, 'manual' for the dashboard button, 'cli'
                for `python -m app.scraper.run` invoked locally.
        triggered_by_user_id: only set for runner='manual' (the user who
                clicked the button — for audit).
        notes: free-text. The GHA workflow stuffs the run URL here.
    """
    if runner not in ("cron", "manual", "cli"):
        log("scraper_runs.bad_runner", level="warn", runner=runner)
        runner = "cli"  # fallback rather than crash

    # Sweep zombies before opening a fresh row. We do it here (not in finish)
    # because finish only fires on healthy completion — exactly the path we
    # don't need to clean. Stale rows show up when a previous run was KILLED.
    _sweep_stale_running_runs()

    payload: dict[str, Any] = {
        "runner": runner,
        "status": "running",
        "started_at": _now_iso(),
        "totals": {},
        "errors": [],
    }
    if triggered_by_user_id:
        payload["triggered_by_user_id"] = triggered_by_user_id
    if notes:
        payload["notes"] = notes[:500]  # bound to a sane length

    try:
        res = (
            get_service_client()
            .table("scraper_runs")
            .insert(payload)
            .execute()
        )
        rows = list(res.data or [])
        if not rows:
            log("scraper_runs.insert_no_data", level="warn")
            return None
        return rows[0].get("id")
    except Exception as e:  # noqa: BLE001
        # Telemetry path — never block the actual scrape. Log + continue.
        log("scraper_runs.start_failed", level="warn",
            error=f"{type(e).__name__}: {e}")
        return None


def finish_run(
    run_id: str | None,
    *,
    status: str = "ok",
    totals: dict[str, Any] | None = None,
    errors: list[dict[str, Any]] | None = None,
    llm_quota: dict[str, Any] | None = None,
    notes: str | None = None,
) -> bool:
    """Mark a run as finished. Returns True if the update went through.

    No-op (returns False) when run_id is None — happens when start_run() failed.
    Errors are bounded server-side too, but we trim client-side to keep the
    payload compact.

    `status` conventions :
        'ok'      : every user processed without unhandled exception
        'partial' : at least one user errored but the run completed
        'failed'  : the run itself crashed (top-level exception)
    """
    if run_id is None:
        return False
    if status not in ("ok", "failed", "partial"):
        log("scraper_runs.bad_status", level="warn", status=status)
        status = "failed"  # be conservative — flag it for review

    patch: dict[str, Any] = {
        "status": status,
        "finished_at": _now_iso(),
        "totals": totals or {},
        "errors": (errors or [])[:_MAX_ERRORS_PER_RUN],
    }
    if llm_quota is not None:
        patch["llm_quota"] = llm_quota
    if notes is not None:
        # Don't OVERWRITE a notes value that was set at start_run time —
        # only set it here if a caller explicitly passes one. (We append
        # rather than replace by passing the new value verbatim ; the
        # caller is expected to merge if they care.)
        patch["notes"] = notes[:500]

    try:
        get_service_client().table("scraper_runs").update(patch).eq(
            "id", run_id
        ).execute()
        return True
    except Exception as e:  # noqa: BLE001
        log("scraper_runs.finish_failed", level="warn",
            run_id=run_id, error=f"{type(e).__name__}: {e}")
        return False


def get_last_run(*, only_status: str | None = None) -> dict | None:
    """Return the most recent run row, or None if the table is empty / failed.

    Args:
        only_status: filter on a specific status (e.g. 'ok' to skip the
                     in-flight `running` row when the dashboard polls).
    """
    try:
        q = (
            get_service_client()
            .table("scraper_runs")
            .select("*")
            .order("started_at", desc=True)
            .limit(1)
        )
        if only_status:
            q = q.eq("status", only_status)
        res = q.execute()
        rows = list(res.data or [])
        return rows[0] if rows else None
    except Exception as e:  # noqa: BLE001
        log("scraper_runs.get_last_failed", level="warn",
            error=f"{type(e).__name__}: {e}")
        return None


def get_last_run_for_user(user_id: str) -> dict | None:
    """Return the most recent FINISHED run that's relevant to this user.

    "Relevant" =
      * any cron run (the cron processes every approved user, so it's the
        user's last automatic refresh too) ; OR
      * a manual run this user triggered themselves (`runner='manual'` AND
        `triggered_by_user_id = user_id`).

    `running` rows are excluded so the dashboard never advertises an in-flight
    run as "last completed". Reads via service_role on purpose : the data we
    return (started_at / status / runner) carries no PII, and we narrow the
    WHERE clause server-side to rows the user "owns".

    Returns None on miss / DB error — callers MUST fall back to whatever
    secondary signal they had (e.g. max(scored_at)).
    """
    if not user_id:
        return None
    try:
        # Supabase-py builds OR filters via the `or_` keyword on a
        # comma-separated PostgREST expression.
        res = (
            get_service_client()
            .table("scraper_runs")
            .select("started_at,finished_at,status,runner,triggered_by_user_id")
            .neq("status", "running")
            .or_(f"runner.eq.cron,triggered_by_user_id.eq.{user_id}")
            .order("started_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = list(res.data or [])
        return rows[0] if rows else None
    except Exception as e:  # noqa: BLE001
        log("scraper_runs.get_last_for_user_failed", level="warn",
            user_id=user_id, error=f"{type(e).__name__}: {e}")
        return None


def list_recent_runs(limit: int = 20) -> list[dict]:
    """Return the N most recent runs, newest first. Empty list on failure."""
    try:
        res = (
            get_service_client()
            .table("scraper_runs")
            .select("*")
            .order("started_at", desc=True)
            .limit(max(1, min(200, int(limit))))
            .execute()
        )
        return list(res.data or [])
    except Exception as e:  # noqa: BLE001
        log("scraper_runs.list_failed", level="warn",
            error=f"{type(e).__name__}: {e}")
        return []
