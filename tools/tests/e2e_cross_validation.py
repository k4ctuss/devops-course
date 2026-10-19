
import os
import sys
import time
import itertools

from github import Auth, Github
from github.GithubException import GithubException, UnknownObjectException


SANDBOX = "your-user/your-fork"
BASE = "sandbox-2026"
WORKFLOW_NAME = "cross_task_check.yml"
MARKER = "<!-- cross-task-check -->"
BOT_LOGIN = "github-actions[bot]"
BRANCH_PREFIX = "e2e/"
TITLE_PREFIX = "[e2e] "
TOOL_PATH = "tools/cross_task_check.py"
RUN_TIMEOUT = 120  # seconds
POLL_INTERVAL = 5  # seconds

DEMO = "demo"
PAPER = "scientific-paper"


class InvalidCase(Exception):
    """The setup did not produce the situation the case wants to test."""


# --- Fixtures: file operations ----------------------------------------------

def contrib(task, week, group):
    return "contributions/{}/{}/{}/README.md".format(task, week, group)


def c(task, group, week="week99"):
    return contrib(task, week, group)


def body(path, version=1):
    # The path is part of the content so that two different files never have
    # identical content (keeps git's rename detection unambiguous).
    return "README for {}\nversion {}\n".format(path, version)


def W(path, version=1, content=None):
    """Write (create or update) a file."""
    return ("write", path, body(path, version) if content is None else content)


def D(path):
    """Delete a file."""
    return ("delete", path)


def R(old, new):
    """Rename a file, keeping its content byte for byte."""
    return ("rename", old, new)


# Neutral state pushed once before all tests: unrelated contributions that
# the check must ignore (other weeks, other members). No conflict inside.
BASELINE = [
    W(contrib(DEMO, "week90", "zz1-zz2")),
    W(contrib(PAPER, "week90", "zz3-zz4")),
    W(contrib(PAPER, "week91", "zz1-zz5")),
]


# --- Assertions ---------------------------------------------------------------

def expect(condition, message):
    if not condition:
        raise AssertionError(message)


def find_bot_comment(pr):
    for comment in pr.as_issue().get_comments():
        if MARKER in comment.body and comment.user.login == BOT_LOGIN:
            return comment
    return None


def check_outcome(label, pr, run, conflict, fragments=()):
    expected = "failure" if conflict else "success"
    expect(
        run.conclusion == expected,
        "{}: conclusion is '{}', expected '{}'".format(
            label, run.conclusion, expected
        ),
    )

    comment = find_bot_comment(pr)

    if not conflict:
        expect(comment is None, "{}: unexpected comment on the PR".format(label))
        return

    expect(comment is not None, "{}: no comment on the PR".format(label))

    for fragment in fragments:
        expect(
            fragment in comment.body,
            "{}: comment does not contain {!r}".format(label, fragment),
        )


# --- Sandbox helpers ----------------------------------------------------------

