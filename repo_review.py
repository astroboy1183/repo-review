#!/usr/bin/env python3
"""Repo review agent.

One Telegram message every morning (6:00 IST via GitHub Actions) reviewing
my GitHub account:

  - every commit pushed to any of my repos in the last 24h, reviewed from
    the actual diffs — findings tagged [BUG]/[RISK]/[STYLE], bugs first,
    with a nudge when commit subjects are uninformative
  - a rotating SPOTLIGHT: one repo per day gets a full read-through
    (dead code, naming, error handling, structure), so every repo gets a
    deep review every couple of weeks — plus a deterministic 🏅 hygiene
    score (README/license/tests/CI/…) and 📌 debt markers (TODO/FIXME)
  - 🔴 CI HEALTH: any repo whose latest Actions run failed is called out
    with a link — a silently red repo must not survive a day
  - on Sundays (or with REVIEW_CURATE=1): a PORTFOLIO section — which
    repos to keep, which to finish, which to archive or delete — and a
    🗓 WEEK IN CODE rollup (commits, busiest day, open PRs, extra
    branches) from the activity memory
  - 📈 RISING REPOS: new GitHub repos that crossed a star threshold this
    week (search API, deterministic) — what the ecosystem is excited
    about, each repo shown exactly once (state-remembered)
  - one small suggestion for tomorrow

The agent keeps MEMORY (state/findings.json, committed back to this repo
by the workflow): yesterday's findings are fed into today's prompt so the
review acknowledges fixes, escalates ignored problems, and never repeats
a spotlight verbatim when a repo's turn comes around again.

Two model calls with different tiers: the daily diff pass runs on a cheap
model, the spotlight/portfolio deep read on a stronger one (REVIEW_MODEL_DAILY
/ REVIEW_MODEL_DEEP override either).

Always sends — a quiet coding day still gets the spotlight review.

Same fleet pattern as the other agents: own repo, own schedule, fails alone.
Needs REPOS_READ_TOKEN (read-only PAT) to list and read the account's repos.
"""

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

from agentlib import ask_llm, send_telegram

BASE_DIR = Path(__file__).resolve().parent
IST = ZoneInfo("Asia/Kolkata")
API = "https://api.github.com"

LOOKBACK_HOURS = 24
MAX_COMMITS_PER_REPO = 20  # a busier day than this reviews the newest 20
MAX_DIFF_CHARS = 8000  # per repo; keeps the prompt size sane
MAX_SPOTLIGHT_FILES = 12
MAX_SPOTLIGHT_CHARS = 30000
CURATION_WEEKDAY = 6  # Sunday — keep/finish/delete advice changes slowly
# The deep-read budget (12 files / 30k chars) should be spent on real code,
# not READMEs and config. CODE_EXT is fetched first; DOC_EXT (docs, config,
# lockfile-ish text) only fills whatever budget is left over.
CODE_EXT = (".py", ".sql", ".sh", ".js", ".ts")
DOC_EXT = (".md", ".yml", ".yaml", ".toml")
SOURCE_EXT = CODE_EXT + DOC_EXT

# Rising-repos garnish: new repos gaining stars fast, shown once ever.
RISING_WINDOW_DAYS = 7
RISING_MIN_STARS = 300
RISING_CAP = 3
RISING_KEEP_DAYS = 60  # prune remembered repos after this

# Debt markers surfaced from the spotlight source, and the activity
# memory behind Sunday's 🗓 WEEK IN CODE.
DEBT_RE = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b")
DEBT_CAP = 5           # markers listed; the rest just counted
ACTIVITY_DAYS = 21     # daily commit counts kept this long

# Review memory. The workflow commits this file back to the repo after each
# run, so tomorrow's review knows what today's said.
STATE_FILE = BASE_DIR / "state" / "findings.json"
STATE_DAYS = 14  # how many days of daily findings to keep
STATE_MARKER = "===STATE==="  # separates the message from its JSON memory tail


