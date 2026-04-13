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

OFFSET_KEY = "tg_update_offset"
VALID_ACTIONS = {"good", "bad", "applied"}


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
                    if action in VALID_ACTIONS:
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