class Sandbox:
    def __init__(self, repo):
        self.repo = repo
        self.baseline_sha = None

    # Repository ---------------------------------------------------------------

    def file_sha(self, branch, path):
        try:
            return self.repo.get_contents(path, ref=branch).sha
        except UnknownObjectException:
            return None

    def apply_ops(self, branch, ops):
        """Apply file operations on a branch, one commit per API call."""
        for op in ops:
            kind, path = op[0], op[1]
            sha = self.file_sha(branch, path)

            if kind == "write":
                if sha is None:
                    self.repo.create_file(
                        path, "e2e: write", op[2], branch=branch
                    )
                else:
                    self.repo.update_file(
                        path, "e2e: update", op[2], sha, branch=branch
                    )
            elif kind == "delete":
                self.repo.delete_file(path, "e2e: delete", sha, branch=branch)
            elif kind == "rename":
                content = self.repo.get_contents(
                    path, ref=branch
                ).decoded_content.decode()
                self.repo.delete_file(
                    path, "e2e: rename (delete)", sha, branch=branch
                )
                self.repo.create_file(
                    op[2], "e2e: rename (create)", content, branch=branch
                )

    def sweep(self):
        """Close leftover e2e PRs and delete leftover e2e branches."""
        for pr in self.repo.get_pulls(state="open", base=BASE):
            if pr.title.startswith(TITLE_PREFIX):
                pr.edit(state="closed")

        for ref in self.repo.get_git_matching_refs("heads/" + BRANCH_PREFIX):
            ref.delete()

    def reset_base(self, sha):
        self.repo.get_git_ref("heads/" + BASE).edit(sha, force=True)

    # Pull requests ------------------------------------------------------------

    def open_pr(self, name, ops):
        base_sha = self.repo.get_branch(BASE).commit.sha
        branch = "{}{}-{}".format(BRANCH_PREFIX, name, int(time.time() * 1000))

        self.repo.create_git_ref("refs/heads/" + branch, base_sha)
        # All commits are pushed BEFORE the PR exists: a single "opened" run
        self.apply_ops(branch, ops)

        return self.repo.create_pull(
            title=TITLE_PREFIX + name, body="", base=BASE, head=branch
        )

    def merge(self, pr):
        for attempt in range(3):
            try:
                pr.merge()
                return
            except GithubException:
                if attempt == 2:
                    raise
                time.sleep(3)

    def verify_statuses(self, pr, expected):
        """Make sure GitHub reports the file statuses the case relies on."""
        actual = {f.filename: f.status for f in pr.get_files()}

        for path, status in expected.items():
            if actual.get(path) != status:
                raise InvalidCase(
                    "GitHub reported '{}' for {}, expected '{}'".format(
                        actual.get(path), path, status
                    )
                )

    # Workflow runs ------------------------------------------------------------

    def list_runs(self):
        runs = self.repo.get_workflow_runs(event="pull_request_target")

        return [
            run
            for run in itertools.islice(runs, 100)
            if run.raw_data.get("name") == WORKFLOW_NAME
        ]

    def run_ids(self):
        return {run.id for run in self.list_runs()}

    def wait_new_runs(self, known, count):
        """Wait until exactly `count` new runs exist and are all completed."""
        deadline = time.time() + RUN_TIMEOUT

        while time.time() < deadline:
            runs = [r for r in self.list_runs() if r.id not in known]

            if len(runs) >= count and all(r.status == "completed" for r in runs):
                if len(runs) != count:
                    raise RuntimeError(
                        "expected {} new run(s), found {}".format(
                            count, len(runs)
                        )
                    )
                return runs

            time.sleep(POLL_INTERVAL)

        raise TimeoutError("workflow run(s) did not complete in time")

    def open_and_wait(self, name, ops):
        known = self.run_ids()
        pr = self.open_pr(name, ops)
        (run,) = self.wait_new_runs(known, 1)
        return pr, run

    def rerun_and_wait(self, run):
        """Re-run a run: same run id, run_attempt is incremented."""
        attempt = run.run_attempt or 1

        expect(run.rerun(), "the re-run request was rejected")

        deadline = time.time() + RUN_TIMEOUT

        while time.time() < deadline:
            fresh = self.repo.get_workflow_run(run.id)

            if (fresh.run_attempt or 1) > attempt and fresh.status == "completed":
                return fresh

            time.sleep(POLL_INTERVAL)

        raise TimeoutError("re-run did not complete in time")

    # Test execution ----------------------------------------------------------

    def run_test(self, name, test):
        """Run one test, always restore the sandbox. Return True on success."""
        try:
            test()
            result, ok = "OK", True
        except InvalidCase as error:
            result, ok = "INVALID  {}".format(error), False
        except AssertionError as error:
            result, ok = "FAIL     {}".format(error), False
        except Exception as error:  # API error, timeout, ...
            result, ok = "ERROR    {}: {}".format(type(error).__name__, error), False
        finally:
            # After each test: close PRs, delete branches, reset the base
            self.sweep()
            self.reset_base(self.baseline_sha)

        print("{:<42} {}".format(name, result))
        return ok

    def run_case(self, case):
        # State already "merged": committed directly on the base branch
        self.apply_ops(BASE, case.get("base", []))

        # Open PRs first (one at a time), the PR under test last
        open_numbers = []

        for index, ops in enumerate(case.get("open", [])):
            pr, _ = self.open_and_wait("{}-open{}".format(case["name"], index), ops)
            open_numbers.append(pr.number)

        pr, run = self.open_and_wait(case["name"], case["pr"])
        self.verify_statuses(pr, case.get("expect_status", {}))

        fragments = [f.format(open=open_numbers) for f in case.get("in_comment", [])]
        check_outcome(case["name"], pr, run, case["conflict"], fragments)


