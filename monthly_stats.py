"""Monthly application funnel stats — 1st of each month.

Metrics:
  - Notified offers this month (by axe)
  - Applied count + conversion rate (applied / notified)
  - Average hours between notified_at and applied_at
  - Rentability per axe = applied / notified per axe
  - 👍 / 👎 vote counts

Usage:
    python monthly_stats.py                    # stats for previous month
    python monthly_stats.py --month 2026-03    # explicit month
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import yaml

from db import DB_PATH, connect, init_db, monthly_feedback_stats
from notifier import send_telegram


def _previous_month(today: datetime) -> tuple[int, int]:
    if today.month == 1:
        return today.year - 1, 12
    return today.year, today.month - 1


def format_stats_message(year: int, month: int, stats: dict) -> str:
    month_str = f"{year}-{month:02d}"
    lines = [f"*📊 Bilan mensuel — {month_str}*", ""]

    if stats["notified"] == 0:
        lines.append("_Aucune offre notifiée ce mois-ci._")
        return "\n".join(lines)

    lines.append(f"• Notifiées : *{stats['notified']}*")
    lines.append(
        f"• Postulées : *{stats['applied']}*  "
        f"(conversion : {stats['conversion_pct']}%)"
    )
    lines.append(f"• 👍 : {stats['good']}    👎 : {stats['bad']}")
    if stats["avg_hours_to_apply"] > 0:
        hrs = stats["avg_hours_to_apply"]
        if hrs < 48:
            delay = f"{hrs:.1f}h"
        else:
            delay = f"{hrs / 24:.1f}j"
        lines.append(f"• Délai moyen notif → applied : *{delay}*")

    # Top axes par rentabilité (applied / notified) puis par volume
    by_axe = stats.get("by_axe") or {}
    ranked = sorted(
        by_axe.items(),
        key=lambda kv: (
            (kv[1].get("applied", 0) / kv[1]["notified"]) if kv[1]["notified"] else 0.0,
            kv[1]["notified"],
        ),
        reverse=True,
    )[:8]
    if ranked:
        lines.append("")
        lines.append("*Top axes — rentabilité (applied / notifiées)*")
        for axe, d in ranked:
            n = d["notified"]
            a = d.get("applied", 0)
            g = d.get("good", 0)
            b = d.get("bad", 0)
            pct = (a / n * 100.0) if n else 0.0
            lines.append(
                f"• {_esc(axe)} — {a}/{n} ({pct:.0f}%) · 👍{g} 👎{b}"
            )

    return "\n".join(lines)


def _esc(s: str) -> str:
    return (
        str(s)
        .replace("\\", "\\\\")
        .replace("*", "\\*")
        .replace("_", "\\_")
        .replace("`", "\\`")
        .replace("[", "\\[")
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--month", help="Target month as YYYY-MM (default: previous month)")
    args = p.parse_args()

    if args.month:
        year, month = args.month.split("-")
        year, month = int(year), int(month)
    else:
        year, month = _previous_month(datetime.utcnow())

    cfg_path = Path(__file__).parent / "config.yaml"
    cfg = yaml.safe_load(open(cfg_path))
    tg_cfg = cfg.get("telegram", {})

    init_db(DB_PATH)
    with connect(DB_PATH) as conn:
        stats = monthly_feedback_stats(conn, year, month)
    message = format_stats_message(year, month, stats)
    print(message)
    ok = send_telegram(tg_cfg, message)
    print("✅ Sent." if ok else "❌ Send failed.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
