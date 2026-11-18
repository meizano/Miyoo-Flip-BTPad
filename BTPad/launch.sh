#!/bin/sh
# Gamepad Mode - SpruceOS app launcher (oakMOSS: MagicX Mini Zero 28 & friends)
APPDIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APPDIR" || exit 1

HELPER="/mnt/SDCARD/spruce/scripts/helperFunctions.sh"
[ -f "$HELPER" ] && . "$HELPER"

PY=""
for c in \
    /mnt/SDCARD/spruce/flip/bin/python3 \
    /mnt/sdcard/spruce/flip/bin/python3 \
    /mnt/SDCARD/spruce/bin/python/bin/python3.10 \
    python3; do
    if [ -x "$c" ] || command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
[ -z "$PY" ] && { echo "no python found" > gamepadmode.log; exit 1; }

# Same SDL2 setup PyUI uses on this platform (the helper prints a tmpfs dir
# where the base SDL2 is linked next to PyUI's SDL2_image).
if command -v oakmoss_pyui_sdl_dir >/dev/null 2>&1; then
    SDL_DIR="$(oakmoss_pyui_sdl_dir)"
else
    SDL_DIR="/mnt/SDCARD/App/PyUI/dll"
fi
export PYSDL2_DLL_PATH="$SDL_DIR"
export LD_LIBRARY_PATH="$SDL_DIR:/mnt/SDCARD/spruce/flip/lib:/usr/lib"
export SDL_JOYSTICK_DISABLE_UDEV=1

echo "=== $(date) ===" >> gamepadmode.log

# Kill any previous instance of this app (or the old btpad CLI): two SDL
# processes fight over the framebuffer (flicker) and two pads fight over
# the input grab and the USB gadget. Match /proc cmdlines, never pkill -f
# (this script's own ssh session could match and kill itself).
mypid=$$
for d in /proc/[0-9]*; do
    pid="${d#/proc/}"
    [ "$pid" = "$mypid" ] && continue
    cmd="$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null)"
    case "$cmd" in
        *gamepadmode.py*|*btpad.py*)
            [ -f "$d/cmdline" ] && kill "$pid" 2>/dev/null && \
                echo "killed stale instance $pid: $cmd" >> gamepadmode.log
            ;;
    esac
done
sleep 0.5

exec "$PY" "$APPDIR/gamepadmode.py" >> gamepadmode.log 2>&1
