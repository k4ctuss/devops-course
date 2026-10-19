import time
from github import Auth, Github
from github.GithubException import UnknownObjectException

SANDBOX = "your-user/devops-sandbox"
BASE = "sandbox-2026"
WORKFLOW = "cross-task-check.yml"
MARKER = "<!-- cross-task-check -->"


def contrib(task, week, group):
    return "contributions/{}/{}/{}/README.md".format(task, week, group)


# A None content means "delete this file"
CASES = [
    {
        "name": "demo-vs-paper-open-pr",
        "base": {},
        "open": [{contrib("scientific-paper", "week3", "a-c"): "x"}],
        "pr": {contrib("demo", "week3", "a-b"): "x"},
        "conflict": True,
        "in_comment": ["PR #", "`a`"],
    },
    {
        "name": "demo-vs-paper-merged",
        "base": {contrib("scientific-paper", "week3", "a-c"): "x"},
        "open": [],
        "pr": {contrib("demo", "week3", "a-b"): "x"},
        "conflict": True,
        "in_comment": ["already merged"],
    },
    {
        "name": "different-week",
        "base": {contrib("scientific-paper", "week4", "a-c"): "x"},
        "open": [],
        "pr": {contrib("demo", "week3", "a-b"): "x"},
        "conflict": False,
        "in_comment": [],
    },
]


def commit_files(repo, branch, files):
    for path, content in files.items():
        try:
            existing = repo.get_contents(path, ref=branch)
        except UnknownObjectException:
            existing = None

        if content is None:
            repo.delete_file(path, "test", existing.sha, branch=branch)
        elif existing is None:
            repo.create_file(path, "test", content, branch=branch)
        else:
            repo.update_file(path, "test", content, existing.sha, branch=branch)


def open_pr(repo, name, files):
    base_sha = repo.get_branch(BASE).commit.sha
    branch = "test/{}-{}".format(name, int(time.time() * 1000))
    repo.create_git_ref("refs/heads/" + branch, base_sha)
    commit_files(repo, branch, files)
    pr = repo.create_pull(title=name, body="", base=BASE, head=branch)
    return pr, branch


def known_run_ids(repo):
    runs = repo.get_workflow(WORKFLOW).get_runs(event="pull_request_target")
    return {run.id for run in runs[:50]}


def wait_for_new_runs(repo, known, count, timeout=300):
    deadline = time.time() + timeout

    while time.time() < deadline:
        runs = [
            run
            for run in repo.get_workflow(WORKFLOW).get_runs(
                event="pull_request_target"
            )[:50]
            if run.id not in known
        ]

        if len(runs) >= count and all(r.status == "completed" for r in runs):
            return runs

        time.sleep(5)

    raise TimeoutError("workflow run(s) did not complete in time")


def run_case(repo, case):
    baseline = repo.get_branch(BASE).commit.sha
    opened = []

    try:
        # "Already merged" state: committed directly on the base branch
        commit_files(repo, BASE, case["base"])

        # Open PRs first, so the PR under test sees them
        if case["open"]:
            known = known_run_ids(repo)
            for index, files in enumerate(case["open"]):
                opened.append(open_pr(repo, "{}-open{}".format(case["name"], index), files))
            wait_for_new_runs(repo, known, len(case["open"]))

        # PR under test, last: exactly one new run is expected
        known = known_run_ids(repo)
        pr, branch = open_pr(repo, case["name"], case["pr"])
        opened.append((pr, branch))
        (run,) = wait_for_new_runs(repo, known, 1)

        # Assertion 1: job conclusion
        expected = "failure" if case["conflict"] else "success"
        assert run.conclusion == expected, (case["name"], run.conclusion)

        # Assertion 2: comment presence and content
        comment = next(
            (c for c in pr.as_issue().get_comments() if MARKER in c.body),
            None,
        )

        if case["conflict"]:
            assert comment is not None, case["name"]
            for fragment in case["in_comment"]:
                assert fragment in comment.body, (case["name"], fragment)
        else:
            assert comment is None, case["name"]

        print("OK  ", case["name"])
    finally:
        # Teardown: close PRs, delete branches, reset the base branch
        for pr, branch in opened:
            pr.edit(state="closed")
            repo.get_git_ref("heads/" + branch).delete()

        repo.get_git_ref("heads/" + BASE).edit(baseline, force=True)


if __name__ == "__main__":
    token = input("Sandbox PAT: ")
    repo = Github(auth=Auth.Token(token)).get_repo(SANDBOX)

    for case in CASES:
        run_case(repo, case)