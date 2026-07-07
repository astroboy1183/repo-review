# repo-review

Daily code review of my own GitHub repos → Telegram, ~19:37 IST via
GitHub Actions. One agent, one task, one bot.

Three parts:

- **Today's changes** — every commit pushed to any repo in the account
  in the last 24h, reviewed from the actual diffs: real bugs first, then
  risky patterns, then better idioms.
- **Spotlight** — one repo per day (rotating) gets a full read-through:
  dead code, naming, error handling, structure.
- **Portfolio** (Sundays) — keep/finish/archive/delete advice for the
  whole account from repo metadata.

Always sends — a quiet coding day still gets the spotlight.

## How the code works

`repo_review.py`, in pipeline order:

- **`gh_get(path, accept, **params)`** — the one HTTP helper: GET a
  GitHub API path with the `REPOS_READ_TOKEN` bearer token; raises on
  errors. The `accept` parameter matters — passing
  `application/vnd.github.diff` makes GitHub return a raw unified diff
  instead of JSON.
- **`my_repos()`** — `GET /user/repos` (owner affiliation, first 100,
  sorted by push date), then drops forks and archived repos: only code
  that is actually mine gets reviewed.
- **`day_diff(repo, since)`** — commits on the default branch since the
  24h cutoff (newest first, capped at 20). If any exist, it fetches ONE
  combined diff via the compare API — parent-of-oldest…head — rather
  than a diff per commit. A repo born inside the window has no parent,
  so it falls back to diffing the head commit itself. Diffs are capped
  at `MAX_DIFF_CHARS = 8000` per repo.
- **`spotlight_source(repo)`** — full read of the day's spotlight repo:
  the git tree API (recursive) lists every file; source-looking ones
  (by extension) are fetched raw until the budget runs out
  (12 files / 30k chars). One unreadable blob is skipped, not fatal.
- **Rotation** — `day_of_year % len(repos)` picks the spotlight, so
  every repo comes up every `len(repos)` days with no state to store.
- **`repo_inventory(repos)`** — one metadata line per repo (language,
  description, created, last push, size, open issues) — the input for
  portfolio advice; costs zero extra API calls.
- **`build_prompt(...)`** — assembles the inputs and pins the output
  shape: 🔎 TODAY'S CHANGES (2–4 bullets per changed repo, bugs first,
  file/function named), 💡 SPOTLIGHT (4–6 bullets + the single change
  to make first), and — only when curating — 🗂 PORTFOLIO
  (FINISH / ARCHIVE-DELETE / KEEP, with "DELETE only for the truly
  disposable").
- **`main()`** — per-repo `try/except` feeds a `failed` list that
  becomes a "⚠️ Could not check" footer instead of a dead run. Curation
  triggers on Sundays or `REVIEW_CURATE=1`. One model call, one send.
- **`agentlib.py`** (vendored) — `ask_llm()` one-shot model call;
  `send_telegram()` chunked sends.

## Design notes

- The runner's default token only sees this repo, so account-wide
  reading needs the `REPOS_READ_TOKEN` PAT (below). The workflow
  fail-softs (skips green) while the secret is missing.
- Portfolio advice is weekly, not daily — "delete repo X" doesn't change
  overnight, and daily repetition teaches you to skip the message.
- Two crons + dedupe guard: backup at 20:37 IST delivers only if the
  19:37 primary was dropped or failed.

## Ops

- Schedule: `.github/workflows/repo-review.yml` (`7 14 * * *` UTC = 19:37 IST; backup 20:37)
- Run now: `gh workflow run repo-review.yml -R astroboy1183/repo-review`
- Force portfolio advice: add `-f curate=1`
- Secrets (Actions): `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`,
  `TELEGRAM_CHAT_ID`, `REPOS_READ_TOKEN`

### REPOS_READ_TOKEN

1. github.com → Settings → Developer settings → Fine-grained tokens →
   Generate new token
2. Repository access: **All repositories**. Permissions → Repository →
   **Contents: Read-only** (Metadata comes along automatically).
3. `gh secret set REPOS_READ_TOKEN -R astroboy1183/repo-review`
