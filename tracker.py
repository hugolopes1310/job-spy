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
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

from db import (
    DB_PATH,
    connect,
    fetch_new_above,
    fingerprint_exists,
    init_db,
    insert_job,
    job_exists,
    make_fingerprint,
    mark_notified,
    stats,
)
from notifier import format_job_message, send_telegram
from scorer import score_job

CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def job_id_for(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


def run_scrape(cfg: dict) -> int:
    """Scrape all axes and insert new jobs. Returns count of new rows."""
    try:
        from jobspy import scrape_jobs  # type: ignore
    except ImportError:
        print(
            "[tracker] jobspy is not installed. Run: pip install python-jobspy pyyaml"
        )
        sys.exit(1)

    new_count = 0
    with connect(DB_PATH) as conn:
        for axe in cfg["axes"]:
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
                print(f"[tracker]   0 results")
                continue

            axis_new = 0
            for _, row in df.iterrows():
                url = row.get("job_url") or row.get("url")
                if not url:
                    continue
                jid = job_id_for(url)

                # Dedup 1: exact URL match
                if job_exists(conn, jid):
                    continue

                title = (row.get("title") or "").strip()
                company = (row.get("company") or "").strip()
                location = (row.get("location") or "").strip()
                description = (row.get("description") or "") or ""
                date_posted = str(row.get("date_posted") or "")
                site_name = str(row.get("site") or sites[0])

                # Dedup 2: same title+company across different sources
                fp = make_fingerprint(title, company)
                if fingerprint_exists(conn, fp):
                    continue

                job = {
                    "id": jid,
                    "fingerprint": fp,
                    "axe": name,
                    "title": title,
                    "company": company,
                    "location": location,
                    "url": url,
                    "description": description,
                    "date_posted": date_posted,
                    "site": site_name,
                    "first_seen": datetime.utcnow().isoformat(),
                }
                s, reasons = score_job(job, cfg.get("scoring", {}))
                job["score"] = s
                job["score_reasons"] = reasons

                insert_job(conn, job)
                axis_new += 1
                new_count += 1
            print(f"[tracker]   {len(df)} scraped, {axis_new} new from {name}")
    print(f"[tracker] Done. {new_count} new offers inserted.")
    return new_count


def run_notify(cfg: dict) -> int:
    """Push every new offer above threshold to Telegram, with optional LLM scoring."""
    threshold = int(cfg.get("scoring", {}).get("notify_threshold", 5))
    llm_threshold = int(cfg.get("scoring", {}).get("llm_min_score", 5))
    tg_cfg = cfg.get("telegram", {})

    # Try to import LLM scorer (optional — works without it)
    try:
        from llm_scorer import score_with_llm
        import os
        llm_available = bool(os.environ.get("GEMINI_API_KEY"))
    except ImportError:
        llm_available = False

    sent = 0
    skipped_by_llm = 0
    with connect(DB_PATH) as conn:
        rows = fetch_new_above(conn, threshold)
        print(f"[tracker] {len(rows)} offer(s) above keyword threshold {threshold}")
        if llm_available:
            print(f"[tracker] LLM scoring enabled (Gemini Flash) — min AI score: {llm_threshold}")

        for row in rows:
            llm_score = -1
            llm_reason = ""

            # Second pass: LLM fit scoring
            if llm_available:
                llm_score, llm_reason = score_with_llm(
                    title=row["title"] or "",
                    company=row["company"] or "",
                    location=row["location"] or "",
                    description=row["description"] or "",
                )
                print(f"[tracker]   AI score {llm_score}/10 for: {row['title']} @ {row['company']}")
                if llm_score >= 0 and llm_score < llm_threshold:
                    mark_notified(conn, row["id"])  # mark as processed, don't notify
                    skipped_by_llm += 1
                    continue

            msg = format_job_message(row, llm_score=llm_score, llm_reason=llm_reason)
            ok = send_telegram(tg_cfg, msg)
            if ok:
                mark_notified(conn, row["id"])
                sent += 1

            # Respect Gemini free tier (15 RPM) — stay well under limit
            if llm_available:
                time.sleep(5)

    print(f"[tracker] {sent} notification(s) sent, {skipped_by_llm} filtered by AI.")
    return sent


def cmd_once(cfg: dict) -> None:
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