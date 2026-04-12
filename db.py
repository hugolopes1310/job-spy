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
    status         TEXT NOT NULL DEFAULT 'new',
    first_seen     TEXT NOT NULL,
    notified_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status      ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_axe         ON jobs(axe);
CREATE INDEX IF NOT EXISTS idx_jobs_score       ON jobs(score);
CREATE INDEX IF NOT EXISTS idx_jobs_fingerprint ON jobs(fingerprint);
"""

MIGRATE_FINGERPRINT = """
ALTER TABLE jobs ADD COLUMN fingerprint TEXT;
"""


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
        # migrate older DBs that don't have fingerprint column
        try:
            c.execute(MIGRATE_FINGERPRINT)
        except sqlite3.OperationalError:
            pass  # column already exists


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


def insert_job(conn, job: dict) -> None:
    conn.execute(
        """
        INSERT INTO jobs (id, fingerprint, axe, title, company, location, url,
                          description, date_posted, site, score, score_reasons,
                          status, first_seen)
        VALUES (:id, :fingerprint, :axe, :title, :company, :location, :url,
                :description, :date_posted, :site, :score, :score_reasons,
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