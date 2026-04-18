"""Poll Telegram for callback_query events (👍/👎/✅ button taps) and persist them.

Why polling instead of a webhook: the pipeline runs on GitHub Actions (ephemeral
runners) so we can't host a long-running webhook. `getUpdates` with offset
tracking is a robust fallback — each scrape tick drains new button presses.

Call this at the start of every scrape run.

Usage:
    python feedback_poller.py        # drain once
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

from db import DB_PATH, connect, get_state, init_db, save_feedback, set_state

# Cover letter generation imports (lazy — only used when gen_cl callback arrives)
_cl_deps_loaded = False
_generate_cover_letter = None
_write_cover_letters = None


def _ensure_cl_deps():
    global _cl_deps_loaded, _generate_cover_letter, _write_cover_letters
    if _cl_deps_loaded:
        return
    try:
        from cover_letter import generate_cover_letter
        from cover_letter_docx import write_cover_letters
        _generate_cover_letter = generate_cover_letter
        _write_cover_letters = write_cover_letters
    except ImportError as e:
        print(f"[poller] CL deps not available: {e}")
    _cl_deps_loaded = True


def _fetch_job(conn, job_id: str) -> dict | None:
    row = conn.execute(
        "SELECT id, title, company, location, description, url FROM jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def _handle_gen_cl(conn, job_id: str, token: str, chat_id, message_id, cfg: dict) -> bool:
    """Generate cover letters for a job and send links back via Telegram."""
    _ensure_cl_deps()
    if not _generate_cover_letter or not _write_cover_letters:
        _send_reply(token, chat_id, "❌ Dépendances CL manquantes sur le runner.")
        return False

    job = _fetch_job(conn, job_id)
    if not job:
        _send_reply(token, chat_id, "❌ Offre introuvable dans la base.")
        return False

    print(f"[poller] Generating CL for: {job['title']} @ {job['company']}")
    content = _generate_cover_letter(
        title=job["title"] or "",
        company=job["company"] or "",
        location=job["location"] or "",
        description=job["description"] or "",
    )
    if not content:
        _send_reply(token, chat_id, "❌ Erreur lors de la génération de la CL.")
        return False

    cl_cfg = cfg.get("cover_letter", {}) or {}
    cl_out_dir = Path(__file__).parent / cl_cfg.get("output_dir", "cover_letters")
    cl_raw_base = (cl_cfg.get("github_raw_base") or "").rstrip("/")
    sender = cl_cfg.get("sender", {})

    try:
        fr_path, en_path = _write_cover_letters(
            output_dir=cl_out_dir,
            job_id=job["id"],
            title=job["title"] or "job",
            company=job["company"] or "company",
            location=job["location"] or "",
            sender=sender,
            content=content,
        )
    except Exception as e:
        print(f"[poller] CL write failed: {e}")
        _send_reply(token, chat_id, f"❌ Erreur écriture CL: {e}")
        return False

    # Build URLs
    import urllib.parse as _up
    cl_parts = []
    if cl_raw_base:
        rel_fr = f"{cl_out_dir.name}/{fr_path.name}"
        rel_en = f"{cl_out_dir.name}/{en_path.name}"
        cl_parts.append(f"[📝 CL FR]({cl_raw_base}/{_up.quote(rel_fr)})")
        cl_parts.append(f"[📝 CL EN]({cl_raw_base}/{_up.quote(rel_en)})")

    title_esc = (job["title"] or "?").replace("*", "").replace("_", "")
    company_esc = (job["company"] or "?").replace("*", "").replace("_", "")
    msg = f"📝 *Cover letters générées*\n{title_esc} @ {company_esc}\n"
    if cl_parts:
        msg += "  •  ".join(cl_parts)
    else:
        msg += f"Fichiers: {fr_path.name} / {en_path.name}"

    _send_reply(token, chat_id, msg)
    return True


def _send_reply(token: str, chat_id, text: str):
    """Send a simple Telegram message."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    data = urllib.parse.urlencode(payload).encode()
    try:
        urllib.request.urlopen(url, data=data, timeout=10)
    except Exception as e:
        print(f"[poller] send_reply failed: {e}")

OFFSET_KEY = "tg_update_offset"
VALID_ACTIONS = {"good", "bad", "applied"}
CL_ACTION = "gen_cl"


def _env(cfg_section: dict, key: str) -> str | None:
    env_name = cfg_section.get(f"{key}_env")
    if env_name:
        val = os.environ.get(env_name)
        if val:
            return val
    return cfg_section.get(key)


def _tg_api(token: str, method: str, params: dict | None = None, post: bool = False):
    url = f"https://api.telegram.org/bot{token}/{method}"
    if post:
        data = urllib.parse.urlencode(params or {}).encode()
        req = urllib.request.Request(url, data=data)
    else:
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def _resolve_job_id(conn, prefix: str) -> str | None:
    """Callback data carries only the first 16 chars of job_id — look up the real row."""
    row = conn.execute(
        "SELECT id FROM jobs WHERE id LIKE ? LIMIT 2",
        (f"{prefix}%",),
    ).fetchall()
    if len(row) != 1:
        return None
    return row[0][0]


