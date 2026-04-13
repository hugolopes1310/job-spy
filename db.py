"""SQLite storage for scraped job offers — with cross-source dedup."""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterable

DB_PATH = Path(__file__).parent / "jobs.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id             TEXT PRIMARY KEY,           -- hash(url)
    fingerprint    TEXT,                        -- normalized title+company for cross-source dedup
    axe            TEXT NOT NULL,
    title          TEXT NOT NULL,
    company        TEXT,
    location       TEXT,
    url            TEXT NOT NULL,
    description    TEXT,
    date_posted    TEXT,
    site           TEXT,
    score          INTEGER NOT NULL DEFAULT 0,
    score_reasons  TEXT,
    llm_analysis   TEXT,                        -- JSON blob from structured Groq call
    is_repost      INTEGER NOT NULL DEFAULT 0, -- 1 if this fingerprint was already seen > N days ago
    repost_of      TEXT,                        -- id of the original job (oldest with same fingerprint)
    repost_gap_days INTEGER,                    -- days between original and this repost
    status         TEXT NOT NULL DEFAULT 'new',
    first_seen     TEXT NOT NULL,
    notified_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status      ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_axe         ON jobs(axe);
CREATE INDEX IF NOT EXISTS idx_jobs_score       ON jobs(score);
CREATE INDEX IF NOT EXISTS idx_jobs_fingerprint ON jobs(fingerprint);

CREATE TABLE IF NOT EXISTS companies (
    name          TEXT PRIMARY KEY,   -- normalized company name
    display_name  TEXT,
    enrichment    TEXT,                -- JSON blob (size, positioning, news, etc.)
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL,
    action      TEXT NOT NULL,           -- 'good' | 'bad' | 'applied'
    created_at  TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(id)
);
CREATE INDEX IF NOT EXISTS idx_feedback_job_id ON feedback(job_id);
CREATE INDEX IF NOT EXISTS idx_feedback_action ON feedback(action);
CREATE INDEX IF NOT EXISTS idx_feedback_created ON feedback(created_at);

