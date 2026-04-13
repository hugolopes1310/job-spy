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