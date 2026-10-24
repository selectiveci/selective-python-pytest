from selective_pytest.correlate import is_test_path
from selective_pytest.correlate import under_suite
from selective_pytest.environ import test_map_capability as capability
from selective_pytest.plugin import _relative
from selective_pytest.session import retry_delay
from selective_pytest.textutil import needs_short_id
from selective_pytest.textutil import short_id
from selective_pytest.textutil import wire_id


def test_short_id_is_stable_and_bounded():
    native = "tests/" + ("n" * 400) + ".py::test_case"
    assert needs_short_id(native)
    wired = wire_id(native)
    assert wired == wire_id(native)
    assert len(wired.encode("utf-8")) <= 255
    assert wired.endswith("~" + short_id(native).split("~", 1)[1])
    assert "\x00" not in wired


def test_lone_surrogate_ids_are_stable_utf8():
    native = "tests/test_billing.py::test_tax\ud800"
    wired = wire_id(native)
    assert wired == wire_id(native)
    wired.encode("utf-8")
    assert len(wired.encode("utf-8")) <= 255
    assert wire_id("tests/test_billing.py::test_tax\udc00") != wired


def test_nul_and_plain_ids():
    plain = "tests/test_billing.py::test_tax"
    assert wire_id(plain) == plain
    nulled = "tests/test_billing.py::test_tax\0more"
    wired = wire_id(nulled)
    assert "\x00" not in wired
    assert wired != nulled
    assert len(wired.encode("utf-8")) <= 255


def test_utf8_boundary_prefix():
    native = ("é" * 200) + "tail"
    wired = wire_id(native)
    prefix, _digest = wired.rsplit("~", 1)
    assert len(prefix.encode("utf-8")) <= 190
    prefix.encode("utf-8")


def test_retry_delay():
    assert retry_delay(1) == 1
    assert retry_delay(4) == 4
    assert retry_delay(9) == 4


def test_capability_defaults_to_select(monkeypatch):
    monkeypatch.delenv("SELECTIVE_TEST_MAP_DISABLE", raising=False)
    assert capability() == "select"
    for value in ("1", "true", "YES"):
        monkeypatch.setenv("SELECTIVE_TEST_MAP_DISABLE", value)
        assert capability() == "0"


def test_suite_root_filter():
    assert is_test_path("tests/test_billing.py")
    assert under_suite("tests/test_billing.py", ["tests"])
    assert not under_suite("billing.py", ["tests"])
    assert under_suite("pkg/test_extra.py", ["."])


def test_relative_sibling_is_not_a_child(tmp_path):
    repo = tmp_path / "repo"
    sibling = tmp_path / "repo-sibling" / "test_x.py"
    repo.mkdir()
    sibling.parent.mkdir()
    sibling.write_text("x")
    text = _relative(sibling, repo)
    assert "repo-sibling" in text
    assert not text.startswith("-")
