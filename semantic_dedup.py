"""Lightweight semantic deduplication via Groq.

Only invoked when the exact URL hash AND the normalized fingerprint both miss,
but the same company already has ≥1 recent row whose title shares significant
word overlap. Keeps LLM calls scarce.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from db import normalize

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")


def _token_overlap(a: str, b: str) -> float:
    ta = set(normalize(a).split())
    tb = set(normalize(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def is_semantic_duplicate(new_title: str, existing_titles: list[str]) -> bool:
    """Return True if `new_title` matches any title in `existing_titles` semantically.

    Pre-filters: only consult the LLM for titles with Jaccard >= 0.4 (so that we
    don't spend a token on obvious non-matches).
    """
    api_key = os.environ.get("GROQ_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return False

    candidates = [t for t in existing_titles if _token_overlap(new_title, t) >= 0.4]
    if not candidates:
        return False

    prompt = (
        f"Nouvelle offre: '{new_title}'\n"
        f"Offres déjà vues (même entreprise):\n"
        + "\n".join(f"- {t}" for t in candidates[:10])
        + "\n\nUne de ces offres correspond-elle au MÊME poste (même rôle, même perimètre) ? "
        "Réponds en JSON : {\"duplicate\": true|false}"
    )
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 30,
        "response_format": {"type": "json_object"},
    }
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "job-tracker/1.0",
    }
    try:
        req = urllib.request.Request(GROQ_URL, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode())
        text = body["choices"][0]["message"]["content"].strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        result = json.loads(text)
        return bool(result.get("duplicate"))
    except Exception:
        return False
