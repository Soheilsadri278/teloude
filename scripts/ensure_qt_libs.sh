#!/bin/sh
# Dev-only helper for minimal Linux containers (Debian/Ubuntu based).
#
# PySide6 wheels need Qt's system libraries (libxkbcommon, libxcb-*, libEGL,
# libGL, ...), which tiny container images may lack; the native loader also only
# honors LD_LIBRARY_PATH from process startup. This downloads those packages and
# extracts them into /tmp/syslibs/sysroot, so tests and the app run with:
#
#   export LD_LIBRARY_PATH=/tmp/syslibs/sysroot/usr/lib/x86_64-linux-gnu
#   python -m pytest -q
#
# Windows and full desktop Linux need none of this. Requires `apt-get download`
# and `dpkg-deb` to be available.
set -e

SYSROOT=/tmp/syslibs/sysroot
LIBDIR="$SYSROOT/usr/lib/x86_64-linux-gnu"

PACKAGES="
libxkbcommon0 libxkbcommon-x11-0
libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0
libxcb-render-util0 libxcb-shape0 libxcb-xkb1 libxcb-xinerama0 libxcb-util1
libegl1 libgl1 libglx0 libopengl0
libdbus-1-3 libfontconfig1 libfreetype6 libexpat1 libuuid1
"

mkdir -p /tmp/syslibs "$SYSROOT"
cd /tmp/syslibs
for package in $PACKAGES; do
    if ls "${package}"_*.deb >/dev/null 2>&1; then
        continue  # already downloaded
    fi
    apt-get download "$package" || echo "note: $package unavailable, skipping"
done
for deb in ./*.deb; do
    [ -e "$deb" ] || continue
    dpkg-deb -x "$deb" "$SYSROOT"
done

if [ ! -f "$LIBDIR/libxkbcommon.so.0" ]; then
    echo "ERROR: libxkbcommon.so.0 was not staged; install it manually." >&2
    exit 1
fi

echo "Qt system libs ready in $SYSROOT"
echo "run:  export LD_LIBRARY_PATH=$LIBDIR"
