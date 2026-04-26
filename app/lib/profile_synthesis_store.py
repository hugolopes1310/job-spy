"""CRUD for `profile_syntheses` and `profile_synthesis_proposals`.

Same dual-client pattern as `storage.py` / `jobs_store.py`:
  - `use_service=True`  → service_role, bypasses RLS (scraper, admin, LLM
                          callers running outside a Streamlit session).
  - `use_service=False` → anon + JWT, RLS-enforced (page reads).

A profile synthesis row is **immutable** once created. Editing a profile
means inserting a new row (status='draft' → 'active') and archiving the
previous active one. The atomic flip lives in the Postgres function
`activate_profile_synthesis(uuid)` declared in
`supabase/phase4_profile_synthesis.sql`.
"""
from __future__ import annotations

from typing import Any

from app.lib.auth import get_user_scoped_client
from app.lib.jobs_store import _now, _with_retry
from app.lib.klog import log
from app.lib.supabase_client import get_service_client


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


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def load_active_synthesis(
    user_id: str, *, use_service: bool = False
) -> dict[str, Any] | None:
    """Return the active synthesis row for a user, or None if there is no
    active version yet (new user / freshly reset).

    The structured object lives at `row["synthesis"]`. The other columns
    (id, version, activated_at, llm_model, prompt_version) are useful for
    callers that need to write a foreign key (insert_match) or display
    metadata in the UI.
    """
    def _do():
        return (
            _client(use_service)
            .table("profile_syntheses")
            .select(
                "id, user_id, version, status, synthesis, source_signals, "
                "llm_model, prompt_version, created_at, activated_at"
            )
            .eq("user_id", user_id)
            .eq("status", "active")
            .limit(1)
            .execute()
        )

    res = _with_retry(
        _do,
        op="profile_synthesis.load_active",
        extra_ctx={"user_id": user_id},
    )
    data = res.data or []
    return data[0] if data else None


def load_synthesis_by_id(
    synthesis_id: str, *, use_service: bool = False
) -> dict[str, Any] | None:
    res = (
        _client(use_service)
        .table("profile_syntheses")
        .select("*")
        .eq("id", synthesis_id)
        .limit(1)
        .execute()
    )
    data = res.data or []
    return data[0] if data else None


def list_synthesis_versions(
    user_id: str, *, use_service: bool = False
) -> list[dict[str, Any]]:
    """All versions for a user, newest first. Useful for the history view
    + admin debugging.
    """
    res = (
        _client(use_service)
        .table("profile_syntheses")
        .select(
            "id, version, status, llm_model, prompt_version, "
            "created_at, activated_at, archived_at"
        )
        .eq("user_id", user_id)
        .order("version", desc=True)
        .execute()
    )
    return list(res.data or [])


