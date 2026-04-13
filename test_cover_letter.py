"""End-to-end test of the cover letter pipeline.

Usage:
    python test_cover_letter.py

Uses a hardcoded strong-match offer. Runs:
  1. LLM scoring (Groq)
  2. Cover letter generation (Groq, FR+EN)
  3. docx write to cover_letters/
  4. Git add + commit + push (so raw URLs resolve)
  5. Telegram message with both CL links

Requires GROQ_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID in env,
and `git push` access to the repo.
"""
from __future__ import annotations

import os
import subprocess
import sys
import urllib.parse
from pathlib import Path

import yaml

from cover_letter import generate_cover_letter
from cover_letter_docx import write_cover_letters
from llm_scorer import score_with_llm
from notifier import send_telegram

TEST_OFFER = {
    "id": "testcl12345678",
    "title": "Structureur Produits Dérivés Cross-Asset",
    "company": "BNP Paribas",
    "location": "Genève, Suisse",
    "description": (
        "Au sein de l'équipe Global Markets à Genève, vous concevez des "
        "produits structurés equity et multi-asset pour une clientèle de "
        "banques privées et family offices. Vous maîtrisez les payoffs "
        "(Autocall, Phoenix, CLN), le pricing multi-émetteurs, et utilisez "
        "Python pour automatiser les brochures et termsheets. Interaction "
        "quotidienne avec les Sales et les Traders."
    ),
}


def main() -> int:
    cfg = yaml.safe_load(open("config.yaml"))
    cl_cfg = cfg.get("cover_letter", {})
    sender = cl_cfg.get("sender", {})
    raw_base = (cl_cfg.get("github_raw_base") or "").rstrip("/")
    out_dir = Path(cl_cfg.get("output_dir", "cover_letters"))

    if not os.environ.get("GROQ_API_KEY"):
        print("⚠️  GROQ_API_KEY not set — run: export GROQ_API_KEY=gsk_...")
        return 1

    print("1/5 · LLM scoring...")
    score, reason = score_with_llm(
        TEST_OFFER["title"], TEST_OFFER["company"],
        TEST_OFFER["location"], TEST_OFFER["description"],
    )
    print(f"       AI score: {score}/10 — {reason}")

    print("2/5 · Generating cover letter (FR+EN)...")
    content = generate_cover_letter(
        TEST_OFFER["title"], TEST_OFFER["company"],
        TEST_OFFER["location"], TEST_OFFER["description"],
    )
    if not content:
        print("❌ CL generation failed")
        return 2
    print(f"       FR subject: {content['fr_subject']}")
    print(f"       EN subject: {content['en_subject']}")

    print("3/5 · Writing docx files...")
    fr_path, en_path = write_cover_letters(
        output_dir=out_dir,
        job_id=TEST_OFFER["id"],
        title=TEST_OFFER["title"],
        company=TEST_OFFER["company"],
        location=TEST_OFFER["location"],
        sender=sender,
        content=content,
    )
    print(f"       {fr_path}")
    print(f"       {en_path}")

    print("4/5 · Git commit + push so raw URLs resolve...")
    try:
        subprocess.run(["git", "add", str(out_dir)], check=True)
        subprocess.run(
            ["git", "commit", "-m", "Test cover letter [skip ci]"],
            check=False,  # may be no-op if no changes
        )
        subprocess.run(["git", "push"], check=True)
    except subprocess.CalledProcessError as e:
        print(f"⚠️  git push failed: {e}. Telegram links may 404.")

    print("5/5 · Sending Telegram message...")
    rel_fr = f"{out_dir.name}/{fr_path.name}"
    rel_en = f"{out_dir.name}/{en_path.name}"
    cl_fr_url = f"{raw_base}/{urllib.parse.quote(rel_fr)}"
    cl_en_url = f"{raw_base}/{urllib.parse.quote(rel_en)}"

    msg_lines = [
        f"*[KW:TEST]* *[AI:{score}/10]*",
        f"{TEST_OFFER['title']}",
        f"🏢 {TEST_OFFER['company']}  •  📍 {TEST_OFFER['location']}",
        f"🎯 Test pipeline cover letter",
    ]
    if reason and not reason.startswith("LLM error"):
        msg_lines.append(f"💡 {reason}")
    msg_lines.append("🔗 https://example.com/test")
    msg_lines.append(f"[📝 CL FR]({cl_fr_url})  •  [📝 CL EN]({cl_en_url})")

    ok = send_telegram(cfg.get("telegram", {}), "\n".join(msg_lines))
    print("✅ Telegram sent." if ok else "❌ Telegram failed.")
    return 0 if ok else 3


if __name__ == "__main__":
    sys.exit(main())
