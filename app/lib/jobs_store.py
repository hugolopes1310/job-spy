"""CRUD for the `jobs` and `user_job_matches` tables (Phase 3).

Same dual-client pattern as `storage.py`:
  - `use_service=True`  → service_role, bypasses RLS (scraper, admin CLI)
  - `use_service=False` → anon + JWT, RLS-enforced (Streamlit page reads)
"""
from __future__ import annotations

import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, TypeVar

from app.lib.auth import get_user_scoped_client
from app.lib.klog import log
from app.lib.scrapers import job_id_for
from app.lib.supabase_client import get_service_client


# ---------------------------------------------------------------------------
# Client selector (same convention as storage.py)
# ---------------------------------------------------------------------------
def _client(use_service: bool = False):
    if use_service:
        return get_service_client()
    c = get_user_scoped_client()
    if c is None:
        raise RuntimeError(
            "No authenticated user in session. "
            "Call from a page behind the auth gate, or pass use_service=True."
        )
    return c


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Retry helper — exponential backoff for transient PostgREST/network errors.
# ---------------------------------------------------------------------------
T = TypeVar("T")

# Status codes worth retrying. 408 = request timeout, 5xx = server errors.
# 401/403/404/409/422 are user/data errors → no retry.
_RETRY_STATUS: tuple[int, ...] = (408, 500, 502, 503, 504)
# Stings that show up in transient errors (timeouts, connection resets,
# DNS hiccups, brief proxy 502s). Substring matched, case-insensitive.
_RETRY_SUBSTRINGS: tuple[str, ...] = (
    "timeout",
    "timed out",
    "connection reset",
    "connection aborted",
    "remote disconnected",
    "temporary failure in name resolution",
    "bad gateway",
    "service unavailable",
    "gateway time-out",
    "max retries exceeded",
)


def _is_transient_error(exc: Exception) -> bool:
    """Best-effort detection of retryable errors.

    PostgREST exceptions vary in shape across supabase-py versions; we sniff
    a few attributes (code, status_code, message) plus the str() form.
    """
    # Direct status-code attributes (httpx.HTTPStatusError, postgrest APIError).
    for attr in ("status_code", "code", "status"):
        v = getattr(exc, attr, None)
        try:
            if v is not None and int(v) in _RETRY_STATUS:
                return True
        except (TypeError, ValueError):
            pass
    text = (str(exc) or "").lower()
    if not text:
        return False
    if any(s in text for s in _RETRY_SUBSTRINGS):
        return True
    # Fallback: HTTP <code> form.
    for code in _RETRY_STATUS:
        if f" {code}" in text or f"http {code}" in text or f"{code} " in text:
            return True
    return False


def _with_retry(
    fn: Callable[[], T],
    *,
    op: str,
    attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 4.0,
    extra_ctx: dict[str, Any] | None = None,
) -> T:
    """Run `fn()` with exponential backoff on transient errors.

    Sleeps base_delay × 2**i (capped at max_delay) between attempts.
    Re-raises the last exception when retries are exhausted, or immediately
    if the error is non-transient.
    """
    last: Exception | None = None
    ctx = dict(extra_ctx or {})
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            if i + 1 >= attempts or not _is_transient_error(e):
                # Either out of retries or a permanent error — re-raise.
                log(
                    "db.retry.giving_up",
                    level="error",
                    op=op,
                    attempt=i + 1,
                    error=f"{type(e).__name__}: {e}",
                    **ctx,
                )
                raise
            delay = min(base_delay * (2 ** i), max_delay)
            log(
                "db.retry.transient",
                level="warn",
                op=op,
                attempt=i + 1,
                next_delay_s=round(delay, 2),
                error=f"{type(e).__name__}: {e}",
                **ctx,
            )
            time.sleep(delay)
    # Defensive — shouldn't reach here, but make typing happy.
    assert last is not None
    raise last


# ---------------------------------------------------------------------------
# Fingerprint helpers — identical to V1 db.py so cross-run dedup behaves the
# same between the personal sqlite and the shared Supabase store.
# ---------------------------------------------------------------------------
def _normalize(text: str) -> str:
    text = (text or "").lower().strip()
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def make_fingerprint(title: str, company: str) -> str:
    return _normalize(f"{title} @ {company}")


