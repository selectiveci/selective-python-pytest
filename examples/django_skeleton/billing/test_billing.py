def test_round_half_up(module_hook, session_hook):
    assert session_hook == "warm"
    assert True


def test_round_half_down(module_hook, session_hook):
    assert session_hook == "warm"
    assert True


def test_exempt(module_hook, session_hook):
    assert session_hook == "warm"
    assert True


def test_missing_rate(module_hook, session_hook):
    assert session_hook == "warm"
    assert False