def _get_next_version(user_id: str, *, use_service: bool = True) -> int:
    """Return the next version number for a user. Starts at 1."""
    res = (
        _client(use_service)
        .table("profile_syntheses")
        .select("version")
        .eq("user_id", user_id)
        .order("version", desc=True)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    return (rows[0]["version"] + 1) if rows else 1


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
def insert_synthesis_draft(
    user_id: str,
    synthesis: dict[str, Any],
    *,
    source_signals: dict[str, Any] | None = None,
    llm_model: str | None = None,
    prompt_version: str | None = None,
    use_service: bool = True,
) -> str:
    """Insert a new synthesis row in 'draft' state. Returns the row id.

    The caller is expected to call `activate_synthesis(id)` once it has
    validated the draft (e.g. once the LLM call succeeded and the JSON
    schema check passed). Drafts that never get activated stay as audit
    trail / can be inspected by admins.
    """
    version = _get_next_version(user_id, use_service=use_service)
    row = {
        "user_id": user_id,
        "version": version,
        "status": "draft",
        "synthesis": synthesis,
        "source_signals": source_signals or {},
        "llm_model": llm_model,
        "prompt_version": prompt_version,
        "created_at": _now(),
    }

    def _do():
        return (
            _client(use_service)
            .table("profile_syntheses")
            .insert(row)
            .execute()
        )

    res = _with_retry(
        _do,
        op="profile_synthesis.insert_draft",
        extra_ctx={"user_id": user_id, "version": version},
    )
    data = res.data or []
    if not data:
        raise RuntimeError("insert_synthesis_draft returned no row")
    new_id = data[0]["id"]
    log(
        "profile_synthesis.draft_inserted",
        user_id=user_id,
        synthesis_id=new_id,
        version=version,
        llm_model=llm_model,
    )
    return new_id


def activate_synthesis(synthesis_id: str, *, use_service: bool = True) -> None:
    """Atomically flip a draft → active and archive the previous active one.

    Wraps the Postgres function `public.activate_profile_synthesis(uuid)`,
    which executes both UPDATE statements in the same transaction. If the
    function raises, the database stays in its previous consistent state.
    """
    def _do():
        return (
            _client(use_service)
            .rpc("activate_profile_synthesis", {"p_synthesis_id": synthesis_id})
            .execute()
        )

    _with_retry(
        _do,
        op="profile_synthesis.activate",
        extra_ctx={"synthesis_id": synthesis_id},
    )
    log("profile_synthesis.activated", synthesis_id=synthesis_id)


def archive_active_synthesis(user_id: str, *, use_service: bool = False) -> None:
    """Reset doux : archive la synthèse active de l'user, sans en activer
    de nouvelle. Utilisé par le bouton "Reset profil" dans Mon Profil.

    Après cet appel, l'user n'a plus de synthèse active. Le caller doit
    enchaîner avec `synthesize_profile()` + `activate_synthesis()` pour
    recréer une version 'active'.
    """
    def _do():
        return (
            _client(use_service)
            .rpc("archive_active_profile_synthesis", {"p_user_id": user_id})
            .execute()
        )

    _with_retry(
        _do,
        op="profile_synthesis.archive_active",
        extra_ctx={"user_id": user_id},
    )
    log("profile_synthesis.archived_active", user_id=user_id)


# ---------------------------------------------------------------------------
# Proposals (loop continu — diff suggéré par le job nightly)
# ---------------------------------------------------------------------------
def load_pending_proposal(
    user_id: str, *, use_service: bool = False
) -> dict[str, Any] | None:
    """Return the user's currently pending proposal, or None. The unique
    partial index `uq_proposal_pending_per_user` guarantees at most one.
    """
    res = (
        _client(use_service)
        .table("profile_synthesis_proposals")
        .select("*")
        .eq("user_id", user_id)
        .eq("status", "pending")
        .limit(1)
        .execute()
    )
    data = res.data or []
    return data[0] if data else None


def insert_proposal(
    user_id: str,
    current_synthesis_id: str | None,
    diff: dict[str, Any],
    rationale_fr: str,
    *,
    use_service: bool = True,
) -> str:
    """Insert a pending proposal. Caller must check there isn't one already
    pending — the unique partial index will raise otherwise (treated as an
    expected no-op by the nightly job).
    """
    row = {
        "user_id": user_id,
        "current_synthesis_id": current_synthesis_id,
        "diff": diff,
        "rationale_fr": rationale_fr,
        "status": "pending",
        "created_at": _now(),
    }

    def _do():
        return (
            _client(use_service)
            .table("profile_synthesis_proposals")
            .insert(row)
            .execute()
        )

    res = _with_retry(
        _do,
        op="profile_synthesis.insert_proposal",
        extra_ctx={"user_id": user_id},
    )
    data = res.data or []
    if not data:
        raise RuntimeError("insert_proposal returned no row")
    new_id = data[0]["id"]
    log(
        "profile_synthesis.proposal_inserted",
        user_id=user_id,
        proposal_id=new_id,
    )
    return new_id


def resolve_proposal(
    proposal_id: str,
    status: str,  # 'accepted' | 'dismissed'
    *,
    use_service: bool = False,
) -> None:
    """Mark a proposal as accepted / dismissed."""
    if status not in ("accepted", "dismissed"):
        raise ValueError(f"resolve_proposal status must be accepted|dismissed, got {status!r}")

    def _do():
        return (
            _client(use_service)
            .table("profile_synthesis_proposals")
            .update({"status": status, "resolved_at": _now()})
            .eq("id", proposal_id)
            .execute()
        )

    _with_retry(
        _do,
        op="profile_synthesis.resolve_proposal",
        extra_ctx={"proposal_id": proposal_id, "status": status},
    )
    log(
        "profile_synthesis.proposal_resolved",
        proposal_id=proposal_id,
        status=status,
    )


# ---------------------------------------------------------------------------
# Scraper helper — active synthesis for all approved users in one query.
# Mirrors `list_active_user_configs` in storage.py.
# ---------------------------------------------------------------------------
def list_active_syntheses(*, use_service: bool = True) -> list[dict[str, Any]]:
    """Used by the scraper / nightly proposer — always service_role."""
    res = (
        _client(use_service)
        .table("active_profile_synthesis")
        .select("user_id, synthesis_id, version, synthesis, activated_at, llm_model, prompt_version")
        .execute()
    )
    return list(res.data or [])
