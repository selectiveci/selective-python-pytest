import json
import time
from pathlib import Path

import pytest

from selective_pytest.environ import SelectiveError
from selective_pytest.environ import test_map_capability
from selective_pytest.recording import Recorder
from selective_pytest.reporters import forget_previous
from selective_pytest.reporters import junit_added
from selective_pytest.reporters import junit_span
from selective_pytest.session import Controller
from selective_pytest.session import Terminated
from selective_pytest.session import report_error
from selective_pytest.session import warn
from selective_pytest.session import write_stream
from selective_pytest.textutil import clean_text
from selective_pytest.textutil import wire_id
from selective_pytest.transport import ConnectionLost

GROUP_SCOPES = ("package", "module", "class")


def _terminal_writer(config):
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    return getattr(reporter, "_tw", None)


def _relative(path, cwd):
    try:
        return Path(path).resolve().relative_to(Path(cwd).resolve()).as_posix()
    except ValueError:
        return str(path)


def _project_path(path, cwd):
    if not Path(path).is_file():
        return None
    try:
        relative = Path(path).resolve().relative_to(cwd)
    except ValueError:
        return None
    if {"site-packages", "dist-packages"} & set(relative.parts):
        return None
    return relative.as_posix()


def _failure(item, excinfo, report, cwd):
    if excinfo is None:
        lines = [line for line in report.longreprtext.splitlines() if line.strip()]
        return lines, [_location_frame(item, cwd)]
    frames = []
    for entry in reversed(excinfo.traceback):
        path = _project_path(entry.path, cwd)
        if path is not None:
            frames.append(path + ":" + str(entry.lineno + 1) + ":in " + entry.name)
    return excinfo.exconly(tryshort=True).splitlines(), frames or [_location_frame(item, cwd)]


def _project_fixture(fixturedef, cwd):
    return bool(fixturedef.baseid) or _project_path(fixturedef.func.__code__.co_filename, cwd) is not None


def _location_frame(item, cwd):
    name = getattr(item, "originalname", item.name)
    return _file_path(item, cwd) + ":" + str(_line_number(item)) + ":in " + name


def _pending_message(report):
    if hasattr(report, "wasxfail"):
        return report.wasxfail.removeprefix("reason: ")
    longrepr = report.longrepr
    if isinstance(longrepr, tuple) and len(longrepr) >= 3:
        return str(longrepr[2])
    if longrepr:
        return str(longrepr).splitlines()[0]
    return "skipped"


def _file_path(item, cwd):
    return _relative(item.path, cwd)


def _line_number(item):
    lineno = item.location[1]
    return 0 if lineno is None else lineno + 1


def _set_nodeid(item, nodeid):
    item._nodeid = nodeid
    if hasattr(item, "_id"):
        item._id = type(item._id).parse(nodeid)


def _description(item):
    return clean_text(item.name or item.nodeid)


def _fold(reports):
    phases = [reports.get("setup"), reports.get("call"), reports.get("teardown")]
    phases = [report for report in phases if report is not None]
    failed = [report for report in phases if report.failed]
    if failed:
        return "failed", failed[0]
    skipped = [report for report in phases if report.skipped]
    if skipped:
        return "pending", skipped[0]
    call = reports.get("call")
    return "passed", call or (phases[-1] if phases else None)


def _duration(reports):
    return sum(report.duration for report in reports.values())


def uncollected_payload(test_id):
    return {
        "id": test_id,
        "description": test_id,
        "full_description": test_id,
        "status": "failed",
        "file_path": str(test_id).split("::", 1)[0],
        "line_number": 1,
        "run_time": 0.0,
        "pending_message": None,
        "failure_message_lines": ["test id was not collected: " + str(test_id)],
        "failure_formatted_backtrace": [],
    }


