import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.harness import MockServer
from tests.harness import Player
from tests.harness import client_env
from tests.harness import spawn_client

ROOT = Path(__file__).resolve().parents[1] / "examples" / "django_skeleton"


def _django_ready():
    pytest.importorskip("django")
    pytest.importorskip("pytest_django")


BATCHES = [
    ["test_rolls_back_first", "test_sees_class_data", "test_round_half_up"],
    ["test_rolls_back_second", "test_transactional", "test_class_data_is_rolled_back", "test_round_half_down"],
    ["test_rolls_back_first", "test_sees_class_data", "test_exempt"],
]


def test_django_db_is_created_once_and_rolls_back_per_test_across_batches(tmp_path):
    _django_ready()
    counter = tmp_path / "hooks.json"
    server = MockServer()
    proc = None
    try:
        env = client_env(
            server,
            SELECTIVE_HOOK_COUNTER=str(counter),
            SELECTIVE_CONFORMANCE_IDS="0",
        )
        proc = spawn_client(ROOT, ["pytest", "billing"], env)
        player = Player(server, proc)
        player.step({"op": "send", "message": {"command": "connected"}})
        player.step({"op": "send", "message": {"command": "test_manifest"}})
        manifest = player.step({"op": "expect_manifest", "tree": False, "ids": None})
        by_name = {case["id"].rsplit("::", 1)[1]: case["id"] for case in manifest["data"]["test_cases"]}
        for names in BATCHES:
            ids = [by_name[name] for name in names]
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
    counts = json.loads(counter.read_text())
    assert counts["db_setup"] == 1
    assert counts["setUpTestData"] == 3
    assert counts["module_setup"] == counts["module_teardown"] == 3
    assert counts["session_setup"] == counts["session_teardown"] == 1


def test_django_failed_test_exits_1(tmp_path):
    _django_ready()
    server = MockServer()
    proc = None
    try:
        env = client_env(server, SELECTIVE_CONFORMANCE_IDS="0")
        proc = spawn_client(ROOT, ["pytest", "billing"], env)
        player = Player(server, proc)
        player.step({"op": "send", "message": {"command": "connected"}})
        player.step({"op": "send", "message": {"command": "test_manifest"}})
        manifest = player.step({"op": "expect_manifest", "tree": False, "ids": None})
        failing = next(
            case["id"]
            for case in manifest["data"]["test_cases"]
            if case["id"].endswith("test_missing_rate")
        )
        player.step(
            {"op": "send", "message": {"command": "run_test_cases", "test_case_ids": [failing]}}
        )
        frame = player.next_frame()
        assert frame["type"] == "test_case_result"
        assert frame["data"]["id"] == failing
        assert frame["data"]["status"] == "failed"
        assert frame["data"]["failure_formatted_backtrace"][0].startswith("billing/")
        player.step({"op": "send", "message": {"command": "close", "exit_status": None}})
        player.step({"op": "expect_report", "connection_retries": 0})
        player.step({"op": "expect_exit", "code": 1})
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
        server.close()


def test_django_maxfail_keeps_the_session_fixture(tmp_path):
    _django_ready()
    counter = tmp_path / "hooks.json"
    server = MockServer()
    proc = None
    try:
        env = client_env(
            server,
            SELECTIVE_HOOK_COUNTER=str(counter),
            SELECTIVE_CONFORMANCE_IDS="0",
        )
        proc = spawn_client(ROOT, ["pytest", "billing/test_billing.py", "-x"], env)
        player = Player(server, proc)
        player.step({"op": "send", "message": {"command": "connected"}})
        player.step({"op": "send", "message": {"command": "test_manifest"}})
        manifest = player.step({"op": "expect_manifest", "tree": False, "ids": None})
        ids = [case["id"] for case in manifest["data"]["test_cases"]]
        assert len(ids) == 4
        player.step(
            {"op": "send", "message": {"command": "run_test_cases", "test_case_ids": ids}}
        )
        for test_id in ids:
            frame = player.next_frame()
            assert frame["type"] == "test_case_result"
            assert frame["data"]["id"] == test_id
            if test_id.endswith("test_missing_rate"):
                assert frame["data"]["status"] == "failed"
                assert frame["data"]["failure_formatted_backtrace"][0].startswith("billing/")
            else:
                assert frame["data"]["status"] == "passed"
        player.step({"op": "send", "message": {"command": "close", "exit_status": None}})
        player.step({"op": "expect_report", "connection_retries": 0})
        player.step({"op": "expect_exit", "code": 1})
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
        server.close()
    counts = json.loads(counter.read_text())
    assert counts["session_setup"] == 1
    assert counts["module_setup"] == 4
    assert any("ignoring -x/--maxfail" in line for line in proc.stderr_lines)


def test_manage_py_is_not_the_runner():
    _django_ready()
    proc = subprocess.run(
        [sys.executable, str(ROOT / "manage.py"), "check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
