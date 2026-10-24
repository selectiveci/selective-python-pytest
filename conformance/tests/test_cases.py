import time

import pytest

_SEEN = {}


@pytest.fixture(scope="module")
def warm():
    time.sleep(0.25)


def test_case_pass(warm):
    assert True


def test_case_fail():
    assert False


def test_case_pend():
    pytest.skip("skipped")


def test_case_flake():
    _SEEN["flake"] = _SEEN.get("flake", 0) + 1
    assert _SEEN["flake"] >= 2
