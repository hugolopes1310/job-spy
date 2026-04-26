"""One-shot rescorer — re-rate existing `user_job_matches` with the current prompt.

Use this after a prompt change in `app/lib/scorer.py` to update historic matches
without waiting for the scraper to re-encounter the same jobs.

Examples:
    # Dry-run (show before/after for Hugo, no DB write)
    python -m app.scraper.rescore --user lopeshugo1310@gmail.com --dry-run

    # Full rescore for Hugo
    python -m app.scraper.rescore --user lopeshugo1310@gmail.com

    # Only rescore matches with score < 4 (the ones hurt by the old prompt)
    python -m app.scraper.rescore --user lopeshugo1310@gmail.com --max-old-score 3

    # Cap how many we touch per run (Groq quota safety)
    python -m app.scraper.rescore --user lopeshugo1310@gmail.com --limit 50

    # Re-queue mode: only retry rows that failed parsing last time.
    # Lighter touch: skips healthy rows entirely, ideal for nightly cron.
    python -m app.scraper.rescore --user lopeshugo1310@gmail.com --only-failed

    # Re-queue ALL approved users at once (skip --user). Used by the nightly
    # GitHub Actions workflow.
    python -m app.scraper.rescore --all-users --only-failed

Env vars (or .streamlit/secrets.toml):
    SUPABASE_URL
    SUPABASE_SERVICE_KEY
    GROQ_API_KEY  (or GEMINI_API_KEY)
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Make `app` importable as a module when running as a script.
ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.lib.scorer import analyze_offer_for_user  # noqa: E402
from app.lib.storage import list_active_user_configs  # noqa: E402
from app.lib.supabase_client import get_service_client  # noqa: E402


# Same cooldown as the scraper: Groq free tier = 30 req/min → 4s is comfortable.
LLM_COOLDOWN_SEC = 4.0


def _fetch_matches_for_user(user_id: str) -> list[dict]:
    """Return every existing match for the user, enriched with the job payload.

    Uses two queries (matches + jobs) and joins in Python — avoids depending on
    the `user_matches_enriched` view schema which may evolve.
    """
    svc = get_service_client()
    m_res = (
        svc.table("user_job_matches")
        .select("user_id, job_id, score, analysis, status, scored_at")
        .eq("user_id", user_id)
        .execute()
    )
    matches = list(m_res.data or [])
    if not matches:
        return []

    job_ids = [m["job_id"] for m in matches]

    # Supabase PostgREST `in_` handles ~100 ids comfortably; chunk to be safe.
    jobs_by_id: dict[str, dict] = {}
    CHUNK = 80
    for i in range(0, len(job_ids), CHUNK):
        chunk = job_ids[i:i + CHUNK]
        j_res = (
            svc.table("jobs")
            .select("id, title, company, location, description, url, site, date_posted")
            .in_("id", chunk)
            .execute()
        )
        for row in (j_res.data or []):
            jobs_by_id[row["id"]] = row

    enriched = []
    for m in matches:
        job = jobs_by_id.get(m["job_id"])
        if not job:
            continue  # orphan match (job deleted) — skip
        enriched.append({**m, "job": job})
    return enriched


def _short(s: str | None, n: int = 45) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


def _fmt_score(v) -> str:
    return f"{v:>2}" if isinstance(v, int) else " -"


def _is_failed_analysis(analysis: Any) -> bool:
    """A match needs re-queueing if its analysis carries an _error marker
    (parse_failed, llm_unavailable) OR was produced by the heuristic fallback.

    The heuristic is "good enough" but we'd rather have a real LLM score
    once the quota refreshes — so we re-queue heuristic results too.
    """
    if not isinstance(analysis, dict):
        return analysis is None  # NULL analysis → re-queue
    if analysis.get("_error"):
        return True
    if analysis.get("_method") == "heuristic":
        return True
    return False


def _process_one_user(args, user: dict) -> tuple[int, int]:
    """Rescore eligible matches for a single user. Returns (rescored, failures)."""
    user_id = user["user_id"]
    email = user.get("email") or "?"
    config = user.get("config") or {}
    cv_text = user.get("cv_text") or ""
    print(f"\n[rescore] === user={email}  user_id={user_id}  cv_chars={len(cv_text)} ===")

    matches = _fetch_matches_for_user(user_id)
    print(f"[rescore] fetched {len(matches)} existing match(es)")

    def _eligible(m: dict) -> bool:
        # --only-failed shortcut: skip everything that has a clean LLM analysis.
        if args.only_failed and not _is_failed_analysis(m.get("analysis")):
            return False
        if args.max_old_score is None:
            return True
        old = m.get("score")
        return old is None or old <= args.max_old_score

    targets = [m for m in matches if _eligible(m)]
    if args.limit is not None:
        targets = targets[: args.limit]
    print(f"[rescore] {len(targets)} match(es) in scope "
          f"(only_failed={args.only_failed}, max_old_score={args.max_old_score}, "
          f"limit={args.limit})")
    if not targets:
        print("[rescore] nothing to do for this user.")
        return 0, 0

    svc = get_service_client()
    diffs: list[tuple[dict, int | None, int | None]] = []
    fail = 0

    for i, m in enumerate(targets, 1):
        job = m["job"]
        old_score = m.get("score")
        try:
            analysis = analyze_offer_for_user(config, cv_text, {
                "title": job.get("title"),
                "company": job.get("company"),
                "location": job.get("location"),
                "description": job.get("description"),
            })
        except Exception as e:  # noqa: BLE001
            print(f"[rescore] [{i}/{len(targets)}] LLM error: {e}")
            fail += 1
            time.sleep(args.sleep)
            continue

        if analysis is None:
            print(f"[rescore] [{i}/{len(targets)}] LLM returned None — skipping")
            fail += 1
            time.sleep(args.sleep)
            continue

        new_score = analysis.get("score")
        diffs.append((m, old_score, new_score))

        delta = ""
        if isinstance(old_score, int) and isinstance(new_score, int):
            d = new_score - old_score
            delta = f"  ({'+' if d >= 0 else ''}{d})"

        print(
            f"[rescore] [{i}/{len(targets)}] "
            f"{_fmt_score(old_score)} → {_fmt_score(new_score)}{delta}  "
            f"{_short(job.get('title'), 42)!s:44s} @ {_short(job.get('company'), 22)}"
        )

        if not args.dry_run:
            try:
                svc.table("user_job_matches").update({
                    "score": new_score,
                    "analysis": analysis,
                }).eq("user_id", user_id).eq("job_id", m["job_id"]).execute()
            except Exception as e:  # noqa: BLE001
                print(f"[rescore]   ⚠ upsert failed: {e}")
                fail += 1

        time.sleep(args.sleep)

    return len(diffs), fail


def main() -> None:
    p = argparse.ArgumentParser(description="Rescore existing matches with the current scorer prompt")
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--user", help="Email of the user whose matches to rescore")
    grp.add_argument("--all-users", action="store_true",
                     help="Rescore matches for every approved user (used by nightly cron)")
    p.add_argument("--dry-run", action="store_true", help="Compute new scores, print diff, don't write")
    p.add_argument(
        "--max-old-score",
        type=int,
        default=None,
        help="Only rescore matches with old score <= this (e.g. 3 to focus on losers). "
             "Matches with NULL score are always included.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max number of matches to rescore this run (Groq quota safety). "
             "Applied PER USER when --all-users is set.",
    )
    p.add_argument(
        "--only-failed",
        action="store_true",
        help="Only re-queue rows where the previous analysis failed (parse_failed, "
             "llm_unavailable, or heuristic fallback). Cheap nightly retry mode.",
    )
    p.add_argument(
        "--sleep",
        type=float,
        default=LLM_COOLDOWN_SEC,
        help=f"Seconds to sleep between LLM calls (default {LLM_COOLDOWN_SEC})",
    )
    args = p.parse_args()

    all_users = list_active_user_configs()
    if args.all_users:
        targets_users = all_users
        print(f"[rescore] --all-users: {len(targets_users)} approved user(s) in scope")
    else:
        targets_users = [u for u in all_users
                         if u["email"].lower() == args.user.strip().lower()]
        if not targets_users:
            print(f"[rescore] no approved user with email {args.user!r}")
            sys.exit(1)

    total_rescored = 0
    total_fail = 0
    for u in targets_users:
        try:
            r, f = _process_one_user(args, u)
        except Exception as e:  # noqa: BLE001
            print(f"[rescore] user {u.get('email')!r} crashed: {e} — skipping")
            total_fail += 1
            continue
        total_rescored += r
        total_fail += f

    print()
    print("=== TOTAL ===")
    print(f"  users processed: {len(targets_users)}")
    print(f"  rescored:        {total_rescored}")
    print(f"  failures:        {total_fail}")

    if args.dry_run:
        print("\n(dry-run — no rows written. Re-run without --dry-run to persist.)")


if __name__ == "__main__":
    main()
