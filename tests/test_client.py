import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import urlparse

import pytest

from tests.harness import MockServer
from tests.harness import Player
from tests.harness import client_env
from tests.harness import init_git
from tests.harness import spawn_client
from tests.harness import transport_bin
from tests.harness import write_conformance_suite

def _run_suite(tmp_path, steps, extra_env=None, args=None, git=False):
    root = tmp_path / "suite"
    root.mkdir()
    flake = tmp_path / "flake"
    finish = tmp_path / "finish"
    env_extra = write_conformance_suite(root, flake, finish)
    if extra_env:
        env_extra.update(extra_env)
    if git:
        init_git(root)
    server = MockServer()
    proc = None
    try:
        env = client_env(server, **env_extra)
        proc = spawn_client(root, args or ["pytest", "tests"], env)
        player = Player(server, proc)
        player.play(steps)
        return player, server, finish
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
        server.close()


def _assert_identity(server):
    if not server.uris:
        return
    first = server.uris[0]
    assert "reconnect=true" not in first
    query = parse_qs(urlparse(first).query, keep_blank_values=True)
    metadata = json.loads(query["metadata"][0])
    assert isinstance(metadata, dict)
    assert "api_key" not in metadata
    assert query["language"][0] == "python"
    assert query["framework"][0] == "pytest"
    assert query["test_map"][0] == "record"
    assert query["runner_id"][0] == "runner-1"
    assert query["test_map_record"][0] == ""
    assert query["remove_failed"][0] == "true"


def test_record_without_ctracer_warns_once_and_uploads_nothing(tmp_path):
    steps = [
        {"op": "send", "message": {"command": "connected"}},
        {
            "op": "send",
            "message": {
                "command": "configure_test_map",
                "tree": True,
                "record": True,
                "allocations": True,
            },
        },
        {"op": "send", "message": {"command": "test_manifest"}},
        {"op": "expect_manifest", "tree": True, "ids": ["case-pass", "case-fail", "case-pend", "case-flake"]},
        {"op": "send", "message": {"command": "configure_test_map", "tree": False, "record": True}},
        {"op": "send", "message": {"command": "run_test_cases", "test_case_ids": ["case-pass"]}},
        {"op": "expect_result", "id": "case-pass", "status": "passed"},
        {"op": "send", "message": {"command": "close", "exit_status": None}},
        {"op": "expect_report", "connection_retries": 0},
        {"op": "expect_exit", "code": 0},
    ]
    player, server, _finish = _run_suite(tmp_path, steps, git=True, extra_env={"COVERAGE_CORE": "pytrace"})
    assert parse_qs(urlparse(server.uris[0]).query)["test_map"] == ["select"]
    assert server.http_requests == []
    assert "".join(player.proc.stderr_lines).count("tracer") == 1


def test_log_progress_and_one_reject_reason(tmp_path):
    steps = [
        {"op": "send", "message": {"command": "connected"}},
        {"op": "send", "message": {"command": "print_notice", "message": "Runner ID: runner-1"}},
        {"op": "send", "message": {"command": "test_manifest"}},
        {"op": "expect_manifest", "tree": False, "ids": ["case-pass", "case-fail", "case-pend", "case-flake"]},
        {"op": "send", "message": {"command": "run_test_cases", "test_case_ids": ["case-pass", "case-fail"]}},
        {"op": "expect_result", "id": "case-pass", "status": "passed"},
        {"op": "expect_result", "id": "case-fail", "status": "failed"},
        {"op": "send", "message": {"command": "print_notice", "message": "rejected: nope"}},
        {"op": "send", "message": {"command": "close", "exit_status": 1, "message": "rejected: nope"}},
        {"op": "expect_report", "connection_retries": 0},
        {"op": "expect_exit", "code": 1},
    ]
    player, _server, _finish = _run_suite(tmp_path, steps, args=["--log", "pytest", "tests"])
    stdout = "".join(player.proc.stdout_lines)
    assert stdout.count("|____/") == 1
    assert "  Runner ID: runner-1" in stdout
    assert stdout.count("rejected: nope") == 1
    assert "case-pass ." in stdout and "case-fail F" in stdout
    assert "%]" not in stdout
    log = (tmp_path / "suite" / "log" / "runner-1.log").read_text()
    assert "Sending Response: test_manifest" in log
    assert "Sending Response: test_case_result: case-fail" in log
    assert "Received Command: {'command': 'close'" in log


@pytest.mark.parametrize("remove, code", [(False, 1), (True, 0)])
def test_failure_survives_a_redial_until_removed(tmp_path, remove, code):
    steps = [
        {"op": "send", "message": {"command": "connected"}},
        {"op": "send", "message": {"command": "test_manifest"}},
        {"op": "expect_manifest", "tree": False, "ids": ["case-pass", "case-fail", "case-pend", "case-flake"]},
        {"op": "send", "message": {"command": "run_test_cases", "test_case_ids": ["case-fail"]}},
        {"op": "expect_result", "id": "case-fail", "status": "failed"},
        {"op": "drop"},
        {"op": "expect_reconnect"},
        {"op": "send", "message": {"command": "connected"}},
    ]
    if remove:
        steps.append({"op": "send", "message": {"command": "remove_failed_test_case_result", "test_case_id": "case-fail"}})
    steps += [
        {"op": "send", "message": {"command": "close", "exit_status": None}},
        {"op": "expect_report", "connection_retries": 1},
        {"op": "expect_exit", "code": code},
    ]
    _player, server, _finish = _run_suite(tmp_path, steps)
    assert len(server.uris) == 2
    assert all(parse_qs(urlparse(uri).query)["remove_failed"] == ["true"] for uri in server.uris)


FAST_RETRIES = str(Path(__file__).resolve().parent / "fast_retries")


def _plugin_dir(tmp_path, name, body):
    folder = tmp_path / "plugins"
    folder.mkdir(exist_ok=True)
    (folder / (name + ".py")).write_text(body)
    return FAST_RETRIES + os.pathsep + str(folder)


def test_plugin_group_fixtures_are_not_hook_time(tmp_path):
    path = _plugin_dir(
        tmp_path,
        "slowplugin",
        "import time\nimport pytest\n\n@pytest.fixture(autouse=True, scope='module')\ndef slow_plugin_fixture():\n    time.sleep(0.2)\n    yield\n    time.sleep(0.2)\n",
    )
    root = tmp_path / "suite"
    root.mkdir()
    env_extra = write_conformance_suite(root, tmp_path / "flake", tmp_path / "finish")
    env_extra["PYTHONPATH"] = path
    server = MockServer()
    proc = None
    try:
        proc = spawn_client(root, ["pytest", "tests", "-p", "slowplugin"], client_env(server, **env_extra))
        player = Player(server, proc)
        player.step({"op": "send", "message": {"command": "connected"}})
        player.step({"op": "send", "message": {"command": "test_manifest"}})
        player.step({"op": "expect_manifest", "tree": False, "ids": ["case-pass", "case-fail", "case-pend", "case-flake"]})
        player.step({"op": "send", "message": {"command": "run_test_cases", "test_case_ids": ["case-pass"]}})
        player.step({"op": "expect_result", "id": "case-pass", "status": "passed"})
        player.step({"op": "send", "message": {"command": "close", "exit_status": None}})
        report = player.step({"op": "expect_report", "connection_retries": 0})
        player.step({"op": "expect_exit", "code": 0})
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
        server.close()
    assert report["data"] == {"connection_retries": 0}


