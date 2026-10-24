import os

if os.environ.get("SELECTIVE_CONFORMANCE_FAST") == "1":
    import selective_pytest.session

    selective_pytest.session.retry_delay = lambda attempt: 0
