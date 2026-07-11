#!/usr/bin/env python3
"""Offline unit tests for repo_review — no network, no API keys.

Everything the module would fetch from GitHub or the LLM is stubbed with
synthetic data, so these run anywhere with `python3 test_repo_review.py`.

Covers:
  (a) spotlight rotation is stable/deterministic regardless of input order,
      and cycles every repo exactly once per len(repos) days;
  (b) real code is preferred over docs/config in the spotlight file pick;
  (c) the "(newest 20 commits only)" note appears when the cap is hit;
  (d) identical diffs across repos collapse into one review entry;
  (e) over-budget diffs are trimmed at file boundaries, code first;
  (f) the memory tail is split off the message and survives garbage.
"""

import unittest
from datetime import datetime, timedelta, timezone

import repo_review as rr


# --- tiny fakes -------------------------------------------------------------

class FakeResp:
    """Stands in for a requests.Response: carries JSON or raw text."""

    def __init__(self, json_data=None, text=""):
        self._json = json_data
        self.text = text

    def json(self):
        return self._json


def make_fixed_datetime(fixed):
    """A datetime subclass whose now() always returns `fixed` (tz-aware)."""

    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed

    return Fixed


def repo(name):
    return {
        "name": name,
        "full_name": f"me/{name}",
        "default_branch": "main",
        "fork": False,
        "archived": False,
    }


# --- (a) rotation -----------------------------------------------------------

class RotationTest(unittest.TestCase):
    """Drives main() with everything stubbed and reads back which repo the
    spotlight landed on, so the actual production code path is exercised."""

    NAMES = ["delta", "alpha", "charlie", "bravo", "echo"]

    def _spotlight_for(self, names, fixed):
        sent = {}

        def fake_send(text):
            sent["text"] = text

        repos = [repo(n) for n in names]
        patches = {
            "load_dotenv": lambda *a, **k: None,
            "my_repos": lambda: repos,
            "day_diff": lambda r, since: None,        # quiet day everywhere
            "repo_tree_paths": lambda r: [],
            "spotlight_source": lambda r, paths: f"SRC:{r['name']}",
            "hygiene_score": lambda r, paths: "🏅 Hygiene: 7/7",
            "ci_health": lambda repos: "",
            "week_in_code": lambda state, repos: "",
            "rising_repos": lambda shown: ("", {}),
            "repo_inventory": lambda rs: "INV",
            "ask_llm": lambda prompt, max_tokens=0, model="": "BODY",
            "send_telegram": fake_send,
            "datetime": make_fixed_datetime(fixed),
            "load_state": lambda: {"daily": [], "spotlights": {}},
            "save_state": lambda s: None,             # tests must not touch disk
        }
        saved = {k: getattr(rr, k) for k in patches}
        for k, v in patches.items():
            setattr(rr, k, v)
        try:
            rr.main()
        finally:
            for k, v in saved.items():
                setattr(rr, k, v)
        # Header line: "...spotlight: <name>...)"
        return sent["text"].split("spotlight: ", 1)[1].split(")", 1)[0].split(",")[0]

    def test_stable_regardless_of_input_order(self):
        day = datetime(2026, 3, 14, 12, 0, tzinfo=timezone.utc)
        forward = self._spotlight_for(self.NAMES, day)
        shuffled = self._spotlight_for(list(reversed(self.NAMES)), day)
        self.assertEqual(forward, shuffled)
        # And it is the name-sorted pick, not a push-order pick.
        expected = sorted(self.NAMES)[day.timetuple().tm_yday % len(self.NAMES)]
        self.assertEqual(forward, expected)

    def test_covers_every_repo_once_per_cycle(self):
        base = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        picks = [
            self._spotlight_for(self.NAMES, base + timedelta(days=d))
            for d in range(len(self.NAMES))
        ]
        self.assertEqual(sorted(picks), sorted(self.NAMES))  # each exactly once


# --- (b) spotlight file priority -------------------------------------------

