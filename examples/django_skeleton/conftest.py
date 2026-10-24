import json
import os
from pathlib import Path

import pytest


def bump(name):
    raw = os.environ.get("SELECTIVE_HOOK_COUNTER")
    if not raw:
        return
    path = Path(raw)
    data = json.loads(path.read_text()) if path.exists() else {}
    data[name] = data.get(name, 0) + 1
    path.write_text(json.dumps(data))


@pytest.fixture(scope="module")
def module_hook():
    bump("module_setup")
    yield
    bump("module_teardown")


@pytest.fixture(scope="session")
def django_db_setup(django_db_setup):
    bump("db_setup")


@pytest.fixture(scope="session")
def session_hook():
    from django.conf import settings

    assert settings.configured
    bump("session_setup")
    yield "warm"
    bump("session_teardown")
