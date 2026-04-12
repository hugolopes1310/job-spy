"""SQLite storage for scraped job offers."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterable

DB_PATH = Path(__file__).parent / "jobs.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id             TEXT PRIMARY KEY,           -- hash(url) or job_url
    axe            TEXT NOT NULL,              -- name of the axis it came from
    title          TEXT NOT NULL,
    company        TEXT,
    location       TEXT,
    url            TEXT NOT NULL,
    description    TEXT,
    date_posted    TEXT,
    site           TEXT,
    score          INTEGER NOT NULL DEFAULT 0,
    score_reasons  TEXT,                        -- JSON-ish blob describing why
    status         TEXT NOT NULL DEFAULT 'new', -- new / notified / applied / ignored / interview
    first_seen     TEXT NOT NULL,
    notified_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_axe    ON jobs(axe);
CREATE INDEX IF NOT EXISTS idx_jobs_score  ON jobs(score);
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


def job_exists(conn, job_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return row is not None


def insert_job(conn, job: dict) -> None:
    conn.execute(
        """
        INSERT INTO jobs (id, axe, title, company, location, url, description,
                          date_posted, site, score, score_reasons, status, first_seen)
        VALUES (:id, :axe, :title, :company, :location, :url, :description,
                :date_posted, :site, :score, :score_reasons, :status, :first_seen)
        """,
        {
            "id": job["id"],
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
