"""
One-off: re-score every 'new' job in the DB with the current scoring config.

Needed after the config.yaml fix that moved title_boost/description_boost/
blacklist/location_boost under `scoring:` (they were previously nested under
`cover_letter:`, which silently made the scorer return 0 for everything).

Usage:
    python rescore.py             # rescore all status='new' rows
    python rescore.py --all       # rescore every row (incl. notified)
    python rescore.py --dry-run   # show what would change, no writes
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from db import connect, DB_PATH
from scorer import score_job


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="Rescore every row, not just status='new'")
    ap.add_argument("--dry-run", action="store_true", help="Show diff, don't write")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path("config.yaml").read_text())
    scoring_cfg = cfg.get("scoring", {}) or {}

    where = "" if args.all else "WHERE status = 'new'"
    updated = 0
    diffs_shown = 0

    with connect(DB_PATH) as conn:
        rows = conn.execute(f"SELECT * FROM jobs {where}").fetchall()
        print(f"[rescore] {len(rows)} row(s) to process")

        for row in rows:
            job = {
                "title": row["title"] or "",
                "company": row["company"] or "",
                "location": row["location"] or "",
                "description": row["description"] or "",
            }
            new_score, reasons = score_job(job, scoring_cfg)
            old_score = row["score"] if "score" in row.keys() else 0
            if new_score == old_score:
                continue

            if diffs_shown < 15:
                print(f"  {old_score:+3d} -> {new_score:+3d}  {job['title'][:55]:55s} @ {job['location'][:20]}")
                diffs_shown += 1

            if not args.dry_run:
                conn.execute(
                    "UPDATE jobs SET score = ?, score_reasons = ? WHERE id = ?",
                    (new_score, json.dumps(reasons, ensure_ascii=False), row["id"]),
                )
            updated += 1

        if not args.dry_run:
            conn.commit()

    print(f"[rescore] {updated} row(s) {'would be ' if args.dry_run else ''}updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