def gh_get(path, accept="application/vnd.github+json", **params):
    """One GitHub API GET; raises on HTTP errors."""
    r = requests.get(
        f"{API}{path}",
        params=params,
        headers={
            "Accept": accept,
            "Authorization": f"Bearer {os.environ['REPOS_READ_TOKEN']}",
        },
        timeout=30,
    )
    r.raise_for_status()
    return r


def my_repos():
    """Own, non-fork, non-archived repos — the code that is actually mine.

    One page of 100 is plenty for a personal account."""
    repos = gh_get(
        "/user/repos", affiliation="owner", per_page=100, sort="pushed"
    ).json()
    return [r for r in repos if not r["fork"] and not r["archived"]]


def trim_diff(diff, budget=MAX_DIFF_CHARS):
    """Cut an over-budget diff at file boundaries, source code first.

    A blind [:budget] cut spends the budget on whatever GitHub emitted first
    — often lockfile or README churn — and can stop mid-hunk. Instead, split
    into per-file sections, keep code files first and docs/config after,
    until the budget runs out, and say what was dropped."""
    if len(diff) <= budget:
        return diff
    sections = [s for s in re.split(r"(?=^diff --git )", diff, flags=re.M) if s.strip()]

    def code_first(section):
        head = section.splitlines()[0] if section.splitlines() else ""
        return 0 if head.rsplit(" b/", 1)[-1].endswith(CODE_EXT) else 1

    kept, dropped, used = [], 0, 0
    for s in sorted(sections, key=code_first):
        if used + len(s) <= budget:
            kept.append(s)
            used += len(s)
        else:
            dropped += 1
    if not kept:  # a single file diff bigger than the whole budget
        return diff[:budget] + "\n(… diff truncated for size)"
    out = "".join(kept)
    if dropped:
        out += f"\n(… {dropped} file diffs omitted for size)"
    return out


def day_diff(repo, since):
    """(commit subjects, unified diff, cap note) for one repo's last-24h
    changes, or None if nothing was pushed. The cap note is non-empty only
    when the day overflowed MAX_COMMITS_PER_REPO, so a heavy day is not
    reported as if every commit was reviewed."""
    full = repo["full_name"]
    commits = gh_get(
        f"/repos/{full}/commits",
        sha=repo["default_branch"],
        since=since.isoformat(),
        per_page=MAX_COMMITS_PER_REPO,
    ).json()
    if not commits:
        return None
    note = (
        f" (newest {MAX_COMMITS_PER_REPO} commits only)"
        if len(commits) >= MAX_COMMITS_PER_REPO
        else ""
    )
    subjects = [c["commit"]["message"].splitlines()[0] for c in commits]
    head = commits[0]["sha"]
    parents = commits[-1]["parents"]  # newest first, so [-1] is the oldest
    if parents:
        diff = gh_get(
            f"/repos/{full}/compare/{parents[0]['sha']}...{head}",
            accept="application/vnd.github.diff",
        ).text
    else:  # repo born inside the window: diff the head commit itself
        diff = gh_get(
            f"/repos/{full}/commits/{head}", accept="application/vnd.github.diff"
        ).text
    return subjects, trim_diff(diff), note


def dedupe_changed(changed):
    """Collapse identical diffs pushed to many repos (fleet-wide syncs).

    Reviewing the same diff twelve times reads as twelve sets of findings;
    one review labelled with every repo it applies to is honest and cheaper.
    Grouping is by exact diff hash, so only true clones collapse."""
    groups = {}
    for entry in changed:
        digest = hashlib.sha256(entry[2].encode()).hexdigest()
        groups.setdefault(digest, []).append(entry)
    out = []
    for members in groups.values():
        name, subjects, diff, note = members[0]
        if len(members) > 1:
            others = ", ".join(m[0] for m in members[1:])
            name = f"{name} (same diff in {len(members) - 1} more: {others})"
        out.append((name, subjects, diff, note))
    return out


