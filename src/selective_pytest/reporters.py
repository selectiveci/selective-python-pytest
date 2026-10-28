from _pytest.junitxml import xml_key


def junit_plugin(config):
    return config.stash.get(xml_key, None)


def html_plugin(config):
    for plugin in config.pluginmanager.get_plugins():
        if hasattr(plugin, "_reports") and hasattr(plugin, "_report"):
            return plugin
    return None


def junit_span(config):
    xml = junit_plugin(config)
    if xml is None:
        return None
    return xml, len(xml.node_reporters_ordered), dict(xml.stats), xml.cnt_double_fail_tests


def junit_added(span):
    if span is None:
        return None
    xml, start, stats, doubles = span
    added = {key: xml.stats[key] - count for key, count in stats.items()}
    return xml.node_reporters_ordered[start:], added, xml.cnt_double_fail_tests - doubles


def forget_junit(config, added):
    if added is None:
        return
    reporters, stats, doubles = added
    xml = junit_plugin(config)
    for reporter in reporters:
        xml.node_reporters_ordered.remove(reporter)
    for key, count in stats.items():
        xml.stats[key] -= count
    xml.cnt_double_fail_tests -= doubles


def _html_label(entry):
    row = entry.get("resultsTableRow") if isinstance(entry, dict) else None
    if not row:
        return None
    for cell in row:
        if not isinstance(cell, str) or "col-result" not in cell:
            continue
        start = cell.find(">")
        end = cell.rfind("<")
        if start != -1 and end != -1 and end > start:
            return cell[start + 1 : end].strip().lower()
    return None


def forget_html(config, nodeid):
    plugin = html_plugin(config)
    if plugin is None:
        return
    plugin._reports.pop(nodeid, None)
    report = plugin._report
    data = getattr(report, "data", None)
    tests = data.get("tests") if isinstance(data, dict) else None
    if not isinstance(tests, dict) or nodeid not in tests:
        return
    entries = tests.pop(nodeid)
    outcomes = getattr(report, "outcomes", None)
    if not isinstance(outcomes, dict):
        return
    for entry in entries:
        label = _html_label(entry)
        slot = outcomes.get(label) if label else None
        if isinstance(slot, dict) and slot.get("value", 0) > 0:
            slot["value"] -= 1


def forget_terminal(config, nodeid):
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    stats = getattr(reporter, "stats", None)
    if not isinstance(stats, dict):
        return
    for key, reports in list(stats.items()):
        stats[key] = [
            report for report in reports if report.nodeid != nodeid
        ]


def forget_previous(item, added):
    config = item.config
    forget_junit(config, added)
    forget_html(config, item.nodeid)
    forget_terminal(config, item.nodeid)
