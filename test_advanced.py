"""End-to-end test for rich analysis + company enrichment + Telegram rendering.

Usage:
    export GROQ_API_KEY=gsk_...
    export TELEGRAM_BOT_TOKEN=...
    export TELEGRAM_CHAT_ID=...
    python test_advanced.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml

from company_enrichment import enrich_company
from llm_scorer import analyze_offer
from notifier import format_job_message, send_telegram

TEST_OFFER = {
    "id": "testadv0001",
    "title": "Structured Products Structurer — Cross Asset",
    "company": "Vontobel",
    "location": "Zürich, Switzerland",
    "axe": "A5 — Zurich structured products",
    "score": 12,
    "url": "https://example.com/testadv",
    "description": (
        "Vontobel is looking for a Structurer to join its Cross-Asset Solutions "
        "team in Zürich. You will design bespoke payoffs (Autocallable, Phoenix, "
        "CLN, Callable) for private banks and distribution clients. "
        "Strong pricing, Python and Bloomberg skills required. 2-5 years of "
        "relevant experience. Apply via company career page. Salary range "
        "CHF 120-150k depending on experience. Contact: Anna Müller, Head of "
        "Structuring. Applications close May 31, 2026."
    ),
}


def _row(d: dict):
    # SimpleNamespace doesn't support dict access; build a shim that does both.
    class R(dict):
        def __getitem__(self, k):
            return self.get(k)
    return R(d)


def main() -> int:
    if not os.environ.get("GROQ_API_KEY"):
        print("⚠️  GROQ_API_KEY not set")
        return 1

    cfg = yaml.safe_load(open("config.yaml"))

    print("1/3 · analyze_offer (rich LLM analysis)...")
    analysis = analyze_offer(
        TEST_OFFER["title"], TEST_OFFER["company"],
        TEST_OFFER["location"], TEST_OFFER["description"],
    )
    if not analysis:
        print("❌ analyze_offer failed")
        return 2
    print(json.dumps(analysis, ensure_ascii=False, indent=2))

    print("\n2/3 · enrich_company...")
    enrichment = enrich_company(TEST_OFFER["company"])
    if enrichment:
        print(json.dumps(enrichment, ensure_ascii=False, indent=2))

    print("\n3/3 · Rendering + sending Telegram...")
    msg = format_job_message(
        _row(TEST_OFFER),
        llm_score=int(analysis["score"]),
        llm_reason=str(analysis.get("reason", "")),
        analysis=analysis,
        company_enrichment=enrichment,
    )
    print("\n--- Telegram message preview ---")
    print(msg)
    print("--- /preview ---\n")

    ok = send_telegram(cfg.get("telegram", {}), msg)
    print("✅ Sent." if ok else "❌ Failed.")
    return 0 if ok else 3


if __name__ == "__main__":
    sys.exit(main())
