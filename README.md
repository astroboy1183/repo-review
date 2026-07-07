# repo-review

Daily code review of my own GitHub repos → Telegram, ~19:37 IST via
GitHub Actions.

Two parts every evening:

- **Today's changes** — every commit pushed to any repo in the account in
  the last 24h, reviewed from the actual diffs: real bugs first, then
  risky patterns, then better idioms.
- **Spotlight** — one repo per day (rotating by day of year) gets a full
  read-through: dead code, naming, error handling, structure. Every repo
  comes up every couple of weeks.
- **Portfolio** (Sundays) — keep/finish/archive/delete advice for the
  whole account from each repo's metadata: what looks stalled but worth
  completing, what's a stale experiment or duplicate, what's healthy.
  On demand any day:
  `gh workflow run repo-review.yml -R astroboy1183/repo-review -f curate=1`

Always sends — a quiet coding day still gets the spotlight.

Part of the personal-agents fleet (`[gather] → [summarize] → [Telegram]`):
own repo, own schedule, fails alone. Delivery via the morning bot.

- Schedule: `.github/workflows/repo-review.yml`
  (`7 14 * * *` UTC = 19:37 IST; backup 20:37 with dedupe guard)
- Run now: `gh workflow run repo-review.yml -R astroboy1183/repo-review`
- Secrets (Settings → Secrets → Actions): `ANTHROPIC_API_KEY`,
  `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `REPOS_READ_TOKEN`

## REPOS_READ_TOKEN

The runner's default token can only see this repo, so listing and reading
the whole account needs a personal access token:

1. github.com → Settings → Developer settings → Fine-grained tokens →
   Generate new token
2. Repository access: **All repositories**. Permissions → Repository →
   **Contents: Read-only** (Metadata comes along automatically).
3. Save it as the secret:
   `gh secret set REPOS_READ_TOKEN -R astroboy1183/repo-review`

Until the secret exists, scheduled runs skip politely instead of failing
red. After adding it, trigger the first review with the "Run now" command
above.
