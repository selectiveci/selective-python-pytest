# Agent rules

Each rule is paired with the check that enforces it. Run the command locally; CI runs the same command.

| Rule | Check | Command | Review finding |
| --- | --- | --- | --- |
| Do not keep code with no caller. An attribute that is only assigned, appended, or length-capped is dead. Do not duck-type `LogXML` or wrap `get_plugin` / `stash.get` in `try/except Exception: pass`, and do not call `get_plugin("html")`. | `tools/check_dead_code.py` | `python3 tools/check_dead_code.py` | Simplicity pass on selectiveci/selective-python-pytest#1 at `6fa455b`, finding #4 (`transport.py` stderr ring buffer) and finding #1 (dead plugin lookups) |
| Pin `pytest>=7.4`. Access attributes that version guarantees (`path`, `location`, `name`, `nodeid`, `duration`, `failed`, `skipped`, `passed`, `when`, `outcome`, `longrepr`, `keywords`) directly. Call `SetupState.teardown_exact` with one argument; do not catch `TypeError` to retry a pytest 6 arity. | `tools/check_pytest_contract.py` | `python3 tools/check_pytest_contract.py` | Same pass, finding #7 (`getattr` hedges on item and report) and finding #5 (pytest 6 arity shims) |

An exception belongs on the offending line and must include a reason. `# noqa: dead-code <reason>` or `# noqa: pytest-contract <reason>`. A bare marker does not suppress the finding.

These hedges are judgment calls. Not enforced:

- `getattr(item, "originalname", item.name)` — `originalname` is not on every item.
- `getattr(config.option, "numprocesses" / "stepwise" / "stepwise_skip" / "setuponly" / "cov_context", ...)` — those options exist only when the plugin is loaded.
