import json

from selective_pytest.correlate import _git


def snapshot_tree(binary, cwd):
    proc = _git([binary, "--snapshot", cwd])
    if proc is None:
        return None
    try:
        tree = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return tree if isinstance(tree, dict) else None