class SpotlightFileOrderTest(unittest.TestCase):

    def test_code_before_docs_and_lockfiles_excluded(self):
        # Docs deliberately listed FIRST in tree order; a lockfile and a
        # .json blob are present and must never be picked.
        tree = {
            "tree": [
                {"type": "blob", "path": "README.md"},
                {"type": "blob", "path": "deploy.yml"},
                {"type": "blob", "path": "package-lock.json"},
                {"type": "blob", "path": "pyproject.toml"},
                {"type": "blob", "path": "src/app.py"},
                {"type": "blob", "path": "src/query.sql"},
                {"type": "tree", "path": "src"},
            ]
        }

        def fake_gh_get(path, accept="", **params):
            if "/git/trees/" in path:
                return FakeResp(json_data=tree)
            return FakeResp(text=f"body of {path}")

        saved = rr.gh_get
        rr.gh_get = fake_gh_get
        try:
            paths = rr.repo_tree_paths(repo("proj"))
            out = rr.spotlight_source(repo("proj"), paths)
        finally:
            rr.gh_get = saved

        picked = [line[4:-4] for line in out.splitlines() if line.startswith("--- ")]
        # Code first (tree order within tier), then docs; json/lock excluded.
        self.assertEqual(
            picked,
            ["src/app.py", "src/query.sql", "README.md", "deploy.yml", "pyproject.toml"],
        )
        self.assertNotIn("package-lock.json", picked)


# --- (c) commit-cap note ----------------------------------------------------

class CapNoteTest(unittest.TestCase):

    def _day_diff_note(self, n_commits):
        commits = [
            {
                "sha": f"c{i}",
                "commit": {"message": f"msg {i}\n\nbody"},
                "parents": [{"sha": "parent"}],
            }
            for i in range(n_commits)
        ]

        def fake_gh_get(path, accept="", **params):
            if "/compare/" in path or path.endswith(("/commits/c0",)):
                return FakeResp(text="diff --git a b\n")
            if path.endswith("/commits"):
                return FakeResp(json_data=commits)
            return FakeResp(text="diff --git a b\n")

        saved = rr.gh_get
        rr.gh_get = fake_gh_get
        try:
            result = rr.day_diff(repo("proj"), datetime(2026, 1, 1, tzinfo=timezone.utc))
        finally:
            rr.gh_get = saved
        return result[2]  # the note

    def test_note_present_at_cap(self):
        note = self._day_diff_note(rr.MAX_COMMITS_PER_REPO)
        self.assertEqual(note, f" (newest {rr.MAX_COMMITS_PER_REPO} commits only)")

    def test_no_note_below_cap(self):
        self.assertEqual(self._day_diff_note(3), "")

    def test_note_surfaces_in_prompt(self):
        changed = [("proj", ["m1", "m2"], "diff...", " (newest 20 commits only)")]
        prompt = rr.build_changes_prompt(changed, {})
        self.assertIn("(newest 20 commits only)", prompt)


# --- (d) identical-diff dedupe -----------------------------------------------

class DedupeTest(unittest.TestCase):

    def test_identical_diffs_collapse(self):
        same = "diff --git a/agentlib.py b/agentlib.py\n-old\n+new\n"
        changed = [
            ("alpha", ["sync"], same, ""),
            ("bravo", ["sync"], same, ""),
            ("charlie", ["own change"], "diff --git a/x b/x\n+y\n", ""),
            ("delta", ["sync"], same, ""),
        ]
        out = rr.dedupe_changed(changed)
        self.assertEqual(len(out), 2)
        merged = out[0][0]
        self.assertIn("alpha", merged)
        self.assertIn("(same diff in 2 more: bravo, delta)", merged)
        self.assertEqual(out[1][0], "charlie")  # unique entry untouched

    def test_unique_diffs_pass_through(self):
        changed = [
            ("alpha", ["a"], "diff --git a/1 b/1\n+a\n", ""),
            ("bravo", ["b"], "diff --git a/2 b/2\n+b\n", ""),
        ]
        self.assertEqual(rr.dedupe_changed(changed), changed)


# --- (e) diff trimming --------------------------------------------------------

