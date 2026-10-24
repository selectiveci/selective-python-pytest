import os
import sys
import sysconfig
from multiprocessing.process import BaseProcess
from pathlib import Path

from selective_pytest.correlate import _git

SPAWN_EVENTS = (
    "subprocess.Popen",
    "_posixsubprocess.fork_exec",
    "os.fork",
    "os.posix_spawn",
    "os.system",
    "os.exec",
    "os.spawn",
)


def repo_root(cwd):
    proc = _git(["git", "rev-parse", "--show-toplevel"], cwd)
    return Path(proc.stdout.strip() if proc else cwd).resolve()


def ignored_dirs():
    paths = sysconfig.get_paths()
    dirs = {sys.prefix, sys.exec_prefix, paths["purelib"], paths["platlib"], str(Path(__file__).parent)}
    dirs.update(part.strip() for part in os.environ.get("SELECTIVE_TEST_MAP_IGNORE", "").split(",") if part.strip())
    return [Path(directory).resolve() for directory in dirs]


class Recorder:
    def __init__(self, cov, owned, root):
        self.cov = cov
        self.owned = owned
        self.root = root
        self.ignored = ignored_dirs()
        self.recording = False
        self.current = None
        self.fixtures = []
        self.ran = {}
        self.spawning_tests = set()
        self.spawning_fixtures = set()

    @classmethod
    def start(cls, config):
        try:
            import coverage
        except ImportError:
            return None
        cov = coverage.Coverage.current()
        owned = cov is None
        if not owned and getattr(config.option, "cov_context", None):
            return None
        root = repo_root(Path.cwd())
        if owned:
            cov = coverage.Coverage(data_file=None, source=[str(root)], branch=False, config_file=False)
            cov.start()
        core = dict(cov.sys_info())["core"]
        if owned:
            cov.stop()
        return cls(cov, owned, root) if core == "CTracer" else None

    def begin(self):
        if self.recording:
            return
        self.recording = True
        sys.addaudithook(self._audit)
        start = BaseProcess.start

        def tracked(process):
            self._spawned()
            return start(process)

        BaseProcess.start = tracked
        if self.owned:
            self.cov.start()

    def enter(self, item):
        if self.recording:
            self.current = item.nodeid
            self.cov.switch_context(item.nodeid)

    def leave(self, item):
        if self.recording:
            self.ran[item.nodeid] = set(item.fixturenames)
            self.current = None
            self.cov.switch_context("")

    def _audit(self, event, _args):
        if event.startswith(SPAWN_EVENTS):
            self._spawned()

    def _spawned(self):
        if self.fixtures:
            self.spawning_fixtures.add(self.fixtures[-1])
        elif self.current is not None:
            self.spawning_tests.add(self.current)

    def fragment(self):
        if self.owned:
            self.cov.stop()
        data = self.cov.get_data()
        deps = {test_id: set() for test_id in self.ran}
        for measured in data.measured_files():
            path = self._repo_path(measured)
            if path is None:
                continue
            for contexts in data.contexts_by_lineno(measured).values():
                for context in contexts:
                    if context in deps:
                        deps[context].add(path)
        kept = {
            test_id: paths
            for test_id, paths in deps.items()
            if test_id not in self.spawning_tests and not self.ran[test_id] & self.spawning_fixtures
        }
        files = sorted(set().union(*kept.values()))
        index = {path: position for position, path in enumerate(files)}
        tests = [{"id": test_id, "deps": sorted(index[path] for path in paths)} for test_id, paths in kept.items()]
        return {"version": 1, "files": files, "tests": tests}

    def _repo_path(self, measured):
        path = Path(measured).resolve()
        if not path.is_file() or any(path == folder or folder in path.parents for folder in self.ignored):
            return None
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return None
