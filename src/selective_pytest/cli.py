import os
import sys

import pytest

from selective_pytest import __version__
from selective_pytest.plugin import SelectivePlugin

USAGE = """usage:
  selective [--debug] [--log] pytest [pytest args]
  selective exec pytest [pytest args]
  selective --version
"""

FLAGS = ("--debug", "--log")


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["--version"]:
        print(__version__)
        return 0
    flags = set()
    while args[:1] and args[0] in FLAGS:
        flags.add(args.pop(0))
    mode = "run"
    if args[:1] == ["exec"]:
        mode, args = "exec", args[1:]
    if args[:1] != ["pytest"]:
        print(USAGE, file=sys.stderr)
        return 1
    return _pytest(args[1:], mode, "--debug" in flags, "--log" in flags)


def _pytest(args, mode, debug, logged):
    os.environ.setdefault("COVERAGE_CORE", "ctrace")
    plugin = SelectivePlugin(mode, debug, logged)
    code = pytest.main(["-o", "console_output_style=classic", *args], plugins=[plugin])
    return int(code)
