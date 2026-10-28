import ast
import sys
from pathlib import Path

MUTATORS = {
    "append",
    "extend",
    "insert",
    "remove",
    "discard",
    "add",
    "update",
    "clear",
    "reverse",
    "sort",
    "put",
    "put_nowait",
    "pop",
    "popleft",
}
MAINTENANCE = {"len"}
DUCK_ATTRS = {"node_reporters", "open_reports"}


def find_root(start=None):
    current = Path(start or Path(__file__).resolve()).resolve()
    if current.is_file():
        current = current.parent
    for candidate in [current, *current.parents]:
        if (candidate / "pyproject.toml").is_file() and (candidate / "src" / "selective_pytest").is_dir():
            return candidate
    return Path.cwd()


def python_files(paths):
    found = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            found.extend(sorted(item for item in path.rglob("*.py") if item.is_file()))
        elif path.suffix == ".py" and path.is_file():
            found.append(path)
    return found


def line_of(lines, lineno):
    if lineno < 1 or lineno > len(lines):
        return ""
    return lines[lineno - 1]


def allowed(lines, lineno):
    marker = "# noqa: dead-code"
    text = line_of(lines, lineno)
    if marker not in text:
        return False
    return bool(text.split(marker, 1)[1].strip())


def parents_of(tree):
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def ancestors(node, parents, limit):
    found = []
    current = node
    for _ in range(limit):
        current = parents.get(current)
        if current is None:
            break
        found.append(current)
    return found


def self_attribute(node):
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    )


def observed(node, parents):
    above = ancestors(node, parents, 5)
    parent = above[0] if above else None
    if isinstance(parent, ast.Call) and parent.args and parent.args[0] is node:
        func = parent.func
        if isinstance(func, ast.Name) and func.id in MAINTENANCE:
            return False
    if isinstance(parent, ast.Attribute) and parent.value is node:
        grand = above[1] if len(above) > 1 else None
        great = above[2] if len(above) > 2 else None
        if (
            isinstance(grand, ast.Call)
            and grand.func is parent
            and parent.attr in MUTATORS
            and isinstance(great, ast.Expr)
        ):
            return False
        return True
    if isinstance(parent, ast.Subscript) and parent.value is node:
        grand = above[1] if len(above) > 1 else None
        if isinstance(parent.ctx, ast.Del) or isinstance(grand, ast.Delete):
            return False
        if isinstance(parent.ctx, ast.Store) and not isinstance(grand, ast.AugAssign):
            return False
        if isinstance(grand, ast.Assign) and len(grand.targets) == 1:
            target = grand.targets[0]
            if self_attribute(target) and target.attr == node.attr:
                return False
        return True
    return True


def write_only(tree, lines, filename):
    parents = parents_of(tree)
    found = []
    for class_node in ast.walk(tree):
        if not isinstance(class_node, ast.ClassDef):
            continue
        writes = {}
        seen = set()
        for node in ast.walk(class_node):
            if not self_attribute(node):
                continue
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                writes.setdefault(node.attr, node.lineno)
            elif isinstance(node.ctx, ast.Load) and observed(node, parents):
                seen.add(node.attr)
            elif isinstance(node.ctx, ast.Load):
                writes.setdefault(node.attr, node.lineno)
        for name, lineno in sorted(writes.items(), key=lambda item: item[1]):
            if name in seen or allowed(lines, lineno):
                continue
            found.append(
                filename
                + ":"
                + str(lineno)
                + ": "
                + class_node.name
                + "."
                + name
                + " is written and never read. Delete it. Nothing reads this attribute,"
                + " so a capped buffer with no consumer is dead code (simplicity finding #4)."
            )
    return found


def plugin_lookup(node):
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr == "get_plugin":
        return True
    return (
        node.func.attr == "get"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "stash"
    )


def bare_exception_pass(handler):
    if not isinstance(handler.type, ast.Name) or handler.type.id != "Exception":
        return False
    return len(handler.body) == 1 and isinstance(handler.body[0], ast.Pass)


def dead_lookups(tree, lines, filename):
    found = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "hasattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in DUCK_ATTRS
            and not allowed(lines, node.lineno)
        ):
            found.append(
                filename
                + ":"
                + str(node.lineno)
                + ": hasattr(..., "
                + repr(node.args[1].value)
                + ") never matches. pytest>=7.0 keeps LogXML at config.stash.get(xml_key)."
                + " Delete the duck scan (simplicity finding #1)."
            )
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get_plugin"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "html"
            and not allowed(lines, node.lineno)
        ):
            found.append(
                filename
                + ":"
                + str(node.lineno)
                + ": get_plugin('html') returns the pytest-html module, which has no _reports."
                + " Find HTMLReport by its _reports and _report attributes (simplicity finding #1)."
            )
        if isinstance(node, ast.Try):
            for handler in node.handlers:
                if not bare_exception_pass(handler) or allowed(lines, handler.lineno):
                    continue
                if not any(plugin_lookup(child) for child in ast.walk(node)):
                    continue
                found.append(
                    filename
                    + ":"
                    + str(handler.lineno)
                    + ": pluginmanager.get_plugin and config.stash.get return None and do not raise."
                    + " Delete the try/except Exception (simplicity finding #1)."
                )
    return found


def defined_names(tree):
    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.append(node)
    return found


def referenced_names(tree):
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            found.add(node.id)
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            found.add(node.attr)
    return found


def uncalled(parsed, lines_for, report_paths):
    refs = set()
    for _path, tree in parsed:
        refs.update(referenced_names(tree))
    found = []
    for path, tree in parsed:
        if path not in report_paths:
            continue
        lines = lines_for[path]
        filename = str(path)
        for node in defined_names(tree):
            name = node.name
            if name.startswith("__") and name.endswith("__"):
                continue
            if name.startswith("pytest_") or name == "main":
                continue
            if name in refs or allowed(lines, node.lineno):
                continue
            found.append(
                filename
                + ":"
                + str(node.lineno)
                + ": "
                + name
                + " has no caller. Delete it (simplicity finding #4, dead code)."
            )
    return found


def findings_for(paths, reference_paths=None):
    parsed = []
    lines_for = {}
    found = []
    primary = python_files(paths)
    report_paths = set(primary)
    for path in primary:
        text = path.read_text()
        lines = text.splitlines()
        tree = ast.parse(text, filename=str(path))
        parsed.append((path, tree))
        lines_for[path] = lines
        found.extend(write_only(tree, lines, str(path)))
        found.extend(dead_lookups(tree, lines, str(path)))
    if reference_paths is not None:
        for path in python_files(reference_paths):
            if path in lines_for:
                continue
            text = path.read_text()
            tree = ast.parse(text, filename=str(path))
            parsed.append((path, tree))
            lines_for[path] = text.splitlines()
    found.extend(uncalled(parsed, lines_for, report_paths))
    return found


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    root = find_root()
    if argv:
        found = findings_for(argv)
    else:
        found = findings_for([str(root / "src")], [str(root / "src"), str(root / "tests")])
    for item in found:
        print(item)
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