# ---------------------------------------------------------------------------
# jobs — shared table
# ---------------------------------------------------------------------------
def upsert_job(
    scraped: dict[str, Any],
    *,
    repost_gap_days: int = 45,
) -> tuple[str, bool]:
    """Insert or refresh a scraped job. Returns (job_id, is_new).

    is_new = True  → row didn't exist before, caller should score it.
    is_new = False → same URL already in the DB. We just bump `last_seen`.

    Repost logic: if a job with the same fingerprint existed > gap_days ago
    (different URL), we flag this new row as a repost and link it back.
    Always uses service_role — only the scraper calls this.
    """
    url = scraped["url"]
    jid = job_id_for(url)
    svc = get_service_client()

    # Fast path: already have this URL?
    existing = (
        svc.table("jobs")
        .select("id")
        .eq("id", jid)
        .limit(1)
        .execute()
    )
    if existing.data:
        svc.table("jobs").update({"last_seen": _now()}).eq("id", jid).execute()
        return jid, False

    title = scraped.get("title") or ""
    company = scraped.get("company") or ""
    fp = make_fingerprint(title, company)

    # Repost detection on fingerprint.
    is_repost = False
    repost_of: str | None = None
    gap_days: int | None = None
    if fp:
        prior = (
            svc.table("jobs")
            .select("id, first_seen")
            .eq("fingerprint", fp)
            .order("first_seen", desc=False)
            .limit(1)
            .execute()
        )
        if prior.data:
            prior_id = prior.data[0]["id"]
            first_seen = prior.data[0].get("first_seen")
            try:
                dt = datetime.fromisoformat(first_seen.replace("Z", "+00:00"))
                gap_days = (datetime.now(timezone.utc) - dt).days
            except (AttributeError, ValueError, TypeError):
                gap_days = None
            if gap_days is not None and gap_days >= repost_gap_days:
                is_repost = True
                repost_of = prior_id
            else:
                # Recent dup (same title+company, different URL) — don't insert
                # a new job row. Bump the existing one's last_seen and return
                # its id so callers score against the canonical job.
                svc.table("jobs").update({"last_seen": _now()}).eq("id", prior_id).execute()
                return prior_id, False

    row = {
        "id": jid,
        "url": url,
        "fingerprint": fp or None,
        "title": title,
        "company": company or None,
        "location": scraped.get("location") or None,
        "description": scraped.get("description") or None,
        "date_posted": scraped.get("date_posted") or None,
        "site": scraped.get("site") or None,
        "is_repost": is_repost,
        "repost_of": repost_of,
        "repost_gap_days": gap_days if is_repost else None,
        "first_seen": _now(),
        "last_seen": _now(),
    }
    svc.table("jobs").insert(row).execute()
    return jid, True


def get_job(job_id: str, *, use_service: bool = False) -> dict | None:
    res = (
        _client(use_service)
        .table("jobs")
        .select("*")
        .eq("id", job_id)
        .limit(1)
        .execute()
    )
    data = res.data or []
    return data[0] if data else None


# ---------------------------------------------------------------------------
# user_job_matches
# ---------------------------------------------------------------------------
def match_exists(user_id: str, job_id: str) -> bool:
    """Does the (user, job) match already exist? Scraper check — service_role."""
    res = (
        get_service_client()
        .table("user_job_matches")
        .select("user_id")
        .eq("user_id", user_id)
        .eq("job_id", job_id)
        .limit(1)
        .execute()
    )
    return bool(res.data)


def insert_match(
    user_id: str,
    job_id: str,
    score: int | None,
    analysis: dict | None,
) -> None:
    """Write a fresh (user_id, job_id) row. service_role (scraper)."""
    get_service_client().table("user_job_matches").upsert(
        {
            "user_id": user_id,
            "job_id": job_id,
            "score": score,
            "analysis": analysis,
            "status": "new",
            "scored_at": _now(),
        },
        on_conflict="user_id,job_id",
    ).execute()