class TrimDiffTest(unittest.TestCase):

    @staticmethod
    def _section(path, filler):
        return f"diff --git a/{path} b/{path}\n" + filler + "\n"

    def test_under_budget_untouched(self):
        diff = self._section("app.py", "+x" * 10)
        self.assertEqual(rr.trim_diff(diff, budget=1000), diff)

    def test_code_kept_docs_dropped_with_note(self):
        docs = self._section("README.md", "+d" * 300)
        code = self._section("app.py", "+c" * 100)
        out = rr.trim_diff(docs + code, budget=250)
        self.assertIn("app.py", out)
        self.assertNotIn("+d" * 300, out)
        self.assertIn("1 file diffs omitted", out)

    def test_single_oversized_diff_falls_back_to_hard_cut(self):
        big = self._section("app.py", "+c" * 5000)
        out = rr.trim_diff(big, budget=200)
        self.assertTrue(out.startswith("diff --git a/app.py"))
        self.assertIn("truncated for size", out)


# --- (h) hygiene, debt, CI health, week in code ---------------------------------

class HygieneTest(unittest.TestCase):

    def test_full_marks_and_missing_named(self):
        r = dict(repo("proj"), description="does things", topics=["python"])
        paths = ["README.md", "LICENSE", "tests/test_app.py",
                 ".github/workflows/ci.yml", ".gitignore", "app.py"]
        line = rr.hygiene_score(r, paths)
        self.assertIn("7/7", line)
        self.assertNotIn("missing", line)

        bare = dict(repo("proj"), description="", topics=[])
        line = rr.hygiene_score(bare, ["app.py"])
        self.assertIn("0/7", line)
        self.assertIn("README", line)
        self.assertIn("license", line)

    def test_test_files_detected_both_conventions(self):
        r = dict(repo("p"), description="d", topics=["t"])
        self.assertNotIn("tests", rr.hygiene_score(r, ["test_app.py"]).split("missing:")[-1])
        self.assertNotIn("tests", rr.hygiene_score(r, ["tests/app.py"]).split("missing:")[-1])


class DebtMarkersTest(unittest.TestCase):

    SRC = ("--- app.py ---\n# TODO: fix this\nx = 1  # FIXME\n\n"
           "--- lib.py ---\n# HACK around API\n")

    def test_counts_per_file(self):
        line = rr.debt_markers(self.SRC)
        self.assertIn("3 TODO/FIXME", line)
        self.assertIn("app.py×2", line)
        self.assertIn("lib.py", line)

    def test_clean_source_is_quiet(self):
        self.assertEqual(rr.debt_markers("--- app.py ---\nx = 1\n"), "")


class CiHealthTest(unittest.TestCase):

    def _with_runs(self, runs_by_repo, repos):
        def fake(path, accept="", **params):
            name = path.split("/repos/me/", 1)[1].split("/", 1)[0]
            return FakeResp(json_data={"workflow_runs": runs_by_repo.get(name, [])})
        saved = rr.gh_get
        rr.gh_get = fake
        try:
            return rr.ci_health(repos)
        finally:
            rr.gh_get = saved

    def test_failing_repo_reported_with_link(self):
        out = self._with_runs(
            {"a": [{"conclusion": "failure", "name": "Tests",
                    "html_url": "https://x/run"}],
             "b": [{"conclusion": "success", "name": "Tests"}]},
            [repo("a"), repo("b")],
        )
        self.assertIn("🔴 CI HEALTH", out)
        self.assertIn("a: Tests failure", out)
        self.assertIn("https://x/run", out)
        self.assertNotIn("b:", out)

    def test_all_green_is_silent(self):
        out = self._with_runs(
            {"a": [{"conclusion": "success", "name": "T"}], "b": []},
            [repo("a"), repo("b")],
        )
        self.assertEqual(out, "")


