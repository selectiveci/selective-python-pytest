import gzip
import json
import logging
import os
import signal
import subprocess
import sys
import time
from collections import deque
from urllib.parse import urlencode

import pytest

from selective_pytest import __version__
from selective_pytest.correlate import correlate
from selective_pytest.correlate import git_diff
from selective_pytest.correlate import modified_test_files
from selective_pytest.environ import SelectiveError
from selective_pytest.environ import load_build_env
from selective_pytest.environ import metadata_of
from selective_pytest.environ import num_commits_of
from selective_pytest.environ import runner_id_of
from selective_pytest.environ import test_map_capability
from selective_pytest.environ import test_map_record
from selective_pytest.snapshot import snapshot_tree
from selective_pytest.textutil import clean_json
from selective_pytest.transport import ConnectionLost
from selective_pytest.transport import StdioTransport
from selective_pytest.transport import check_stdio
from selective_pytest.transport import resolve_binary

CONNECTED_WINDOW = 30.0
BANNER = "\n".join(
    (
        " ____       _           _   _",
        "/ ___|  ___| | ___  ___| |_(_)_   _____",
        "\\___ \\ / _ \\ |/ _ \\/ __| __| \\ \\ / / _ \\",
        " ___) |  __/ |  __/ (__| |_| |\\ V /  __/",
        "|____/ \\___|_|\\___|\\___|\\__|_| \\_/ \\___|",
        "________________________________________",
    )
)
SUPPORT = "An error occurred. Please rerun with --debug\nand contact support at https://selective.ci/support\n"

log = logging.getLogger("selective_pytest")
log.propagate = False
log.addHandler(logging.NullHandler())
_banner_shown = False


class ReconnectRequested(Exception):
    pass


class Terminated(pytest.exit.Exception):
    pass


def write_stream(text, error=False):
    if not text.endswith("\n"):
        text += "\n"
    os.write(2 if error else 1, text.encode("utf-8", "replace"))


def warn(message):
    write_stream("WARNING: " + message, error=True)


def _banner():
    global _banner_shown
    shown, _banner_shown = _banner_shown, True
    return "" if shown else BANNER + "\n\n"


def _indented(text):
    return "\n".join("  " + line for line in text.splitlines())


def notice(message):
    write_stream(_indented(_banner() + message))


def report_error(message):
    write_stream(_indented("\x1b[31m\n" + _banner() + SUPPORT + "\n" + message + "\n\x1b[0m"))


