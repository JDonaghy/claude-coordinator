"""``coord.platform_paths.default_coord_dir`` -- the state-root seam (#1156).

POSIX (Linux, and anything else that isn't win32/darwin) keeps ``~/.coord``
for back-compat with every existing deployment.  Windows and macOS resolve
through ``platformdirs`` to an OS-native app-data directory instead.

The Windows branch can't call the *real* ``platformdirs`` Windows backend on
this (Linux) CI box -- it shells out to ``ctypes.windll``, which doesn't
exist here -- so that case stubs ``platformdirs.user_data_dir`` itself and
asserts ``default_coord_dir`` delegates to it with the expected arguments.
The macOS backend is pure path-string logic (no OS calls), so it runs for
real under a monkeypatched ``sys.platform``.
"""

from __future__ import annotations

import sys
from pathlib import Path

from coord.platform_paths import default_coord_dir


def test_linux_resolves_to_dot_coord_back_compat(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert default_coord_dir() == Path.home() / ".coord"


def test_other_posix_platform_resolves_to_dot_coord(monkeypatch) -> None:
    """Any sys.platform other than win32/darwin is treated as POSIX back-compat."""
    monkeypatch.setattr(sys, "platform", "freebsd13")
    assert default_coord_dir() == Path.home() / ".coord"


def test_darwin_resolves_via_platformdirs_not_dot_coord(monkeypatch) -> None:
    """macOS is POSIX (fcntl/termios/tty all work there) but still gets an
    OS-native dir per the issue's explicit "Windows/mac" scope, not ~/.coord."""
    monkeypatch.setattr(sys, "platform", "darwin")
    result = default_coord_dir()
    assert result != Path.home() / ".coord"
    assert result == Path.home() / "Library" / "Application Support" / "coord"


def test_windows_delegates_to_platformdirs_user_data_dir(monkeypatch) -> None:
    import platformdirs

    monkeypatch.setattr(sys, "platform", "win32")
    calls = []

    def fake_user_data_dir(appname, appauthor=None, **kwargs):
        calls.append((appname, appauthor))
        return r"C:\Users\bob\AppData\Local\coord"

    monkeypatch.setattr(platformdirs, "user_data_dir", fake_user_data_dir)

    result = default_coord_dir()
    assert calls == [("coord", False)]
    assert result == Path(r"C:\Users\bob\AppData\Local\coord")


def test_state_root_modules_derive_from_default_coord_dir() -> None:
    """The four constants this issue routes through platformdirs all agree
    with the shared resolver on THIS process's real platform -- guards
    against one of them keeping a stale ``Path.home() / ".coord"`` literal."""
    import coord.agent
    import coord.config
    import coord.db
    import coord.state

    expected = default_coord_dir()
    assert coord.db.COORD_DIR == expected
    assert coord.state.COORD_DIR == expected
    assert coord.agent.DEFAULT_STATE_DIR == expected
    assert coord.config.USER_CONFIG_PATH == expected / "coordinator.yml"
