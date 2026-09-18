# teloude/tests/conftest.py
"""Test bootstrap: Qt on minimal containers, and the two hang watchdogs.

PySide6 wheels need libxkbcommon, which tiny containers may lack (Windows
and desktop Linux are unaffected). The native loader only honors
LD_LIBRARY_PATH from process startup, so tweaking os.environ here would be
too late; instead we preload the library by absolute path before any Qt
import. Run scripts/ensure_qt_libs.sh first on such containers.

The watchdogs that keep a blocked test from holding the whole CI job live in
`teloude/tests/watchdog_plugin.py`. Their pytest hooks are imported below, which
registers them for this directory, and `teloude/tests/test_hang_watchdog.py`
proves that both layers terminate a hang promptly, name the test and exit
non-zero.
"""
import ctypes
import ctypes.util
import os
import sys

_EXTRA_LIB_DIRS = (
    "/tmp/syslibs/sysroot/usr/lib/x86_64-linux-gnu",
    os.path.expanduser("~/.syslibs/usr/lib/x86_64-linux-gnu"),
)


def _ensure_qt_system_libs() -> None:
    if ctypes.util.find_library("xkbcommon") is not None:
        return
    for directory in _EXTRA_LIB_DIRS:
        probe = os.path.join(directory, "libxkbcommon.so.0")
        if os.path.isfile(probe):
            try:
                ctypes.CDLL(probe, mode=ctypes.RTLD_GLOBAL)
            except OSError:
                continue
            return


_ensure_qt_system_libs()

# Importing hook functions registers them with pytest; they stay defined in the
# plugin module, where the watchdog state lives.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from watchdog_plugin import (  # noqa: E402,F401  (pytest finds these hooks by name)
    pytest_collection_finish,
    pytest_configure,
    pytest_runtest_logfinish,
    pytest_runtest_logstart,
    pytest_sessionstart,
    pytest_terminal_summary,
    pytest_unconfigure,
)