class SelectivePlugin:
    def __init__(self, mode, debug, logged):
        self.mode = mode
        self.debug = debug
        self.logged = logged
        self.override = None
        self.collect_failures = []
        self.results = {}
        self.phase_reports = {}
        self.phase_failures = {}
        self.native_ids = {}
        self.items_by_id = {}
        self.ordered = []
        self.hook_seconds = {"before": 0.0, "after": 0.0}
        self.hook_failed = False
        self.session = None
        self.config = None
        self.junit_for = {}
        self.inflight = None
        self.recorder = None

    def pytest_configure(self, config):
        self.config = config
        numprocesses = getattr(config.option, "numprocesses", None)
        if numprocesses:
            raise pytest.UsageError(
                "selective pytest does not load pytest-xdist -n; the server assigns batches"
            )
        if getattr(config.option, "stepwise", False) or getattr(
            config.option, "stepwise_skip", False
        ):
            raise pytest.UsageError(
                "selective pytest does not support --sw; the server assigns batches"
            )
        if getattr(config.option, "setuponly", False):
            raise pytest.UsageError(
                "selective pytest does not support --setup-only or --setup-plan"
            )
        if config.option.maxfail:
            warn("ignoring -x/--maxfail; session fixtures stay up")
        config.option.maxfail = 0
        if self.mode == "run" and test_map_capability() != "0":
            self.recorder = Recorder.start(config)

    def capability(self):
        if test_map_capability() == "0":
            return "0"
        return "select" if self.recorder is None else "record"

    @pytest.hookimpl(tryfirst=True)
    def pytest_sessionstart(self, session):
        if self.mode == "exec" or session.config.option.collectonly:
            self._silence_terminal()

    def _silence_terminal(self):
        writer = _terminal_writer(self.config)
        if writer is None:
            return
        writer.write = lambda *args, **kwargs: None

    def pytest_collectreport(self, report):
        if report.failed:
            self.collect_failures.append(report)

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, items):
        self._assign_ids(items)

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        outcome = yield
        report = outcome.get_result()
        self.phase_reports.setdefault(item.nodeid, {})[report.when] = report
        if report.failed:
            failures = self.phase_failures.setdefault(item.nodeid, {})
            failures[report.when] = _failure(item, call.excinfo, report, Path.cwd().resolve())

    @pytest.hookimpl(hookwrapper=True)
    def pytest_fixture_setup(self, fixturedef, request):
        start = time.perf_counter()
        if self.recorder is not None:
            self.recorder.fixtures.append(fixturedef.argname)
        yield
        if self.recorder is not None:
            self.recorder.fixtures.pop()
        if fixturedef.scope in GROUP_SCOPES and _project_fixture(fixturedef, Path.cwd().resolve()):
            self.hook_seconds["before"] += time.perf_counter() - start
            fixturedef._finalizers = [self._timed_finalizer(finalizer) for finalizer in fixturedef._finalizers]

    def _timed_finalizer(self, finalizer):
        def wrapped():
            start = time.perf_counter()
            try:
                return finalizer()
            finally:
                self.hook_seconds["after"] += time.perf_counter() - start

        return wrapped

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtestloop(self, session):
        self.session = session
        continue_on_errors = bool(session.config.option.continue_on_collection_errors)
        if self.collect_failures and not continue_on_errors:
            count = len(self.collect_failures)
            noun = "error" if count == 1 else "errors"
            write_stream(str(count) + " " + noun + " during collection", error=True)
            self.override = 2
            return True
        if not session.items:
            write_stream("no tests collected", error=True)
            self.override = 1
            return True
        if self.mode == "exec" or self._cli_collect_only():
            write_stream(json.dumps({"test_cases": self.manifest_cases()}))
            if self.collect_failures:
                self.override = 1
            return True
        if session.config.option.collectonly:
            write_stream(
                "selective pytest does not collect from addopts; use selective exec pytest",
                error=True,
            )
            self.override = 1
            return True
        try:
            Controller(self, self.debug, self.logged).run()
        except SelectiveError as exc:
            if self.debug:
                raise
            report_error(str(exc))
            self.override = 1
        except Terminated as exc:
            self.override = exc.returncode
        return True

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session, exitstatus):
        if self.override is not None:
            session.exitstatus = self.override
            return
        if self.mode == "exec" or self._cli_collect_only():
            if self.collect_failures:
                session.exitstatus = 1
            return
        if exitstatus in (0, 1):
            session.exitstatus = self.framework_status()

    def _cli_collect_only(self):
        args = self.config.invocation_params.args
        return "--collect-only" in args or "--collectonly" in args or "--co" in args

    def _assign_ids(self, items):
        seen = set()
        for item in items:
            original = item.nodeid
            wired = wire_id(original)
            suffix = 2
            while wired in seen:
                wired = wire_id(original + "~" + str(suffix))
                suffix += 1
            if suffix > 2:
                warn("duplicate test id " + original + "; disambiguated as " + wired)
            _set_nodeid(item, wired)
            self.native_ids[wired] = original
            seen.add(wired)
        self.ordered = list(items)
        self.items_by_id = {item.nodeid: item for item in items}

    def manifest_cases(self):
        cwd = Path.cwd()
        return [
            {
                "id": item.nodeid,
                "file_path": _file_path(item, cwd),
                "line_number": _line_number(item),
                "description": _description(item),
                "full_description": self._full_description(item),
                "run_time": 0.0,
            }
            for item in self.ordered
        ]

    def test_file_paths(self):
        cwd = Path.cwd()
        return list(dict.fromkeys(_file_path(item, cwd) for item in self.ordered))

    def suite_roots(self):
        cwd = Path.cwd()
        return [_relative(arg, cwd) for arg in self.config.args]

    def run_cases(self, ids, emit):
        cwd = Path.cwd()
        for test_id in ids:
            item = self.items_by_id.get(test_id)
            if item is None:
                payload = uncollected_payload(test_id)
                self._send(payload, emit)
                continue
            if test_id in self.junit_for:
                forget_previous(item, self.junit_for.pop(test_id))
            span = junit_span(item.config)
            if self.recorder is not None:
                self.recorder.enter(item)
            item.ihook.pytest_runtest_protocol(item=item, nextitem=item.session)
            if self.recorder is not None:
                self.recorder.leave(item)
            self.junit_for[test_id] = junit_added(span)
            reports = self.phase_reports.pop(test_id, {})
            failures = self.phase_failures.pop(test_id, {})
            status, report = _fold(reports)
            self._send(self._payload(item, status, report, reports, failures, cwd), emit)

    def _send(self, payload, emit):
        self.inflight = payload
        try:
            emit(payload)
        except ConnectionLost:
            self.results.pop(payload["id"], None)
            raise
        finally:
            self.inflight = None
        self.results[payload["id"]] = payload

    def _full_description(self, item):
        return clean_text(self.native_ids[item.nodeid])

    def _payload(self, item, status, report, reports, failures, cwd):
        payload = {
            "id": item.nodeid,
            "description": _description(item),
            "full_description": self._full_description(item),
            "status": status,
            "file_path": _file_path(item, cwd),
            "line_number": _line_number(item),
            "run_time": _duration(reports),
            "pending_message": _pending_message(report) if status == "pending" else None,
        }
        if status == "failed":
            lines, frames = failures[report.when]
            payload["failure_message_lines"] = [clean_text(line) for line in lines]
            payload["failure_formatted_backtrace"] = [clean_text(frame) for frame in frames]
        return payload

    def remove_failure(self, test_id):
        if self.results.get(test_id, {}).get("status") != "failed":
            return
        del self.results[test_id]
        item = self.items_by_id.get(test_id)
        if item is not None:
            forget_previous(item, self.junit_for.pop(test_id, None))

    def framework_status(self):
        if self.hook_failed or self.collect_failures:
            return 1
        sent = [*self.results.values(), self.inflight or {}]
        return int(any(payload.get("status") == "failed" for payload in sent))

    def finish_report(self, retries):
        data = {"connection_retries": retries}
        for hook, seconds in self.hook_seconds.items():
            if seconds > 0:
                data["seconds_in_" + hook + "_all_hooks"] = seconds
        return data

    def finish_hooks(self):
        try:
            self.session._setupstate.teardown_exact(None)
        except Exception:
            self.hook_failed = True