class WeekInCodeTest(unittest.TestCase):

    def test_rolls_up_activity_and_prs(self):
        state = {"activity": {
            "2026-07-06": {"a": 3, "b": 1},
            "2026-07-07": {"a": 2},
        }}
        def fake(path, accept="", **params):
            if path.endswith("/pulls"):
                return FakeResp(json_data=[{
                    "number": 7, "title": "Add ledger",
                    "created_at": "2026-07-01T00:00:00Z"}])
            return FakeResp(json_data=[{"name": "main"}])
        saved = rr.gh_get
        rr.gh_get = fake
        try:
            out = rr.week_in_code(state, [repo("a")])
        finally:
            rr.gh_get = saved
        self.assertIn("6 commits across 2 repos", out)
        self.assertIn("a#7", out)
        self.assertIn("Add ledger", out)
        self.assertNotIn("extra branches", out)  # only main exists

    def test_empty_memory_and_clean_repos_is_quiet(self):
        def fake(path, accept="", **params):
            return FakeResp(json_data=[] if path.endswith("/pulls")
                            else [{"name": "main"}])
        saved = rr.gh_get
        rr.gh_get = fake
        try:
            out = rr.week_in_code({"activity": {}}, [repo("a")])
        finally:
            rr.gh_get = saved
        self.assertEqual(out, "")

    def test_load_state_prunes_old_activity(self):
        import json as _json
        import tempfile
        from pathlib import Path
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with tempfile.TemporaryDirectory() as tmp:
            saved = rr.STATE_FILE
            rr.STATE_FILE = Path(tmp) / "findings.json"
            try:
                rr.STATE_FILE.write_text(_json.dumps(
                    {"activity": {today: {"a": 1}, "2020-01-01": {"b": 2}}}))
                state = rr.load_state()
            finally:
                rr.STATE_FILE = saved
        self.assertEqual(list(state["activity"]), [today])


# --- (g) rising repos -----------------------------------------------------------

class RisingReposTest(unittest.TestCase):
    """The deterministic weekly-stars garnish: new repos show once, ever."""

    def _payload(self, *names):
        return {"items": [
            {"full_name": n, "stargazers_count": 500, "description": "d",
             "html_url": f"https://github.com/{n}"} for n in names
        ]}

    def test_new_repos_shown_remembered_skipped(self):
        saved = rr.gh_get
        rr.gh_get = lambda *a, **k: FakeResp(json_data=self._payload("a/one", "b/two"))
        try:
            text, new = rr.rising_repos(shown={"a/one": "2026-07-10"})
        finally:
            rr.gh_get = saved
        self.assertNotIn("a/one", text)
        self.assertIn("b/two", text)
        self.assertIn("★500", text)
        self.assertEqual(list(new), ["b/two"])

    def test_nothing_new_is_quiet(self):
        saved = rr.gh_get
        rr.gh_get = lambda *a, **k: FakeResp(json_data=self._payload("a/one"))
        try:
            out = rr.rising_repos(shown={"a/one": "2026-07-10"})
        finally:
            rr.gh_get = saved
        self.assertEqual(out, ("", {}))

    def test_load_state_prunes_old_rising_entries(self):
        import json as _json
        import tempfile
        from pathlib import Path
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with tempfile.TemporaryDirectory() as tmp:
            saved = rr.STATE_FILE
            rr.STATE_FILE = Path(tmp) / "findings.json"
            try:
                rr.STATE_FILE.write_text(_json.dumps(
                    {"rising": {"a/b": today, "c/d": "2020-01-01"}}))
                state = rr.load_state()
            finally:
                rr.STATE_FILE = saved
        self.assertEqual(list(state["rising"]), ["a/b"])


# --- (f) memory tail ----------------------------------------------------------

class SplitStateTest(unittest.TestCase):

    def test_splits_message_and_json(self):
        reply = f'the review\n{rr.STATE_MARKER}\n{{"proj": "one bug"}}'
        text, mem = rr.split_state(reply)
        self.assertEqual(text, "the review")
        self.assertEqual(mem, {"proj": "one bug"})

    def test_no_marker_means_no_memory(self):
        text, mem = rr.split_state("just a review")
        self.assertEqual((text, mem), ("just a review", {}))

    def test_garbage_tail_costs_memory_not_message(self):
        reply = f"the review\n{rr.STATE_MARKER}\n{{not json"
        text, mem = rr.split_state(reply)
        self.assertEqual(text, "the review")
        self.assertEqual(mem, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
