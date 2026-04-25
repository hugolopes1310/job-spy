"""CV structured extraction — Groq primary, Gemini fallback.

The goal is to *pre-fill* the onboarding wizard with everything a CV can give us
so the user's only job becomes editing / confirming, not typing.

We deliberately keep this separate from `config_extractor.py`:
  - `config_extractor.py` (Groq) synthesises the final scraping+scoring config
    from CV + cover letter + free-form brief + structured answers.
  - `cv_extractor.py` (here) takes ONLY the CV text and returns structured
    fields that pre-populate the form (education, languages, a suggested
    free-form brief…). It runs once at the start of the wizard.

Provider strategy:
  1. Try Groq (Llama 3.3 70B with JSON mode). It's the same provider we
     already use for the final config extraction, so auth + quotas are one
     single knob to worry about.
  2. If Groq fails (no key, HTTP error, invalid JSON), fall back to Gemini.
  3. If both fail, return an empty shell with `_error` set so the UI can
     warn the user.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from app.lib.supabase_client import _secret as _get_secret

GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)


# ---------------------------------------------------------------------------
# Schemas (doc-only — the LLM gets the JSON shape via the system prompt)
# ---------------------------------------------------------------------------
_CV_EXTRACTION_SCHEMA = """
{
  "current_role": "string (latest job title, or '' if student)",
  "years_xp": "int (sum of post-graduation professional years, best estimate)",
  "top_skills": ["string", ...],
  "industries_past": ["string", ...],
  "experiences": [
    {"company": "string", "title": "string", "start": "YYYY-MM or YYYY", "end": "YYYY-MM, YYYY or 'present'", "location": "string"}
  ],
  "education": [
    {"school": "string", "degree": "string", "year": "int|null"}
  ],
  "languages": [
    {"lang": "ISO-639-1 2-letter code ONLY (fr, en, es, de, it, pt, nl, zh, ja, ar, ru, ...)", "level": "native|C2|C1|B2|B1|A2|A1"}
  ],
  "motivations_from_cv": "string (content of any 'Objectif', 'Summary', 'About' section — empty if absent)",
  "draft_brief": "string (2-3 sentences proposing what the candidate seems to be looking for — in French, tu form — to be edited by the user)"
}
"""


_SYSTEM_PROMPT = f"""Tu es un recruteur senior qui lit rapidement un CV pour en extraire la substance.

Ta tâche : à partir du texte brut d'un CV, produire UNIQUEMENT un JSON strict au format suivant :

{_CV_EXTRACTION_SCHEMA}

Règles :
1. Ne jamais inventer. Si une info est absente du CV, mets null / [] / "".
2. `years_xp` : somme des années d'expérience PROFESSIONNELLE (post-études, hors stages courts). Estime au mieux si les dates sont floues.
3. `top_skills` : 5-12 compétences utiles pour matcher des offres (ex : "Python", "dérivés actions", "product discovery", "SQL"). Pas de soft skills vagues.
4. `industries_past` : secteurs concrets (ex : "banque d'investissement", "SaaS B2B", "pharma"). Max 6.
5. `experiences` : TOUTES les expériences pro du CV, du plus récent au plus ancien. `company` = nom de la boîte, `title` = intitulé de poste, `start`/`end` = dates au format YYYY-MM si possible sinon YYYY. Pour l'expérience en cours, `end` = "present". Inclure stages et alternances. Max 10 entrées.
6. `education` : du plus récent au plus ancien. Si le diplôme n'a pas d'année, mets year=null. Max 5 entrées.
7. `languages` : IMPORTANT — `lang` DOIT être un code ISO 639-1 à 2 lettres en minuscules ("fr", "en", "es", "pt", "de", "it", "nl", "zh", "ja", "ar", "ru"...). JAMAIS le nom complet. `level` = CEFR standard (C2/C1/B2/B1/A2/A1). Mapping : "bilingue" → C2, "courant" → C1, "intermédiaire avancé" → B2, "intermédiaire" → B1, "notions" → A2, "langue maternelle" / "native" → "native".
8. `motivations_from_cv` : recopie verbatim un éventuel bloc "Objectif professionnel" / "Summary" / "About" / "Profile" / "Centres d'intérêt" si pertinent. Max 400 caractères. Si le CV n'en a pas, mets "".
9. `draft_brief` : formule une proposition de 2-3 phrases courtes en français, tutoiement, qui synthétise ce que le candidat semble rechercher. Base-toi UNIQUEMENT sur le parcours (rôle actuel, seniorité, secteurs). Ne parle pas de géo ni de salaire (ces infos seront ajoutées par le user après). Ex : "Tu vises un poste de X dans Y, idéalement chez un Z. Tu as N ans d'XP et tu veux capitaliser sur A et B."

