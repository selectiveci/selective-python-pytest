import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.check_dead_code import find_root
from tools.check_dead_code import line_of
from tools.check_dead_code import python_files

GUARANTEED = {
    "path",
    "location",
    "name",
    "nodeid",
    "duration",
    "failed",
    "skipped",
    "passed",
    "when",
    "outcome",
    "longrepr",
    "keywords",
}
PIN = re.compile(r"pytest\s*(>=|~=)\s*(\d+)(?:\.(\d+))?")


def allowed(lines, lineno):
    marker = "# noqa: pytest-contract"
    text = line_of(lines, lineno)
    if marker not in text:
        return False
    return bool(text.split(marker, 1)[1].strip())


def attribute_name(node):
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != "getattr":
        return None
    if len(node.args) >= 3 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
        return node.args[1].value
    if len(node.args) >= 2 and any(keyword.arg == "default" for keyword in node.keywords):
        if isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
            return node.args[1].value
    return None


def typeerror_handler(handler):
    type_node = handler.type
    if isinstance(type_node, ast.Name):
        return type_node.id == "TypeError"
    if isinstance(type_node, ast.Tuple):
        return any(isinstance(elt, ast.Name) and elt.id == "TypeError" for elt in type_node.elts)
    return False


def calls_teardown_exact(node):
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) and child.func.attr == "teardown_exact":
            return True
    return False


def high_enough(text):
    matches = list(PIN.finditer(text))
    if not matches:
        return False
    for match in matches:
        major = int(match.group(2))
        minor = int(match.group(3) or 0)
        if major > 7 or (major == 7 and minor >= 4):
            return True
    return False


def pin_finding(text, filename):
    if high_enough(text):
        return []
    return [
        filename
        + ":1: pin pytest>=7.4. Item.path, Report.duration, and"
        + " SetupState.teardown_exact(nextitem) are only guaranteed from that version"
        + " (simplicity findings #5 and #7)."
    ]


def source_findings(tree, lines, filename):
    found = []
    for node in ast.walk(tree):
        attr = attribute_name(node)
        if attr in GUARANTEED and not allowed(lines, node.lineno):
            found.append(
                filename
                + ":"
                + str(node.lineno)
                + ": to access the attribute directly because pytest>=7.4 guarantees it ("
                + attr
                + ")"
            )
        if isinstance(node, ast.Try) and any(typeerror_handler(handler) for handler in node.handlers):
            if calls_teardown_exact(node) and not allowed(lines, node.lineno):
                found.append(
                    filename
                    + ":"
                    + str(node.lineno)
                    + ": call teardown_exact with one argument because pytest>=7.4 guarantees"
                    + " that signature. Do not catch TypeError to retry a pytest-6 arity"
                    + " (simplicity finding #5)."
                )
    return found


def findings_for(paths, project_text=None, project_name="pyproject.toml"):
    found = []
    if project_text is not None:
        found.extend(pin_finding(project_text, project_name))
    for path in python_files(paths):
        text = path.read_text()
        lines = text.splitlines()
        tree = ast.parse(text, filename=str(path))
        found.extend(source_findings(tree, lines, str(path)))
    return found


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    root = find_root()
    paths = argv or [str(root / "src")]
    text = (root / "pyproject.toml").read_text()
    found = findings_for(paths, text, str(root / "pyproject.toml"))
    for item in found:
        print(item)
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
