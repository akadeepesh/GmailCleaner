"""
Tests for desktop/__main__.py (the dev launcher)

Covers:
  - pick_port prefers 8765 and falls back to a free port when it's taken
  - prepare_environment sets the home, a fresh strong secret per launch and the port
  - argument parsing
"""

import os
import socket

import pytest

from desktop import __main__ as launcher


@pytest.fixture(autouse=True)
def restore_env():
    """prepare_environment writes os.environ directly; put it back afterwards."""
    names = ("GMAILCLEANER_HOME", "DASHBOARD_SECRET", "GMAILCLEANER_PORT")
    saved = {name: os.environ.get(name) for name in names}
    for name in names:
        os.environ.pop(name, None)
    yield
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def test_pick_port_prefers_given_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
    assert launcher.pick_port(free) == free


def test_pick_port_falls_back_when_taken():
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        busy = taken.getsockname()[1]
        port = launcher.pick_port(busy)
    assert port != busy and port > 0


def test_prepare_environment(tmp_path):
    home = tmp_path / "Ünïcode home"
    first = launcher.prepare_environment(home, 8765)
    assert home.is_dir()
    assert os.environ["GMAILCLEANER_HOME"] == str(home)
    assert os.environ["GMAILCLEANER_PORT"] == "8765"
    assert os.environ["DASHBOARD_SECRET"] == first and len(first) >= 40
    assert launcher.prepare_environment(home, 8765) != first   # new secret per launch


def test_parse_args():
    args = launcher.parse_args(["--home", "x", "--port", "9000", "--no-browser"])
    assert (args.home, args.port, args.no_browser) == ("x", 9000, True)
    assert launcher.parse_args([]).port == 0
