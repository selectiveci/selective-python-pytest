import subprocess
from pathlib import Path


def is_test_path(path):
    name = Path(path).name
    if name.startswith("test_") and name.endswith(".py"):
        return True
    if name.endswith("_test.py"):
        return True
    parts = Path(path).parts
    return "tests" in parts or "test" in parts


def under_suite(path, roots):
    text = path.replace("\\", "/")
    for root in roots:
        cleaned = str(root).replace("\\", "/").rstrip("/")
        if cleaned in ("", "."):
            if is_test_path(text):
                return True
            continue
        if text == cleaned or text.startswith(cleaned + "/"):
            if is_test_path(text) or text.endswith(".py"):
                return True
    return False


def _git(args, cwd=None):
    try:
        proc = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return proc


def git_diff(target_branch, num_commits, cwd):
    fetch = _git(
        ["git", "fetch", "origin", target_branch, "--depth=" + str(num_commits)],
        cwd,
    )
    if fetch is None:
        return None, ""
    diff = _git(["git", "diff", "origin/" + target_branch, "--name-only", "--relative"], cwd)
    if diff is None:
        return None, ""
    paths = [line.strip() for line in diff.stdout.splitlines() if line.strip()]
    return paths, ""


def modified_test_files(paths, roots):
    return [path for path in paths if under_suite(path, roots)]


def correlate(target_branch, num_commits, test_paths, cwd):
    log = _git(
        [
            "git",
            "log",
            "origin/" + target_branch,
            "-n",
            str(num_commits),
            "--name-only",
            "--relative",
            "--pretty=format:%x1e",
        ],
        cwd,
    )
    if log is None:
        return None
    tests = {path.replace("\\", "/") for path in test_paths}
    correlated = {}
    uncorrelated = {}
    commits = log.stdout.split("\x1e")
    for commit in commits:
        files = [
            line.strip().replace("\\", "/") for line in commit.splitlines() if line.strip()
        ]
        if not files:
            continue
        commit_tests = [path for path in files if path in tests or is_test_path(path)]
        commit_code = [path for path in files if path not in commit_tests]
        if commit_tests and not commit_code:
            for test in commit_tests:
                uncorrelated[test] = uncorrelated.get(test, 0) + 1
        for code in commit_code:
            for test in commit_tests:
                bucket = correlated.setdefault(code, {})
                bucket[test] = bucket.get(test, 0) + 1
    return {
        "correlated_files": correlated,
        "uncorrelated_files": uncorrelated,
    }
