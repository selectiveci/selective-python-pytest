import json
import os
import secrets
import subprocess

REQUIRED = (
    ("host", "SELECTIVE_HOST"),
    ("api_key", "SELECTIVE_API_KEY"),
    ("platform", "SELECTIVE_PLATFORM"),
    ("run_id", "SELECTIVE_RUN_ID"),
    ("run_attempt", "SELECTIVE_RUN_ATTEMPT"),
    ("branch", "SELECTIVE_BRANCH"),
    ("sha", "SELECTIVE_SHA"),
    ("git_repo_full_name", "SELECTIVE_GIT_REPO"),
    ("git_provider", "SELECTIVE_GIT_PROVIDER"),
)

METADATA_OMIT = {"host", "runner_id", "api_key", "run_id", "run_attempt"}
DISABLED = {"1", "true", "yes"}


class SelectiveError(Exception):
    pass


def test_map_capability():
    raw = os.environ.get("SELECTIVE_TEST_MAP_DISABLE", "")
    if raw.lower() in DISABLED:
        return "0"
    return "select"


def test_map_record():
    return os.environ.get("SELECTIVE_TEST_MAP_RECORD", "")


def load_build_env(binary):
    proc = subprocess.run(
        [binary, "--build-env"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise SelectiveError(detail or "transport --build-env failed")
    try:
        env = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise SelectiveError("transport --build-env emitted invalid JSON") from exc
    missing = []
    for key, name in REQUIRED:
        value = env.get(key)
        if value is None or value == "":
            missing.append(name)
    if missing:
        raise SelectiveError(
            "Missing required environment variables: " + ", ".join(missing)
        )
    host = env.get("host") or ""
    if not (host.startswith("ws://") or host.startswith("wss://")):
        raise SelectiveError("Invalid host: " + host)
    return env


def runner_id_of(env):
    raw = env.get("runner_id") or ""
    if not raw.strip():
        raw = "selgen-" + secrets.token_hex(4)
    return "".join(char if char.isalnum() or char in "-_." else "_" for char in raw)


def metadata_of(env):
    return {key: value for key, value in env.items() if key not in METADATA_OMIT}


def num_commits_of(env):
    raw = env.get("num_commits")
    try:
        if raw is None or raw == "":
            return 1000
        return int(raw)
    except (TypeError, ValueError):
        return 1000
