"""Telegram notifier.

Sends Markdown messages via the Telegram Bot API. Uses only urllib so no
extra dependency is needed beyond the standard library.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request


def _env(cfg_section: dict, key: str) -> str | None:
    env_name = cfg_section.get(f"{key}_env")
    if env_name:
        val = os.environ.get(env_name)
        if val:
            return val
    return cfg_section.get(key)


def send_telegram(
    cfg: dict,
    text: str,
    disable_preview: bool = True,
    reply_markup: dict | None = None,
) -> bool:
    token = _env(cfg, "bot_token")
    chat_id = _env(cfg, "chat_id")
    if not token or not chat_id:
        print("[notifier] Telegram not configured — skipping send")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": disable_preview,
    }
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup)
    data = urllib.parse.urlencode(payload).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=10) as r:
            resp = json.loads(r.read().decode())
            if not resp.get("ok"):
                print(f"[notifier] Telegram error: {resp}")
                return False
            return True
    except Exception as e:  # noqa: BLE001
        print(f"[notifier] Telegram exception: {e}")
        return False


def feedback_keyboard(job_id: str) -> dict:
    """Inline keyboard with 👍 / 👎 / ✅ Applied / 📝 CL — callback_data carries the job id short prefix."""
    jid = job_id[:16]  # Telegram callback_data limited to 64 bytes, prefix is enough
    return {
        "inline_keyboard": [
            [
                {"text": "👍 Good", "callback_data": f"good:{jid}"},
                {"text": "👎 Bad", "callback_data": f"bad:{jid}"},
                {"text": "✅ Applied", "callback_data": f"applied:{jid}"},
            ],
            [
                {"text": "📝 Générer CL", "callback_data": f"gen_cl:{jid}"},
            ],
        ]
    }


def format_job_message(
    row,
    llm_score: int = -1,
    llm_reason: str = "",
    cl_fr_url: str = "",
    cl_en_url: str = "",
    analysis: dict | None = None,
    company_enrichment: dict | None = None,
) -> str:
    """Format one SQLite row as a Markdown Telegram message.

    If llm_score >= 0, adds the AI fit assessment.
    """
    title = row["title"] or "?"
    company = row["company"] or "?"
    location = row["location"] or "?"
    axe = row["axe"] or "?"
    kw_score = row["score"]
    url = row["url"]

    def esc(s: str) -> str:
        return (
            str(s)
            .replace("\\", "\\\\")
            .replace("*", "\\*")
            .replace("_", "\\_")
            .replace("`", "\\`")
            .replace("[", "\\[")
        )

    lines = [
        f"*[KW:{kw_score}]*",
    ]
    if llm_score >= 0:
        lines[0] += f" *[AI:{llm_score}/10]*"
    lines.append(f"{esc(title)}")
    lines.append(f"🏢 {esc(company)}  •  📍 {esc(location)}")
    lines.append(f"🎯 {esc(axe)}")
    if llm_reason and not llm_reason.startswith("LLM error"):
        lines.append(f"💡 {esc(llm_reason)}")

    # Structured sub-scores + atouts + red flags
    if analysis:
        sub = []
        for key, label in [
            ("match_finance", "finance"),
            ("match_geo", "geo"),
            ("match_seniorite", "sen"),
        ]:
            v = analysis.get(key)
            if isinstance(v, int) and v >= 0:
                sub.append(f"{label}:{v}")
        if sub:
            lines.append(f"📈 {' · '.join(sub)}")

        atouts = analysis.get("atouts") or []
        if atouts:
            lines.append(f"✅ {esc(' · '.join(atouts[:3]))}")
        red_flags = analysis.get("red_flags") or []
        if red_flags:
            lines.append(f"⚠️ {esc(' · '.join(red_flags[:2]))}")

        facts = []
        if analysis.get("salary"):
            facts.append(f"💰 {esc(analysis['salary'])}")
        if analysis.get("deadline"):
            facts.append(f"📅 {esc(analysis['deadline'])}")
        if analysis.get("contact"):
            facts.append(f"👤 {esc(analysis['contact'])}")
        if facts:
            lines.append(" · ".join(facts))

        if analysis.get("apply_hint"):
            lines.append(f"➡️ {esc(analysis['apply_hint'])}")

    # Company mini-fiche (first time we see this company)
    if company_enrichment:
        ce_parts = []
        if company_enrichment.get("type"):
            ce_parts.append(esc(company_enrichment["type"]))
        if company_enrichment.get("size"):
            ce_parts.append(esc(company_enrichment["size"]))
        header = " · ".join(ce_parts)
        if header or company_enrichment.get("positioning"):
            lines.append("")
            lines.append(f"🏷 *Company* {header}")
            if company_enrichment.get("positioning"):
                lines.append(f"  _{esc(company_enrichment['positioning'])}_")
            if company_enrichment.get("relevance"):
                lines.append(f"  🎯 {esc(company_enrichment['relevance'])}")
            issues = company_enrichment.get("known_issues") or []
            if issues:
                lines.append(f"  ⚠️ {esc(' · '.join(issues[:2]))}")

    lines.append(f"🔗 {url}")
    if cl_fr_url or cl_en_url:
        cl_parts = []
        if cl_fr_url:
            cl_parts.append(f"[📝 CL FR]({cl_fr_url})")
        if cl_en_url:
            cl_parts.append(f"[📝 CL EN]({cl_en_url})")
        lines.append("  •  ".join(cl_parts))

    return "\n".join(lines)