Réponds UNIQUEMENT avec le JSON, pas de markdown, pas de commentaire.
"""


# ---------------------------------------------------------------------------
# Groq call (primary provider)
# ---------------------------------------------------------------------------
def _call_groq(cv_slice: str) -> tuple[dict | None, str]:
    """Return (parsed_dict, error_message). error_message is '' on success."""
    api_key = _get_secret("GROQ_API_KEY")
    if not api_key:
        return None, "GROQ_API_KEY absente"

    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": f"=== CV ===\n{cv_slice}"},
        ],
        "temperature": 0.15,
        "max_tokens": 1800,
        "response_format": {"type": "json_object"},
    }
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "kairo-cv-extractor/1.0",
    }

    for attempt in range(3):
        try:
            req = urllib.request.Request(
                GROQ_URL, data=data, headers=headers, method="POST"
            )
            with urllib.request.urlopen(req, timeout=45) as resp:
                body = json.loads(resp.read().decode())
            text = body["choices"][0]["message"]["content"].strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0]
            return json.loads(text), ""
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode()[:300]
            except Exception:  # noqa: BLE001
                err_body = ""
            if e.code == 429 and attempt < 2:
                wait = 3 * (2 ** attempt)
                print(f"[cv_extractor] Groq 429, retry in {wait}s")
                time.sleep(wait)
                continue
            print(f"[cv_extractor] Groq HTTP {e.code}: {err_body}")
            return None, f"Groq HTTP {e.code}"
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            print(f"[cv_extractor] Groq parse error: {e}")
            return None, f"Groq invalid JSON: {e}"
        except Exception as e:  # noqa: BLE001
            print(f"[cv_extractor] Groq error: {e}")
            return None, f"Groq error: {e}"
    return None, "Groq retries exhausted"


# ---------------------------------------------------------------------------
# Gemini call (fallback provider)
# ---------------------------------------------------------------------------
def _call_gemini(cv_slice: str) -> tuple[dict | None, str]:
    api_key = _get_secret("GEMINI_API_KEY")
    if not api_key:
        return None, "GEMINI_API_KEY absente"

    payload = {
        "systemInstruction": {"parts": [{"text": _SYSTEM_PROMPT}]},
        "contents": [
            {"role": "user", "parts": [{"text": f"=== CV ===\n{cv_slice}"}]}
        ],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 1800,
            "responseMimeType": "application/json",
        },
    }
    data = json.dumps(payload).encode("utf-8")
    url = f"{GEMINI_URL.format(model=GEMINI_MODEL)}?key={api_key}"
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "kairo-cv-extractor/1.0",
    }

    for attempt in range(2):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read().decode())
            candidates = body.get("candidates") or []
            if not candidates:
                return None, "Gemini vide"
            parts = candidates[0].get("content", {}).get("parts") or []
            if not parts:
                return None, "Gemini vide"
            text = (parts[0].get("text") or "").strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0]
            return json.loads(text), ""
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode()[:300]
            except Exception:  # noqa: BLE001
                err_body = ""
            if e.code == 429 and attempt < 1:
                time.sleep(3)
                continue
            print(f"[cv_extractor] Gemini HTTP {e.code}: {err_body}")
            return None, f"Gemini HTTP {e.code}"
        except Exception as e:  # noqa: BLE001
            print(f"[cv_extractor] Gemini error: {e}")
            return None, f"Gemini error: {e}"
    return None, "Gemini retries exhausted"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def empty_cv_extraction() -> dict[str, Any]:
    """Default shape when the CV extractor is unavailable or fails."""
    return {
        "current_role": "",
        "years_xp": 0,
        "top_skills": [],
        "industries_past": [],
        "experiences": [],
        "education": [],
        "languages": [],
        "motivations_from_cv": "",
        "draft_brief": "",
        "_error": "",
    }


# Mapping for when the LLM slips and returns the full language name.
_LANG_NAME_TO_ISO = {
    "français": "fr", "francais": "fr", "french": "fr", "fr": "fr",
    "anglais": "en", "english": "en", "en": "en",
    "espagnol": "es", "spanish": "es", "español": "es", "es": "es",
    "portugais": "pt", "portuguese": "pt", "português": "pt", "pt": "pt",
    "allemand": "de", "german": "de", "deutsch": "de", "de": "de",
    "italien": "it", "italian": "it", "italiano": "it", "it": "it",
    "néerlandais": "nl", "neerlandais": "nl", "dutch": "nl", "nl": "nl",
    "chinois": "zh", "chinese": "zh", "mandarin": "zh", "zh": "zh",
    "japonais": "ja", "japanese": "ja", "ja": "ja",
    "arabe": "ar", "arabic": "ar", "ar": "ar",
    "russe": "ru", "russian": "ru", "ru": "ru",
    "coréen": "ko", "coreen": "ko", "korean": "ko", "ko": "ko",
    "hindi": "hi", "hi": "hi",
    "polonais": "pl", "polish": "pl", "pl": "pl",
    "catalan": "ca", "ca": "ca",
}


def _normalise_lang_code(raw: str) -> str:
    """Turn 'Français' / 'French' / 'FR' / 'fr' → 'fr'. Unknown → first 2 chars."""
    s = (raw or "").strip().lower()
    if not s:
        return ""
    if s in _LANG_NAME_TO_ISO:
        return _LANG_NAME_TO_ISO[s]
    # fallback: take first 2 chars (handles iso codes we didn't list)
    return s[:2]


def extract_cv_structured(cv_text: str) -> dict[str, Any]:
    """Run Groq then Gemini on the raw CV text and return a normalised dict.

    Never raises — on any failure returns `empty_cv_extraction()` augmented
    with an `_error` field that the UI can surface to the user.
    """
    if not cv_text or not cv_text.strip():
        out = empty_cv_extraction()
        out["_error"] = "CV vide."
        return out

    # Truncate to keep tokens sane. Real CVs rarely exceed 6-8 KB.
    cv_slice = cv_text[:12000]

    # --- 1) Groq primary
    result, err_groq = _call_groq(cv_slice)
    if result is None:
        # --- 2) Gemini fallback
        result, err_gem = _call_gemini(cv_slice)
        if result is None:
            out = empty_cv_extraction()
            out["_error"] = (
                f"LLM indisponible (Groq: {err_groq} ; Gemini: {err_gem}). "
                "Remplis à la main ou réessaie."
            )
            return out
        provider_note = f"via Gemini (fallback — Groq: {err_groq})"
    else:
        provider_note = "via Groq"

    normalised = _normalise(result)
    normalised["_provider"] = provider_note

    # Surface a soft warning if the LLM returned valid JSON but all empty.
    populated = (
        normalised["current_role"]
        or normalised["years_xp"]
        or normalised["top_skills"]
        or normalised["education"]
        or normalised["languages"]
    )
    if not populated:
        normalised["_error"] = (
            "L'IA n'a rien trouvé d'exploitable dans ce CV. "
            "Vérifie que le PDF n'est pas scanné (image) — sinon remplis à la main."
        )
    return normalised


def _normalise(raw: dict[str, Any]) -> dict[str, Any]:
    """Ensure every expected key is present and correctly typed."""
    out = empty_cv_extraction()
    out["current_role"] = _str(raw.get("current_role"))
    out["years_xp"] = _int(raw.get("years_xp"))
    out["top_skills"] = _list_str(raw.get("top_skills"))
    out["industries_past"] = _list_str(raw.get("industries_past"))
    out["motivations_from_cv"] = _str(raw.get("motivations_from_cv"))[:400]
    out["draft_brief"] = _str(raw.get("draft_brief"))

    exp_in = raw.get("experiences") or []
    out["experiences"] = [
        {
            "company": _str(e.get("company")),
            "title": _str(e.get("title")),
            "start": _str(e.get("start")),
            "end": _str(e.get("end")) or "present",
            "location": _str(e.get("location")),
        }
        for e in exp_in
        if isinstance(e, dict) and (e.get("company") or e.get("title"))
    ][:10]

    edu_in = raw.get("education") or []
    out["education"] = [
        {
            "school": _str(e.get("school")),
            "degree": _str(e.get("degree")),
            "year": _int_or_none(e.get("year")),
        }
        for e in edu_in
        if isinstance(e, dict) and (e.get("school") or e.get("degree"))
    ][:5]

    # Languages: dedup by ISO code, map full names → ISO, keep best level.
    _LEVEL_RANK = {"native": 7, "C2": 6, "C1": 5, "B2": 4, "B1": 3, "A2": 2, "A1": 1}
    buckets: dict[str, str] = {}
    for l in (raw.get("languages") or []):
        if not isinstance(l, dict) or not l.get("lang"):
            continue
        code = _normalise_lang_code(_str(l.get("lang")))
        if not code:
            continue
        level_raw = _str(l.get("level"))
        level = level_raw if level_raw.lower() == "native" else level_raw.upper()
        if level.lower() != "native" and level not in _LEVEL_RANK:
            level = "B2"
        if level.lower() == "native":
            level = "native"
        prev = buckets.get(code)
        if prev is None or _LEVEL_RANK.get(level, 0) > _LEVEL_RANK.get(prev, 0):
            buckets[code] = level
    out["languages"] = [{"lang": k, "level": v} for k, v in buckets.items()][:10]

    return out


# ---------------------------------------------------------------------------
# Tiny coercion helpers — keep typing predictable for the UI layer.
# ---------------------------------------------------------------------------
def _str(v: Any) -> str:
    return str(v).strip() if v is not None else ""


def _int(v: Any) -> int:
    try:
        return int(v) if v is not None else 0
    except (TypeError, ValueError):
        return 0


def _int_or_none(v: Any) -> int | None:
    try:
        return int(v) if v not in (None, "", "null") else None
    except (TypeError, ValueError):
        return None


def _list_str(v: Any) -> list[str]:
    if not isinstance(v, list):
        return []
    return [str(x).strip() for x in v if str(x).strip()]


# ---------------------------------------------------------------------------
# Smoke test : python -m app.lib.cv_extractor cv.pdf
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    from app.lib.cv_parser import parse_cv_from_path

    if len(sys.argv) < 2:
        print("usage: python -m app.lib.cv_extractor <cv.pdf>")
        sys.exit(1)
    cv = parse_cv_from_path(sys.argv[1])
    result = extract_cv_structured(cv)
    print(json.dumps(result, ensure_ascii=False, indent=2))
