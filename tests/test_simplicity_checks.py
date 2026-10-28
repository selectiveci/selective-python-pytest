import ast
import shutil
from pathlib import Path

from tools import check_dead_code
from tools import check_pytest_contract
from tools.check_dead_code import dead_lookups
from tools.check_dead_code import findings_for
from tools.check_dead_code import write_only
from tools.check_pytest_contract import high_enough
from tools.check_pytest_contract import source_findings

ROOT = Path(__file__).resolve().parents[1]

JUNIT_BEFORE = """
def junit_plugin(config):
    try:
        from _pytest.junitxml import xml_key
        found = config.stash.get(xml_key, None)
        if found is not None:
            return found
    except Exception:
        pass
    for plugin in config.pluginmanager.get_plugins():
        if hasattr(plugin, "node_reporters") and hasattr(plugin, "stats"):
            return plugin
    return None

def html_plugin(config):
    plugin = config.pluginmanager.get_plugin("html")
    reports = getattr(plugin, "_reports", None)
    if reports is not None:
        return plugin
    return None

def use(config):
    return junit_plugin(config), html_plugin(config)

def main():
    return use(None)
"""

JUNIT_AFTER = """
from _pytest.junitxml import xml_key

def junit_plugin(config):
    return config.stash.get(xml_key, None)

def html_plugin(config):
    for plugin in config.pluginmanager.get_plugins():
        if hasattr(plugin, "_reports") and hasattr(plugin, "_report"):
            return plugin
    return None

def use(config):
    return junit_plugin(config), html_plugin(config)

def main():
    return use(None)
"""

HEDGE_BEFORE = """
def _file_path(item, cwd):
    path = getattr(item, "path", None)
    if path is None:
        path = item.location[0]
    return path

def _line_number(item):
    location = getattr(item, "location", (None, None))
    lineno = location[1]
    return 0 if lineno is None else lineno + 1

def _duration(reports):
    return sum(getattr(report, "duration", 0) for report in reports.values())

def finish_hooks(self, item, nextitem):
    try:
        self.session._setupstate.teardown_exact(item, nextitem)
    except TypeError:
        self.session._setupstate.teardown_exact(nextitem)
"""

HEDGE_AFTER = """
def _file_path(item, cwd):
    return item.path

def _line_number(item):
    lineno = item.location[1]
    return 0 if lineno is None else lineno + 1

def _duration(reports):
    return sum(report.duration for report in reports.values())

def finish_hooks(self):
    try:
        self.session._setupstate.teardown_exact(None)
    except Exception:
        self.hook_failed = True
"""


def parsed(text):
    return ast.parse(text), text.splitlines()


def ring_buffer(original):
    injected = original.replace(
        "        self.closed = False\n",
        "        self.closed = False\n"
        "        self.stderr_lines = []\n"
        "        self._stderr = threading.Thread(target=self._drain_stderr, daemon=True)\n"
        "        self._stderr.start()\n",
        1,
    )
    drain = (
        "    def _drain_stderr(self):\n"
        "        try:\n"
        "            for line in self.proc.stderr:\n"
        '                self.stderr_lines.append(line.rstrip("\\n"))\n'
        "                if len(self.stderr_lines) > 50:\n"
        "                    del self.stderr_lines[:-50]\n"
        "        except Exception:\n"
        "            pass\n"
        "\n"
    )
    injected = injected.replace("    def _read_stdout(self):\n", drain + "    def _read_stdout(self):\n", 1)
    return injected.replace(
        "            stdout=subprocess.PIPE,\n",
        "            stdout=subprocess.PIPE,\n            stderr=subprocess.PIPE,\n",
        1,
    )


def test_unread_stderr_ring_buffer_fails_until_deleted(tmp_path):
    original = (ROOT / "src" / "selective_pytest" / "transport.py").read_text()
    tree, lines = parsed(ring_buffer(original))
    red = write_only(tree, lines, "transport.py")
    assert any("stderr_lines" in item and "never read" in item for item in red), red
    tree, lines = parsed(original)
    assert write_only(tree, lines, "transport.py") == []
    src = tmp_path / "src"
    shutil.copytree(ROOT / "src", src)
    transport = src / "selective_pytest" / "transport.py"
    transport.write_text(ring_buffer(original))
    cli_red = findings_for([str(src)], [str(src), str(ROOT / "tests")])
    assert len(cli_red) == 1 and "stderr_lines" in cli_red[0] and "never read" in cli_red[0]
    transport.write_text(original)
    assert findings_for([str(src)], [str(src), str(ROOT / "tests")]) == []


def test_dead_plugin_lookups_fail_until_removed():
    tree, lines = parsed(JUNIT_BEFORE)
    red = dead_lookups(tree, lines, "reporters.py")
    assert any("node_reporters" in item for item in red), red
    assert any("do not raise" in item for item in red), red
    assert any("get_plugin('html')" in item for item in red), red
    tree, lines = parsed(JUNIT_AFTER)
    assert dead_lookups(tree, lines, "reporters.py") == []


def test_framework_hedges_fail_until_direct_access():
    tree, lines = parsed(HEDGE_BEFORE)
    red = source_findings(tree, lines, "plugin.py")
    text = "\n".join(red)
    assert "to access the attribute directly because pytest>=7.4 guarantees it (path)" in text
    assert "to access the attribute directly because pytest>=7.4 guarantees it (location)" in text
    assert "to access the attribute directly because pytest>=7.4 guarantees it (duration)" in text
    assert "pytest-6 arity" in text
    tree, lines = parsed(HEDGE_AFTER)
    assert source_findings(tree, lines, "plugin.py") == []


def test_pytest_floor_is_7_4():
    assert high_enough('dependencies = [\n  "pytest>=7.4",\n]\n')
    assert high_enough('dependencies = [\n  "pytest>=8",\n]\n')
    assert not high_enough('dependencies = [\n  "pytest>=7",\n]\n')
    assert not high_enough('dependencies = [\n  "pytest>=6.2",\n]\n')
    assert not high_enough("dependencies = []\n")
    assert high_enough((ROOT / "pyproject.toml").read_text())


def test_exception_on_the_line_needs_a_reason():
    bare = "class Box:\n    def __init__(self):\n        self.stderr_lines = []  # noqa: dead-code\n"
    tree, lines = parsed(bare)
    assert write_only(tree, lines, "box.py")
    reasoned = (
        "class Box:\n"
        "    def __init__(self):\n"
        "        self.stderr_lines = []  # noqa: dead-code harness Popen, not the transport buffer\n"
    )
    tree, lines = parsed(reasoned)
    assert write_only(tree, lines, "box.py") == []
    hedge = 'def f(item):\n    return getattr(item, "path", None)  # noqa: pytest-contract\n'
    tree, lines = parsed(hedge)
    assert source_findings(tree, lines, "plugin.py")
    hedge = (
        'def f(item):\n'
        '    return getattr(item, "path", None)  # noqa: pytest-contract not a pytest Item\n'
    )
    tree, lines = parsed(hedge)
    assert source_findings(tree, lines, "plugin.py") == []


def test_repository_sources_pass():
    assert check_dead_code.main([]) == 0
    assert check_pytest_contract.main([]) == 0