def list_matches_for_user(
    user_id: str,
    *,
    min_score: int | None = None,
    status: str | None = None,
    favorites_only: bool = False,
    since_hours: int | None = None,
    limit: int = 200,
    use_service: bool = False,
) -> list[dict]:
    """Return the user's matches enriched with job metadata.

    Relies on the `user_matches_enriched` view defined in phase3_jobs.sql
    (updated by migration_tracking.sql to expose `is_favorite`, `notes`, etc.).

    Args:
        min_score:      filter score >= this value
        status:         filter exact status
        favorites_only: only is_favorite = TRUE
        since_hours:    only matches scored in the last N hours
        limit:          max rows (default 200)
    """
    q = (
        _client(use_service)
        .table("user_matches_enriched")
        .select("*")
        .eq("user_id", user_id)
    )
    if min_score is not None:
        q = q.gte("score", min_score)
    if status:
        q = q.eq("status", status)
    if favorites_only:
        q = q.eq("is_favorite", True)
    if since_hours is not None and since_hours > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
        q = q.gte("scored_at", cutoff)
    res = q.order("score", desc=True).order("scored_at", desc=True).limit(limit).execute()
    return list(res.data or [])


# Sentinel used to distinguish "don't touch" from "explicitly clear" on fields
# where NULL is a meaningful value (e.g. dates). Plain None means "clear".
_UNSET: Any = object()


def update_match(
    user_id: str,
    job_id: str,
    *,
    status: str | None = None,
    feedback: str | None = None,
    seen: bool = False,
    is_favorite: bool | None = None,
    notes: str | None = None,
    applied_at: Any = _UNSET,
    next_action_at: Any = _UNSET,
) -> None:
    """User-facing update (feedback / seen / status / favorite / notes / dates).
    RLS-gated.

    Conventions:
      - For string/bool fields, `None` means "do not touch this column".
      - For date fields (`applied_at`, `next_action_at`), the sentinel `_UNSET`
        means "do not touch". Passing `None` explicitly clears the field ;
        passing a string (ISO-8601) or a `date`/`datetime` object writes it.

    Side effect:
      Passing `seen=True` sets `seen_at = NOW()`. Flip to status='seen' is
      handled by the caller (we keep this function single-responsibility).
    """
    patch: dict[str, Any] = {}
    if status is not None:
        patch["status"] = status
    if feedback is not None:
        patch["feedback"] = feedback
    if is_favorite is not None:
        patch["is_favorite"] = bool(is_favorite)
    if notes is not None:
        patch["notes"] = notes.strip() or None
    if applied_at is not _UNSET:
        patch["applied_at"] = _coerce_iso(applied_at)
    if next_action_at is not _UNSET:
        patch["next_action_at"] = _coerce_iso(next_action_at)
    if seen:
        patch["seen_at"] = _now()
    if not patch:
        return

    def _do() -> None:
        (
            _client(use_service=False)
            .table("user_job_matches")
            .update(patch)
            .eq("user_id", user_id)
            .eq("job_id", job_id)
            .execute()
        )

    _with_retry(
        _do,
        op="update_match",
        extra_ctx={"user_id": user_id, "job_id": job_id, "fields": list(patch.keys())},
    )


def _coerce_iso(value: Any) -> str | None:
    """Coerce a date/datetime/string (or None) to an ISO-8601 string suitable
    for Supabase. Returns None for null inputs, raises for invalid ones.
    """
    if value is None:
        return None
    # datetime.date / datetime.datetime — both have .isoformat()
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        return iso()
    # Plain string — trust the caller, Supabase parses ISO-8601 and DATE.
    return str(value)


def toggle_favorite(user_id: str, job_id: str, value: bool) -> None:
    """Shortcut for the star button on match cards."""
    update_match(user_id, job_id, is_favorite=value)


def set_status(user_id: str, job_id: str, new_status: str) -> None:
    """Move a match through the pipeline. Valid values defined in the DB CHECK.

    Side effects handled by DB trigger `ujm_touch_status` :
      - status_changed_at = NOW()
      - applied_at        = NOW() on first transition to 'applied'
    """
    update_match(user_id, job_id, status=new_status)