def test_sigterm_lets_coverage_style_plugins_finish(tmp_path):
    marker = tmp_path / "loop-finished"
    path = _plugin_dir(
        tmp_path,
        "covprobe",
        "import os\nimport pytest\n\n@pytest.hookimpl(hookwrapper=True)\ndef pytest_runtestloop(session):\n    outcome = yield\n    outcome.get_result()\n    open(os.environ['LOOP_MARKER'], 'w').write('saved')\n",
    )
    steps = [
        {"op": "send", "message": {"command": "connected"}},
        {"op": "send", "message": {"command": "test_manifest"}},
        {"op": "expect_manifest", "tree": False, "ids": ["case-pass", "case-fail", "case-pend", "case-flake"]},
        {"op": "send", "message": {"command": "run_test_cases", "test_case_ids": ["case-fail"]}},
        {"op": "expect_result", "id": "case-fail", "status": "failed"},
        {"op": "signal_term"},
        {"op": "expect_terminating", "signal": "TERM"},
        {"op": "expect_exit", "code": 1},
    ]
    _run_suite(
        tmp_path,
        steps,
        extra_env={"PYTHONPATH": path, "LOOP_MARKER": str(marker)},
        args=["pytest", "tests", "-p", "covprobe"],
    )
    assert marker.read_text() == "saved"


RECORDED_PROJECT = {
    "app/__init__.py": "",
    "app/calc.py": "def add(a, b):\n    return a + b\n",
    "app/text.py": "def shout(word):\n    return word.upper()\n",
    "pytest.ini": "[pytest]\npythonpath = .\n",
    "tests/conftest.py": (
        "import subprocess\nimport pytest\n\n"
        "@pytest.fixture(scope='module')\ndef server():\n    subprocess.run(['true'], check=True)\n    yield\n"
    ),
    "tests/test_calc.py": (
        "import subprocess\nfrom app.calc import add\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n\n"
        "def test_spawns():\n    subprocess.run(['true'], check=True)\n"
    ),
    "tests/test_text.py": (
        "import multiprocessing\nfrom app.text import shout\n\n"
        "def test_shout():\n    assert shout('a') == 'A'\n\n"
        "def test_uses_server(server):\n    assert shout('b') == 'B'\n\n"
        "def test_forks_a_process():\n    process = multiprocessing.get_context('fork').Process(target=len, args=([],))\n"
        "    process.start()\n    process.join()\n"
    ),
}


def test_fragment_maps_each_test_to_the_repo_files_it_ran(tmp_path):
    repo = tmp_path / "repo"
    backend = repo / "backend"
    for name, body in RECORDED_PROJECT.items():
        (backend / name).parent.mkdir(parents=True, exist_ok=True)
        (backend / name).write_text(body)
    init_git(repo)
    server = MockServer()
    proc = None
    try:
        proc = spawn_client(backend, ["pytest", "tests"], client_env(server, SELECTIVE_CONFORMANCE_IDS="0"))
        player = Player(server, proc)
        player.step({"op": "send", "message": {"command": "connected"}})
        player.step({"op": "send", "message": {"command": "configure_test_map", "tree": True, "record": True}})
        player.step({"op": "send", "message": {"command": "test_manifest"}})
        ids = [case["id"] for case in player.step({"op": "expect_manifest", "tree": True, "ids": None})["data"]["test_cases"]]
        player.step({"op": "send", "message": {"command": "run_test_cases", "test_case_ids": ids}})
        for test_id in ids:
            player.step({"op": "expect_result", "id": test_id, "status": "passed"})
        player.step({"op": "send", "message": {"command": "close", "exit_status": None}})
        player.step({"op": "expect_report", "connection_retries": 0})
        fragment = player.step({"op": "expect_fragment", "ids": ["tests/test_calc.py::test_add", "tests/test_text.py::test_shout"]})
        player.step({"op": "expect_exit", "code": 0})
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
        server.close()
    assert parse_qs(urlparse(server.uris[0]).query)["test_map"] == ["record"]
    deps = {test["id"]: {fragment["files"][index] for index in test["deps"]} for test in fragment["tests"]}
    assert {"backend/app/calc.py", "backend/tests/test_calc.py"} <= deps["tests/test_calc.py::test_add"]
    assert "backend/app/text.py" not in deps["tests/test_calc.py::test_add"]
    assert {"backend/app/text.py", "backend/tests/test_text.py"} <= deps["tests/test_text.py::test_shout"]
    assert "backend/app/calc.py" not in deps["tests/test_text.py::test_shout"]
    assert all((repo / path).is_file() for path in fragment["files"])


def test_tree_omitted_outside_a_checkout(tmp_path):
    steps = [
        {"op": "send", "message": {"command": "connected"}},
        {"op": "send", "message": {"command": "configure_test_map", "tree": True, "record": False}},
        {"op": "send", "message": {"command": "test_manifest"}},
        {"op": "expect_manifest", "tree": False, "ids": ["case-pass", "case-fail", "case-pend", "case-flake"]},
        {"op": "send", "message": {"command": "close", "exit_status": None}},
        {"op": "expect_report", "connection_retries": 0},
        {"op": "expect_exit", "code": 0},
    ]
    _run_suite(tmp_path, steps, git=False)


def test_remove_then_assign(tmp_path):
    steps = [
        {"op": "send", "message": {"command": "connected"}},
        {"op": "send", "message": {"command": "test_manifest"}},
        {"op": "expect_manifest", "tree": False, "ids": ["case-pass", "case-fail", "case-pend", "case-flake"]},
        {"op": "send", "message": {"command": "run_test_cases", "test_case_ids": ["case-flake"]}},
        {"op": "expect_result", "id": "case-flake", "status": "failed"},
        {"op": "send", "message": {"command": "remove_failed_test_case_result", "test_case_id": "case-flake"}},
        {"op": "send", "message": {"command": "remove_failed_test_case_result", "test_case_id": "missing"}},
        {"op": "send", "message": {"command": "run_test_cases", "test_case_ids": ["case-flake"]}},
        {"op": "expect_result", "id": "case-flake", "status": "passed"},
        {"op": "send", "message": {"command": "close", "exit_status": None}},
        {"op": "expect_report", "connection_retries": 0},
        {"op": "expect_exit", "code": 0},
    ]
    _run_suite(tmp_path, steps)


def test_uncollected_id_fails_and_batch_continues(tmp_path):
    steps = [
        {"op": "send", "message": {"command": "connected"}},
        {"op": "send", "message": {"command": "test_manifest"}},
        {"op": "expect_manifest", "tree": False, "ids": ["case-pass", "case-fail", "case-pend", "case-flake"]},
        {
            "op": "send",
            "message": {
                "command": "run_test_cases",
                "test_case_ids": ["not-a-test", "case-pass"],
            },
        },
        {"op": "expect_result", "id": "not-a-test", "status": "failed"},
        {"op": "expect_result", "id": "case-pass", "status": "passed"},
        {"op": "send", "message": {"command": "close", "exit_status": None}},
        {"op": "expect_report", "connection_retries": 0},
        {"op": "expect_exit", "code": 1},
    ]
    _run_suite(tmp_path, steps)


def _real_suite(root):
    tests = root / "tests"
    tests.mkdir(parents=True)
    (tests / "test_real.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "import pytest\n"
        "\n"
        "def _flip(name):\n"
        "    path = Path(os.environ['MARKS']) / name\n"
        "    if path.exists():\n"
        "        return\n"
        "    path.parent.mkdir(parents=True, exist_ok=True)\n"
        "    path.write_text('1')\n"
        "    assert False\n"
        "\n"
        "def test_flaky():\n"
        "    _flip('flaky')\n"
        "\n"
        "class TestTax:\n"
        "    def test_rate(self):\n"
        "        assert False\n"
        "\n"
        "@pytest.mark.parametrize('n', [1])\n"
        "def test_param(n):\n"
        "    _flip('param')\n"
    )


def _ids_ending(manifest):
    found = {}
    for case in manifest["data"]["test_cases"]:
        test_id = case["id"]
        if test_id.endswith("::test_flaky"):
            found["flaky"] = test_id
        elif test_id.endswith("::test_rate"):
            found["rate"] = test_id
        elif test_id.endswith("::test_param[1]"):
            found["param"] = test_id
    assert set(found) == {"flaky", "rate", "param"}
    return found