def poll_once(cfg: dict) -> int:
    """Drain pending updates once. Returns number of feedback rows stored."""
    tg_cfg = cfg.get("telegram", {})
    token = _env(tg_cfg, "bot_token")
    if not token:
        print("[poller] No Telegram token — skipping")
        return 0

    init_db(DB_PATH)
    saved = 0
    with connect(DB_PATH) as conn:
        offset_str = get_state(conn, OFFSET_KEY, "0") or "0"
        offset = int(offset_str)
        try:
            resp = _tg_api(token, "getUpdates", {"offset": offset, "timeout": 0})
        except Exception as e:  # noqa: BLE001
            print(f"[poller] getUpdates failed: {e}")
            return 0
        if not resp.get("ok"):
            print(f"[poller] Telegram error: {resp}")
            return 0

        updates = resp.get("result", [])
        last_id = offset - 1
        for upd in updates:
            last_id = max(last_id, upd["update_id"])

            cq = upd.get("callback_query")
            if cq:
                data = cq.get("data") or ""
                callback_id = cq["id"]
                msg_ref = cq.get("message") or {}
                chat_id = (msg_ref.get("chat") or {}).get("id")
                message_id = msg_ref.get("message_id")
                saved_this = False
                if ":" in data:
                    action, prefix = data.split(":", 1)
                    if action == CL_ACTION:
                        jid = _resolve_job_id(conn, prefix)
                        if jid:
                            cl_ok = _handle_gen_cl(conn, jid, token, chat_id, message_id, cfg)
                            answer_text = "📝 CL en cours..." if cl_ok else "❌ Erreur CL"
                            confirm_label = "📝 CL générée" if cl_ok else None
                            saved_this = cl_ok
                        else:
                            answer_text = "❓ Offre introuvable"
                            confirm_label = None
                    elif action in VALID_ACTIONS:
                        jid = _resolve_job_id(conn, prefix)
                        if jid:
                            save_feedback(conn, jid, action)
                            saved += 1
                            saved_this = True
                            answer_text = {
                                "good": "👍 Noté (good)",
                                "bad": "👎 Noté (bad) — pattern à éviter",
                                "applied": "✅ Applied enregistré",
                            }[action]
                            confirm_label = {
                                "good": "👍 Good — enregistré",
                                "bad": "👎 Bad — enregistré",
                                "applied": "✅ Applied — enregistré",
                            }[action]
                        else:
                            answer_text = "❓ Offre introuvable"
                            confirm_label = None
                    else:
                        answer_text = "Action inconnue"
                        confirm_label = None
                else:
                    answer_text = "Callback malformé"
                    confirm_label = None

                # Best-effort toast (may 400 if callback expired — non-fatal)
                try:
                    _tg_api(token, "answerCallbackQuery", {
                        "callback_query_id": callback_id,
                        "text": answer_text,
                    }, post=True)
                except urllib.error.HTTPError as e:
                    if e.code != 400:
                        print(f"[poller] answerCallbackQuery failed: {e}")
                except Exception as e:  # noqa: BLE001
                    print(f"[poller] answerCallbackQuery failed: {e}")

                # Persistent confirmation: replace the inline keyboard on the
                # original message with a single disabled chip showing the
                # action taken. This survives callback TTL.
                if saved_this and chat_id and message_id and confirm_label:
                    new_markup = {
                        "inline_keyboard": [[
                            {"text": confirm_label, "callback_data": "noop"}
                        ]]
                    }
                    try:
                        _tg_api(token, "editMessageReplyMarkup", {
                            "chat_id": chat_id,
                            "message_id": message_id,
                            "reply_markup": json.dumps(new_markup),
                        }, post=True)
                    except Exception as e:  # noqa: BLE001
                        print(f"[poller] editMessageReplyMarkup failed: {e}")

            # Slash-command support (optional: /applied <jobid>)
            msg = upd.get("message")
            if msg and isinstance(msg.get("text"), str):
                parts = msg["text"].strip().split()
                if parts and parts[0].startswith("/"):
                    cmd = parts[0][1:].lower()
                    if cmd in VALID_ACTIONS and len(parts) >= 2:
                        jid = _resolve_job_id(conn, parts[1])
                        if jid:
                            save_feedback(conn, jid, cmd)
                            saved += 1

        if updates:
            set_state(conn, OFFSET_KEY, str(last_id + 1))

    print(f"[poller] Processed {len(updates) if 'updates' in dir() else 0} update(s), saved {saved} feedback row(s)")
    return saved


def main() -> int:
    cfg_path = Path(__file__).parent / "config.yaml"
    cfg = yaml.safe_load(open(cfg_path))
    poll_once(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