_TRACKED_COLUMNS = (
    "job_id, title, company, location, score, status, is_favorite, "
    "notes, applied_at, status_changed_at, next_action_at, "
    "scored_at, url, site, description, analysis, is_repost"
)

_PIPELINE_APPLIED_STATUSES: tuple[str, ...] = (
    "applied", "interview", "offer", "rejected", "archived",
)


def _tracked_base_query(user_id: str, use_service: bool):
    return (
        _client(use_service)
        .table("user_matches_enriched")
        .select(_TRACKED_COLUMNS)
        .eq("user_id", user_id)
    )


def list_tracked_matches(
    user_id: str,
    *,
    view: str = "all",  # 'all' | 'favorites' | 'applied' | 'pipeline'
    use_service: bool = False,
) -> list[dict]:
    """Used by the Suivi page. Returns matches filtered by view:

    - 'favorites'  : is_favorite = TRUE
    - 'applied'    : status IN ('applied', 'interview', 'offer')
    - 'pipeline'   : is_favorite = TRUE OR status IN (applied,interview,offer,rejected,archived)
    - 'all'        : no filter (every match)

    For the 'pipeline' view we run two simple queries and merge in Python rather
    than relying on PostgREST's `or=(a,b.in.(...))` syntax (which can trip on
    comma-separated IN lists when combined with `or_`). More robust, same result.
    """
    if view == "favorites":
        q = _tracked_base_query(user_id, use_service).eq("is_favorite", True)
    elif view == "applied":
        q = _tracked_base_query(user_id, use_service).in_(
            "status", list(("applied", "interview", "offer")),
        )
    elif view == "pipeline":
        # Two simpler queries, merged + deduped by job_id.
        fav_rows = (
            _tracked_base_query(user_id, use_service)
            .eq("is_favorite", True)
            .order("status_changed_at", desc=True)
            .order("scored_at", desc=True)
            .execute().data or []
        )
        tracked_rows = (
            _tracked_base_query(user_id, use_service)
            .in_("status", list(_PIPELINE_APPLIED_STATUSES))
            .order("status_changed_at", desc=True)
            .order("scored_at", desc=True)
            .execute().data or []
        )
        seen: set[str] = set()
        merged: list[dict] = []
        # Favorites first so they keep priority in the dedupe.
        for r in fav_rows + tracked_rows:
            jid = r.get("job_id")
            if jid and jid not in seen:
                seen.add(jid)
                merged.append(r)
        return merged
    else:
        q = _tracked_base_query(user_id, use_service)

    q = q.order("is_favorite", desc=True) \
         .order("status_changed_at", desc=True) \
         .order("scored_at", desc=True)
    return list((q.execute().data) or [])


# Pipeline statuses in display order (for dropdowns and UI).
PIPELINE_STATUSES: tuple[str, ...] = (
    "new", "seen", "applied", "interview", "offer", "rejected", "archived",
)

STATUS_LABELS: dict[str, str] = {
    "new":       "Nouveau",
    "seen":      "Vu",
    "applied":   "Postulé",
    "interview": "Entretien",
    "offer":     "Offre",
    "rejected":  "Refusé",
    "archived":  "Archivé",
}

STATUS_TONES: dict[str, str] = {
    "new":       "muted",
    "seen":      "muted",
    "applied":   "success",
    "interview": "warn",
    "offer":     "success",
    "rejected":  "danger",
    "archived":  "muted",
}


