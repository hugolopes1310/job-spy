"""Lazy-initialized Supabase clients.

Two clients are exposed:
    - `get_anon_client()`     : uses the anon key + RLS (for user-scoped reads/writes).
    - `get_service_client()`  : uses the service_role key (admin operations,
                                bypasses RLS — use sparingly).

Credentials are read from `st.secrets` first (Streamlit Cloud / .streamlit/secrets.toml),
then from environment variables as a fallback (handy for the scraper & CI).
"""
from __future__ import annotations

import os
from functools import lru_cache

try:
    from supabase import Client, create_client  # type: ignore
except ImportError as e:  # pragma: no cover
    raise RuntimeError(
        "supabase-py missing — `pip install supabase` (it's in requirements.txt)"
    ) from e


def _secret(key: str) -> str | None:
    """Look up a secret in st.secrets first, then in env vars."""
    # st.secrets is only available inside a Streamlit runtime — avoid hard import
    # so this module can also be used by the scraper / CLI tools.
    try:
        import streamlit as st  # type: ignore

        if key in st.secrets:
            return str(st.secrets[key])
    except (ImportError, FileNotFoundError, RuntimeError):
        pass
    return os.environ.get(key)


def _require(key: str) -> str:
    val = _secret(key)
    if not val:
        raise RuntimeError(
            f"Missing required secret: {key}. "
            f"Set it in .streamlit/secrets.toml or as an env var."
        )
    return val


@lru_cache(maxsize=1)
def get_anon_client() -> Client:
    """Client with anon key — RLS-enforced. Use for end-user code paths."""
    url = _require("SUPABASE_URL")
    key = _require("SUPABASE_ANON_KEY")
    return create_client(url, key)


@lru_cache(maxsize=1)
def get_service_client() -> Client:
    """Client with service_role key — bypasses RLS. Use for admin / scraper jobs.

    NEVER expose this client's results directly to end-user code.
    """
    url = _require("SUPABASE_URL")
    key = _require("SUPABASE_SERVICE_KEY")
    return create_client(url, key)


def reset_clients() -> None:
    """Clear the lru caches — useful in tests or after a credential change."""
    get_anon_client.cache_clear()
    get_service_client.cache_clear()