CREATE TABLE IF NOT EXISTS telegram_state (
    key    TEXT PRIMARY KEY,
    value  TEXT
);
"""

MIGRATIONS = [
    "ALTER TABLE jobs ADD COLUMN fingerprint TEXT",
    "ALTER TABLE jobs ADD COLUMN llm_analysis TEXT",
    "ALTER TABLE jobs ADD COLUMN is_repost INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN repost_of TEXT",
    "ALTER TABLE jobs ADD COLUMN repost_gap_days INTEGER",
]


@contextmanager
def connect(db_path: Path = DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: Path = DB_PATH) -> None:
    with connect(db_path) as c:
        c.executescript(SCHEMA)
        for stmt in MIGRATIONS:
            try:
                c.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already exists


def save_llm_analysis(conn, job_id: str, analysis_json: str) -> None:
    conn.execute("UPDATE jobs SET llm_analysis = ? WHERE id = ?", (analysis_json, job_id))


def get_company(conn, name: str):
    return conn.execute(
        "SELECT * FROM companies WHERE name = ?", (normalize(name),),
    ).fetchone()


def save_company(conn, display_name: str, enrichment_json: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO companies (name, display_name, enrichment, created_at) "
        "VALUES (?, ?, ?, ?)",
        (normalize(display_name), display_name, enrichment_json, datetime.utcnow().isoformat()),
    )


def fetch_recent_titles_for_company(conn, company: str, days: int = 14):
    """Return (id, title) for rows from same company in last N days (for semantic dedup)."""
    from datetime import timedelta
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    return conn.execute(
        "SELECT id, title FROM jobs WHERE company = ? AND first_seen > ? ORDER BY first_seen DESC",
        (company, cutoff),
    ).fetchall()


def fetch_notified_last_week(conn):
    """All rows notified in the last 7 days — used by the weekly summary."""
    from datetime import timedelta
    cutoff = (datetime.utcnow() - timedelta(days=7)).isoformat()
    return conn.execute(
        "SELECT * FROM jobs WHERE status = 'notified' AND notified_at > ? ORDER BY score DESC, notified_at DESC",
        (cutoff,),
    ).fetchall()


def save_feedback(conn, job_id: str, action: str) -> None:
    """Record a user feedback action on a job (good/bad/applied).

    Idempotent: avoid duplicates for the same (job_id, action).
    """
    existing = conn.execute(
        "SELECT 1 FROM feedback WHERE job_id = ? AND action = ?",
        (job_id, action),
    ).fetchone()
    if existing:
        return
    conn.execute(
        "INSERT INTO feedback (job_id, action, created_at) VALUES (?, ?, ?)",
        (job_id, action, datetime.utcnow().isoformat()),
    )


def get_feedback_for_job(conn, job_id: str) -> list[str]:
    return [
        r["action"]
        for r in conn.execute(
            "SELECT action FROM feedback WHERE job_id = ? ORDER BY created_at ASC",
            (job_id,),
        ).fetchall()
    ]


def fetch_recent_feedback_examples(conn, action: str, limit: int = 5):
    """Return recent feedback rows joined with job metadata for few-shot prompting."""
    return conn.execute(
        """
        SELECT j.title, j.company, j.location, j.axe, f.created_at
        FROM feedback f
        JOIN jobs j ON j.id = f.job_id
        WHERE f.action = ?
        ORDER BY f.created_at DESC
        LIMIT ?
        """,
        (action, limit),
    ).fetchall()


def get_state(conn, key: str, default: str | None = None) -> str | None:
    row = conn.execute(
        "SELECT value FROM telegram_state WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else default


def set_state(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO telegram_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def monthly_feedback_stats(conn, year: int, month: int) -> dict:
    """Return aggregate stats for a given month:
       {notified, applied, good, bad, conversion_pct, avg_hours_to_apply,
        by_axe: {axe: {notified, applied, good, bad}}}."""
    from datetime import timedelta
    start = datetime(year, month, 1)
    if month == 12:
        end = datetime(year + 1, 1, 1)
    else:
        end = datetime(year, month + 1, 1)
    start_iso, end_iso = start.isoformat(), end.isoformat()

    # Notified during the month
    notified_rows = conn.execute(
        "SELECT id, axe, notified_at FROM jobs "
        "WHERE status = 'notified' AND notified_at >= ? AND notified_at < ?",
        (start_iso, end_iso),
    ).fetchall()
    notified_total = len(notified_rows)
    by_axe: dict[str, dict[str, int]] = {}
    notified_by_id = {}
    for r in notified_rows:
        axe = r["axe"] or "?"
        d = by_axe.setdefault(axe, {"notified": 0, "applied": 0, "good": 0, "bad": 0})
        d["notified"] += 1
        notified_by_id[r["id"]] = (axe, r["notified_at"])

    # Feedback during the month (we count the feedback date, not the job date,
    # to match "this month's user activity").
    fb_rows = conn.execute(
        "SELECT job_id, action, created_at FROM feedback "
        "WHERE created_at >= ? AND created_at < ?",
        (start_iso, end_iso),
    ).fetchall()
    applied, good, bad = 0, 0, 0
    delays_hours: list[float] = []
    for f in fb_rows:
        action = f["action"]
        if action == "applied":
            applied += 1
        elif action == "good":
            good += 1
        elif action == "bad":
            bad += 1
        if action in ("applied", "good", "bad"):
            axe_info = notified_by_id.get(f["job_id"])
            if axe_info:
                axe, notified_at = axe_info
                d = by_axe.setdefault(axe, {"notified": 0, "applied": 0, "good": 0, "bad": 0})
                d[action] = d.get(action, 0) + 1
                if action == "applied" and notified_at:
                    try:
                        dt_n = datetime.fromisoformat(notified_at)
                        dt_a = datetime.fromisoformat(f["created_at"])
                        delays_hours.append((dt_a - dt_n).total_seconds() / 3600.0)
                    except ValueError:
                        pass

    conv_pct = (applied / notified_total * 100.0) if notified_total else 0.0
    avg_hours = (sum(delays_hours) / len(delays_hours)) if delays_hours else 0.0
    return {
        "notified": notified_total,
        "applied": applied,
        "good": good,
        "bad": bad,
        "conversion_pct": round(conv_pct, 1),
        "avg_hours_to_apply": round(avg_hours, 1),
        "by_axe": by_axe,
    }


def normalize(text: str) -> str:
    """Lowercase, strip accents, remove punctuation and extra spaces."""
    text = text.lower().strip()
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def make_fingerprint(title: str, company: str) -> str:
    """Create a fingerprint from title+company for cross-source dedup."""
    return normalize(f"{title} @ {company}")


def job_exists(conn, job_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return row is not None


def fingerprint_exists(conn, fingerprint: str) -> bool:
    """Check if a job with the same title+company combo already exists."""
    if not fingerprint:
        return False
    row = conn.execute(
        "SELECT 1 FROM jobs WHERE fingerprint = ?", (fingerprint,)
    ).fetchone()
    return row is not None


def fingerprint_status(conn, fingerprint: str):
    """Return (oldest_id, oldest_first_seen_iso, count) for a fingerprint, or None if new.

    Used for repost detection: an offer with the same fingerprint resurfacing after
    a long gap is a repost (likely turnover or unfilled position).
    """
    if not fingerprint:
        return None
    row = conn.execute(
        """
        SELECT id, first_seen, COUNT(*) OVER() AS cnt
        FROM jobs
        WHERE fingerprint = ?
        ORDER BY first_seen ASC
        LIMIT 1
        """,
        (fingerprint,),
    ).fetchone()
    if row is None:
        return None
    return row["id"], row["first_seen"], row["cnt"]


def insert_job(conn, job: dict) -> None:
    conn.execute(
        """
        INSERT INTO jobs (id, fingerprint, axe, title, company, location, url,
                          description, date_posted, site, score, score_reasons,
                          is_repost, repost_of, repost_gap_days,
                          status, first_seen)
        VALUES (:id, :fingerprint, :axe, :title, :company, :location, :url,
                :description, :date_posted, :site, :score, :score_reasons,
                :is_repost, :repost_of, :repost_gap_days,
                :status, :first_seen)
        """,
        {
            "id": job["id"],
            "fingerprint": job.get("fingerprint"),
            "axe": job["axe"],
            "title": job["title"],
            "company": job.get("company"),
            "location": job.get("location"),
            "url": job["url"],
            "description": job.get("description"),
            "date_posted": job.get("date_posted"),
            "site": job.get("site"),
            "score": job.get("score", 0),
            "score_reasons": job.get("score_reasons"),
            "is_repost": 1 if job.get("is_repost") else 0,
            "repost_of": job.get("repost_of"),
            "repost_gap_days": job.get("repost_gap_days"),
            "status": job.get("status", "new"),
            "first_seen": job.get("first_seen") or datetime.utcnow().isoformat(),
        },
    )


def mark_notified(conn, job_id: str) -> None:
    conn.execute(
        "UPDATE jobs SET status = 'notified', notified_at = ? WHERE id = ? AND status = 'new'",
        (datetime.utcnow().isoformat(), job_id),
    )


def fetch_new_above(conn, threshold: int) -> Iterable[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM jobs WHERE status = 'new' AND score >= ? ORDER BY score DESC, first_seen DESC",
        (threshold,),
    ).fetchall()


def stats(conn) -> dict:
    rows = conn.execute(
        "SELECT axe, status, COUNT(*) as n FROM jobs GROUP BY axe, status"
    ).fetchall()
    out: dict = {}
    for r in rows:
        out.setdefault(r["axe"], {})[r["status"]] = r["n"]
    return out