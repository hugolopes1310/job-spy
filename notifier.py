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


def send_telegram(cfg: dict, text: str, disable_preview: bool = True) -> bool:
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


def format_job_message(row, llm_score: int = -1, llm_reason: str = "") -> str:
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
    if llm_reason:
        lines.append(f"💡 {esc(llm_reason)}")
    lines.append(f"🔗 {url}")

    return "\n".join(lines)