def _junit_suite(path):
    root = ET.parse(path).getroot()
    suite = root.find("testsuite")
    return suite if suite is not None else root


@pytest.mark.parametrize("prefix", ["", "ci"])
def test_junit_real_ids_drop_retried_and_removed_failures(tmp_path, prefix):
    root = tmp_path / "suite"
    marks = tmp_path / "marks"
    _real_suite(root)
    junit = tmp_path / "junit.xml"
    args = ["pytest", "tests", "--junitxml=" + str(junit)]
    if prefix:
        args.append("--junit-prefix=" + prefix)
    server = MockServer()
    proc = None
    try:
        env = client_env(server, SELECTIVE_CONFORMANCE_IDS="0", MARKS=str(marks))
        proc = spawn_client(root, args, env)
        player = Player(server, proc)
        player.step({"op": "send", "message": {"command": "connected"}})
        player.step({"op": "send", "message": {"command": "test_manifest"}})
        manifest = player.step({"op": "expect_manifest", "tree": False, "ids": None})
        ids = _ids_ending(manifest)
        player.step(
            {
                "op": "send",
                "message": {
                    "command": "run_test_cases",
                    "test_case_ids": [ids["flaky"], ids["rate"], ids["param"]],
                },
            }
        )
        for key in ("flaky", "rate", "param"):
            player.step({"op": "expect_result", "id": ids[key], "status": "failed"})
        player.step(
            {
                "op": "send",
                "message": {
                    "command": "remove_failed_test_case_result",
                    "test_case_id": ids["rate"],
                },
            }
        )
        player.step(
            {
                "op": "send",
                "message": {
                    "command": "run_test_cases",
                    "test_case_ids": [ids["flaky"], ids["param"]],
                },
            }
        )
        player.step({"op": "expect_result", "id": ids["flaky"], "status": "passed"})
        player.step({"op": "expect_result", "id": ids["param"], "status": "passed"})
        player.step({"op": "send", "message": {"command": "close", "exit_status": None}})
        player.step({"op": "expect_report", "connection_retries": 0})
        player.step({"op": "expect_exit", "code": 0})
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
        server.close()
    suite = _junit_suite(junit)
    assert suite.attrib["failures"] == "0"
    assert suite.attrib["errors"] == "0"
    assert suite.attrib["tests"] == "2"
    cases = suite.findall("testcase")
    names = sorted(case.attrib.get("name") for case in cases)
    assert names == ["test_flaky", "test_param[1]"]
    assert suite.find("testcase/failure") is None
    assert suite.find("testcase/error") is None
    if prefix:
        assert all(case.attrib["classname"].startswith(prefix + ".") for case in cases)


def test_junit_teardown_error_retry_counts_one_test(tmp_path):
    root = tmp_path / "suite"
    tests = root / "tests"
    tests.mkdir(parents=True)
    marks = tmp_path / "marks"
    (tests / "test_boom.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "import pytest\n"
        "\n"
        "@pytest.fixture\n"
        "def boom():\n"
        "    yield\n"
        "    path = Path(os.environ['MARKS']) / 'boom'\n"
        "    path.parent.mkdir(parents=True, exist_ok=True)\n"
        "    if path.exists():\n"
        "        return\n"
        "    path.write_text('1')\n"
        "    raise RuntimeError('teardown boom')\n"
        "\n"
        "def test_ok(boom):\n"
        "    assert True\n"
    )
    junit = tmp_path / "junit.xml"
    server = MockServer()
    proc = None
    try:
        env = client_env(server, SELECTIVE_CONFORMANCE_IDS="0", MARKS=str(marks))
        proc = spawn_client(root, ["pytest", "tests", "--junitxml=" + str(junit)], env)
        player = Player(server, proc)
        player.step({"op": "send", "message": {"command": "connected"}})
        player.step({"op": "send", "message": {"command": "test_manifest"}})
        manifest = player.step({"op": "expect_manifest", "tree": False, "ids": None})
        test_id = manifest["data"]["test_cases"][0]["id"]
        player.step(
            {"op": "send", "message": {"command": "run_test_cases", "test_case_ids": [test_id]}}
        )
        player.step({"op": "expect_result", "id": test_id, "status": "failed"})
        player.step(
            {"op": "send", "message": {"command": "run_test_cases", "test_case_ids": [test_id]}}
        )
        player.step({"op": "expect_result", "id": test_id, "status": "passed"})
        player.step({"op": "send", "message": {"command": "close", "exit_status": None}})
        player.step({"op": "expect_report", "connection_retries": 0})
        player.step({"op": "expect_exit", "code": 0})
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
        server.close()
    suite = _junit_suite(junit)
    assert suite.attrib["tests"] == "1"
    assert suite.attrib["failures"] == "0"
    assert suite.attrib["errors"] == "0"
    assert len(suite.findall("testcase")) == 1


def test_html_retry_replaces_the_failure(tmp_path):
    pytest.importorskip("pytest_html")
    root = tmp_path / "suite"
    marks = tmp_path / "marks"
    _real_suite(root)
    html = tmp_path / "report.html"
    server = MockServer()
    proc = None
    try:
        env = client_env(server, SELECTIVE_CONFORMANCE_IDS="0", MARKS=str(marks))
        proc = spawn_client(
            root,
            ["pytest", "tests", "--html=" + str(html), "--self-contained-html"],
            env,
        )
        player = Player(server, proc)
        player.step({"op": "send", "message": {"command": "connected"}})
        player.step({"op": "send", "message": {"command": "test_manifest"}})
        manifest = player.step({"op": "expect_manifest", "tree": False, "ids": None})
        flaky = _ids_ending(manifest)["flaky"]
        player.step(
            {"op": "send", "message": {"command": "run_test_cases", "test_case_ids": [flaky]}}
        )
        player.step({"op": "expect_result", "id": flaky, "status": "failed"})
        player.step(
            {"op": "send", "message": {"command": "run_test_cases", "test_case_ids": [flaky]}}
        )
        player.step({"op": "expect_result", "id": flaky, "status": "passed"})
        player.step({"op": "send", "message": {"command": "close", "exit_status": None}})
        player.step({"op": "expect_report", "connection_retries": 0})
        player.step({"op": "expect_exit", "code": 0})
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
        server.close()
    text = html.read_text()
    assert 'class="failed">0 ' in text
    assert 'class="passed">1 ' in text
    assert "test_flaky" in text