# --- Single-PR cases -----------------------------------------------------------

CASES = [
    {
        "name": "no-contribution",
        "pr": [W("docs/e2e.md")],
        "conflict": False,
    },
    {
        "name": "conflict-with-open-pr",
        "open": [[W(c(PAPER, "tst1-tst3"))]],
        "pr": [W(c(DEMO, "tst1-tst2"))],
        "conflict": True,
        "in_comment": ["PR #{open[0]}", "`tst1`"],
    },
    {
        "name": "conflict-with-merged-state",
        "base": [W(c(PAPER, "tst1-tst3"))],
        "pr": [W(c(DEMO, "tst1-tst2"))],
        "conflict": True,
        "in_comment": ["already merged", "`tst1`"],
    },
    {
        "name": "same-task-same-week",
        "base": [W(c(DEMO, "tst1-tst3"))],
        "pr": [W(c(DEMO, "tst1-tst2"))],
        "conflict": False,
    },
    {
        "name": "different-week",
        "base": [W(c(PAPER, "tst1-tst3", week="week98"))],
        "pr": [W(c(DEMO, "tst1-tst2"))],
        "conflict": False,
    },
    {
        "name": "no-common-member",
        "base": [W(c(PAPER, "tst3-tst4"))],
        "pr": [W(c(DEMO, "tst1-tst2"))],
        "conflict": False,
    },
    {
        "name": "conflict-inside-the-pr",
        "pr": [W(c(DEMO, "tst1-tst2")), W(c(PAPER, "tst1-tst3"))],
        "conflict": True,
        "in_comment": ["this PR"],
    },
    {
        # The base already contains a conflict, but the PR only removes
        "name": "pr-only-removes-contribution",
        "base": [W(c(DEMO, "tst1-tst2")), W(c(PAPER, "tst1-tst3"))],
        "pr": [D(c(DEMO, "tst1-tst2"))],
        "conflict": False,
    },
    {
        "name": "pr-modifies-conflicting-contribution",
        "base": [W(c(DEMO, "tst1-tst2")), W(c(PAPER, "tst1-tst3"))],
        "pr": [W(c(DEMO, "tst1-tst2"), version=2)],
        "conflict": True,
        "in_comment": ["already merged", "`tst1`"],
    },
    {
        # The old demo file is removed from the tree view, so tst1 is free
        "name": "move-from-demo-to-paper",
        "base": [W(c(DEMO, "tst1-tst2"))],
        "pr": [D(c(DEMO, "tst1-tst2")), W(c(PAPER, "tst1-tst3"))],
        "conflict": False,
    },
    {
        "name": "rename-frees-old-group",
        "base": [W(c(DEMO, "tst1-tst2"))],
        "pr": [
            R(c(DEMO, "tst1-tst2"), c(DEMO, "tst3-tst4")),
            W(c(PAPER, "tst1-tst5")),
        ],
        "conflict": False,
        "expect_status": {c(DEMO, "tst3-tst4"): "renamed"},
    },
    {
        "name": "rename-into-conflict",
        "base": [W(c(DEMO, "tst8-tst9")), W(c(PAPER, "tst1-tst3"))],
        "pr": [R(c(DEMO, "tst8-tst9"), c(DEMO, "tst1-tst2"))],
        "conflict": True,
        "in_comment": ["`tst1`"],
        "expect_status": {c(DEMO, "tst1-tst2"): "renamed"},
    },
    {
        # The PR replaces the check script with a no-op: the base version
        # must run anyway, so the conflict must still be detected.
        "name": "pr-tampers-with-the-script",
        "base": [W(c(PAPER, "tst1-tst3"))],
        "pr": [
            W(TOOL_PATH, content="import sys\nsys.exit(0)\n"),
            W(c(DEMO, "tst1-tst2")),
        ],
        "conflict": True,
    },
]


