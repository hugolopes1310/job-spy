"""Phase 3 scraper runner — multi-user.

Pulls the list of approved users + their configs from Supabase, derives scraper
queries per user, scrapes LinkedIn / Indeed / Google Jobs via `python-jobspy`,
dedupes into the shared `jobs` table, then scores each new (user × job) pair
via Groq and writes the match to `user_job_matches`.

Designed to run as a GitHub Actions job. Also works locally:

    # Full run (all approved users)
    python -m app.scraper.run

    # Target one user (by email)
    python -m app.scraper.run --user lopeshugo1310@gmail.com

    # Dry run — scrape + print, no writes
    python -m app.scraper.run --dry-run

    # Also drop stale jobs (not seen for 60+ days)
    python -m app.scraper.run --cleanup

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

from app.lib.career_sites import scrape_career_site  # noqa: E402
from app.lib.jobs_store import (  # noqa: E402
    cleanup_stale_jobs,
    insert_match,
    match_exists,
    upsert_job,
)
from app.lib.klog import bind  # noqa: E402
from app.lib.query_builder import build_queries  # noqa: E402
from app.lib.scorer import (  # noqa: E402
    analyze_offer_for_user,
    llm_quota_state,
    make_parse_failed_analysis,
)
from app.lib.scrapers import scrape_via_jobspy  # noqa: E402
from app.lib.storage import (  # noqa: E402
    list_active_user_configs,
    update_custom_source_status,
)


# ---------------------------------------------------------------------------
# Config knobs
# ---------------------------------------------------------------------------
# Pause between LLM calls — Groq free tier is 30 req/min = 2s min.
# 4s gives comfortable headroom + accounts for network jitter + 429 retries.
LLM_COOLDOWN_SEC = 4.0

# Cap how many jobs we score per user per run, to avoid blowing up the Groq
# quota on the first run (when every job is new).
MAX_JOBS_SCORED_PER_USER_PER_RUN = 40

# How many results to request per (role × location) query.
RESULTS_PER_QUERY = 25

# How far back to look. 24h matches the V1 cadence when running hourly; we keep
# it to 24h because GitHub Actions may skip runs and we want overlap.
HOURS_OLD = 24


def _score_and_persist(
    raw_jobs: list[dict],
    *,
    user_id: str,
    config: dict,
    cv_text: str,
    stats: dict[str, int],
    scored_this_run: int,
    dry_run: bool,
    source_label: str,
    logger,
) -> int:
    """Shared tail of the scrape loop: upsert + dedupe + score + insert match.

    Per-row try/except: a single bad job (upsert error, score crash, insert
    failure) MUST NOT abort the whole batch — we log it, increment the right
    counter, and move on.

    Returns the updated `scored_this_run` counter.
    """
    for raw_job in raw_jobs:
        if scored_this_run >= MAX_JOBS_SCORED_PER_USER_PER_RUN:
            logger.info("scrape.cap_reached", source=source_label,
                        cap=MAX_JOBS_SCORED_PER_USER_PER_RUN)
            break

        if dry_run:
            print(
                f"[run]   [dry] would upsert ({source_label}): "
                f"{raw_job['title'][:60]} @ {raw_job.get('company')}"
            )
            continue

        try:
            job_id, is_new = upsert_job(raw_job)
        except Exception as e:  # noqa: BLE001
            logger.error(
                "scrape.upsert_failed",
                source=source_label,
                title=(raw_job.get("title") or "")[:80],
                error=f"{type(e).__name__}: {e}",
            )
            stats["failed_upsert"] += 1
            continue
        if is_new:
            stats["new_jobs"] += 1

        if match_exists(user_id, job_id):
            stats["skipped_dup"] += 1
            continue

        job_for_llm = {
            "title": raw_job["title"],
            "company": raw_job.get("company"),
            "location": raw_job.get("location"),
            "description": raw_job.get("description"),
        }
        try:
            analysis = analyze_offer_for_user(config, cv_text, job_for_llm)
        except Exception as e:  # noqa: BLE001
            # Defensive: scorer is supposed to never raise (returns None on
            # failure), but if a refactor ever breaks that contract we don't
            # want to lose the rest of the batch.
            logger.error(
                "scorer.unexpected_error",
                job_id=job_id,
                error=f"{type(e).__name__}: {e}",
            )
            analysis = None

        try:
            if analysis is None:
                stats["failed_llm"] += 1
                # Store a structured "indisponible" marker so the UI can show
                # something actionable (vs. a silent NULL the user can't tell
                # apart from "not yet scored").
                insert_match(
                    user_id, job_id,
                    score=None,
                    analysis=make_parse_failed_analysis("llm_unavailable"),
                )
            else:
                insert_match(
                    user_id, job_id,
                    score=analysis.get("score"),
                    analysis=analysis,
                )
                stats["scored"] += 1
                scored_this_run += 1
        except Exception as e:  # noqa: BLE001
            logger.error(
                "scrape.insert_match_failed",
                job_id=job_id,
                error=f"{type(e).__name__}: {e}",
            )
            stats["failed_insert"] += 1
        time.sleep(LLM_COOLDOWN_SEC)

    return scored_this_run


def _process_user(
    user: dict,
    *,
    dry_run: bool,
) -> dict[str, int]:
    """Scrape + score for one user. Returns a per-user counter dict.

    Each query and each custom source is wrapped in its own try/except so an
    error in one site doesn't lose the work already done on others — the
    "checkpoint" is implicit: writes happen as each query completes, never
    in a giant batch at the end.
    """
    user_id = user["user_id"]
    email = user["email"]
    config = user["config"] or {}
    cv_text = user.get("cv_text") or ""
    logger = bind(user_id=user_id, email=email)

    stats = {
        "queries": 0,
        "scraped": 0,
        "new_jobs": 0,
        "skipped_dup": 0,
        "scored": 0,
        "failed_llm": 0,
        "failed_upsert": 0,
        "failed_insert": 0,
        "failed_queries": 0,
        "custom_sources_ok": 0,
        "custom_sources_failed": 0,
    }

    queries = build_queries(config, hours_old=HOURS_OLD, results_per_query=RESULTS_PER_QUERY)
    custom_sources = config.get("custom_career_sources") or []

    if not queries and not custom_sources:
        logger.info("scrape.skip_user", reason="no_queries_no_sources")
        return stats

    stats["queries"] = len(queries)
    if queries:
        logger.info("scrape.user_start", queries=len(queries), custom_sources=len(custom_sources))

    scored_this_run = 0
    for q in queries:
        q_label = f"{q['search_term']!r} @ {q.get('location') or 'any'}"
        q_logger = logger.bind(query=q_label)
        try:
            jobs = scrape_via_jobspy(**q)
        except Exception as e:  # noqa: BLE001
            q_logger.error("scrape.query_failed", error=f"{type(e).__name__}: {e}")
            stats["failed_queries"] += 1
            # Checkpoint semantics: stats so far ARE persisted (writes happen
            # synchronously). Just skip this query and continue with the next.
            continue
        stats["scraped"] += len(jobs)
        q_logger.info("scrape.query_done", results=len(jobs))

        scored_this_run = _score_and_persist(
            jobs,
            user_id=user_id,
            config=config,
            cv_text=cv_text,
            stats=stats,
            scored_this_run=scored_this_run,
            dry_run=dry_run,
            source_label=q_label,
            logger=q_logger,
        )

        if scored_this_run >= MAX_JOBS_SCORED_PER_USER_PER_RUN:
            break

    # ------------------------------------------------------------------
    # Custom career sites — one fetch per source, failures are soft-flagged.
    # ------------------------------------------------------------------
    if custom_sources and scored_this_run < MAX_JOBS_SCORED_PER_USER_PER_RUN:
        logger.info("scrape.custom_start", count=len(custom_sources))
    for src in custom_sources:
        if not isinstance(src, dict):
            continue
        src_url = (src.get("url") or "").strip()
        if not src_url:
            continue
        src_label = src.get("label") or src_url
        src_logger = logger.bind(custom=src_label)
        if scored_this_run >= MAX_JOBS_SCORED_PER_USER_PER_RUN:
            break

        try:
            jobs = scrape_career_site(src)
        except Exception as e:  # noqa: BLE001
            err_msg = f"{type(e).__name__}: {e}"
            src_logger.error("scrape.custom_failed", error=err_msg)
            stats["custom_sources_failed"] += 1
            if not dry_run:
                try:
                    update_custom_source_status(
                        user_id,
                        src_url,
                        status="not_scrapable",
                        last_error=err_msg,
                        error_count_delta=1,
                    )
                except Exception as e2:  # noqa: BLE001
                    src_logger.error("scrape.custom_flag_failed", error=str(e2))
            continue

        stats["scraped"] += len(jobs)
        stats["custom_sources_ok"] += 1
        src_logger.info("scrape.custom_done", results=len(jobs))
        if not dry_run:
            try:
                update_custom_source_status(user_id, src_url, status="ok")
            except Exception as e:  # noqa: BLE001
                src_logger.warn("scrape.custom_flag_failed", error=str(e))

        scored_this_run = _score_and_persist(
            jobs,
            user_id=user_id,
            config=config,
            cv_text=cv_text,
            stats=stats,
            scored_this_run=scored_this_run,
            dry_run=dry_run,
            source_label=f"custom:{src_label}",
            logger=src_logger,
        )

    logger.info("scrape.user_done", **stats)
    return stats


def main() -> None:
    p = argparse.ArgumentParser(description="Multi-user scraper (Phase 3)")
    p.add_argument("--user", help="Only process this user (by email)")
    p.add_argument("--dry-run", action="store_true", help="Scrape + print, no writes")
    p.add_argument(
        "--cleanup",
        action="store_true",
        help="Also delete jobs not seen for 60 days",
    )
    args = p.parse_args()

    print("[run] fetching active users…")
    users = list_active_user_configs()
    if args.user:
        users = [u for u in users if u["email"].lower() == args.user.strip().lower()]
        if not users:
            print(f"[run] no approved user with email {args.user!r}")
            sys.exit(1)
    print(f"[run] {len(users)} user(s) to process")

    totals = {
        "queries": 0, "scraped": 0, "new_jobs": 0, "scored": 0,
        "failed_llm": 0, "failed_upsert": 0, "failed_insert": 0,
        "failed_queries": 0, "custom_sources_ok": 0, "custom_sources_failed": 0,
    }
    for user in users:
        s = _process_user(user, dry_run=args.dry_run)
        for k in totals:
            totals[k] += s.get(k, 0)

    print("\n=== Run summary ===")
    for k, v in totals.items():
        print(f"  {k}: {v}")

    quota = llm_quota_state()
    if quota["groq_tpd"] or quota["gemini_quota"]:
        print(
            f"\n[run] LLM quota: groq_tpd={quota['groq_tpd']} "
            f"gemini_quota={quota['gemini_quota']} "
            f"all_exhausted={quota['all_exhausted']}"
        )

    if args.cleanup and not args.dry_run:
        deleted = cleanup_stale_jobs(days=60)
        print(f"[run] cleanup: deleted {deleted} stale job(s)")


if __name__ == "__main__":
    main()
