# selective-python-pytest

Selective pytest runner. One pytest process collects the suite, then runs only the batches the Selective server assigns. Session fixtures and Django setup stay up for the life of the process. Module and class fixtures run again for every assigned test. A package fixture does too when it is defined in that package's conftest (the directory has `__init__.py`). A package fixture in a test module, or in a directory without `__init__.py`, stays up the way plain pytest runs it.

This package is the sidecar face. It speaks to `transport --stdio` and does not open its own WebSocket. Nothing here is published to a package registry yet.

## How it works

`selective pytest` loads a plugin that replaces `pytest_runtestloop`. Collection happens once. A failed `CollectReport` exits 2 before any manifest, unless `--continue-on-collection-errors`, which still runs the collected tests and exits 1. An empty collection exits 1.

The plugin spawns `transport --stdio` with the socket URL in `SELECTIVE_TRANSPORT_URL`. The key never goes on argv. Required `SELECTIVE_*` values come from `transport --build-env`. The runner advertises `test_map=record` when coverage.py's C tracer starts (`selective` sets `COVERAGE_CORE=ctrace` unless it is already set), otherwise `select`, and `0` with `SELECTIVE_TEST_MAP_DISABLE=1`. It also advertises `remove_failed=true` on every connection, including redials: it applies `remove_failed_test_case_result` in arrival order and keeps its failures across a redial. `SELECTIVE_TEST_MAP_RECORD` is forwarded unchanged. On `record: true` the runner records each test's files under the repository root as a coverage context. It reuses pytest-cov's tracer when pytest-cov is running, and ignores the virtualenv, site-packages and `SELECTIVE_TEST_MAP_IGNORE`. A test that starts a process, directly or through a fixture it uses, is left out of the map, so it keeps running. At close it uploads the fragment through `transport --upload-fragment`. Without the C tracer, a `record: true` warns once and uploads nothing.

After `connected`, the runner sends the manifest. When the server asks for a tree, the manifest includes the JSON from `transport --snapshot`. Each `run_test_cases` calls `pytest_runtest_protocol` with `nextitem` set to the session, so non-session fixtures tear down and session fixtures do not. A repeated id drops the earlier junitxml, pytest-html, and terminal report before the new run, including when the first result never reached the server. `remove_failed_test_case_result` drops a failed result. A normal finish is 0 or 1 from that per-id store. Pytest's own statuses 2 through 4 are left as pytest set them. An integer `close.exit_status` is the process status and skips finish hooks. `-x` and `--maxfail` are ignored so session fixtures are not torn down. SIGINT and SIGTERM send `terminating`, stop the transport, and end the session through pytest, so session teardown and plugins such as pytest-cov still finish; the exit status is the per-id store's, or an earlier integer close's. `report_at_finish` carries Ruby's `seconds_in_before_all_hooks` and `seconds_in_after_all_hooks`: the setup and teardown time of package, module and class fixtures defined in the project, not in plugins. `--sw`, `--setup-only`, and `--setup-plan` are rejected.

`selective exec pytest` collects and prints `{"test_cases":[...]}` without a socket and without CI env. `selective pytest --collect-only` does the same. `addopts` and `PYTEST_ADDOPTS` cannot turn a run into a collect.

## How to run it

Python 3.9+ and pytest 7.4+. CI runs Python 3.10 through 3.14 against pytest 8.4.2 and 9.1.1, Python 3.9 against pytest 7.4.4 and 8.4.2, the Django example on Django 5.2 (Python 3.10) and 6.0 (Python 3.14), and a canary on the newest pytest every Monday.

```
pip install -e ".[dev]"
export SELECTIVE_TRANSPORT=/path/to/transport
selective pytest tests
selective exec pytest tests -k tax
```

The Linux x86_64 platform wheel (`py3-none-manylinux_2_17_x86_64.musllinux_1_1_x86_64`) carries a static musl transport, so it runs on glibc and musl systems alike. `packaging/wheel.sh <transport binary> <platform tag>` builds it, and CI builds it from the pinned transport SHA. The client uses `SELECTIVE_TRANSPORT` when set, then the bundled binary, then `transport` on `PATH`. The binary must support `--stdio`. A missing binary, a transport without `--stdio`, and one that cannot execute each get their own error, and the transport's own stderr reaches the terminal. Set the usual Selective CI variables (`SELECTIVE_HOST`, `SELECTIVE_API_KEY`, `SELECTIVE_PLATFORM`, `SELECTIVE_RUN_ID`, `SELECTIVE_RUN_ATTEMPT`, `SELECTIVE_BRANCH`, `SELECTIVE_SHA`, `SELECTIVE_GIT_REPO`, `SELECTIVE_GIT_PROVIDER`). pytest-xdist workers are rejected with exit 4 whether `-n` comes from the command line or `addopts`; `-n0` is allowed.

A setup error prints a red block with the message and exits 1; `selective --debug pytest` raises it with a traceback instead. `selective --log pytest` writes every command received and every response sent to `log/<runner_id>.log`. Diff hints are relative to the directory pytest runs from, so a project in a subdirectory of the repository gets them too.

The example under `examples/django_skeleton` is a pytest-django project with database tests: `django_db`, a transactional test, and a `TestCase` with `setUpTestData`. Install its `requirements.txt` and run `selective pytest billing` from that directory.

## Tests

Build [transport](https://github.com/selectiveci/transport) and its conformance runner at `44a9c7abde5a35d869c5f71780d56e26c0c6f8bd`, using a fresh `CARGO_TARGET_DIR`:

```
cargo install --locked --git https://github.com/selectiveci/transport --rev 44a9c7abde5a35d869c5f71780d56e26c0c6f8bd --bin transport --bin transport-conformance --root /path/to/transport
```

Run the unit tests, then the transport's conformance suite against the fixture project in `conformance/`:

```
SELECTIVE_TRANSPORT=/path/to/transport/bin/transport python -m pytest
cd conformance
/path/to/transport/bin/transport-conformance run --client "python -m selective_pytest pytest tests" --fixture-ids fixture-ids.json --transport /path/to/transport/bin/transport
```
