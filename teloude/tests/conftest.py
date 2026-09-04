# teloude/tests/conftest.py
"""Test bootstrap: makes Qt importable on minimal Linux containers.

PySide6 wheels need libxkbcommon, which tiny containers may lack (Windows
and desktop Linux are unaffected). The native loader only honors
LD_LIBRARY_PATH from process startup, so tweaking os.environ here would be
too late; instead we preload the library by absolute path before any Qt
import. Run scripts/ensure_qt_libs.sh first on such containers.
"""
import ctypes
import ctypes.util
import os

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