def repo_tree_paths(repo):
    """Every blob path in the repo — one tree call, shared by the
    spotlight read and the hygiene score."""
    tree = gh_get(
        f"/repos/{repo['full_name']}/git/trees/{repo['default_branch']}",
        recursive=1,
    ).json()
    return [n["path"] for n in tree.get("tree", []) if n["type"] == "blob"]


def spotlight_source(repo, paths):
    """Up to ~30k chars of a repo's source files for the deep read."""
    full = repo["full_name"]
    branch = repo["default_branch"]
    blobs = [p for p in paths if p.endswith(SOURCE_EXT)]
    # Real code before docs/config, so the budget is spent reviewing code
    # rather than READMEs and YAML. Stable sort keeps tree order within a tier.
    blobs.sort(key=lambda p: 0 if p.endswith(CODE_EXT) else 1)
    picked, budget = [], MAX_SPOTLIGHT_CHARS
    for path in blobs:
        if len(picked) >= MAX_SPOTLIGHT_FILES or budget <= 0:
            break
        try:
            text = gh_get(
                f"/repos/{full}/contents/{quote(path)}",
                accept="application/vnd.github.raw",
                ref=branch,
            ).text[:budget]
        except requests.RequestException:
            continue  # one unreadable blob must not sink the spotlight
        picked.append(f"--- {path} ---\n{text}")
        budget -= len(text)
    return "\n\n".join(picked)


def hygiene_score(repo, paths):
    """🏅 Deterministic repo-hygiene checklist — the portfolio-polish
    signal: does this repo present itself well to a visitor?"""
    lower = [p.lower() for p in paths]
    checks = {
        "README": any(p.startswith("readme") for p in lower),
        "description": bool((repo.get("description") or "").strip()),
        "license": any(p.startswith(("license", "licence")) for p in lower),
        "tests": any(
            p.rsplit("/", 1)[-1].startswith("test_") or "/tests/" in f"/{p}"
            for p in lower
        ),
        "CI workflow": any(
            p.startswith(".github/workflows/") and p.endswith((".yml", ".yaml"))
            for p in lower
        ),
        ".gitignore": ".gitignore" in lower,
        "topics": bool(repo.get("topics")),
    }
    missing = [name for name, ok in checks.items() if not ok]
    score = len(checks) - len(missing)
    line = f"🏅 Hygiene: {score}/{len(checks)}"
    if missing:
        line += " — missing: " + ", ".join(missing)
    return line


def debt_markers(spot_src):
    """📌 TODO/FIXME/HACK/XXX markers in the spotlight source, counted per
    file (the source carries '--- path ---' section headers)."""
    counts, current = {}, "?"
    for line in spot_src.splitlines():
        if line.startswith("--- ") and line.endswith(" ---"):
            current = line[4:-4]
        elif DEBT_RE.search(line):
            counts[current] = counts.get(current, 0) + 1
    if not counts:
        return ""
    total = sum(counts.values())
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:DEBT_CAP]
    detail = ", ".join(f"{path}×{n}" if n > 1 else path for path, n in top)
    more = f" +{len(counts) - len(top)} more files" if len(counts) > DEBT_CAP else ""
    return f"📌 Debt markers: {total} TODO/FIXME ({detail}{more})"


def ci_health(repos):
    """🔴 Repos whose LATEST Actions run concluded badly — with links.

    Deterministic, one API call per repo; a repo with no runs (or an API
    hiccup) is skipped, never fatal."""
    lines = []
    for repo in repos:
        try:
            runs = gh_get(
                f"/repos/{repo['full_name']}/actions/runs", per_page=1
            ).json().get("workflow_runs", [])
        except Exception:
            continue
        if not runs:
            continue
        run = runs[0]
        if run.get("conclusion") in ("failure", "timed_out", "startup_failure"):
            lines.append(
                f"• {repo['name']}: {run.get('name', 'workflow')} "
                f"{run['conclusion']} — {run.get('html_url', '')}"
            )
    if not lines:
        return ""
    return "🔴 CI HEALTH — latest run failing:\n" + "\n".join(lines)


