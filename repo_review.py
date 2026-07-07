#!/usr/bin/env python3
"""Repo review agent.

One Telegram message every evening (~19:37 IST via GitHub Actions) reviewing
my GitHub account:

  - every commit pushed to any of my repos in the last 24h, reviewed from
    the actual diffs — real bugs first, then risky patterns, then idioms
  - a rotating SPOTLIGHT: one repo per day gets a full read-through
    (dead code, naming, error handling, structure), so every repo gets a
    deep review every couple of weeks
  - on Sundays (or with REVIEW_CURATE=1): a PORTFOLIO section — which
    repos to keep, which to finish, which to archive or delete
  - one small suggestion for tomorrow

Always sends — a quiet coding day still gets the spotlight review.

Same fleet pattern as the other agents: own repo, own schedule, fails alone.
Needs REPOS_READ_TOKEN (read-only PAT) to list and read the account's repos.
"""

import os
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
SOURCE_EXT = (".py", ".sql", ".sh", ".js", ".ts", ".yml", ".yaml", ".toml", ".md")


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


def day_diff(repo, since):
    """(commit subjects, unified diff) for one repo's last-24h changes,
    or None if nothing was pushed."""
    full = repo["full_name"]
    commits = gh_get(
        f"/repos/{full}/commits",
        sha=repo["default_branch"],
        since=since.isoformat(),
        per_page=MAX_COMMITS_PER_REPO,
    ).json()
    if not commits:
        return None
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
    return subjects, diff[:MAX_DIFF_CHARS]


def spotlight_source(repo):
    """Up to ~30k chars of a repo's source files for the deep read."""
    full = repo["full_name"]
    branch = repo["default_branch"]
    tree = gh_get(f"/repos/{full}/git/trees/{branch}", recursive=1).json()
    picked, budget = [], MAX_SPOTLIGHT_CHARS
    for node in tree.get("tree", []):
        if node["type"] != "blob" or not node["path"].endswith(SOURCE_EXT):
            continue
        if len(picked) >= MAX_SPOTLIGHT_FILES or budget <= 0:
            break
        try:
            text = gh_get(
                f"/repos/{full}/contents/{quote(node['path'])}",
                accept="application/vnd.github.raw",
                ref=branch,
            ).text[:budget]
        except requests.RequestException:
            continue  # one unreadable blob must not sink the spotlight
        picked.append(f"--- {node['path']} ---\n{text}")
        budget -= len(text)
    return "\n\n".join(picked)


def repo_inventory(repos):
    """One metadata line per repo — enough to judge keep/finish/delete."""
    return "\n".join(
        f"- {r['name']} | {r.get('language') or 'no code detected'} | "
        f"{(r.get('description') or '(no description)')[:80]} | "
        f"created {r['created_at'][:10]} | last push {r['pushed_at'][:10]} | "
        f"{r['size']} KB | {r['open_issues_count']} open issues"
        for r in repos
    )


def build_prompt(changed, spot_name, spot_src, inventory=None):
    change_blocks = [
        f"=== {name} — commits: {'; '.join(subjects)} ===\n{diff}"
        for name, subjects, diff in changed
    ] or ["(no commits pushed in the last 24h)"]

    return (
        "You are reviewing my personal GitHub account. I am a data engineer; "
        "these repos are study projects and a small fleet of Telegram agents. "
        "Plain text only — no markdown headers or bold.\n\n"
        "=== INPUT 1: diffs pushed in the last 24h, per repo ===\n\n"
        + "\n\n".join(change_blocks)
        + f"\n\n=== INPUT 2: source of today's spotlight repo, {spot_name} ===\n\n"
        + (spot_src or "(unavailable)")
        + (
            "\n\n=== INPUT 3: full repo inventory (weekly portfolio check) ===\n\n"
            + inventory
            if inventory
            else ""
        )
        + "\n\nProduce EXACTLY this output structure:\n\n"
        "🔎 TODAY'S CHANGES\n"
        "Per repo with commits: 2-4 specific review bullets drawn from the "
        "diff — real bugs first, then risky patterns, then better idioms; "
        "name the file/function each time. Honest but kind; praise only "
        "what earned it. If there were no commits: exactly one line "
        "saying so.\n\n"
        f"💡 SPOTLIGHT: {spot_name}\n"
        "4-6 bullets from the full source: overall verdict in one line, "
        "then the highest-value concrete improvements (dead code, naming, "
        "error handling, structure, missing tests) with file references. "
        "End the section with the single change you would make first.\n\n"
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
        + "Close with ONE small, concrete suggestion for tomorrow."
    )


def main():
    load_dotenv(BASE_DIR / ".env")
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=LOOKBACK_HOURS)

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

    # Rotate the deep dive by day of year so every repo comes up every
    # len(repos) days without any state to store.
    spot = repos[now.timetuple().tm_yday % len(repos)] if repos else None
    spot_src = ""
    if spot:
        try:
            spot_src = spotlight_source(spot)
        except Exception as exc:
            failed.append(f"spotlight {spot['name']} ({type(exc).__name__})")
            spot = None
    spot_name = spot["name"] if spot else "(unavailable)"

    curate = (
        datetime.now(IST).weekday() == CURATION_WEEKDAY
        or bool(os.environ.get("REVIEW_CURATE"))
    )
    body = ask_llm(
        build_prompt(
            changed, spot_name, spot_src, repo_inventory(repos) if curate else None
        ),
        max_tokens=2500 if curate else 1800,
    )
    if failed:
        body += "\n\n⚠️ Could not check: " + ", ".join(failed)

    header = (
        f"🔍 Repo review — {datetime.now(IST):%a %d %b %Y}\n"
        f"({len(changed)} repos with commits today, spotlight: {spot_name}"
        + (", weekly portfolio check" if curate else "")
        + ")\n\n"
    )
    send_telegram(header + body)


if __name__ == "__main__":
    main()
