"""Job tracker main loop — multi-source with cross-source dedup.

Usage:
    python tracker.py --once          # one pass of all axes, notify new offers
    python tracker.py --init          # just create the DB schema
    python tracker.py --stats         # print tracking stats
    python tracker.py --test-telegram # send a test message to verify the bot
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path


def _os_env(name: str):
    return os.environ.get(name)

import yaml

import json as _json

from db import (
    DB_PATH,
    connect,
    fetch_new_above,
    fetch_recent_titles_for_company,
    fingerprint_exists,
    fingerprint_status,
    get_company,
    init_db,
    insert_job,
    job_exists,
    make_fingerprint,
    mark_notified,
    save_company,
    save_llm_analysis,
    stats,
)
from notifier import feedback_keyboard, format_job_message, send_telegram
from scorer import score_job

CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def job_id_for(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


REPOST_GAP_DAYS_DEFAULT = 45


def _ingest_row(
    conn,
    axe_name: str,
    raw: dict,
    cfg: dict,
    sem_enabled: bool,
    sem_check=None,
    repost_gap_days: int = REPOST_GAP_DAYS_DEFAULT,
) -> int:
    """Insert one scraped row through the dedup pipeline. Returns 1 if inserted, 0 otherwise.

    Handles:
      - URL-level dedup (exact)
      - Fingerprint dedup with repost detection (old fingerprint = repost, flagged
        but re-inserted; recent fingerprint = blocked)
      - Semantic dedup on same-company titles (optional)
      - Keyword scoring
    """
    url = raw.get("job_url") or raw.get("url")
    if not url:
        return 0
    jid = job_id_for(url)
    if job_exists(conn, jid):
        return 0

    title = (raw.get("title") or "").strip()
    company = str(raw.get("company") or "").strip()
    if company.lower() == "nan": company = ""
    location = (raw.get("location") or "").strip()
    description = raw.get("description") or ""
    date_posted = str(raw.get("date_posted") or "")
    site_name = str(raw.get("site") or "unknown")

    fp = make_fingerprint(title, company)
    is_repost = False
    repost_of = None
    gap_days = None
    fp_status = fingerprint_status(conn, fp)
    if fp_status is not None:
        oldest_id, oldest_first_seen, _cnt = fp_status
        try:
            first_dt = datetime.fromisoformat(oldest_first_seen)
            gap_days = (datetime.now(datetime.timezone.utc) - first_dt).days
        except (ValueError, TypeError):
            gap_days = None
        if gap_days is not None and gap_days >= repost_gap_days:
            # Repost detected — insert but flag it
            is_repost = True
            repost_of = oldest_id
            print(f"[tracker]   ♻️  repost detected ({gap_days}d gap): {title} @ {company}")
        else:
            # Recent duplicate — block
            return 0

    # Semantic dedup on same-company titles
    if sem_enabled and sem_check and company:
        existing = [r["title"] for r in fetch_recent_titles_for_company(conn, company)]
        if existing and sem_check(title, existing):
            print(f"[tracker]   semantic dup skipped: {title} @ {company}")
            return 0

    job = {
        "id": jid,
        "fingerprint": fp,
        "axe": axe_name,
        "title": title,
        "company": company,
        "location": location,
        "url": url,
        "description": description,
        "date_posted": date_posted,
        "site": site_name,
        "is_repost": is_repost,
        "repost_of": repost_of,
        "repost_gap_days": gap_days if is_repost else None,
        "first_seen": datetime.now(datetime.timezone.utc).isoformat(),
    }
    s, reasons = score_job(job, cfg.get("scoring", {}))
    job["score"] = s
    job["score_reasons"] = reasons
    insert_job(conn, job)
    return 1


def run_scrape(cfg: dict) -> int:
    """Scrape all sources and insert new jobs. Returns count of new rows."""
    # Optional semantic dedup (requires Groq)
    sem_cfg = cfg.get("semantic_dedup", {}) or {}
    sem_enabled = bool(sem_cfg.get("enabled", False)) and bool(
        _os_env("GROQ_API_KEY") or _os_env("GEMINI_API_KEY")
    )
    sem_check = None
    if sem_enabled:
        try:
            from semantic_dedup import is_semantic_duplicate as sem_check  # type: ignore
        except ImportError:
            sem_enabled = False
            sem_check = None

    repost_gap = int(cfg.get("repost", {}).get("gap_days", REPOST_GAP_DAYS_DEFAULT))

    new_count = 0
    with connect(DB_PATH) as conn:
        # 1) Core jobspy loop (LinkedIn / Indeed / Google Jobs)
        try:
            from jobspy import scrape_jobs  # type: ignore
        except ImportError:
            print("[tracker] jobspy not installed — skipping core sources")
            scrape_jobs = None  # type: ignore

        if scrape_jobs is not None:
            for axe in cfg.get("axes", []):
                name = axe["name"]
                sites = axe.get("sites", [axe.get("site", "linkedin")])
                if isinstance(sites, str):
                    sites = [sites]
                print(f"[tracker] Scraping axe: {name} (sites: {', '.join(sites)})")
                try:
                    kwargs = dict(
                        site_name=sites,
                        search_term=axe["search_term"],
                        location=axe.get("location"),
                        results_wanted=axe.get("results_wanted", 25),
                        hours_old=axe.get("hours_old", 24),
                        linkedin_fetch_description=True,
                    )
                    if axe.get("distance"):
                        kwargs["distance"] = axe["distance"]
                    if "indeed" in sites:
                        kwargs["country_indeed"] = axe.get("country_indeed", "france")
                    df = scrape_jobs(**kwargs)
                except Exception as e:  # noqa: BLE001
                    print(f"[tracker] Error scraping {name}: {e}")
                    continue
                if df is None or df.empty:
                    print("[tracker]   0 results")
                    continue
                axis_new = 0
                for _, row in df.iterrows():
                    raw = {
                        "title": row.get("title"),
                        "company": row.get("company"),
                        "location": row.get("location"),
                        "description": row.get("description"),
                        "date_posted": row.get("date_posted"),
                        "site": row.get("site") or sites[0],
                        "job_url": row.get("job_url") or row.get("url"),
                    }
                    axis_new += _ingest_row(conn, name, raw, cfg, sem_enabled, sem_check, repost_gap)
                new_count += axis_new
                print(f"[tracker]   {len(df)} scraped, {axis_new} new from {name}")

        # 2) Extra scrapers (WTTJ / JobUp.ch / eFinancialCareers)
        extra_cfg = cfg.get("extra_sources", {}) or {}
        if extra_cfg.get("enabled"):
            try:
                from extra_scrapers import SCRAPERS as _EXTRA
            except ImportError as e:
                print(f"[tracker] extra_scrapers unavailable: {e}")
                _EXTRA = {}
            for source, scfg in extra_cfg.items():
                if source == "enabled" or not isinstance(scfg, dict):
                    continue
                if not scfg.get("enabled"):
                    continue
                fn = _EXTRA.get(source)
                if not fn:
                    continue
                for q in scfg.get("queries", []):
                    axe_name = q.get("axe") or f"extra_{source}"
                    try:
                        rows = fn(q["search"], q.get("location"), q.get("limit", 25))
                    except Exception as e:  # noqa: BLE001
                        print(f"[tracker] {source} error: {e}")
                        continue
                    print(f"[tracker] {source} '{q['search']}' → {len(rows)} hits")
                    added = 0
                    for r in rows:
                        added += _ingest_row(conn, axe_name, r, cfg, sem_enabled, sem_check, repost_gap)
                    new_count += added
                    print(f"[tracker]   +{added} new from {source}")

        # 3) Company feeds (Greenhouse / Lever / SmartRecruiters)
        feeds_cfg = cfg.get("company_feeds", {}) or {}
        if feeds_cfg.get("enabled"):
            try:
                from company_feeds import fetch_all
            except ImportError as e:
                print(f"[tracker] company_feeds unavailable: {e}")
                fetch_all = None  # type: ignore
            if fetch_all is not None:
                companies = feeds_cfg.get("companies", [])
                max_age = feeds_cfg.get("max_age_days", 14)
                try:
                    results = fetch_all(companies, max_age_days=max_age)
                except Exception as e:  # noqa: BLE001
                    print(f"[tracker] company_feeds error: {e}")
                    results = []
                for axe_name, jobs in results:
                    added = 0
                    for r in jobs:
                        added += _ingest_row(conn, axe_name, r, cfg, sem_enabled, sem_check, repost_gap)
                    if added:
                        new_count += added
                        print(f"[tracker]   +{added} new from {axe_name} (direct feed)")

    print(f"[tracker] Done. {new_count} new offers inserted.")
    return new_count


def run_notify(cfg: dict) -> int:
    """Push every new offer above threshold to Telegram, with rich LLM analysis."""
    threshold = int(cfg.get("scoring", {}).get("notify_threshold", 5))
    llm_threshold = int(cfg.get("scoring", {}).get("llm_min_score", 5))
    cl_threshold = int(cfg.get("scoring", {}).get("cover_letter_min_ai", 99))
    tg_cfg = cfg.get("telegram", {})

    # LLM: structured analysis
    try:
        from llm_scorer import analyze_offer
        llm_available = bool(_os_env("GROQ_API_KEY") or _os_env("GEMINI_API_KEY"))
    except ImportError:
        llm_available = False
        analyze_offer = None  # type: ignore

    # Company enrichment
    enrich_available = llm_available
    if enrich_available:
        try:
            from company_enrichment import enrich_company
        except ImportError:
            enrich_available = False

    # Cover letter setup
    cl_cfg = cfg.get("cover_letter", {}) or {}
    cl_enabled = bool(cl_cfg.get("enabled", False)) and llm_available
    cl_out_dir = Path(__file__).parent / cl_cfg.get("output_dir", "cover_letters")
    cl_raw_base = (cl_cfg.get("github_raw_base") or "").rstrip("/")
    if cl_enabled:
        try:
            from cover_letter import generate_cover_letter
            from cover_letter_docx import write_cover_letters
        except ImportError as e:
            print(f"[tracker] Cover letter deps missing ({e}), disabling CL generation")
            cl_enabled = False

    sent = 0
    skipped_by_llm = 0
    with connect(DB_PATH) as conn:
        rows = fetch_new_above(conn, threshold)
        print(f"[tracker] {len(rows)} offer(s) above keyword threshold {threshold}")
        if llm_available:
            print(f"[tracker] LLM analysis enabled (Groq Llama 3.3 70B) — min AI score: {llm_threshold}")

        for row in rows:
            llm_score = -1
            llm_reason = ""
            analysis = None

            # Structured LLM analysis (score + sub-scores + extraction)
            if llm_available:
                analysis = analyze_offer(
                    title=row["title"] or "",
                    company=row["company"] or "",
                    location=row["location"] or "",
                    description=row["description"] or "",
                )
                if analysis:
                    llm_score = int(analysis.get("score", 0))
                    llm_reason = str(analysis.get("reason", ""))
                    # Repost penalty: -2 on the AI score and add a red flag
                    try:
                        is_repost = bool(row["is_repost"])
                    except (IndexError, KeyError):
                        is_repost = False
                    if is_repost and llm_score > 0:
                        penalty = int(cfg.get("repost", {}).get("score_penalty", 2))
                        llm_score = max(0, llm_score - penalty)
                        analysis["score"] = llm_score
                        gap = 0
                        try:
                            gap = int(row["repost_gap_days"] or 0)
                        except (IndexError, KeyError, TypeError, ValueError):
                            gap = 0
                        flag = (
                            f"♻️ Republiée après {gap}j — signal de turnover / poste non pourvu"
                        )
                        rf = analysis.setdefault("red_flags", [])
                        if isinstance(rf, list):
                            rf.append(flag)
                    save_llm_analysis(conn, row["id"], _json.dumps(analysis, ensure_ascii=False))
                else:
                    llm_reason = "LLM error"
                print(f"[tracker]   AI score {llm_score}/10 for: {row['title']} @ {row['company']}")
                if llm_score >= 0 and llm_score < llm_threshold:
                    mark_notified(conn, row["id"])  # processed, skip notification
                    skipped_by_llm += 1
                    continue

            # Company enrichment (first time we see the company)
            company_enrichment = None
            company_name = (row["company"] or "").strip()
            if enrich_available and company_name:
                existing = get_company(conn, company_name)
                if existing is None:
                    enriched = enrich_company(company_name)
                    if enriched:
                        save_company(conn, company_name, _json.dumps(enriched, ensure_ascii=False))
                        company_enrichment = enriched
                    time.sleep(1)  # small buffer
                # else: we don't re-display for subsequent offers from same company

            # Cover letter generation for strong matches
            cl_fr_url = cl_en_url = ""
            if cl_enabled and llm_score >= cl_threshold:
                print(f"[tracker]   Generating cover letter (AI={llm_score}) for {row['title']} @ {row['company']}")
                content = generate_cover_letter(
                    title=row["title"] or "",
                    company=row["company"] or "",
                    location=row["location"] or "",
                    description=row["description"] or "",
                )
                if content:
                    try:
                        fr_path, en_path = write_cover_letters(
                            output_dir=cl_out_dir,
                            job_id=row["id"],
                            title=row["title"] or "job",
                            company=row["company"] or "company",
                            location=row["location"] or "",
                            sender=cl_cfg.get("sender", {}),
                            content=content,
                        )
                        if cl_raw_base:
                            rel_fr = f"{cl_out_dir.name}/{fr_path.name}"
                            rel_en = f"{cl_out_dir.name}/{en_path.name}"
                            cl_fr_url = f"{cl_raw_base}/{urllib.parse.quote(rel_fr)}"
                            cl_en_url = f"{cl_raw_base}/{urllib.parse.quote(rel_en)}"
                    except Exception as e:  # noqa: BLE001
                        print(f"[tracker]   CL write failed: {e}")

            msg = format_job_message(
                row, llm_score=llm_score, llm_reason=llm_reason,
                cl_fr_url=cl_fr_url, cl_en_url=cl_en_url,
                analysis=analysis, company_enrichment=company_enrichment,
            )
            ok = send_telegram(tg_cfg, msg, reply_markup=feedback_keyboard(row["id"]))
            if ok:
                mark_notified(conn, row["id"])
                sent += 1

            # Stay well under Groq 30 RPM free tier
            if llm_available:
                time.sleep(5)

    print(f"[tracker] {sent} notification(s) sent, {skipped_by_llm} filtered by AI.")
    return sent


def cmd_once(cfg: dict) -> None:
    # Drain any pending Telegram feedback (button taps / slash commands) first,
    # so the LLM has the latest user signals to condition on.
    try:
        from feedback_poller import poll_once
        poll_once(cfg)
    except Exception as e:  # noqa: BLE001
        print(f"[tracker] feedback poll failed (non-fatal): {e}")
    run_scrape(cfg)
    run_notify(cfg)


def cmd_stats() -> None:
    with connect(DB_PATH) as conn:
        s = stats(conn)
    if not s:
        print("No jobs tracked yet.")
        return
    for axe, breakdown in s.items():
        total = sum(breakdown.values())
        parts = ", ".join(f"{k}={v}" for k, v in breakdown.items())
        print(f"{axe}: total={total}  ({parts})")


def cmd_test_telegram(cfg: dict) -> None:
    ok = send_telegram(
        cfg.get("telegram", {}),
        "✅ *Job tracker* is wired up.\nYou will receive new offers here.",
    )
    print("Telegram test sent." if ok else "Telegram test FAILED — check env vars.")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true", help="Scrape + notify one pass")
    p.add_argument("--init", action="store_true", help="Create DB schema")
    p.add_argument("--stats", action="store_true", help="Print stats")
    p.add_argument("--test-telegram", action="store_true", help="Send a test message")
    args = p.parse_args()

    cfg = load_config()
    init_db(DB_PATH)

    if args.init:
        print(f"DB initialised at {DB_PATH}")
        return
    if args.test_telegram:
        cmd_test_telegram(cfg)
        return
    if args.stats:
        cmd_stats()
        return
    if args.once:
        cmd_once(cfg)
        return
    p.print_help()


if __name__ == "__main__":
    main()