def week_in_code(state, repos):
    """🗓 Sunday rollup from the activity memory + a PR/branch sweep.

    Commit counts come from what the daily runs already recorded (zero
    extra API calls); open PRs and extra branches are one call per repo,
    Sundays only."""
    week = sorted(state.get("activity", {}).items())[-7:]
    total = sum(sum(day.values()) for _, day in week)
    lines = []
    if total:
        touched = {name for _, day in week for name in day}
        busiest_date, busiest = max(
            week, key=lambda kv: sum(kv[1].values())
        )
        lines.append(
            f"{total} commits across {len(touched)} repos this week · "
            f"busiest {datetime.strptime(busiest_date, '%Y-%m-%d'):%a} "
            f"({sum(busiest.values())})"
        )
    prs, branchy = [], []
    for repo in repos:
        full = repo["full_name"]
        try:
            for pr in gh_get(f"/repos/{full}/pulls", state="open").json():
                age = (
                    datetime.now(timezone.utc)
                    - datetime.strptime(
                        pr["created_at"], "%Y-%m-%dT%H:%M:%SZ"
                    ).replace(tzinfo=timezone.utc)
                ).days
                prs.append(f"{repo['name']}#{pr['number']} ({age}d): {pr['title'][:60]}")
            branches = gh_get(f"/repos/{full}/branches", per_page=100).json()
            if len(branches) > 1:
                branchy.append(f"{repo['name']} ({len(branches) - 1} extra)")
        except Exception:
            continue  # a sweep miss must not sink the review
    if prs:
        lines.append("open PRs: " + "; ".join(prs))
    if branchy:
        lines.append("repos with extra branches: " + ", ".join(branchy))
    if not lines:
        return ""
    return "🗓 WEEK IN CODE\n" + "\n".join(f"• {l}" for l in lines)


def rising_repos(shown):
    """📈 RISING REPOS — new GitHub repos crossing RISING_MIN_STARS this
    week, via the search API (same read token as the reviews).

    Deterministic, and the state memory ensures a repo is shown exactly
    once. Returns (block text, {full_name: date} of newly shown).
    ('', {}) when quiet — and the caller treats any failure the same
    way: a garnish must never sink the review."""
    since = (
        datetime.now(timezone.utc) - timedelta(days=RISING_WINDOW_DAYS)
    ).strftime("%Y-%m-%d")
    items = gh_get(
        "/search/repositories",
        q=f"created:>{since} stars:>{RISING_MIN_STARS}",
        sort="stars",
        order="desc",
        per_page=10,
    ).json().get("items", [])
    fresh = [i for i in items if i.get("full_name") not in shown][:RISING_CAP]
    if not fresh:
        return "", {}
    today = datetime.now(IST).strftime("%Y-%m-%d")
    lines = ["📈 RISING REPOS — new this week"]
    for i in fresh:
        desc = " ".join((i.get("description") or "").split())[:100]
        lines.append(
            f"• {i['full_name']} ★{i.get('stargazers_count', 0)}"
            + (f" — {desc}" if desc else "")
        )
        lines.append(f"  {i.get('html_url', '')}")
    return "\n".join(lines), {i["full_name"]: today for i in fresh}


def repo_inventory(repos):
    """One metadata line per repo — enough to judge keep/finish/delete."""
    return "\n".join(
        f"- {r['name']} | {r.get('language') or 'no code detected'} | "
        f"{(r.get('description') or '(no description)')[:80]} | "
        f"created {r['created_at'][:10]} | last push {r['pushed_at'][:10]} | "
        f"{r['size']} KB | {r['open_issues_count']} open issues"
        for r in repos
    )


# --- memory -------------------------------------------------------------


