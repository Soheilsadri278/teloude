#!/bin/sh
# Dev-only helper for minimal Linux containers missing Qt system libraries
# (Debian/Ubuntu). Not needed on Windows or full desktop Linux.
# Extracts libxkbcommon into /tmp/syslibs; tests pick it up via conftest.py.
set -e
mkdir -p /tmp/syslibs
cd /tmp/syslibs
if [ ! -f sysroot/usr/lib/x86_64-linux-gnu/libxkbcommon.so.0 ]; then
  apt-get download libxkbcommon0
  rm -rf sysroot
  dpkg-deb -x libxkbcommon0_*_amd64.deb sysroot
fi
echo "Qt system libs ready in /tmp/syslibs/sysroot"
