"""Job tracker main loop.

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
from datetime import datetime
from pathlib import Path

import yaml

from db import (
    DB_PATH,
    connect,
    fetch_new_above,
    init_db,
    insert_job,
    job_exists,
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
            print(f"[tracker] Scraping axe: {name}")
            try:
                df = scrape_jobs(
                    site_name=[axe.get("site", "linkedin")],
                    search_term=axe["search_term"],
                    location=axe.get("location"),
                    distance=axe.get("distance", 50),
                    results_wanted=axe.get("results_wanted", 25),
                    hours_old=axe.get("hours_old", 48),
                    country_indeed="france",
                    linkedin_fetch_description=True,
                )
            except Exception as e:  # noqa: BLE001
                print(f"[tracker] Error scraping {name}: {e}")
                continue

            if df is None or df.empty:
                print(f"[tracker]   0 results")
                continue

            for _, row in df.iterrows():
                url = row.get("job_url") or row.get("url")
                if not url:
                    continue
                jid = job_id_for(url)
                if job_exists(conn, jid):
                    continue

                title = (row.get("title") or "").strip()
                company = (row.get("company") or "").strip()
                location = (row.get("location") or "").strip()
                description = (row.get("description") or "") or ""
                date_posted = str(row.get("date_posted") or "")

                job = {
                    "id": jid,
                    "axe": name,
                    "title": title,
                    "company": company,
                    "location": location,
                    "url": url,
                    "description": description,
                    "date_posted": date_posted,
                    "site": axe.get("site", "linkedin"),
                    "first_seen": datetime.utcnow().isoformat(),
                }
                s, reasons = score_job(job, cfg.get("scoring", {}))
                job["score"] = s
                job["score_reasons"] = reasons

                insert_job(conn, job)
                new_count += 1
            print(f"[tracker]   ingested {len(df)} rows from {name}")
    print(f"[tracker] Done. {new_count} new offers inserted.")
    return new_count


def run_notify(cfg: dict) -> int:
    """Push every new offer above threshold to Telegram."""
    threshold = int(cfg.get("scoring", {}).get("notify_threshold", 5))
    tg_cfg = cfg.get("telegram", {})
    sent = 0
    with connect(DB_PATH) as conn:
        rows = fetch_new_above(conn, threshold)
        print(f"[tracker] {len(rows)} offer(s) above threshold {threshold}")
        for row in rows:
            msg = format_job_message(row)
            ok = send_telegram(tg_cfg, msg)
            if ok:
                mark_notified(conn, row["id"])
                sent += 1
    print(f"[tracker] {sent} notification(s) sent.")
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
