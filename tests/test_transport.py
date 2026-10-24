import os

import pytest

from selective_pytest import transport
from selective_pytest.environ import SelectiveError


def _script(tmp_path, body, mode=0o755):
    path = tmp_path / "transport"
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(mode)
    return str(path)


def test_selective_transport_wins_and_must_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("SELECTIVE_TRANSPORT", str(tmp_path / "gone"))
    with pytest.raises(SelectiveError, match="which does not exist"):
        transport.resolve_binary()
    configured = _script(tmp_path, 'echo usage')
    monkeypatch.setenv("SELECTIVE_TRANSPORT", configured)
    assert transport.resolve_binary() == configured


def test_bundled_binary_comes_before_path(tmp_path, monkeypatch):
    monkeypatch.delenv("SELECTIVE_TRANSPORT", raising=False)
    bundled = tmp_path / "bin" / "transport"
    bundled.parent.mkdir()
    bundled.write_text("")
    monkeypatch.setattr(transport, "BUNDLED", bundled)
    monkeypatch.setenv("PATH", str(tmp_path))
    _script(tmp_path, 'echo usage')
    assert transport.resolve_binary() == str(bundled)


def test_missing_everywhere_names_every_place_looked(tmp_path, monkeypatch):
    monkeypatch.delenv("SELECTIVE_TRANSPORT", raising=False)
    monkeypatch.setattr(transport, "BUNDLED", tmp_path / "none")
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(SelectiveError) as caught:
        transport.resolve_binary()
    message = str(caught.value)
    assert "not found" in message and "SELECTIVE_TRANSPORT" in message and "platform wheel" in message


@pytest.mark.parametrize(
    "body, mode, expected",
    [
        ('echo "usage: transport --stdio"', 0o755, None),
        ('echo "Usage: transport <url> <runner_id>" >&2; exit 1', 0o755, "has no --stdio mode"),
        ('echo "usage: transport --stdio"', 0o644, "cannot be executed: Permission denied"),
        ('echo "/t: version GLIBC_2.39 not found" >&2; exit 1', 0o755, "cannot run: /t: version GLIBC_2.39 not found"),
    ],
)
def test_check_stdio_names_the_failure(tmp_path, body, mode, expected):
    binary = _script(tmp_path, body, mode)
    if expected is None:
        transport.check_stdio(binary)
        return
    with pytest.raises(SelectiveError, match=expected):
        transport.check_stdio(binary)


def test_missing_loader_is_called_out(tmp_path):
    binary = tmp_path / "transport"
    binary.write_text("#!/lib/ld-musl-missing.so.1\n")
    binary.chmod(0o755)
    with pytest.raises(SelectiveError, match="the loader it names is missing"):
        transport.check_stdio(str(binary))
    assert os.path.isfile(binary)