# ---------------------------------------------------------------------------
# KPIs — landing page dashboard
# ---------------------------------------------------------------------------
def get_user_kpis(user_id: str, *, use_service: bool = False) -> dict[str, Any]:
    """Lightweight KPIs for the landing page.

    Returns a dict with:
      - total:          total matches scorés pour ce user
      - top_matches:    matches avec score >= 8
      - new_24h:        matches scorés dans les dernières 24h
      - new_7d:         matches scorés dans les 7 derniers jours
      - unseen:         matches encore au statut 'new' (jamais ouverts)
      - applied:        matches au statut 'applied'
      - last_scored_at: ISO timestamp du dernier match scoré (ou None)
      - avg_top5:       score moyen du top-5 (indicateur de qualité), arrondi à 1 décimale

    Trois requêtes légères (count + fetch top 5) — safe à appeler à chaque page load.
    """
    c = _client(use_service)
    now = datetime.now(timezone.utc)
    cutoff_1h  = (now - timedelta(hours=1)).isoformat()
    cutoff_24h = (now - timedelta(hours=24)).isoformat()
    cutoff_7d  = (now - timedelta(days=7)).isoformat()

    def _count(builder) -> int:
        try:
            r = builder.execute()
            return int(getattr(r, "count", None) or 0)
        except Exception:  # noqa: BLE001
            return 0

    total = _count(
        c.table("user_job_matches").select("job_id", count="exact", head=True).eq("user_id", user_id)
    )
    top_matches = _count(
        c.table("user_job_matches").select("job_id", count="exact", head=True)
         .eq("user_id", user_id).gte("score", 8)
    )
    new_1h = _count(
        c.table("user_job_matches").select("job_id", count="exact", head=True)
         .eq("user_id", user_id).gte("scored_at", cutoff_1h)
    )
    new_24h = _count(
        c.table("user_job_matches").select("job_id", count="exact", head=True)
         .eq("user_id", user_id).gte("scored_at", cutoff_24h)
    )
    new_7d = _count(
        c.table("user_job_matches").select("job_id", count="exact", head=True)
         .eq("user_id", user_id).gte("scored_at", cutoff_7d)
    )
    unseen = _count(
        c.table("user_job_matches").select("job_id", count="exact", head=True)
         .eq("user_id", user_id).eq("status", "new")
    )
    applied = _count(
        c.table("user_job_matches").select("job_id", count="exact", head=True)
         .eq("user_id", user_id).eq("status", "applied")
    )

    last_scored_at: str | None = None
    avg_top5: float | None = None
    try:
        r = (
            c.table("user_job_matches")
             .select("score, scored_at")
             .eq("user_id", user_id)
             .order("scored_at", desc=True)
             .limit(1)
             .execute()
        )
        rows = r.data or []
        if rows:
            last_scored_at = rows[0].get("scored_at")
    except Exception:  # noqa: BLE001
        pass

    try:
        r = (
            c.table("user_job_matches")
             .select("score")
             .eq("user_id", user_id)
             .not_.is_("score", "null")
             .order("score", desc=True)
             .limit(5)
             .execute()
        )
        scores = [row["score"] for row in (r.data or []) if row.get("score") is not None]
        if scores:
            avg_top5 = round(sum(scores) / len(scores), 1)
    except Exception:  # noqa: BLE001
        pass

    return {
        "total": total,
        "top_matches": top_matches,
        "new_1h": new_1h,
        "new_24h": new_24h,
        "new_7d": new_7d,
        "unseen": unseen,
        "applied": applied,
        "last_scored_at": last_scored_at,
        "avg_top5": avg_top5,
    }


def list_top_matches_preview(
    user_id: str, *, limit: int = 3, use_service: bool = False
) -> list[dict]:
    """Top N matches (score DESC) for the landing page preview. RLS-gated."""
    q = (
        _client(use_service)
        .table("user_matches_enriched")
        .select("job_id, title, company, location, score, scored_at, status, url, site")
        .eq("user_id", user_id)
        .not_.is_("score", "null")
        .order("score", desc=True)
        .order("scored_at", desc=True)
        .limit(limit)
    )
    return list((q.execute().data) or [])


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------
def cleanup_stale_jobs(days: int = 60) -> int:
    """Delete `jobs` rows not seen for `days` days. service_role only.

    Matches are cascade-deleted via FK ON DELETE CASCADE.
    Returns the number of rows deleted.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    res = (
        get_service_client()
        .table("jobs")
        .delete()
        .lt("last_seen", cutoff)
        .execute()
    )
    return len(res.data or [])