def test_group_hooks_rerun_per_assigned_test(tmp_path):
    root = tmp_path / "suite"
    pkg = root / "pkg"
    pkg.mkdir(parents=True)
    counter = tmp_path / "hooks.json"
    (pkg / "__init__.py").write_text("")
    (pkg / "count.py").write_text(
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        "\n"
        "def bump(name):\n"
        "    path = Path(os.environ['SELECTIVE_HOOK_COUNTER'])\n"
        "    data = json.loads(path.read_text()) if path.exists() else {}\n"
        "    data[name] = data.get(name, 0) + 1\n"
        "    path.write_text(json.dumps(data))\n"
    )
    (pkg / "conftest.py").write_text(
        "import pytest\n"
        "from pkg.count import bump\n"
        "\n"
        "@pytest.fixture(scope='package')\n"
        "def package_hook():\n"
        "    bump('package_setup')\n"
        "    yield\n"
        "    bump('package_teardown')\n"
        "\n"
        "@pytest.fixture(scope='module')\n"
        "def module_hook():\n"
        "    bump('module_setup')\n"
        "    yield\n"
        "    bump('module_teardown')\n"
        "\n"
        "@pytest.fixture(scope='session')\n"
        "def session_hook():\n"
        "    bump('session_setup')\n"
        "    yield\n"
        "    bump('session_teardown')\n"
    )
    (pkg / "test_group.py").write_text(
        "import pytest\n"
        "from pkg.count import bump\n"
        "\n"
        "@pytest.fixture(scope='class')\n"
        "def class_hook():\n"
        "    bump('class_setup')\n"
        "    yield\n"
        "    bump('class_teardown')\n"
        "\n"
        "class TestGroup:\n"
        "    def test_a(self, package_hook, module_hook, class_hook, session_hook):\n"
        "        assert True\n"
        "\n"
        "    def test_b(self, package_hook, module_hook, class_hook, session_hook):\n"
        "        assert True\n"
        "\n"
        "    def test_c(self, package_hook, module_hook, class_hook, session_hook):\n"
        "        assert True\n"
    )
    (root / "pytest.ini").write_text("[pytest]\npythonpath = .\n")
    server = MockServer()
    proc = None
    try:
        env = client_env(server, SELECTIVE_HOOK_COUNTER=str(counter), SELECTIVE_CONFORMANCE_IDS="0")
        proc = spawn_client(root, ["pytest", "pkg"], env)
        player = Player(server, proc)
        player.step({"op": "send", "message": {"command": "connected"}})
        player.step({"op": "send", "message": {"command": "test_manifest"}})
        manifest = player.step({"op": "expect_manifest", "tree": False, "ids": None})
        ids = [case["id"] for case in manifest["data"]["test_cases"]]
        assert len(ids) == 3
        player.step({"op": "send", "message": {"command": "run_test_cases", "test_case_ids": ids[:2]}})
        for test_id in ids[:2]:
            player.step({"op": "expect_result", "id": test_id, "status": "passed"})
        time.sleep(0.3)
        player.step({"op": "send", "message": {"command": "run_test_cases", "test_case_ids": ids[2:]}})
        player.step({"op": "expect_result", "id": ids[2], "status": "passed"})
        player.step({"op": "send", "message": {"command": "close", "exit_status": None}})
        report = player.step({"op": "expect_report", "connection_retries": 0})
        player.step({"op": "expect_exit", "code": 0})
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
        server.close()
    counts = json.loads(counter.read_text())
    assert counts["module_setup"] == 3
    assert counts["module_teardown"] == 3
    assert counts["class_setup"] == 3
    assert counts["class_teardown"] == 3
    assert counts["package_setup"] == 3
    assert counts["package_teardown"] == 3
    assert counts["session_setup"] == 1
    assert counts["session_teardown"] == 1
    assert set(report["data"]) == {"connection_retries", "seconds_in_before_all_hooks", "seconds_in_after_all_hooks"}
    assert report["data"]["seconds_in_before_all_hooks"] > 0
    assert report["data"]["seconds_in_after_all_hooks"] > 0


def test_integer_close_skips_finish(tmp_path):
    steps = [
        {"op": "send", "message": {"command": "connected"}},
        {"op": "send", "message": {"command": "run_test_cases", "test_case_ids": ["case-pass"]}},
        {"op": "expect_result", "id": "case-pass", "status": "passed"},
        {"op": "send", "message": {"command": "close", "exit_status": 0, "error_code": "run_completed"}},
        {"op": "expect_report", "connection_retries": 0},
        {"op": "expect_exit", "code": 0},
    ]
    _player, server, finish = _run_suite(tmp_path, steps)
    _assert_identity(server)
    assert not finish.exists()