def load_state():
    """Review memory; an unreadable file costs the memory, never the run."""
    try:
        state = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        state = {}
    state.setdefault("daily", [])
    state.setdefault("spotlights", {})
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=RISING_KEEP_DAYS)
    ).strftime("%Y-%m-%d")
    state["rising"] = {
        k: v
        for k, v in state.get("rising", {}).items()
        if isinstance(v, str) and v >= cutoff
    }
    act_cutoff = (
        datetime.now(timezone.utc) - timedelta(days=ACTIVITY_DAYS)
    ).strftime("%Y-%m-%d")
    state["activity"] = {
        d: counts
        for d, counts in state.get("activity", {}).items()
        if isinstance(d, str) and d >= act_cutoff and isinstance(counts, dict)
    }
    return state


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")


def split_state(reply):
    """(message text, memory dict) from a model reply.

    The model appends a JSON tail after STATE_MARKER; a missing or
    malformed tail costs the memory, never the message."""
    if STATE_MARKER not in reply:
        return reply.strip(), {}
    text, _, tail = reply.partition(STATE_MARKER)
    start, end = tail.find("{"), tail.rfind("}")
    parsed = {}
    if start != -1 and end > start:
        try:
            parsed = json.loads(tail[start : end + 1])
        except ValueError:
            parsed = {}
    return text.strip(), parsed if isinstance(parsed, dict) else {}


def recent_findings(state, names):
    """{repo: ['date: summary', …]} — memory for the repos changed today."""
    out = {}
    for entry in state.get("daily", []):
        for name, summary in entry.get("findings", {}).items():
            if name in names:
                out.setdefault(name, []).append(f"{entry.get('date')}: {summary}")
    return out


# --- prompts ------------------------------------------------------------


def build_changes_prompt(changed, history):
    change_blocks = [
        f"=== {name}{note} — commits: {'; '.join(subjects)} ===\n{diff}"
        for name, subjects, diff, note in changed
    ]
    return (
        "You are reviewing today's pushes to my personal GitHub account. "
        "I am a data engineer; these repos are study projects and a small "
        "fleet of Telegram agents. Plain text only — no markdown headers "
        "or bold.\n\n"
        "=== INPUT 1: diffs pushed in the last 24h, per repo ===\n\n"
        + "\n\n".join(change_blocks)
        + "\n\n=== INPUT 2: your own recent findings for these repos ===\n\n"
        + (json.dumps(history, indent=1) if history else "(none on record)")
        + "\n\nProduce EXACTLY this output structure:\n\n"
        "🔎 TODAY'S CHANGES\n"
        "Per repo with commits: 2-4 review bullets drawn from the diff, "
        "each tagged [BUG] (wrong behavior on real input), [RISK] "
        "(fragile or unsafe pattern) or [STYLE] (idiom/clarity) — bugs "
        "first. Every bullet names the file (and function when visible) "
        "AND states the concrete fix in the same breath; a finding "
        "without a fix is not worth sending. At most ONE [STYLE] bullet "
        "per repo; a repo with nothing above style gets exactly "
        "'no significant findings'. Honest but kind; praise only what "
        "earned it.\n"
        "Use your recent findings: when today's diff fixes something you "
        "flagged, open that repo's bullets by acknowledging it; when a "
        "flagged problem is still there and being built on, escalate it "
        "in one line — never re-describe it verbatim. If a repo header "
        "is marked '(newest N commits only)', carry that caveat into its "
        "bullets so a heavy day is not read as fully reviewed.\n"
        "Commit subjects are part of the review: when a repo's subjects "
        "are uninformative ('fix', 'update', 'wip', 'changes'), end that "
        "repo's bullets with ONE line suggesting what the subject should "
        "have said. Good subjects earn no comment.\n\n"
        f"Then output the line {STATE_MARKER} and ONE JSON object mapping "
        "each repo to a one-line summary of today's key findings for it. "
        "Keys must be bare repo names (for a grouped header, the first "
        "name). No text after the JSON."
    )


