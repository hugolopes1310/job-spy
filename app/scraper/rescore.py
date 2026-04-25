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


def main() -> None:
    p = argparse.ArgumentParser(description="Rescore existing matches with the current scorer prompt")
    p.add_argument("--user", required=True, help="Email of the user whose matches to rescore")
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
        help="Max number of matches to rescore this run (Groq quota safety).",
    )
    p.add_argument(
        "--sleep",
        type=float,
        default=LLM_COOLDOWN_SEC,
        help=f"Seconds to sleep between LLM calls (default {LLM_COOLDOWN_SEC})",
    )
    args = p.parse_args()

    print(f"[rescore] looking up user {args.user!r}…")
    users = [u for u in list_active_user_configs()
             if u["email"].lower() == args.user.strip().lower()]
    if not users:
        print(f"[rescore] no approved user with email {args.user!r}")
        sys.exit(1)
    user = users[0]
    user_id = user["user_id"]
    config = user.get("config") or {}
    cv_text = user.get("cv_text") or ""
    print(f"[rescore] user_id={user_id}  config_keys={list(config.keys())}  cv_chars={len(cv_text)}")

    matches = _fetch_matches_for_user(user_id)
    print(f"[rescore] fetched {len(matches)} existing match(es)")

    def _eligible(m: dict) -> bool:
        if args.max_old_score is None:
            return True
        old = m.get("score")
        return old is None or old <= args.max_old_score

    targets = [m for m in matches if _eligible(m)]
    if args.limit is not None:
        targets = targets[: args.limit]
    print(f"[rescore] {len(targets)} match(es) in scope "
          f"(max_old_score={args.max_old_score}, limit={args.limit})")
    if not targets:
        print("[rescore] nothing to do.")
        return

    svc = get_service_client()

    # ------------------------------------------------------------------
    # Process
    # ------------------------------------------------------------------
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
                    # Don't touch status/scored_at/seen_at/is_favorite/notes — we're
                    # only updating the scoring output, not re-treating the match as new.
                }).eq("user_id", user_id).eq("job_id", m["job_id"]).execute()
            except Exception as e:  # noqa: BLE001
                print(f"[rescore]   ⚠ upsert failed: {e}")
                fail += 1

        time.sleep(args.sleep)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    ups = [d for d in diffs
           if isinstance(d[1], int) and isinstance(d[2], int) and d[2] > d[1]]
    downs = [d for d in diffs
             if isinstance(d[1], int) and isinstance(d[2], int) and d[2] < d[1]]
    same = [d for d in diffs
            if isinstance(d[1], int) and isinstance(d[2], int) and d[2] == d[1]]

    print()
    print("=== Summary ===")
    print(f"  rescored: {len(diffs)}   failures: {fail}")
    print(f"  ↑ boosted: {len(ups)}    ↓ lowered: {len(downs)}    = unchanged: {len(same)}")

    def _bucket(v):
        if not isinstance(v, int):
            return "N/A"
        if v >= 8:
            return ">=8"
        if v >= 6:
            return "6-7"
        if v >= 4:
            return "4-5"
        return "<4"

    buckets_before: dict[str, int] = {}
    buckets_after: dict[str, int] = {}
    for _, o, n in diffs:
        buckets_before[_bucket(o)] = buckets_before.get(_bucket(o), 0) + 1
        buckets_after[_bucket(n)] = buckets_after.get(_bucket(n), 0) + 1
    print()
    print("  distribution:")
    print("    bucket  before  after")
    for b in (">=8", "6-7", "4-5", "<4", "N/A"):
        bf = buckets_before.get(b, 0)
        af = buckets_after.get(b, 0)
        if bf or af:
            print(f"    {b:>6}  {bf:>6}  {af:>5}")

    if ups:
        print()
        print("  TOP BOOSTS (old → new):")
        ups_sorted = sorted(ups, key=lambda d: (d[2] - d[1]), reverse=True)[:10]
        for m, o, n in ups_sorted:
            j = m["job"]
            print(f"    {_fmt_score(o)} → {_fmt_score(n)}  "
                  f"{_short(j.get('title'), 46):48s} @ {_short(j.get('company'), 22)}")

    if args.dry_run:
        print()
        print("(dry-run — no rows written. Re-run without --dry-run to persist.)")


if __name__ == "__main__":
    main()
