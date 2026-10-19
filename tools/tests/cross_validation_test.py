import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import task_cross_validation as check

BASE_REF = "2026"
CURRENT_PR = 1


def contrib(task, week, group):
    return "contributions/{}/{}/{}/README.md".format(task, week, group)


def changed(path, status="added", previous=None):
    """Fake entry of pr.get_files()."""
    return SimpleNamespace(
        filename=path, status=status, previous_filename=previous
    )


class FakePR:
    def __init__(self, number, files):
        self.number = number
        self._files = files

    def get_files(self):
        return self._files


class FakeRepo:
    def __init__(self, prs, merged_paths=(), truncated=False):
        self.prs = {pr.number: pr for pr in prs}
        self.merged_paths = merged_paths
        self.truncated = truncated

    def get_pull(self, number):
        return self.prs[number]

    def get_pulls(self, **kwargs):
        # The real API also returns the current PR: the code must skip it
        return list(self.prs.values())

    def get_branch(self, ref):
        return SimpleNamespace(commit=SimpleNamespace(sha="deadbeef"))

    def get_git_tree(self, sha, recursive):
        return SimpleNamespace(
            raw_data={"truncated": self.truncated},
            tree=[
                SimpleNamespace(type="blob", path=path)
                for path in self.merged_paths
            ],
        )


def run(current_files, open_prs=(), merged=(), truncated=False):
    """open_prs: iterable of (pr_number, files)."""
    prs = [FakePR(CURRENT_PR, current_files)]
    prs += [FakePR(number, files) for number, files in open_prs]
    repo = FakeRepo(prs, merged, truncated)
    return check.find_cross_task_conflicts(repo, BASE_REF, CURRENT_PR)


# --- Cases without conflict -------------------------------------------------

def test_pr_without_contribution():
    assert run([changed("docs/readme.md")]) is None


def test_pr_that_only_removes_a_contribution():
    files = [changed(contrib("demo", "week3", "a-b"), status="removed")]
    merged = [contrib("scientific-paper", "week3", "a-c")]
    assert run(files, merged=merged) is None


def test_same_task_same_week_is_allowed():
    files = [changed(contrib("demo", "week3", "a-b"))]
    open_prs = [(2, [changed(contrib("demo", "week3", "a-c"))])]
    assert run(files, open_prs=open_prs) is None


def test_different_week_is_allowed():
    files = [changed(contrib("demo", "week3", "a-b"))]
    open_prs = [(2, [changed(contrib("scientific-paper", "week4", "a-c"))])]
    assert run(files, open_prs=open_prs) is None


def test_no_common_member_is_allowed():
    files = [changed(contrib("demo", "week3", "a-b"))]
    merged = [contrib("scientific-paper", "week3", "c-d")]
    assert run(files, merged=merged) is None


# --- Conflicts --------------------------------------------------------------

def test_conflict_with_open_pr():
    files = [changed(contrib("demo", "week3", "a-b"))]
    open_prs = [(2, [changed(contrib("scientific-paper", "week3", "a-c"))])]

    message = run(files, open_prs=open_prs)

    assert message is not None
    assert "PR #2" in message
    assert "`a`" in message
    assert "`b`" not in message  # only common members are listed


def test_conflict_with_merged_state():
    files = [changed(contrib("demo", "week3", "a-b"))]
    merged = [contrib("scientific-paper", "week3", "a-c")]

    message = run(files, merged=merged)

    assert message is not None
    assert "already merged" in message


def test_conflict_inside_the_current_pr():
    files = [
        changed(contrib("demo", "week3", "a-b")),
        changed(contrib("scientific-paper", "week3", "a-c")),
    ]

    message = run(files)

    assert message is not None
    assert "this PR" in message


def test_same_path_in_open_pr_and_tree_is_reported_once():
    # Simulates a PR merged between the two API calls
    path = contrib("scientific-paper", "week3", "a-c")
    files = [changed(contrib("demo", "week3", "a-b"))]

    message = run(
        files, open_prs=[(2, [changed(path)])], merged=[path]
    )

    assert message.count("- Week") == 1


# --- Modified / removed / renamed contributions -----------------------------

def test_removed_contribution_is_ignored_in_the_tree():
    # The student moves from a demo to a paper in the same PR
    files = [
        changed(contrib("demo", "week3", "a-b"), status="removed"),
        changed(contrib("scientific-paper", "week3", "a-c")),
    ]
    merged = [contrib("demo", "week3", "a-b")]

    assert run(files, merged=merged) is None


def test_renamed_contribution_old_path_is_ignored_in_the_tree():
    old = contrib("demo", "week3", "a-b")
    files = [
        changed(
            contrib("demo", "week3", "c-d"),
            status="renamed",
            previous=old,
        ),
        changed(contrib("scientific-paper", "week3", "a-e")),
    ]

    assert run(files, merged=[old]) is None


# --- Robustness -------------------------------------------------------------

def test_truncated_tree_fails_loudly():
    files = [changed(contrib("demo", "week3", "a-b"))]

    with pytest.raises(check.CheckError):
        run(files, truncated=True)


def test_path_with_newline_is_not_a_contribution():
    path = "contributions/demo/week3/a\n-b/README.md"
    assert check.parse_contribution(path, "x") is None