def build_deep_prompt(spot_name, spot_src, prior, inventory=None):
    return (
        "You are deep-reviewing one repo from my personal GitHub account "
        "(I am a data engineer). Plain text only — no markdown headers or "
        "bold.\n\n"
        f"=== INPUT 1: source of today's spotlight repo, {spot_name} ===\n\n"
        + (spot_src or "(unavailable)")
        + (
            f"\n\n=== INPUT 2: your notes from this repo's previous "
            f"spotlight ({prior['date']}) ===\n\n{prior['notes']}"
            if prior
            else ""
        )
        + (
            "\n\n=== INPUT 3: full repo inventory (weekly portfolio check) ===\n\n"
            + inventory
            if inventory
            else ""
        )
        + "\n\nProduce EXACTLY this output structure:\n\n"
        f"💡 SPOTLIGHT: {spot_name}\n"
        + (
            "Start with follow-through on your previous notes: one line "
            "per prior item — fixed, partial, or ignored (escalate the "
            "ignored ones). Then only NEW findings; never repeat an old "
            "one verbatim.\n"
            if prior
            else ""
        )
        + "4-6 bullets from the full source: overall verdict in one line, "
        "then the highest-value concrete improvements tagged "
        "[BUG]/[RISK]/[STYLE] (dead code, naming, error handling, "
        "structure, missing tests) with file references. End the section "
        "with the single change you would make first.\n\n"
        + (
            "🗂 PORTFOLIO\n"
            "From the inventory, judged by name, description, size and last "
            "push:\n"
            "FINISH — up to 5 repos that look started-but-stalled yet worth "
            "completing, each with one line on what done would look like.\n"
            "ARCHIVE/DELETE — up to 8 candidates (stale experiments, empty "
            "repos, likely duplicates — flag near-identical names), each "
            "with a one-line reason; say DELETE only for the truly "
            "disposable, ARCHIVE when in doubt.\n"
            "KEEP — one closing line: how many look healthy and why.\n\n"
            if inventory
            else ""
        )
        + "Close with ONE small, concrete suggestion for tomorrow.\n\n"
        f"Then output the line {STATE_MARKER} and ONE JSON object: "
        '{"spotlight": "<3-4 lines: your key spotlight findings and, if '
        'there were prior notes, the follow-through status>"}. '
        "No text after the JSON."
    )


# --- entry point ---------------------------------------------------------