def open_log(runner_id):
    os.makedirs("log", exist_ok=True)
    handler = logging.FileHandler(os.path.join("log", runner_id + ".log"), encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def retry_delay(attempt):
    return min(int(attempt), 4)


class Controller:
    def __init__(self, plugin, debug, logged):
        self.plugin = plugin
        self.debug = debug
        self.logged = logged
        self.transport = None
        self.pending = deque()
        self.retries = 0
        self.stopping = False
        self.tree = False
        self.record_warned = False
        self.binary = None
        self.env = None
        self.runner = None
        self.forced_exit = None

    def run(self):
        reconnect = False
        while True:
            try:
                self._connect_and_run(reconnect)
                return
            except ReconnectRequested:
                self._shutdown()
                reconnect = True
            except SelectiveError:
                self._shutdown()
                raise
            except ConnectionLost:
                self.retries += 1
                self._shutdown()
                if self.retries > 10:
                    raise SelectiveError("Too many retries")
                if self.debug:
                    write_stream(
                        "Retrying in " + str(retry_delay(self.retries)) + " seconds...",
                        error=True,
                    )
                time.sleep(retry_delay(self.retries))
                reconnect = True

    def _connect_and_run(self, reconnect):
        if self.binary is None:
            self.binary = resolve_binary()
            check_stdio(self.binary)
        if self.env is None:
            self.env = load_build_env(self.binary)
            self.runner = runner_id_of(self.env)
            if self.logged:
                open_log(self.runner)
        self.pending.clear()
        url = self._url(reconnect)
        self.transport = StdioTransport.open(self.binary, url)
        self._install_signals()
        self._wait_connected()
        self._loop()

    def _url(self, reconnect):
        env = self.env
        query = {
            "api_key": env["api_key"],
            "run_id": env["run_id"],
            "run_attempt": env["run_attempt"],
            "runner_id": self.runner,
            "language": "python",
            "core_version": __version__,
            "framework": "pytest",
            "framework_version": pytest.__version__,
            "framework_wrapper_version": __version__,
            "runtime_version": ".".join(str(part) for part in sys.version_info[:3]),
            "test_map": self.plugin.capability(),
            "test_map_record": test_map_record(),
            "remove_failed": "true",
            "metadata": json.dumps(
                metadata_of(env), ensure_ascii=False, separators=(",", ":")
            ),
        }
        if reconnect:
            query["reconnect"] = "true"
        return env["host"].rstrip("/") + "/transport/websocket?" + urlencode(query)

    def _wait_connected(self):
        deadline = time.monotonic() + CONNECTED_WINDOW
        while True:
            try:
                message = self.transport.read(timeout=max(0.0, deadline - time.monotonic()))
            except TimeoutError:
                self._shutdown()
                raise SelectiveError("Timed out waiting for the Selective server to accept the connection")
            if not isinstance(message, dict):
                if self.debug:
                    raise RuntimeError("Unknown command received: " + str(message))
                continue
            if message.get("command") == "connected":
                return
            self.pending.append(message)

    def _loop(self):
        while not self.stopping:
            self._dispatch(self._next())

    def _next(self):
        if self.pending:
            return self.pending.popleft()
        return self.transport.read(timeout=None)

    def _dispatch(self, message):
        log.info("Received Command: %s", message)
        if not isinstance(message, dict):
            if self.debug:
                raise RuntimeError("Unknown command received: " + str(message))
            return
        command = message.get("command")
        handler = {
            "configure_test_map": self._configure,
            "test_manifest": self._manifest,
            "run_test_cases": self._run,
            "remove_failed_test_case_result": self._remove,
            "reconnect": self._reconnect,
            "close": self._close,
            "print_notice": self._notice,
            "print_message": self._message,
            "connected": self._ignore,
        }.get(command)
        if handler is not None:
            handler(message)
        elif self.debug and command:
            raise RuntimeError("Unknown command received: " + str(command))

    def _ignore(self, _message):
        pass

    def _notice(self, message):
        text = message.get("message")
        if text:
            notice(str(text))

    def _message(self, message):
        text = message.get("message")
        if text:
            write_stream("WARNING: " + str(text))

    def _configure(self, message):
        recorder = self.plugin.recorder
        if message.get("record") is True:
            if recorder is not None:
                recorder.begin()
            elif not self.record_warned:
                self.record_warned = True
                warn(
                    "Selective asked this runner to record a test map, but the tracer isn't available. Tests will run normally."
                )
        if message.get("tree") is True and test_map_capability() != "0":
            self.tree = True
        log.info("Test map configured: record=%s tree=%s", recorder is not None and recorder.recording, self.tree)

    def _manifest(self, _message):
        data = {"test_cases": self.plugin.manifest_cases()}
        if self.tree:
            tree = snapshot_tree(self.binary, os.getcwd())
            if tree is not None:
                data["test_map_tree"] = tree
        self._attach_diff(data)
        log.info("Sending Response: test_manifest")
        self.transport.write_json({"type": "test_manifest", "data": clean_json(data)})

    def _attach_diff(self, data):
        target = self.env.get("target_branch") or ""
        if not str(target).strip():
            return
        commits = num_commits_of(self.env)
        paths, detail = git_diff(str(target), commits, os.getcwd())
        if paths is None:
            warn(
                "Selective was unable to diff with the target branch. This may result in a sub-optimal test order. "
                + detail
            )
            return
        roots = self.plugin.suite_roots()
        limited = modified_test_files(paths, roots)
        data["modified_test_files"] = limited
        hint = correlate(
            str(target),
            commits,
            self.plugin.test_file_paths(),
            os.getcwd(),
        )
        if hint is None:
            warn(
                "Selective was unable to correlate the diff to test files. This may result in a sub-optimal test order."
            )
            return
        data["correlated_files"] = hint

    def _run(self, message):
        ids = message.get("test_case_ids") or []
        self.plugin.run_cases(ids, self._emit)

    def _emit(self, payload):
        log.info("Sending Response: test_case_result: %s", payload["id"])
        self.transport.write_json(
            {"type": "test_case_result", "data": clean_json(payload)}
        )

    def _remove(self, message):
        self.plugin.remove_failure(message.get("test_case_id"))

    def _reconnect(self, _message):
        raise ReconnectRequested()

    def _close(self, message):
        self.stopping = True
        status = message.get("exit_status")
        self.forced_exit = status if type(status) is int else None
        report = {
            "type": "report_at_finish",
            "data": self.plugin.finish_report(self.retries),
        }
        try:
            self.transport.write_json(clean_json(report))
        except ConnectionLost:
            pass
        if self.forced_exit is None:
            self.plugin.finish_hooks()
        self._upload()
        self._shutdown()
        if self.forced_exit is not None:
            os._exit(self.forced_exit)

    def _upload(self):
        recorder = self.plugin.recorder
        if recorder is None or not recorder.ran:
            return
        fragment = recorder.fragment()
        command = [
            self.binary,
            "--upload-fragment",
            "--run-id",
            str(self.env["run_id"]),
            "--run-attempt",
            str(self.env["run_attempt"]),
            "--runner-id",
            self.runner,
        ]
        proc = subprocess.run(
            command,
            input=gzip.compress(json.dumps(fragment, separators=(",", ":")).encode("utf-8")),
            env=dict(os.environ, SELECTIVE_TEST_MAP_UPLOAD_KEY=str(self.env["api_key"])),
            capture_output=True,
            check=False,
        )
        if proc.returncode == 0:
            log.info("Uploaded test map fragment: %s tests", len(fragment["tests"]))
            return
        warn(
            "Selective could not upload this runner's test map ("
            + proc.stderr.decode("utf-8", "replace").strip()
            + "). Its tests will run on the next build rather than be skipped."
        )

    def _exit_now(self):
        if self.forced_exit is not None:
            return self.forced_exit
        return self.plugin.framework_status()

    def _shutdown(self):
        transport = self.transport
        self.transport = None
        if transport is not None:
            transport.close()

    def _install_signals(self):
        def handle(signum, _frame):
            name = "INT" if signum == signal.SIGINT else "TERM"
            transport = self.transport
            if transport is not None and transport.lock.acquire(blocking=False):
                try:
                    transport.write_json_unlocked({"type": "terminating", "data": {"signal": name}})
                    time.sleep(0.25)
                except ConnectionLost:
                    pass
                finally:
                    transport.lock.release()
            self._shutdown()
            if self.forced_exit is not None:
                os._exit(self.forced_exit)
            raise Terminated("selective pytest stopped by SIG" + name, returncode=self._exit_now())

        signal.signal(signal.SIGINT, handle)
        signal.signal(signal.SIGTERM, handle)