def test_exec_collects_without_a_socket(tmp_path):
    root = tmp_path / "suite"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_sample.py").write_text("def test_tax():\n    assert True\n")
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("SELECTIVE_"):
            env.pop(key)
    proc = subprocess.run(
        [sys.executable, "-m", "selective_pytest", "exec", "pytest", "tests"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["test_cases"][0]["id"].endswith("test_tax")
    assert payload["test_cases"][0]["run_time"] == 0.0


def test_empty_suite_exits_1(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("SELECTIVE_"):
            env.pop(key)
    proc = subprocess.run(
        [sys.executable, "-m", "selective_pytest", "exec", "pytest", str(root)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert "no tests collected" in proc.stderr


def test_collection_error_exits_before_spawn(tmp_path):
    root = tmp_path / "suite"
    root.mkdir()
    (root / "test_bad.py").write_text("def test_ok(\n    pass\n")
    mark = tmp_path / "spawned"
    binary = tmp_path / "transport"
    binary.write_text("#!/bin/sh\ntouch \"$SELECTIVE_SPAWN_MARK\"\nexit 1\n")
    binary.chmod(0o755)
    env = os.environ.copy()
    env["SELECTIVE_TRANSPORT"] = str(binary)
    env["SELECTIVE_SPAWN_MARK"] = str(mark)
    env.update(
        {
            "SELECTIVE_API_KEY": "k",
            "SELECTIVE_HOST": "ws://127.0.0.1:9",
            "SELECTIVE_PLATFORM": "github_actions",
            "SELECTIVE_RUN_ID": "run-1",
            "SELECTIVE_RUN_ATTEMPT": "1",
            "SELECTIVE_BRANCH": "main",
            "SELECTIVE_SHA": "abc",
            "SELECTIVE_GIT_REPO": "o/r",
            "SELECTIVE_GIT_PROVIDER": "github",
        }
    )
    proc = subprocess.run(
        [sys.executable, "-m", "selective_pytest", "pytest", str(root)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2, proc.stderr
    assert not mark.exists()
    assert "during collection" in proc.stderr


def test_continue_on_collection_errors_exits_1(tmp_path):
    root = tmp_path / "suite"
    root.mkdir()
    (root / "test_good.py").write_text("def test_ok():\n    assert True\n")
    (root / "test_bad.py").write_text("import this_module_does_not_exist_xyz\n")
    steps = [
        {"op": "send", "message": {"command": "connected"}},
        {"op": "send", "message": {"command": "test_manifest"}},
        {"op": "expect_manifest", "tree": False, "ids": None},
        {"op": "send", "message": {"command": "close", "exit_status": None}},
        {"op": "expect_report", "connection_retries": 0},
        {"op": "expect_exit", "code": 1},
    ]
    server = MockServer()
    proc = None
    try:
        env = client_env(server, SELECTIVE_CONFORMANCE_IDS="0")
        proc = spawn_client(
            root,
            ["pytest", "--continue-on-collection-errors", str(root)],
            env,
        )
        player = Player(server, proc)
        manifest = None
        player.step(steps[0])
        player.step(steps[1])
        manifest = player.step(steps[2])
        ids = [case["id"] for case in manifest["data"]["test_cases"]]
        assert len(ids) == 1
        assert ids[0].endswith("test_ok")
        player.step(steps[3])
        player.step(steps[4])
        player.step(steps[5])
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
        server.close()


def test_filters_and_duplicate_and_long_ids(tmp_path):
    root = tmp_path / "suite"
    tests = root / "tests"
    tests.mkdir(parents=True)
    long_name = "test_" + ("a" * 280)
    (tests / "test_filters.py").write_text(
        "import pytest\n"
        "\n"
        "@pytest.mark.slow\n"
        "def test_slow():\n"
        "    assert True\n"
        "\n"
        "def test_fast():\n"
        "    assert True\n"
        "\n"
        "def test_other():\n"
        "    assert True\n"
        "\n"
        "def " + long_name + "():\n"
        "    assert True\n"
    )
    (tests / "conftest.py").write_text(
        "def pytest_collection_modifyitems(items):\n"
        "    fast = None\n"
        "    other = None\n"
        "    for item in items:\n"
        "        if item.name == 'test_fast':\n"
        "            fast = item\n"
        "        elif item.name == 'test_other':\n"
        "            other = item\n"
        "    if fast is not None and other is not None:\n"
        "        other._nodeid = fast.nodeid\n"
    )
    (root / "pytest.ini").write_text("[pytest]\nmarkers =\n    slow: slow\n")
    server = MockServer()
    proc = None
    try:
        env = client_env(server, SELECTIVE_CONFORMANCE_IDS="0")
        proc = spawn_client(root, ["pytest", "tests", "-m", "slow"], env)
        player = Player(server, proc)
        player.step({"op": "send", "message": {"command": "connected"}})
        player.step({"op": "send", "message": {"command": "test_manifest"}})
        manifest = player.step({"op": "expect_manifest", "tree": False, "ids": None})
        ids = [case["id"] for case in manifest["data"]["test_cases"]]
        assert ids == [next(case["id"] for case in manifest["data"]["test_cases"])]
        assert ids[0].endswith("test_slow")
        player.step({"op": "send", "message": {"command": "close", "exit_status": None}})
        player.step({"op": "expect_report", "connection_retries": 0})
        player.step({"op": "expect_exit", "code": 0})
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
        server.close()
    server = MockServer()
    proc = None
    try:
        env = client_env(server, SELECTIVE_CONFORMANCE_IDS="0")
        proc = spawn_client(root, ["pytest", "tests", "-k", "not slow"], env)
        player = Player(server, proc)
        player.step({"op": "send", "message": {"command": "connected"}})
        player.step({"op": "send", "message": {"command": "test_manifest"}})
        manifest = player.step({"op": "expect_manifest", "tree": False, "ids": None})
        ids = [case["id"] for case in manifest["data"]["test_cases"]]
        assert len(ids) == 3
        assert any(len(test_id.encode("utf-8")) <= 255 and "~" in test_id for test_id in ids)
        assert len({*ids}) == 3
        assert any("duplicate test id" in line for line in player.proc.stderr_lines)
        player.step({"op": "send", "message": {"command": "run_test_cases", "test_case_ids": ids}})
        for test_id in ids:
            player.step({"op": "expect_result", "id": test_id, "status": "passed"})
        player.step({"op": "send", "message": {"command": "close", "exit_status": None}})
        player.step({"op": "expect_report", "connection_retries": 0})
        player.step({"op": "expect_exit", "code": 0})
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
        server.close()


def test_url_stays_off_argv(tmp_path):
    log = tmp_path / "argv"
    wrapper = tmp_path / "transport"
    binary = transport_bin()
    wrapper.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--stdio\" ]; then\n"
        "  printf '%s\\n' \"$*\" > \"$SELECTIVE_ARGV_LOG\"\n"
        "  printf '%s\\n' \"$SELECTIVE_TRANSPORT_URL\" >> \"$SELECTIVE_ARGV_LOG\"\n"
        "fi\n"
        "exec \"" + binary + "\" \"$@\"\n"
    )
    wrapper.chmod(0o755)
    steps = [
        {"op": "send", "message": {"command": "connected"}},
        {"op": "send", "message": {"command": "close", "exit_status": 0}},
        {"op": "expect_report", "connection_retries": 0},
        {"op": "expect_exit", "code": 0},
    ]
    _run_suite(
        tmp_path,
        steps,
        extra_env={
            "SELECTIVE_TRANSPORT": str(wrapper),
            "SELECTIVE_ARGV_LOG": str(log),
        },
    )
    lines = log.read_text().splitlines()
    assert lines[0] == "--stdio"
    assert "api_key" not in lines[0]
    assert "api_key=conformance-key" in lines[1]
    assert "ws://127.0.0.1:" in lines[1]


def test_build_env_round_trip(tmp_path, monkeypatch):
    message = "back\\slash\tand\nline"
    steps = [
        {"op": "send", "message": {"command": "connected"}},
        {"op": "send", "message": {"command": "close", "exit_status": 0}},
        {"op": "expect_report", "connection_retries": 0},
        {"op": "expect_exit", "code": 0},
    ]
    _player, server, _finish = _run_suite(
        tmp_path,
        steps,
        extra_env={"SELECTIVE_COMMIT_MESSAGE": message},
    )
    query = parse_qs(urlparse(server.uris[0]).query)
    metadata = json.loads(query["metadata"][0])
    assert metadata["commit_message"] == message


@pytest.mark.parametrize("args, addopts", [(["-n", "2"], ""), (["-nauto"], ""), ([], "-n 2")])
def test_xdist_workers_are_rejected_the_same_way(tmp_path, args, addopts):
    pytest.importorskip("xdist")
    (tmp_path / "test_one.py").write_text("def test_ok():\n    assert True\n")
    env = {**os.environ, "PYTEST_ADDOPTS": addopts}
    proc = subprocess.run(
        [sys.executable, "-m", "selective_pytest", "pytest", *args, str(tmp_path)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 4, proc.stderr
    assert "the server assigns batches" in proc.stderr


def test_xdist_n0_is_allowed(tmp_path):
    pytest.importorskip("xdist")
    (tmp_path / "test_one.py").write_text("def test_ok():\n    assert True\n")
    env = {key: value for key, value in os.environ.items() if not key.startswith("SELECTIVE_")}
    proc = subprocess.run(
        [sys.executable, "-m", "selective_pytest", "pytest", "-n0", str(tmp_path)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert "the server assigns batches" not in proc.stderr
    assert proc.returncode == 1
    assert "An error occurred. Please rerun with --debug" in proc.stdout


def test_disable_forces_zero_and_skips_snapshot(tmp_path):
    steps = [
        {"op": "send", "message": {"command": "connected"}},
        {"op": "send", "message": {"command": "configure_test_map", "tree": True}},
        {"op": "send", "message": {"command": "test_manifest"}},
        {"op": "expect_manifest", "tree": False, "ids": ["case-pass", "case-fail", "case-pend", "case-flake"]},
        {"op": "send", "message": {"command": "close", "exit_status": None}},
        {"op": "expect_report", "connection_retries": 0},
        {"op": "expect_exit", "code": 0},
    ]
    _player, server, _finish = _run_suite(
        tmp_path,
        steps,
        extra_env={"SELECTIVE_TEST_MAP_DISABLE": "yes", "SELECTIVE_TEST_MAP_RECORD": "1"},
        git=True,
    )
    query = parse_qs(urlparse(server.uris[0]).query)
    assert query["test_map"][0] == "0"
    assert query["test_map_record"][0] == "1"


def test_missing_binary_exits_1(tmp_path, monkeypatch):
    root = tmp_path / "suite"
    root.mkdir()
    (root / "test_one.py").write_text("def test_ok():\n    assert True\n")
    env = os.environ.copy()
    env["SELECTIVE_TRANSPORT"] = str(tmp_path / "missing-transport")
    env.update(
        {
            "SELECTIVE_API_KEY": "k",
            "SELECTIVE_HOST": "ws://127.0.0.1:9",
            "SELECTIVE_PLATFORM": "github_actions",
            "SELECTIVE_RUN_ID": "1",
            "SELECTIVE_RUN_ATTEMPT": "1",
            "SELECTIVE_BRANCH": "main",
            "SELECTIVE_SHA": "abc",
            "SELECTIVE_GIT_REPO": "o/r",
            "SELECTIVE_GIT_PROVIDER": "github",
        }
    )
    proc = subprocess.run(
        [sys.executable, "-m", "selective_pytest", "pytest", str(root)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert "An error occurred. Please rerun with --debug" in proc.stdout
    assert "missing-transport, which does not exist" in proc.stdout


def test_transport_stderr_reaches_the_user(tmp_path):
    root = tmp_path / "suite"
    root.mkdir()
    (root / "test_one.py").write_text("def test_ok():\n    assert True\n")
    env_json = (
        '{"host": "ws://127.0.0.1:9", "api_key": "k", "platform": "local", "run_id": "1", '
        '"run_attempt": "1", "branch": "main", "sha": "abc", "git_repo_full_name": "o/r", '
        '"git_provider": "github"}'
    )
    fake = tmp_path / "transport"
    fake.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        '  --help) echo "usage: transport --stdio" ;;\n'
        "  --build-env) echo '" + env_json + "' ;;\n"
        '  --stdio) echo "websocket pong timeout" >&2; exit 1 ;;\n'
        "esac\n"
    )
    fake.chmod(0o755)
    server = MockServer()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "selective_pytest", "pytest", str(root)],
            env=client_env(server, SELECTIVE_TRANSPORT=str(fake)),
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        server.close()
    assert proc.returncode == 1
    assert "websocket pong timeout" in proc.stderr
    assert "Too many retries" in proc.stdout


def _git(cwd, *args):
    subprocess.check_call(["git", *args], cwd=cwd, stdout=subprocess.DEVNULL)


@pytest.mark.parametrize("subdir", ["", "backend"])
def test_diff_and_correlate_ride_on_the_manifest(tmp_path, subdir):
    repo = tmp_path / "suite"
    work = repo / subdir
    tests = work / "tests"
    tests.mkdir(parents=True)
    (work / "billing.py").write_text("RATE = 1\n")
    (tests / "test_billing.py").write_text("def test_tax():\n    assert True\n")
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", str(bare))
    _git(repo, "init")
    _git(repo, "config", "user.email", "tests@example.com")
    _git(repo, "config", "user.name", "tests")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "tax")
    _git(repo, "branch", "-M", "main")
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "-u", "origin", "main")
    (tests / "test_billing.py").write_text("def test_tax():\n    assert 1 == 1\n")
    steps = [
        {"op": "send", "message": {"command": "connected"}},
        {"op": "send", "message": {"command": "test_manifest"}},
        {"op": "expect_manifest", "tree": False, "ids": None},
        {"op": "send", "message": {"command": "close", "exit_status": None}},
        {"op": "expect_report", "connection_retries": 0},
        {"op": "expect_exit", "code": 0},
    ]
    server = MockServer()
    proc = None
    try:
        env = client_env(
            server,
            SELECTIVE_CONFORMANCE_IDS="0",
            SELECTIVE_TARGET_BRANCH="main",
            SELECTIVE_NUM_COMMITS="20",
        )
        proc = spawn_client(work, ["pytest", "tests"], env)
        player = Player(server, proc)
        player.step(steps[0])
        player.step(steps[1])
        manifest = player.step(steps[2])
        player.step(steps[3])
        player.step(steps[4])
        player.step(steps[5])
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
        server.close()
    data = manifest["data"]
    assert data["modified_test_files"] == ["tests/test_billing.py"]
    hint = data["correlated_files"]
    assert hint["correlated_files"]["billing.py"]["tests/test_billing.py"] >= 1


def test_bare_pytest_reports_modified_test_files(tmp_path):
    work = tmp_path / "suite"
    tests = work / "tests"
    tests.mkdir(parents=True)
    (tests / "test_billing.py").write_text("def test_tax():\n    assert True\n")
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", str(bare))
    _git(work, "init")
    _git(work, "config", "user.email", "tests@example.com")
    _git(work, "config", "user.name", "tests")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "tax")
    _git(work, "branch", "-M", "main")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-u", "origin", "main")
    (tests / "test_billing.py").write_text("def test_tax():\n    assert 1 == 1\n")
    server = MockServer()
    proc = None
    try:
        env = client_env(
            server,
            SELECTIVE_CONFORMANCE_IDS="0",
            SELECTIVE_TARGET_BRANCH="main",
        )
        proc = spawn_client(work, ["pytest"], env)
        player = Player(server, proc)
        player.step({"op": "send", "message": {"command": "connected"}})
        player.step({"op": "send", "message": {"command": "test_manifest"}})
        manifest = player.step({"op": "expect_manifest", "tree": False, "ids": None})
        player.step({"op": "send", "message": {"command": "close", "exit_status": None}})
        player.step({"op": "expect_report", "connection_retries": 0})
        player.step({"op": "expect_exit", "code": 0})
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
        server.close()
    assert manifest["data"]["modified_test_files"] == ["tests/test_billing.py"]


def test_missing_git_still_sends_the_manifest(tmp_path):
    root = tmp_path / "suite"
    root.mkdir()
    (root / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    empty = tmp_path / "bin"
    empty.mkdir()
    server = MockServer()
    proc = None
    try:
        env = client_env(
            server,
            SELECTIVE_CONFORMANCE_IDS="0",
            SELECTIVE_TARGET_BRANCH="main",
        )
        env["PATH"] = str(empty)
        proc = spawn_client(root, ["pytest"], env)
        player = Player(server, proc)
        player.step({"op": "send", "message": {"command": "connected"}})
        player.step({"op": "send", "message": {"command": "test_manifest"}})
        manifest = player.step({"op": "expect_manifest", "tree": False, "ids": None})
        player.step({"op": "send", "message": {"command": "close", "exit_status": None}})
        player.step({"op": "expect_report", "connection_retries": 0})
        player.step({"op": "expect_exit", "code": 0})
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
        server.close()
    assert "modified_test_files" not in manifest["data"]
    assert "unable to diff" in "".join(proc.stderr_lines)


def _wait_uris(server, proc, count):
    deadline = time.time() + 15
    while time.time() < deadline:
        if proc.poll() is not None:
            raise AssertionError((proc.returncode, proc.stderr_lines[-20:], proc.stdout_lines[-10:]))
        if len(server.uris) >= count:
            return
        time.sleep(0.05)
    raise AssertionError((len(server.uris), proc.stderr_lines[-20:]))


def test_long_duplicate_ids_stay_within_255(tmp_path):
    root = tmp_path / "suite"
    tests = root / "tests"
    tests.mkdir(parents=True)
    native = "tests/test_long.py::test_" + ("x" * 300)
    (tests / "test_long.py").write_text(
        "def test_a():\n"
        "    assert True\n"
        "\n"
        "def test_b():\n"
        "    assert True\n"
        "\n"
        "def test_c():\n"
        "    assert True\n"
    )
    (tests / "conftest.py").write_text(
        "def pytest_collection_modifyitems(items):\n"
        "    native = " + repr(native) + "\n"
        "    for item in items:\n"
        "        item._nodeid = native\n"
    )
    server = MockServer()
    proc = None
    try:
        env = client_env(server, SELECTIVE_CONFORMANCE_IDS="0")
        proc = spawn_client(root, ["pytest", "tests"], env)
        player = Player(server, proc)
        player.step({"op": "send", "message": {"command": "connected"}})
        player.step({"op": "send", "message": {"command": "test_manifest"}})
        manifest = player.step({"op": "expect_manifest", "tree": False, "ids": None})
        ids = [case["id"] for case in manifest["data"]["test_cases"]]
        assert len(ids) == 3
        assert len(set(ids)) == 3
        assert all(len(test_id.encode("utf-8")) <= 255 for test_id in ids)
        assert any("duplicate test id" in line for line in player.proc.stderr_lines)
        player.step({"op": "send", "message": {"command": "run_test_cases", "test_case_ids": ids}})
        for test_id in ids:
            player.step({"op": "expect_result", "id": test_id, "status": "passed"})
        player.step({"op": "send", "message": {"command": "close", "exit_status": None}})
        player.step({"op": "expect_report", "connection_retries": 0})
        player.step({"op": "expect_exit", "code": 0})
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
        server.close()


def test_maxfail_keeps_the_session_fixture(tmp_path):
    root = tmp_path / "suite"
    tests = root / "tests"
    tests.mkdir(parents=True)
    count = tmp_path / "count"
    (tests / "test_warm.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "import pytest\n"
        "\n"
        "@pytest.fixture(scope='session')\n"
        "def db():\n"
        "    path = Path(os.environ['COUNT'])\n"
        "    current = int(path.read_text()) if path.exists() else 0\n"
        "    path.write_text(str(current + 1))\n"
        "    yield\n"
        "\n"
        "def test_first(db):\n"
        "    assert False\n"
        "\n"
        "def test_second(db):\n"
        "    assert True\n"
        "\n"
        "def test_third(db):\n"
        "    assert True\n"
    )
    for args in (["pytest", "tests", "-x"], ["pytest", "tests", "--maxfail=1"]):
        count.unlink(missing_ok=True)
        server = MockServer()
        proc = None
        try:
            env = client_env(server, SELECTIVE_CONFORMANCE_IDS="0", COUNT=str(count))
            proc = spawn_client(root, args, env)
            player = Player(server, proc)
            player.step({"op": "send", "message": {"command": "connected"}})
            player.step({"op": "send", "message": {"command": "test_manifest"}})
            manifest = player.step({"op": "expect_manifest", "tree": False, "ids": None})
            ids = [case["id"] for case in manifest["data"]["test_cases"]]
            assert len(ids) == 3
            player.step(
                {"op": "send", "message": {"command": "run_test_cases", "test_case_ids": ids}}
            )
            for test_id in ids:
                status = "failed" if test_id.endswith("::test_first") else "passed"
                player.step({"op": "expect_result", "id": test_id, "status": status})
            player.step({"op": "send", "message": {"command": "close", "exit_status": None}})
            player.step({"op": "expect_report", "connection_retries": 0})
            player.step({"op": "expect_exit", "code": 1})
        finally:
            if proc is not None and proc.poll() is None:
                proc.kill()
            server.close()
        assert count.read_text() == "1"
        assert any("ignoring -x/--maxfail" in line for line in proc.stderr_lines)


DROP_FAILED = """#!/usr/bin/env python3
import os
import subprocess
import sys
binary = BINARY
if "--stdio" not in sys.argv:
    os.execv(binary, [binary, *sys.argv[1:]])
child = subprocess.Popen([binary, *sys.argv[1:]], stdin=subprocess.PIPE)
buf = b""
while True:
    chunk = os.read(0, 1024)
    if not chunk:
        break
    buf += chunk
    if b"\\n" not in buf and (b'"status":"failed"' in buf or len(buf) >= 8192):
        child.kill()
        child.wait()
        os._exit(1)
    while b"\\n" in buf:
        line, buf = buf.split(b"\\n", 1)
        child.stdin.write(line + b"\\n")
        child.stdin.flush()
child.stdin.close()
child.wait()
"""


def test_lost_send_still_replaces_the_reporter(tmp_path):
    root = tmp_path / "suite"
    tests = root / "tests"
    tests.mkdir(parents=True)
    marks = tmp_path / "marks"
    (tests / "test_real.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "\n"
        "def test_flaky():\n"
        "    path = Path(os.environ['MARKS']) / 'flaky'\n"
        "    if path.exists():\n"
        "        assert True\n"
        "        return\n"
        "    path.parent.mkdir(parents=True, exist_ok=True)\n"
        "    path.write_text('1')\n"
        "    assert False, 'x' * 200000\n"
    )
    wrapper = tmp_path / "drop_failed.py"
    wrapper.write_text(DROP_FAILED.replace("BINARY", json.dumps(transport_bin())))
    wrapper.chmod(0o755)
    junit = tmp_path / "junit.xml"
    server = MockServer()
    proc = None
    try:
        env = client_env(
            server,
            SELECTIVE_CONFORMANCE_IDS="0",
            SELECTIVE_TRANSPORT=str(wrapper),
            MARKS=str(marks),
        )
        proc = spawn_client(root, ["pytest", "tests", "--junitxml=" + str(junit)], env)
        player = Player(server, proc)
        player.step({"op": "send", "message": {"command": "connected"}})
        player.step({"op": "send", "message": {"command": "test_manifest"}})
        manifest = player.step({"op": "expect_manifest", "tree": False, "ids": None})
        flaky = manifest["data"]["test_cases"][0]["id"]
        assert flaky.endswith("::test_flaky")
        player.step(
            {"op": "send", "message": {"command": "run_test_cases", "test_case_ids": [flaky]}}
        )
        _wait_uris(server, proc, 2)
        time.sleep(0.2)
        player.step({"op": "send", "message": {"command": "connected"}})
        player.step(
            {"op": "send", "message": {"command": "run_test_cases", "test_case_ids": [flaky]}}
        )
        player.step({"op": "expect_result", "id": flaky, "status": "passed"})
        player.step({"op": "send", "message": {"command": "close", "exit_status": None}})
        player.step({"op": "expect_report", "connection_retries": 1})
        player.step({"op": "expect_exit", "code": 0})
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
        server.close()
    suite = _junit_suite(junit)
    assert suite.attrib["failures"] == "0"
    assert suite.attrib["tests"] == "1"
    assert [case.attrib.get("name") for case in suite.findall("testcase")] == ["test_flaky"]
    assert suite.find("testcase/failure") is None


PAYLOAD_SUITE = """import pytest


def helper():
    assert 1 == 2


class TestTax:
    def test_rate(self):
        assert False


def test_helper():
    helper()


@pytest.mark.xfail(reason="known bug 123")
def test_xfail():
    assert False


def test_xfail_imperative():
    pytest.xfail("imperative xfail")


@pytest.mark.xfail(reason="strict pass", strict=True)
def test_xpass_strict():
    pass
"""


@pytest.mark.parametrize("tb", ["--tb=no", "--tb=long"])
def test_failure_payloads_come_from_the_exception_not_the_report(tmp_path, tb):
    root = tmp_path / "suite"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_payload.py").write_text(PAYLOAD_SUITE)
    server = MockServer()
    proc = None
    try:
        proc = spawn_client(root, ["pytest", "tests", tb], client_env(server, SELECTIVE_CONFORMANCE_IDS="0"))
        player = Player(server, proc)
        player.step({"op": "send", "message": {"command": "connected"}})
        player.step({"op": "send", "message": {"command": "test_manifest"}})
        cases = player.step({"op": "expect_manifest", "tree": False, "ids": None})["data"]["test_cases"]
        ids = [case["id"] for case in cases]
        player.step({"op": "send", "message": {"command": "run_test_cases", "test_case_ids": ids}})
        statuses = ["failed", "failed", "pending", "pending", "failed"]
        rate, helper, xfail, imperative, strict = [
            player.step({"op": "expect_result", "id": test_id, "status": status})["data"]
            for test_id, status in zip(ids, statuses)
        ]
        player.step({"op": "send", "message": {"command": "close", "exit_status": None}})
        player.step({"op": "expect_report", "connection_retries": 0})
        player.step({"op": "expect_exit", "code": 1})
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
        server.close()
    assert rate["failure_formatted_backtrace"] == ["tests/test_payload.py:10:in test_rate"]
    assert rate["failure_message_lines"] == ["assert False"]
    assert [case["full_description"] for case in cases] == ids
    assert rate["full_description"] == "tests/test_payload.py::TestTax::test_rate"
    assert helper["failure_formatted_backtrace"] == [
        "tests/test_payload.py:5:in helper",
        "tests/test_payload.py:14:in test_helper",
    ]
    assert helper["failure_message_lines"][0] == "assert 1 == 2"
    assert xfail["pending_message"] == "known bug 123"
    assert imperative["pending_message"] == "imperative xfail"
    line = cases[4]["line_number"]
    assert strict["failure_formatted_backtrace"] == ["tests/test_payload.py:" + str(line) + ":in test_xpass_strict"]
    assert strict["failure_message_lines"] == ["[XPASS(strict)] strict pass"]


def _exit_during(tmp_path, body, debug=False):
    root = tmp_path / "suite"
    root.mkdir()
    (root / "test_exit.py").write_text(body)
    server = MockServer()
    proc = None
    args = ["pytest", "."]
    if debug:
        args = ["--debug", *args]
    try:
        env = client_env(server, SELECTIVE_CONFORMANCE_IDS="0")
        proc = spawn_client(root, args, env)
        player = Player(server, proc)
        player.step({"op": "send", "message": {"command": "connected"}})
        player.step({"op": "send", "message": {"command": "test_manifest"}})
        manifest = player.step({"op": "expect_manifest", "tree": False, "ids": None})
        test_id = manifest["data"]["test_cases"][0]["id"]
        player.step(
            {"op": "send", "message": {"command": "run_test_cases", "test_case_ids": [test_id]}}
        )
        player.step({"op": "expect_exit", "code": 2})
        return proc
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
        server.close()


def test_pytest_exit_in_a_test_stays_interrupted(tmp_path):
    _exit_during(
        tmp_path,
        "import pytest\n"
        "def test_down():\n"
        "    pytest.exit('db down')\n",
    )


def test_pytest_exit_in_a_fixture_stays_interrupted(tmp_path):
    _exit_during(
        tmp_path,
        "import pytest\n"
        "@pytest.fixture\n"
        "def db():\n"
        "    pytest.exit('db down')\n"
        "def test_needs_db(db):\n"
        "    assert True\n",
    )


def test_unknown_command_stays_internal_error(tmp_path):
    root = tmp_path / "suite"
    root.mkdir()
    (root / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    server = MockServer()
    proc = None
    try:
        env = client_env(server, SELECTIVE_CONFORMANCE_IDS="0")
        proc = spawn_client(root, ["--debug", "pytest", "."], env)
        player = Player(server, proc)
        player.step({"op": "send", "message": {"command": "connected"}})
        player.step({"op": "send", "message": {"command": "frobnicate"}})
        player.step({"op": "expect_exit", "code": 3})
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
        server.close()


def test_junk_frame_stays_internal_error(tmp_path):
    root = tmp_path / "suite"
    root.mkdir()
    (root / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    server = MockServer()
    proc = None
    try:
        env = client_env(server, SELECTIVE_CONFORMANCE_IDS="0")
        proc = spawn_client(root, ["--debug", "pytest", "."], env)
        player = Player(server, proc)
        player.step({"op": "send", "message": {"command": "connected"}})
        player.ensure()
        server.send([1, 2, 3])
        player.step({"op": "expect_exit", "code": 3})
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
        server.close()


def test_print_message_goes_to_stdout(tmp_path):
    root = tmp_path / "suite"
    root.mkdir()
    (root / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    server = MockServer()
    proc = None
    try:
        env = client_env(server, SELECTIVE_CONFORMANCE_IDS="0")
        proc = spawn_client(root, ["pytest", "."], env)
        player = Player(server, proc)
        player.step({"op": "send", "message": {"command": "connected"}})
        player.step({"op": "send", "message": {"command": "print_message", "message": "map is stale"}})
        player.step({"op": "send", "message": {"command": "close", "exit_status": None}})
        player.step({"op": "expect_report", "connection_retries": 0})
        player.step({"op": "expect_exit", "code": 0})
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
        server.close()
    stdout = "".join(proc.stdout_lines)
    stderr = "".join(proc.stderr_lines)
    assert "WARNING: map is stale" in stdout
    assert "map is stale" not in stderr


def test_non_object_frame_is_ignored(tmp_path):
    root = tmp_path / "suite"
    root.mkdir()
    (root / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    server = MockServer()
    proc = None
    try:
        env = client_env(server, SELECTIVE_CONFORMANCE_IDS="0")
        proc = spawn_client(root, ["pytest", "."], env)
        player = Player(server, proc)
        player.step({"op": "send", "message": {"command": "connected"}})
        player.ensure()
        server.send([1, 2, 3])
        player.step({"op": "send", "message": {"command": "close", "exit_status": None}})
        player.step({"op": "expect_report", "connection_retries": 0})
        player.step({"op": "expect_exit", "code": 0})
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
        server.close()


def _reject(tmp_path, args, ini="", env_extra=None):
    slot = tmp_path / "cases"
    slot.mkdir(exist_ok=True)
    root = slot / str(len(list(slot.iterdir())))
    root.mkdir()
    marker = tmp_path / "ran"
    (root / "test_body.py").write_text(
        "from pathlib import Path\n"
        "def test_body():\n"
        "    Path(" + json.dumps(str(marker)) + ").write_text('ran')\n"
        "    assert False\n"
    )
    if ini:
        (root / "pytest.ini").write_text(ini)
    proc = subprocess.run(
        [sys.executable, "-m", "selective_pytest", *args],
        cwd=root,
        env=env_extra,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert not marker.exists()
    return proc


def test_setup_flags_are_rejected(tmp_path):
    for flag in ("--setup-only", "--setup-plan", "--setuponly"):
        proc = _reject(tmp_path, ["pytest", flag, "."])
        assert proc.returncode == 4, (flag, proc.returncode, proc.stderr)
        assert "setup-only" in proc.stderr or "setup-plan" in proc.stderr
    proc = _reject(tmp_path, ["pytest", "."], ini="[pytest]\naddopts = --setup-only\n")
    assert proc.returncode == 4, proc.stderr
    assert "setup-only" in proc.stderr


def test_stepwise_is_rejected(tmp_path):
    for flag in ("--sw", "--stepwise", "--sw-skip"):
        proc = _reject(tmp_path, ["pytest", flag, "."])
        assert proc.returncode == 4, (flag, proc.returncode, proc.stderr)
        assert "--sw" in proc.stderr


def _collect_env(tmp_path, server):
    stamp = tmp_path / "spawned"
    wrapper = tmp_path / "transport"
    binary = transport_bin()
    wrapper.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--stdio\" ]; then\n"
        "  touch \"$SELECTIVE_SPAWN_STAMP\"\n"
        "fi\n"
        "exec \"" + binary + "\" \"$@\"\n"
    )
    wrapper.chmod(0o755)
    env = client_env(
        server,
        SELECTIVE_CONFORMANCE_IDS="0",
        SELECTIVE_TRANSPORT=str(wrapper),
        SELECTIVE_SPAWN_STAMP=str(stamp),
    )
    return env, stamp


@pytest.mark.parametrize("source", ["pytest.ini", "PYTEST_ADDOPTS"])
def test_addopts_collect_only_is_not_a_pass(tmp_path, source):
    root = tmp_path / "suite"
    root.mkdir()
    marker = tmp_path / "ran"
    (root / "test_body.py").write_text(
        "from pathlib import Path\n"
        "def test_body():\n"
        "    Path(" + json.dumps(str(marker)) + ").write_text('ran')\n"
        "    assert True\n"
    )
    if source == "pytest.ini":
        (root / "pytest.ini").write_text("[pytest]\naddopts = --collect-only\n")
    server = MockServer()
    try:
        env, stamp = _collect_env(tmp_path, server)
        if source == "PYTEST_ADDOPTS":
            env["PYTEST_ADDOPTS"] = "--collect-only"
        proc = subprocess.run(
            [sys.executable, "-m", "selective_pytest", "pytest", "."],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
    finally:
        server.close()
    assert proc.returncode == 1, proc.stderr
    assert "selective exec pytest" in proc.stderr
    assert not marker.exists()
    assert not stamp.exists()


@pytest.mark.parametrize("flag", ["--collect-only", "--co"])
def test_cli_collect_only_still_prints_json(tmp_path, flag):
    root = tmp_path / "suite"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_sample.py").write_text("def test_tax():\n    assert True\n")
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("SELECTIVE_"):
            env.pop(key)
    proc = subprocess.run(
        [sys.executable, "-m", "selective_pytest", "pytest", flag, "tests"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["test_cases"][0]["id"].endswith("test_tax")


def test_surrogate_nodeid_is_wire_safe(tmp_path):
    root = tmp_path / "suite"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_sample.py").write_text("def test_tax():\n    assert True\n")
    (root / "tests" / "conftest.py").write_text(
        "def pytest_collection_modifyitems(items):\n"
        "    for item in items:\n"
        "        item._nodeid = item.nodeid + '\\ud800'\n"
    )
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("SELECTIVE_"):
            env.pop(key)
    proc = subprocess.run(
        [sys.executable, "-m", "selective_pytest", "exec", "pytest", "tests"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    test_id = payload["test_cases"][0]["id"]
    assert len(test_id.encode("utf-8")) <= 255
    assert "~" in test_id