# --- Multi-step scenarios --------------------------------------------------------

def scenario_symmetry_and_unblock(sb):
    pr1, run1 = sb.open_and_wait("sym-first", [W(c(DEMO, "tst1-tst2"))])
    check_outcome("first PR", pr1, run1, False)

    pr2, run2 = sb.open_and_wait("sym-second", [W(c(PAPER, "tst1-tst3"))])
    check_outcome("second PR", pr2, run2, True, ["PR #{}".format(pr1.number)])

    # The first result was computed before the second PR existed: a re-run
    # now sees it, so the first PR becomes red too (symmetry).
    run1 = sb.rerun_and_wait(run1)
    check_outcome(
        "first PR re-run", pr1, run1, True, ["PR #{}".format(pr2.number)]
    )

    # Closing the second PR does not trigger anything: re-run by hand
    pr2.edit(state="closed")
    run1 = sb.rerun_and_wait(run1)
    check_outcome("first PR re-run after closing the second", pr1, run1, False)


def scenario_merged_blocker_then_fix(sb):
    pr1, run1 = sb.open_and_wait("merged-blocker", [W(c(DEMO, "tst1-tst2"))])
    check_outcome("blocker PR", pr1, run1, False)

    paper = c(PAPER, "tst1-tst3")
    pr2, run2 = sb.open_and_wait("merged-blocked", [W(paper)])
    check_outcome("blocked PR", pr2, run2, True, ["PR #{}".format(pr1.number)])

    # A real merge: the blocker is now part of the branch tree
    sb.merge(pr1)
    run2 = sb.rerun_and_wait(run2)
    check_outcome(
        "blocked PR after the merge", pr2, run2, True, ["already merged"]
    )

    # Fix by pushing: one single commit, hence exactly one "synchronize" run
    known = sb.run_ids()
    sb.apply_ops(pr2.head.ref, [D(paper)])
    (run3,) = sb.wait_new_runs(known, 1)
    check_outcome("blocked PR after the fix push", pr2, run3, False)


SCENARIOS = [
    ("scenario: symmetry and unblock", scenario_symmetry_and_unblock),
    ("scenario: merged blocker, then fix", scenario_merged_blocker_then_fix),
]


# --- Entry point -----------------------------------------------------------------

def main():
    token = os.environ["SANDBOX_TOKEN"]
    repo = Github(auth=Auth.Token(token)).get_repo(SANDBOX)
    sandbox = Sandbox(repo)

    original_sha = repo.get_branch(BASE).commit.sha
    results = []

    try:
        # Before all: clean leftovers, push the neutral baseline
        sandbox.sweep()
        sandbox.apply_ops(BASE, BASELINE)
        sandbox.baseline_sha = repo.get_branch(BASE).commit.sha

        for case in CASES:
            results.append(
                sandbox.run_test(
                    case["name"], lambda case=case: sandbox.run_case(case)
                )
            )

        for name, scenario in SCENARIOS:
            results.append(
                sandbox.run_test(
                    name, lambda scenario=scenario: scenario(sandbox)
                )
            )
    finally:
        # After all: restore the sandbox branch even if something crashed
        sandbox.sweep()
        sandbox.reset_base(original_sha)

    passed = sum(results)
    print("\n{}/{} passed".format(passed, len(results)))
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()