#!/bin/sh
# BTPad launcher for SpruceOS (Miyoo Flip, MagicX Mini Zero 28 and other oakMOSS devices)
APPDIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APPDIR" || exit 1

PY=""
for c in \
    /mnt/sdcard/spruce/flip/bin/python3 \
    /mnt/SDCARD/spruce/flip/bin/python3 \
    /mnt/sdcard/spruce/bin/python/bin/python3.10 \
    /mnt/SDCARD/spruce/bin/python/bin/python3.10 \
    /mnt/sdcard/spruce/bin/python/bin/python3 \
    /mnt/SDCARD/spruce/bin/python/bin/python3 \
    /root/bin/python3 \
    python3; do
    if [ -x "$c" ] || command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
[ -z "$PY" ] && { echo "no python found" > btpad.log; exit 1; }

echo "=== $(date) ===" >> btpad.log
exec "$PY" btpad.py "$@" >> btpad.log 2>&1
