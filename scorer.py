"""Relevance scoring for scraped jobs.

The scoring is intentionally simple and tunable via config.yaml:
    score = sum(title_boost matches)
          + sum(description_boost matches)
          + sum(location_boost matches)
          - sum(blacklist matches)

Score reasons are returned so we can debug why an offer was (not) notified.
"""
from __future__ import annotations

import json
import re
from typing import Any


def _contains(haystack: str, needle: str) -> bool:
    # Word-boundary match, case-insensitive.
    pattern = r"\b" + re.escape(needle.lower()) + r"\b"
    return re.search(pattern, haystack.lower()) is not None


def score_job(job: dict[str, Any], scoring_cfg: dict[str, Any]) -> tuple[int, str]:
    title = (job.get("title") or "").lower()
    description = (job.get("description") or "").lower()
    location = (job.get("location") or "").lower()

    total = 0
    reasons: list[dict] = []

    for kw, pts in (scoring_cfg.get("title_boost") or {}).items():
        if _contains(title, kw):
            total += int(pts)
            reasons.append({"kw": kw, "where": "title", "pts": int(pts)})

    for kw, pts in (scoring_cfg.get("description_boost") or {}).items():
        if _contains(description, kw):
            total += int(pts)
            reasons.append({"kw": kw, "where": "desc", "pts": int(pts)})

    for kw, pts in (scoring_cfg.get("location_boost") or {}).items():
        if _contains(location, kw):
            total += int(pts)
            reasons.append({"kw": kw, "where": "loc", "pts": int(pts)})

    for kw, pts in (scoring_cfg.get("blacklist") or {}).items():
        # blacklist hits can show up anywhere
        if _contains(title, kw) or _contains(description, kw):
            total += int(pts)  # pts are already negative in config
            reasons.append({"kw": kw, "where": "black", "pts": int(pts)})

    return total, json.dumps(reasons, ensure_ascii=False)
