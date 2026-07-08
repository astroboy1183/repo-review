# repo-review

Daily code review of my own GitHub repos → Telegram, ~19:37 IST via
GitHub Actions. One agent, one task, one bot.

Three parts:

- **Today's changes** — every commit pushed to any repo in the account
  in the last 24h, reviewed from the actual diffs. Findings are tagged
  `[BUG]` / `[RISK]` / `[STYLE]`, bugs first, every one with a file
  reference and its concrete fix.
- **Spotlight** — one repo per day (rotating) gets a full read-through:
  dead code, naming, error handling, structure.
- **Portfolio** (Sundays) — keep/finish/archive/delete advice for the
  whole account from repo metadata.

The agent has **memory**: it stores its findings after every run
(`state/findings.json`, committed back to this repo) and reads them the
next day — so it acknowledges fixes, escalates ignored problems, and a
repo's second spotlight opens with follow-through on the first instead
of repeating it.

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
  24h cutoff (newest first, capped at `MAX_COMMITS_PER_REPO = 20`). If
  any exist, it fetches ONE combined diff via the compare API —
  parent-of-oldest…head — rather than a diff per commit. A repo born
  inside the window has no parent, so it falls back to diffing the head
  commit itself. When a day overflows 20 commits the review only covers
  the newest 20, so the repo is tagged `(newest 20 commits only)` and
  the model is told to echo that caveat — a heavy day never looks fully
  reviewed when it isn't.
- **`trim_diff(diff)`** — over-budget diffs (`MAX_DIFF_CHARS = 8000`)
  are cut at *file boundaries*, code files kept first, docs/config
  dropped first, with an explicit "N file diffs omitted" note — a blind
  character cut would spend the budget on lockfile churn and stop
  mid-hunk. A single file diff bigger than the whole budget falls back
  to a hard cut with a note.
- **`dedupe_changed(changed)`** — fleet-wide syncs push the *same* diff
  to many repos; those collapse (by diff hash) into one review entry
  labelled "repo-a (same diff in 11 more: …)" instead of twelve
  repetitive reviews.
- **`spotlight_source(repo)`** — full read of the day's spotlight repo:
  the git tree API (recursive) lists every file; source-looking ones
  (by extension) are fetched raw until the budget runs out
  (12 files / 30k chars). Real code (`CODE_EXT`: `.py/.sql/.sh/.js/.ts`)
  is fetched before docs and config (`DOC_EXT`: `.md/.yml/.yaml/.toml`),
  so the budget is spent reviewing code rather than READMEs and YAML;
  `.json`/`.lock` files never qualify. One unreadable blob is skipped,
  not fatal.
- **Rotation** — the repo list is sorted by name into a fixed order, then
  `day_of_year % len(repos)` picks the spotlight, so every repo genuinely
  comes up once every `len(repos)` days. (`my_repos()` sorts by push date
  for the diff scan; the spotlight deliberately ignores that so the pick
  doesn't jump around with activity.) A `workflow_dispatch` run can force
  the target with the `spotlight` input (`REVIEW_SPOTLIGHT` env).
- **`repo_inventory(repos)`** — one metadata line per repo (language,
  description, created, last push, size, open issues) — the input for
  portfolio advice; costs zero extra API calls.
- **Memory** (`load_state` / `save_state` / `split_state` /
  `recent_findings`) — each model reply ends with a `===STATE===` line
  and a small JSON summary of its own findings, which the code splits
  off (the Telegram message never includes it) and stores in
  `state/findings.json`: the last 14 days of per-repo findings plus the
  latest spotlight notes per repo. Next day, the changed repos' history
  goes into the diff prompt and the spotlight repo's prior notes go into
  the deep prompt with explicit follow-through instructions. A malformed
  tail costs the memory, never the message; the workflow commits the
  state file back to this repo after each run (best-effort).
- **Two model calls, two tiers** — the day's diffs run on a cheap model
  (default `claude-haiku-4-5`; skipped entirely on quiet days), the
  spotlight + portfolio deep read on a stronger one (default
  `claude-sonnet-5`). Override either via the `REVIEW_MODEL_DAILY` /
  `REVIEW_MODEL_DEEP` secrets.
- **`build_changes_prompt` / `build_deep_prompt`** — pin the output
  shapes: 🔎 TODAY'S CHANGES (2–4 tagged bullets per changed repo, at
  most one `[STYLE]` each, every bullet carrying its fix, or exactly
  "no significant findings"), 💡 SPOTLIGHT (follow-through on prior
  notes first, then 4–6 tagged bullets + the single change to make
  first), and — only when curating — 🗂 PORTFOLIO (FINISH /
  ARCHIVE-DELETE / KEEP, with "DELETE only for the truly disposable").
- **`main()`** — per-repo `try/except` feeds a `failed` list that
  becomes a "⚠️ Could not check" footer instead of a dead run. Curation
  triggers on Sundays or `REVIEW_CURATE=1`. Memory is saved *after* the
  send, so a state failure never costs the review itself.
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

- Tests run in CI on every push (`.github/workflows/tests.yml`).

## Ops

- Schedule: `.github/workflows/repo-review.yml` (`7 14 * * *` UTC = 19:37 IST; backup 20:37)
- Run now: `gh workflow run repo-review.yml -R astroboy1183/repo-review`
- Force portfolio advice: add `-f curate=1`
- Force a spotlight target: add `-f spotlight=<repo-name>`
- Secrets (Actions): `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`,
  `TELEGRAM_CHAT_ID`, `REPOS_READ_TOKEN`; optional `REVIEW_MODEL_DAILY`
  / `REVIEW_MODEL_DEEP` to change models (e.g. set `REVIEW_MODEL_DEEP`
  to `claude-haiku-4-5` to make the deep read cheap too)
- Review memory lives in `state/findings.json` — delete the file (and
  push) to wipe the agent's memory

### REPOS_READ_TOKEN

1. github.com → Settings → Developer settings → Fine-grained tokens →
   Generate new token
2. Repository access: **All repositories**. Permissions → Repository →
   **Contents: Read-only** (Metadata comes along automatically).
3. `gh secret set REPOS_READ_TOKEN -R astroboy1183/repo-review`
