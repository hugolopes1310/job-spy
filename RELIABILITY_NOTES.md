# Reliability pass — what changed

Companion to `HANDOFF.md`. Captures the second reliability pass landed in
this session, plus the two non-code items still on Hugo's plate.

## Code changes shipped

### 1. Structured logging (`app/lib/klog.py`)

`log("event.name", level="info", **fields)` and `bind(**ctx).info(...)` for
context-carrying loggers. Output is grep-friendly by default (`event=… key=val`)
and switches to one-line JSON when `LOG_AS_JSON=1`. Used by `jobs_store`,
`scorer`, `scraper/run`, `auth`. Tests in `app/lib/test_klog.py`.

> Why `klog`? `logging.py` collides with the stdlib when a script puts
> `app/lib/` on `sys.path[0]` (which `python app/lib/test_*.py` does).

### 2. DB retry helper (`jobs_store._with_retry`)

Exponential backoff on transient errors (5xx, 408, common timeout/reset
strings). Wraps `update_match`. 4xx and other permanent errors bubble up
immediately. Tests in `app/lib/test_jobs_store_retry.py`.

### 3. Scorer: robust JSON parsing + quota tracking

`_parse_llm_json` runs strict JSON.loads, then a fence-strip retry, then
brace-balanced extraction. When all three fail it logs `scorer.parse_failed`
and returns None instead of raising.

`make_parse_failed_analysis(reason)` returns a stable shape so the UI shows
"Analyse indisponible" (with `_error: "llm_unavailable"` from the scraper)
instead of `analysis = NULL`.

Quota state is exposed via `llm_quota_state()` → `{groq_tpd, gemini_quota,
all_exhausted}`. Both providers flip flags on daily-quota 429s; the scraper
prints the state at run end.

Score and sub-scores now clamp to `[0, 10]`; non-int values fall back to 0
(global) or `-1` (sub-scores, the "not provided" sentinel).

Tests in `app/lib/test_scorer_parse.py`.

### 4. Scraper checkpoints + structured logs (`app/scraper/run.py`)

Every query and every custom source is in its own try/except, so a single
site failing doesn't lose the rest of the user's run. New per-user counters:
`failed_upsert`, `failed_insert`, `failed_queries`. Per-row LLM failures now
write a `parse_failed` analysis instead of a silent NULL. End-of-run prints
the LLM quota state when either provider is exhausted.

### 5. CV upload — graceful errors (`app/lib/cv_parser.py`)

New error hierarchy: `CVParseError` (UI-safe) with subclasses
`UnsupportedFormatError`, `FileTooLargeError`, `EmptyFileError`,
`CorruptedFileError`, `EncryptedPDFError`, `ImageOnlyPDFError`. Plus
`CVParseConfigError` for missing pypdf/python-docx (deploy-time issue).

Limits: 10 MiB cap, ≥80 chars required for PDF text (anything less is
flagged image-only). `.doc` rejects fast with a clear "convert to .docx"
message rather than the cryptic `PackageNotFoundError`.

`1_onboarding.py` shows specific messages per error class (lock icon for
encrypted, frame icon for image-only, etc.).

Tests in `app/lib/test_cv_parser.py` (uses real pypdf to build encrypted
and blank PDFs in-memory).

### 6. Auth — session expiry banner

`get_current_user()` now sets a one-shot session-state flag
(`auth.SESSION_EXPIRED_KEY`) when a refresh fails. The login screen
(`streamlit_app.py`) and the unauth-redirect page (`page_setup.py`) read
the message via `consume_session_expired_message()` and show
"Ta session a expiré. Reconnecte-toi pour continuer." once.

Tests in `app/lib/test_auth_session_expired.py`.

### 7. CI workflow (`.github/workflows/tests.yml`)

Discovers `app/**/test_*.py` via find-glob and runs each as a Python
script. No pytest required — the test files already use a
`if __name__ == "__main__"` driver pattern. Triggers on push to main,
PRs, and manual dispatch. 10-minute timeout, concurrency cancels older
runs on the same branch.

Currently 8 test files / ~60 assertions; all green locally.

## Files touched

- new: `app/lib/klog.py`, `app/lib/test_klog.py`
- new: `app/lib/test_jobs_store_retry.py`, `app/lib/test_scorer_parse.py`
- new: `app/lib/test_cv_parser.py`, `app/lib/test_auth_session_expired.py`
- new: `.github/workflows/tests.yml`
- new: `RELIABILITY_NOTES.md`
- modified: `app/lib/jobs_store.py` (retry helper, klog wiring)
- modified: `app/lib/scorer.py` (parser, quota state, score clamp)
- modified: `app/lib/cv_parser.py` (error hierarchy, limits, encrypted/image detection)
- modified: `app/lib/auth.py` (session-expired flag)
- modified: `app/lib/page_setup.py` (banner on unauth redirect)
- modified: `app/streamlit_app.py` (banner on login screen)
- modified: `app/pages/1_onboarding.py` (typed error handling)
- modified: `app/scraper/run.py` (per-row try/except, structured logs, quota print)

## Still on Hugo's plate (no code change needed)

### A — Supabase Redirect URLs (was brief #15)

After the `kairo.streamlit.app` rename, log into Supabase → Authentication
→ URL Configuration. Add `https://kairo.streamlit.app/**` to **Redirect
URLs** (and update **Site URL** to `https://kairo.streamlit.app`).
Otherwise: OTP magic links land on the old domain and 404.

Quick sanity check after the change: send yourself an OTP, click the link
from the email, confirm you land on the dashboard. If it bounces to login,
the redirect URL still has the old domain.

### B — Rescore verify (was brief #23)

After the rescore CLI run, confirm Pictet (and other dream-company offers)
surface with score ≥ 7 in the dashboard:

```bash
cd /Users/hugo/PycharmProjects/PythonProject/job_spy
python -m app.scraper.rescore --user lopeshugo1310@gmail.com --dry-run
# eyeball the diff, then for real:
python -m app.scraper.rescore --user lopeshugo1310@gmail.com
```

Then open the dashboard with the score filter at 7+ and check that the
Pictet entries appear.

## Running the tests locally

```bash
cd /Users/hugo/PycharmProjects/PythonProject/job_spy
for f in $(find app -type f -name 'test_*.py' | sort); do
  echo "=== $f ==="
  PYTHONPATH=. python "$f" || break
done
```

Or rely on the CI: push to a branch, open a PR, watch the **Tests** check.
