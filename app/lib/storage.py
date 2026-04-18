"""Storage layer — Supabase backend (Phase 2).

The public API is identical to the Phase 1 local-JSON version — same function
names, same return shapes — so pages written against Phase 1 keep working.

Two call patterns:
    - End-user code (Streamlit page): pass `use_service=False` (the default)
      so RLS enforces `auth.uid() = user_id`. Requires an authenticated user.
    - Admin / scraper code: pass `use_service=True` to bypass RLS.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from app.lib.auth import get_user_scoped_client
from app.lib.supabase_client import get_service_client

UserStatus = Literal["pending", "approved", "revoked"]


# ---------------------------------------------------------------------------
# Client selector
# ---------------------------------------------------------------------------
def _client(use_service: bool = False):
    """Pick the right Supabase client for the call site."""
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
# Profile
# ---------------------------------------------------------------------------
def get_profile(email: str, *, use_service: bool = False) -> dict[str, Any] | None:
    """Fetch a profile by email, or None if missing.

    Note: with `use_service=False`, RLS only lets a non-admin see their own
    profile. An admin can see any row.
    """
    res = (
        _client(use_service)
        .table("profiles")
        .select("*")
        .eq("email", email.strip().lower())
        .limit(1)
        .execute()
    )
    data = res.data or []
    return data[0] if data else None


def get_profile_by_id(user_id: str, *, use_service: bool = False) -> dict[str, Any] | None:
    res = (
        _client(use_service)
        .table("profiles")
        .select("*")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    data = res.data or []
    return data[0] if data else None


def update_full_name(user_id: str, full_name: str) -> dict[str, Any]:
    """A user setting their own display name (RLS-gated)."""
    res = (
        _client(use_service=False)
        .table("profiles")
        .update({"full_name": full_name.strip()})
        .eq("user_id", user_id)
        .execute()
    )
    data = res.data or []
    if not data:
        raise RuntimeError("Update failed (empty response)")
    return data[0]


# --- admin-only operations ---
def list_profiles(
    status: UserStatus | None = None, *, use_service: bool = False
) -> list[dict[str, Any]]:
    q = _client(use_service).table("profiles").select("*")
    if status:
        q = q.eq("status", status)
    res = q.order("requested_at", desc=False).execute()
    return list(res.data or [])


def update_status(
    email: str, new_status: UserStatus, *, use_service: bool = False
) -> dict[str, Any]:
    patch: dict[str, Any] = {"status": new_status}
    if new_status == "approved":
        patch["approved_at"] = _now()
        patch["revoked_at"] = None
    elif new_status == "revoked":
        patch["revoked_at"] = _now()
    res = (
        _client(use_service)
        .table("profiles")
        .update(patch)
        .eq("email", email.strip().lower())
        .execute()
    )
    data = res.data or []
    if not data:
        raise RuntimeError(f"No profile found for {email!r}")
    return data[0]


def set_admin(email: str, is_admin: bool = True, *, use_service: bool = True) -> dict[str, Any]:
    """Promote/demote admin. Defaults to service_role because bootstrapping the
    first admin happens before any user has admin rights."""
    patch: dict[str, Any] = {"is_admin": bool(is_admin)}
    if is_admin:
        # An admin has to be approved by definition.
        patch["status"] = "approved"
        patch["approved_at"] = _now()
    res = (
        _client(use_service)
        .table("profiles")
        .update(patch)
        .eq("email", email.strip().lower())
        .execute()
    )
    data = res.data or []
    if not data:
        raise RuntimeError(f"No profile found for {email!r}")
    return data[0]


def list_active_user_emails(*, use_service: bool = True) -> list[str]:
    """Emails of approved users — for the scraper to loop over.

    Defaults to service_role: the scraper runs in CI without a user session.
    """
    res = (
        _client(use_service)
        .table("profiles")
        .select("email")
        .eq("status", "approved")
        .execute()
    )
    return [r["email"] for r in (res.data or [])]


# ---------------------------------------------------------------------------
# User configs
# ---------------------------------------------------------------------------
def save_user_config(user_id: str, config: dict[str, Any]) -> None:
    """Upsert the user's config (one row per user_id)."""
    _client(use_service=False).table("user_configs").upsert(
        {"user_id": user_id, "config": config},
        on_conflict="user_id",
    ).execute()


def load_user_config(user_id: str, *, use_service: bool = False) -> dict[str, Any] | None:
    res = (
        _client(use_service)
        .table("user_configs")
        .select("config")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    data = res.data or []
    return data[0]["config"] if data else None


def save_cv_text(user_id: str, text: str) -> None:
    """Update cv_text on an existing user_configs row.

    Must be called after save_user_config, which creates the row with the
    required `config` column. We use UPDATE (not UPSERT) because an UPSERT
    without `config` would try an INSERT with config=NULL and violate NOT NULL.
    """
    (
        _client(use_service=False)
        .table("user_configs")
        .update({"cv_text": text})
        .eq("user_id", user_id)
        .execute()
    )


def save_cover_letter_text(user_id: str, text: str) -> None:
    """Update cover_letter_text on an existing user_configs row. See save_cv_text."""
    (
        _client(use_service=False)
        .table("user_configs")
        .update({"cover_letter_text": text})
        .eq("user_id", user_id)
        .execute()
    )


def load_cv_text(user_id: str, *, use_service: bool = False) -> str:
    res = (
        _client(use_service)
        .table("user_configs")
        .select("cv_text")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    data = res.data or []
    return data[0].get("cv_text") or "" if data else ""


def load_cover_letter_text(user_id: str, *, use_service: bool = False) -> str:
    res = (
        _client(use_service)
        .table("user_configs")
        .select("cover_letter_text")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    data = res.data or []
    return data[0].get("cover_letter_text") or "" if data else ""


# ---------------------------------------------------------------------------
# Scraper helper — all approved users + their configs, in one query
# ---------------------------------------------------------------------------
def list_active_user_configs() -> list[dict[str, Any]]:
    """Used by the Phase 3 scraper — always service_role."""
    res = (
        get_service_client()
        .table("active_user_configs")
        .select("user_id, email, full_name, config, cv_text, updated_at")
        .execute()
    )
    return list(res.data or [])


# ---------------------------------------------------------------------------
# CLI — bootstrap the first admin without using the Streamlit app
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print(
            "usage:\n"
            "  python -m app.lib.storage list\n"
            "  python -m app.lib.storage admin <email>\n"
            "  python -m app.lib.storage approve <email>\n"
            "  python -m app.lib.storage revoke <email>\n"
            "All commands use the service_role key (admin bootstrap)."
        )
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "list":
        for prof in list_profiles(use_service=True):
            mark = "★" if prof.get("is_admin") else " "
            print(f"  [{prof['status']:>8}] {mark} {prof['email']} ({prof.get('full_name') or '?'})")
    elif cmd == "admin" and len(sys.argv) == 3:
        p = set_admin(sys.argv[2], True, use_service=True)
        print(f"OK — {p['email']} is now admin (status={p['status']})")
    elif cmd == "approve" and len(sys.argv) == 3:
        p = update_status(sys.argv[2], "approved", use_service=True)
        print(f"OK — {p['email']} is now approved")
    elif cmd == "revoke" and len(sys.argv) == 3:
        p = update_status(sys.argv[2], "revoked", use_service=True)
        print(f"OK — {p['email']} is now revoked")
    else:
        print("unknown command")
        sys.exit(1)
