#!/bin/sh
# Gamepad Mode - SpruceOS app launcher
# Finds a Python that actually runs here, picks an SDL2 directory that loads,
# kills stale instances of itself, and starts the app. It only reads files
# from SpruceOS; everything it writes stays in this GamepadMode directory.
APPDIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APPDIR" || exit 1

echo "=== $(date) ===" >> gamepadmode.log

HELPER="/mnt/SDCARD/spruce/scripts/helperFunctions.sh"
[ -f "$HELPER" ] && . "$HELPER"

# --- Python: some images put an aarch64 binary at the "flip" path even on
# armv7 hardware, so test each candidate rather than trusting the path.
PY=""
for c in \
    /mnt/SDCARD/spruce/flip/bin/python3 \
    /mnt/sdcard/spruce/flip/bin/python3 \
    /mnt/SDCARD/spruce/bin/python/bin/python3.10 \
    /mnt/sdcard/spruce/bin/python/bin/python3.10 \
    python3; do
    if [ -x "$c" ] || command -v "$c" >/dev/null 2>&1; then
        if "$c" -c "print(1)" >/dev/null 2>&1; then PY="$c"; break; fi
        echo "skipping unusable python: $c" >> gamepadmode.log
    fi
done
[ -z "$PY" ] && { echo "no usable python found" >> gamepadmode.log; exit 1; }
echo "using python: $PY" >> gamepadmode.log

BITS="$("$PY" -c 'import struct; print(struct.calcsize("P") * 8)' 2>/dev/null)"
if [ "$BITS" = "32" ]; then
    # armv7 handhelds (Miyoo Mini / Zero 28 family): their Python needs
    # libutil from miyoomini/lib; SDL2+TTF live in miyoo/lib, with freetype
    # and bz2 in these auxiliary dirs.
    BASE_LIBS="/mnt/SDCARD/spruce/miyoomini/lib:/mnt/SDCARD/miyoo/lib:/mnt/SDCARD/Emu/NDS/libs:/mnt/SDCARD/spruce/flip/lib32:/usr/lib"
    SDL_CANDIDATES="/mnt/SDCARD/miyoo/lib /mnt/SDCARD/App/PyUI/dll /mnt/SDCARD/App/PyUI/dll-mali"
else
    # aarch64 handhelds (Miyoo Flip / Zero 28): PyUI's SDL2 setup.
    BASE_LIBS="/mnt/SDCARD/spruce/flip/lib:/usr/lib"
    SDL_CANDIDATES="/mnt/SDCARD/App/PyUI/dll /mnt/SDCARD/App/PyUI/dll-mali"
fi

# The oakMOSS helper knows where this platform links its SDL2; try it first.
if command -v oakmoss_pyui_sdl_dir >/dev/null 2>&1; then
    SDL_CANDIDATES="$(oakmoss_pyui_sdl_dir) $SDL_CANDIDATES"
fi

export LD_LIBRARY_PATH="$BASE_LIBS"
SDL_DIR=""
for d in $SDL_CANDIDATES; do
    [ -d "$d" ] || continue
    if PYSDL2_DLL_PATH="$d" "$PY" -c "import sdl2, sdl2.sdlttf" >/dev/null 2>&1; then
        SDL_DIR="$d"
        break
    fi
    echo "skipping unusable SDL2 dir: $d" >> gamepadmode.log
done
[ -z "$SDL_DIR" ] && echo "warning: no SDL2+TTF combination found; UI cannot start" >> gamepadmode.log
export PYSDL2_DLL_PATH="$SDL_DIR"
export LD_LIBRARY_PATH="$SDL_DIR:$BASE_LIBS"
export SDL_JOYSTICK_DISABLE_UDEV=1
echo "SDL2: $SDL_DIR (python $BITS-bit)" >> gamepadmode.log

# --- Single instance: two copies fight over the framebuffer, the input grab
# and the USB gadget. Match /proc cmdlines, never pkill -f (an ssh session
# running this command could match itself and get killed).
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
