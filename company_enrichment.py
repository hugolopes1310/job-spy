"""Generate a short company fiche via Groq (first time a company appears).

Uses only the LLM's training knowledge — no web search. Returns a structured
dict that the notifier can render compactly.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

SYSTEM_PROMPT = """Tu es un analyste senior en finance qui produit des mini-fiches entreprise
à partir de ta connaissance générale. Format JSON strict :

{
  "type": "<Banque privée|Banque d'investissement|Asset Manager|Private Equity|Fintech|Autre>",
  "size": "<taille ordre de grandeur, ex: 2000 employés>",
  "positioning": "<1 phrase FR sur le positionnement>",
  "relevance": "<1 phrase FR: pourquoi c'est pertinent ou non pour un structureur cross-asset>",
  "known_issues": ["<red flags récents si tu en as connaissance>"]
}

Si tu ne connais pas l'entreprise, renvoie tous les champs à null et known_issues à []
(n'invente jamais). Sois synthétique.
"""


def enrich_company(name: str) -> dict | None:
    """Return a dict for the company or None on failure."""
    api_key = os.environ.get("GROQ_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None

    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Entreprise : {name}"},
        ],
        "temperature": 0.1,
        "max_tokens": 350,
        "response_format": {"type": "json_object"},
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
            with urllib.request.urlopen(req, timeout=25) as resp:
                body = json.loads(resp.read().decode())
            text = body["choices"][0]["message"]["content"].strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0]
            result = json.loads(text)
            if not isinstance(result.get("known_issues"), list):
                result["known_issues"] = []
            return result
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(5 * (2 ** attempt))
                continue
            return None
        except Exception as e:  # noqa: BLE001
            print(f"[company] error: {e}")
            return None
    return None