def main():
    load_dotenv(BASE_DIR / ".env")
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=LOOKBACK_HOURS)
    # env read after load_dotenv so .env values work too
    daily_model = os.environ.get("REVIEW_MODEL_DAILY") or "claude-haiku-4-5"
    deep_model = os.environ.get("REVIEW_MODEL_DEEP") or "claude-sonnet-5"

    state = load_state()
    repos = my_repos()
    changed, failed = [], []
    for repo in repos:
        try:
            got = day_diff(repo, since)
        except Exception as exc:  # one repo failing must not kill the review
            failed.append(f"{repo['name']} ({type(exc).__name__})")
            continue
        if got:
            changed.append((repo["name"], *got))
    changed = dedupe_changed(changed)

    # Spotlight: manual override (workflow input) wins; otherwise rotate by
    # day of year over a FIXED (name-sorted) order, so every repo genuinely
    # comes up every len(repos) days regardless of push activity.
    rotation = sorted(repos, key=lambda r: r["name"])
    spot = None
    override = os.environ.get("REVIEW_SPOTLIGHT", "").strip()
    if override:
        spot = next((r for r in rotation if r["name"] == override), None)
        if spot is None:
            failed.append(f"spotlight override '{override}' not found; rotating")
    if spot is None and rotation:
        spot = rotation[now.timetuple().tm_yday % len(rotation)]
    spot_src, spot_extras = "", []
    if spot:
        try:
            paths = repo_tree_paths(spot)
            spot_src = spotlight_source(spot, paths)
            spot_extras.append(hygiene_score(spot, paths))
            debt = debt_markers(spot_src)
            if debt:
                spot_extras.append(debt)
        except Exception as exc:
            failed.append(f"spotlight {spot['name']} ({type(exc).__name__})")
            spot = None
            spot_extras = []
    spot_name = spot["name"] if spot else "(unavailable)"

    curate = (
        datetime.now(IST).weekday() == CURATION_WEEKDAY
        or bool(os.environ.get("REVIEW_CURATE"))
    )

    # Call 1 — the day's diffs, cheap model, skipped entirely on quiet days.
    if changed:
        base_names = [entry[0].split(" (same diff", 1)[0] for entry in changed]
        history = recent_findings(state, base_names)
        # The token budget must scale with the repo count: a fixed cap on a
        # busy day truncates the reply mid-review and cuts off the memory
        # tail (which comes last) entirely.
        reply = ask_llm(
            build_changes_prompt(changed, history),
            max_tokens=min(4000, 600 + 200 * len(changed)),
            model=daily_model,
        )
        changes_text, changes_mem = split_state(reply)
    else:
        changes_text = "🔎 TODAY'S CHANGES\nNo commits pushed in the last 24h."
        changes_mem = {}

    # Call 2 — the deep read (+ portfolio on curate days), stronger model.
    prior = state["spotlights"].get(spot_name) if spot else None
    reply = ask_llm(
        build_deep_prompt(
            spot_name, spot_src, prior, repo_inventory(repos) if curate else None
        ),
        max_tokens=2200 if curate else 1500,
        model=deep_model,
    )
    deep_text, deep_mem = split_state(reply)

    body = changes_text + "\n\n" + deep_text
    # The deterministic spotlight extras ride under the spotlight section.
    if spot_extras:
        body += "\n" + "\n".join(spot_extras)

    # A silently red repo must not survive a day.
    try:
        red = ci_health(repos)
    except Exception:
        red = ""  # never sink the review
    if red:
        body += "\n\n" + red

    # Sunday: the week's shape, from the activity memory + a PR sweep.
    if curate:
        try:
            week = week_in_code(state, repos)
        except Exception:
            week = ""
        if week:
            body += "\n\n" + week

    # Deterministic garnish: what the ecosystem is starring this week.
    try:
        rising_text, rising_new = rising_repos(state.get("rising", {}))
    except Exception:
        rising_text, rising_new = "", {}  # never sink the review
    if rising_text:
        body += "\n\n" + rising_text

    if failed:
        body += "\n\n⚠️ Could not check: " + ", ".join(failed)
    header = (
        f"🔍 Repo review — {datetime.now(IST):%a %d %b %Y}\n"
        f"({len(changed)} repos with commits today, spotlight: {spot_name}"
        + (", weekly portfolio check" if curate else "")
        + ")\n\n"
    )
    send_telegram(header + body)

    # Persist memory last — the message already went out, so a state failure
    # only costs tomorrow's context, never today's review.
    today = datetime.now(IST).strftime("%Y-%m-%d")
    # activity: commit counts per repo — the raw material for WEEK IN CODE.
    # A grouped fleet-sync entry counts once under its first repo name;
    # that undercounts clones, which is the right bias for "how much did I
    # actually write this week".
    state.setdefault("activity", {})[today] = {
        entry[0].split(" (same diff", 1)[0]: len(entry[1]) for entry in changed
    }
    if changes_mem:
        state["daily"].append({"date": today, "findings": changes_mem})
        state["daily"] = state["daily"][-STATE_DAYS:]
    if spot and deep_mem.get("spotlight"):
        state["spotlights"][spot_name] = {
            "date": today,
            "notes": str(deep_mem["spotlight"])[:1500],
        }
    state.setdefault("rising", {}).update(rising_new)
    try:
        save_state(state)
    except OSError:
        pass


if __name__ == "__main__":
    main()
