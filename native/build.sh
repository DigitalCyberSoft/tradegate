#!/bin/sh
# Build libtwsquiet.so next to this script.
# Requires: gcc, libX11-devel + libXi-devel (Fedora) /
#           libx11-dev + libxi-dev (Debian).
set -eu
cd "$(dirname "$0")"
gcc -shared -fPIC -O2 -Wall -Wextra -o libtwsquiet.so twsquiet.c \
    -lX11 -lXi -ldl -lpthread
echo "built $(pwd)/libtwsquiet.so"
