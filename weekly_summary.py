"""Weekly market summary — Sunday evening.

Queries the DB for offers notified in the last 7 days, asks Groq to produce
a short qualitative summary, and sends it to Telegram.

Usage:
    python weekly_summary.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

import yaml

from db import DB_PATH, connect, fetch_notified_last_week, init_db


def _row_get(row, key, default=None):
    """Safe access for sqlite3.Row that may not have the column."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return default
from notifier import send_telegram

GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

SYSTEM_PROMPT = """Tu es un analyste marché. À partir d'une liste d'offres d'emploi scorées,
tu rédiges un résumé hebdomadaire synthétique en français pour Hugo Lopes (structureur
cross-asset Genève-Zurich / AM-PE Lyon / fintech Lyon). Format Markdown Telegram strict :

*📊 Semaine du <X au Y>*
- Offres pertinentes : <N>  (dont <M> AI≥8)
- Top 3 axes : <axe1>, <axe2>, <axe3>
- Top entreprises : <E1>, <E2>, <E3>

*Signaux forts*
- <1 signal de tendance, ex: "3 rôles structurer equity à Genève cette semaine">
- <2e signal ou entreprise à prioriser>

*Recommandations*
- <2-3 actions concrètes qu'Hugo devrait faire cette semaine>

Sois bref (max 10 lignes), concret, évite les généralités.
"""


def build_summary_input(rows) -> str:
    lines = []
    for r in rows[:40]:  # cap to stay within token limits
        analysis = {}
        raw = _row_get(r, "llm_analysis")
        if raw:
            try:
                analysis = json.loads(raw)
            except Exception:
                analysis = {}
        ai = analysis.get("score", "?")
        lines.append(
            f"- [KW:{r['score']} AI:{ai}] {r['title']} @ {r['company']} ({r['location']}) "
            f"| axe={r['axe']}"
        )
    return "\n".join(lines)


def ask_groq(prompt_data: str) -> str | None:
    api_key = os.environ.get("GROQ_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt_data},
        ],
        "temperature": 0.3,
        "max_tokens": 600,
    }
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "job-tracker/1.0",
    }
    for attempt in range(3):
        try:
            req = urllib.request.Request(GROQ_URL, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=40) as resp:
                body = json.loads(resp.read().decode())
            return body["choices"][0]["message"]["content"].strip()
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(5 * (2 ** attempt))
                continue
            return None
        except Exception:
            return None
    return None


def main() -> int:
    cfg_path = Path(__file__).parent / "config.yaml"
    cfg = yaml.safe_load(open(cfg_path))
    tg_cfg = cfg.get("telegram", {})

    init_db(DB_PATH)  # ensures llm_analysis column exists on older DBs
    with connect(DB_PATH) as conn:
        rows = fetch_notified_last_week(conn)

    if not rows:
        send_telegram(tg_cfg, "*📊 Résumé hebdo* — aucune offre notifiée cette semaine.")
        return 0

    # Fallback stats — always included so we always have data
    axes_counter = Counter(r["axe"] for r in rows)
    companies_counter = Counter(r["company"] for r in rows if r["company"])
    strong_count = 0
    for r in rows:
        try:
            a = json.loads(_row_get(r, "llm_analysis") or "{}")
            if int(a.get("score", 0)) >= 8:
                strong_count += 1
        except Exception:
            pass

    header = (
        f"*📊 Résumé hebdomadaire*\n"
        f"• {len(rows)} offre(s) notifiée(s), dont {strong_count} AI≥8\n"
        f"• Top axes : {', '.join(a for a, _ in axes_counter.most_common(3))}\n"
        f"• Top entreprises : {', '.join(c for c, _ in companies_counter.most_common(3))}\n\n"
    )

    llm_part = ask_groq(build_summary_input(rows))
    message = header + (llm_part or "_Résumé IA indisponible cette semaine._")

    # Telegram messages hard-capped at 4096 chars
    if len(message) > 4000:
        message = message[:3990] + "\n…"

    ok = send_telegram(tg_cfg, message)
    print("✅ Weekly summary sent." if ok else "❌ Weekly summary failed.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
