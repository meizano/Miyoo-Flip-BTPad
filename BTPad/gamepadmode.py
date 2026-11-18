#!/usr/bin/env python3
"""Gamepad Mode - SpruceOS app UI for BTPad.

A menu on the handheld itself: start the pad, test the controls without
connecting, remap or disable buttons, set analog sensitivity, reset, save.
While the pad runs, the screen shows a RetroArch-style controller layout
with live press feedback and the battery percentage.

Runs on pysdl2 + SDL_ttf (the libraries Spruce's PyUI already ships), draws
at the panel's native size (the MagicX Mini Zero 28 is 640x480) and reads
the pad straight from evdev. The pad itself is the imported btpad module,
so Bluetooth and USB behaviour are exactly the CLI's.
"""
import ctypes
import json
import os
import select
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import btpad as B

try:                       # only needed on the device
    import sdl2
    import sdl2.sdlttf as ttf
except ImportError:
    sdl2 = ttf = None      # Ui() reports this clearly

EVENT_FMT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FMT)
EV_KEY, EV_ABS = 1, 3

# UI controls (evdev codes on the Zero 28's simplepad)
KEY_UP, KEY_DOWN, KEY_LEFT, KEY_RIGHT = 103, 108, 105, 106
KEY_A, KEY_B, KEY_MENU = 304, 305, 158

FONT_DIR = "/mnt/SDCARD/App/PyUI/fonts"
FONT_REGULAR = os.path.join(FONT_DIR, "BeVietnamPro-Regular.ttf")
FONT_BOLD = os.path.join(FONT_DIR, "BeVietnamPro-SemiBold.ttf")
SETTINGS_FILE = os.path.join(HERE, "settings_ui.json")

BG = (24, 26, 32)
FG = (235, 235, 235)
DIM = (110, 115, 125)
HI = (255, 170, 40)      # selection highlight
ON = (90, 220, 120)      # pressed / enabled
OFF = (70, 74, 84)       # disabled
BODY = (52, 56, 66)
WARN = (255, 90, 90)

# Stick axes report 0..255 around a 128 centre; the Zero 28's raw range.
STICK_RANGES = {0: (-32768, 32767), 1: (-32768, 32767),
                2: (-32768, 32767), 5: (-32768, 32767)}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def default_settings():
    """Logical name -> evdev code, from the Zero 28 profile (the codes we
    verified against the hardware)."""
    by_hid = {hid: name for name, hid in B.LOGICAL_BUTTONS}
    remap = {}
    for code, hid in sorted(B.ZERO28_KEYMAP.items()):
        if hid in by_hid:
            remap[by_hid[hid]] = code
    return {"remap": remap, "disabled": [],
            "sensitivity": {a: 100 for a in B.AXIS_NAMES}}


def load_settings():
    d = default_settings()
    try:
        with open(SETTINGS_FILE) as f:
            saved = json.load(f)
        for k in ("remap", "sensitivity"):
            if isinstance(saved.get(k), dict):
                d[k].update({a: b for a, b in saved[k].items() if a in d[k]})
        if isinstance(saved.get("disabled"), list):
            d["disabled"] = [x for x in saved["disabled"] if x in d["remap"]]
    except (OSError, ValueError):
        pass
    return d


def save_settings(d):
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, SETTINGS_FILE)


def read_battery():
    try:
        with open("/sys/class/power_supply/axp2202-battery/capacity") as f:
            pct = f.read().strip() + "%"
        with open("/sys/class/power_supply/axp2202-battery/status") as f:
            st = f.read().strip()
        return pct + (" " + st if st == "Charging" else "")
    except OSError:
        return "?"


class ShimPad:
    """Just enough of btpad.Pad for State.feed in the test screen."""
    ranges = STICK_RANGES


class TestReader:
    """Raw evdev reader for the Test Gamepad screen: keys AND axes, from
    every event node (the pad's node name/number varies by device)."""

    def __init__(self):
        self.fds = []
        try:
            nodes = sorted("/dev/input/" + d for d in os.listdir("/dev/input")
                           if d.startswith("event"))
        except OSError:
            nodes = ["/dev/input/event2"]
        for p in nodes:
            try:
                self.fds.append(os.open(p, os.O_RDONLY | os.O_NONBLOCK))
            except OSError:
                pass

    def poll(self, state):
        """Feed one pass of events into a btpad.State."""
        for fd in self.fds:
            r, _, _ = select.select([fd], [], [], 0)
            if not r:
                continue
            try:
                data = os.read(fd, EVENT_SIZE * 32)
            except OSError:
                continue
            for off in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
                _s, _u, typ, code, val = struct.unpack(EVENT_FMT, data[off:off + EVENT_SIZE])
                if typ in (EV_KEY, EV_ABS):
                    state.feed(ShimPad, typ, code, val)

    def close(self):
        for fd in self.fds:
            try:
                os.close(fd)
            except OSError:
                pass


class PadInput:
    """Non-blocking evdev reader with key repeat for menu navigation."""

    def __init__(self, paths=None):
        if paths is None:
            try:
                paths = sorted("/dev/input/" + d for d in os.listdir("/dev/input")
                               if d.startswith("event"))
            except OSError:
                paths = ["/dev/input/event2", "/dev/input/event0"]
        self.fds = []
        for p in paths:
            try:
                self.fds.append(os.open(p, os.O_RDONLY | os.O_NONBLOCK))
            except OSError:
                pass
        self.held = {}
        self.queue = []

    def poll(self):
        """-> list of key-down codes this pass (with repeats applied)."""
        for fd in self.fds:
            r, _, _ = select.select([fd], [], [], 0)
            if not r:
                continue
            try:
                data = os.read(fd, EVENT_SIZE * 16)
            except OSError:
                continue
            for off in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
                _s, _u, typ, code, val = struct.unpack(EVENT_FMT, data[off:off + EVENT_SIZE])
                if typ != EV_KEY:
                    continue
                if val == 1:
                    self.held[code] = time.time()
                    self.queue.append((code, 1))
                elif val == 0:
                    self.held.pop(code, None)
                    self.queue.append((code, 0))
        now = time.time()
        for code, since in list(self.held.items()):
            if now - since > 0.45:
                self.held[code] = now - 0.33   # repeat every 0.12s
                self.queue.append((code, 1))
        out, self.queue = self.queue, []
        return out

    def close(self):
        for fd in self.fds:
            try:
                os.close(fd)
            except OSError:
                pass


class Ui:
    def __init__(self):
        if sdl2 is None:
            raise RuntimeError("pysdl2 is not available on this python; "
                               "Gamepad Mode needs Spruce's python with PyUI's SDL2")
        self.sdl, self.ttf = sdl2, ttf
        if sdl2.SDL_Init(sdl2.SDL_INIT_VIDEO) != 0:
            raise RuntimeError("SDL_Init failed: %s" % sdl2.SDL_GetError().decode())
        if ttf.TTF_Init() != 0:
            raise RuntimeError("TTF_Init failed")
        self.window = sdl2.SDL_CreateWindow(b"Gamepad Mode",
                                            sdl2.SDL_WINDOWPOS_CENTERED, sdl2.SDL_WINDOWPOS_CENTERED,
                                            0, 0, sdl2.SDL_WINDOW_FULLSCREEN_DESKTOP)
        if not self.window:
            raise RuntimeError("SDL_CreateWindow failed: %s" % sdl2.SDL_GetError().decode())
        self.ren = sdl2.SDL_CreateRenderer(self.window, -1,
                                           sdl2.SDL_RENDERER_ACCELERATED | sdl2.SDL_RENDERER_PRESENTVSYNC)
        if not self.ren:
            self.ren = sdl2.SDL_CreateRenderer(self.window, -1, 0)
        if not self.ren:
            raise RuntimeError("SDL_CreateRenderer failed: %s" % sdl2.SDL_GetError().decode())
        pw, ph = ctypes.c_int(), ctypes.c_int()
        sdl2.SDL_GetWindowSize(self.window, ctypes.byref(pw), ctypes.byref(ph))
        self.w = pw.value if pw.value > 0 else 640
        self.h = ph.value if ph.value > 0 else 480
        log("screen %dx%d" % (self.w, self.h))
        self.fonts = {}
        self.cache = {}
        sdl2.SDL_SetRenderDrawColor(self.ren, *BG, 255)
        sdl2.SDL_RenderClear(self.ren)
        sdl2.SDL_RenderPresent(self.ren)

    def font(self, size, bold=False):
        key = (size, bold)
        if key not in self.fonts:
            path = FONT_BOLD if bold and os.path.exists(FONT_BOLD) else FONT_REGULAR
            self.fonts[key] = self.ttf.TTF_OpenFont(path.encode(), size)
        return self.fonts[key]

    def text(self, s, size, color, bold=False):
        key = (s, size, color, bold)
        tex = self.cache.get(key)
        if tex is None:
            surf = self.ttf.TTF_RenderUTF8_Blended(self.font(size, bold), s.encode(),
                                                   sdl2.SDL_Color(*color, 255))
            tex = sdl2.SDL_CreateTextureFromSurface(self.ren, surf)
            sdl2.SDL_FreeSurface(surf)
            if len(self.cache) > 300:
                for k in list(self.cache)[:100]:
                    sdl2.SDL_DestroyTexture(self.cache.pop(k))
            self.cache[key] = tex
        return tex

    def draw_text(self, s, x, y, size, color=FG, bold=False, center=None):
        tex = self.text(s, size, color, bold)
        pw, ph = ctypes.c_int(), ctypes.c_int()
        if sdl2.SDL_QueryTexture(tex, None, None, ctypes.byref(pw), ctypes.byref(ph)) != 0:
            return
        if center == "x":
            x -= pw.value // 2
        rect = sdl2.SDL_Rect(int(x), int(y), pw.value, ph.value)
        sdl2.SDL_RenderCopy(self.ren, tex, None, ctypes.byref(rect))
        return pw.value, ph.value

    def rect(self, x, y, w, h, color, outline=False):
        sdl2.SDL_SetRenderDrawColor(self.ren, *color, 255)
        r = sdl2.SDL_Rect(int(x), int(y), int(w), int(h))
        if outline:
            sdl2.SDL_RenderDrawRect(self.ren, ctypes.byref(r))
        else:
            sdl2.SDL_RenderFillRect(self.ren, ctypes.byref(r))

    def circle(self, cx, cy, radius, color, fill=True):
        sdl2.SDL_SetRenderDrawColor(self.ren, *color, 255)
        for dy in range(-radius, radius + 1):
            span = int((radius * radius - dy * dy) ** 0.5)
            if fill:
                sdl2.SDL_RenderDrawLine(self.ren, cx - span, cy + dy, cx + span, cy + dy)
            else:
                for sx in (-span, span):
                    sdl2.SDL_RenderDrawPoint(self.ren, cx + sx, cy + dy)

    def line(self, x1, y1, x2, y2, color, width=1):
        sdl2.SDL_SetRenderDrawColor(self.ren, *color, 255)
        if width <= 1:
            sdl2.SDL_RenderDrawLine(self.ren, int(x1), int(y1), int(x2), int(y2))
            return
        if abs(y2 - y1) < 2:
            self.rect(min(x1, x2), y1 - width // 2, abs(x2 - x1), width, color)
        elif abs(x2 - x1) < 2:
            self.rect(x1 - width // 2, min(y1, y2), width, abs(y2 - y1), color)

    def present(self):
        sdl2.SDL_RenderPresent(self.ren)

    def quit(self):
        for t in self.cache.values():
            self.sdl.SDL_DestroyTexture(t)
        for f in self.fonts.values():
            self.ttf.TTF_CloseFont(f)
        if self.ren:
            self.sdl.SDL_DestroyRenderer(self.ren)
        if self.window:
            self.sdl.SDL_DestroyWindow(self.window)
        self.ttf.TTF_Quit()
        self.sdl.SDL_Quit()


class App:
    """Screens: main / settings / remap / remap_wait / disable / sens /
    confirm / padtest, plus the live layout while the pad runs."""

    def __init__(self):
        self.ui = Ui()
        self.input = PadInput()
        self.settings = load_settings()
        self.screen = "main"
        self.sel = 0
        self.toast = None
        self.toast_until = 0
        self.wait_for = None        # logical button waiting to be remapped
        self.confirm = None         # (question, on_yes)
        self.dirty = True
        self.bat = read_battery()
        self.bat_at = time.time()
        self.test = None            # (reader, state) while testing the pad
        self._last_draw = 0.0
        self._usb_at = 0.0
        self._usb_st_cache = None

    # ------------------------------------------------------------- helpers
    def say(self, s, secs=1.4):
        self.toast = s
        self.toast_until = time.time() + secs
        self.dirty = True

    def logical_of_code(self, code):
        for name, hid in B.LOGICAL_BUTTONS:
            if self.settings["remap"].get(name) == code:
                return name
        return None

    def code_of_logical(self, name):
        return self.settings["remap"].get(name, 0)

    def is_disabled(self, name):
        return name in self.settings["disabled"]

    def main_items(self):
        return ["Start Gamepad Mode", "Test Gamepad", "Settings", "Exit"]

    def settings_items(self):
        return ["Remap Buttons", "Disable Buttons", "Analog Sensitivity",
                "Reset to Defaults", "Save Configuration", "OK"]

    def handle(self, key, val=1):
        """One evdev key event, dispatched by screen. val is 1 (press),
        0 (release); only remap_wait looks at releases."""
        scr = self.screen
        if scr == "remap_wait":
            # Any single press becomes the new button; the commit happens on
            # RELEASE, so pressing Start+Select together (both held when the
            # second one goes down) cancels instead of remapping either.
            if val == 1:
                self.cand = key
                if 314 in self.input.held and 315 in self.input.held:
                    self.cand = None
                    self.screen = "remap"
                    self.say("Cancelled")
            elif key == getattr(self, "cand", None):
                self.settings["remap"][self.wait_for] = key
                log("remap: %s -> evdev %d" % (self.wait_for, key))
                self.cand = None
                self.screen = "remap"
            self.dirty = True
            return
        if val == 0:
            return
        if scr == "confirm":
            if key == KEY_A and self.confirm:
                act = self.confirm[1]
                self.confirm = None
                act()
            elif key == KEY_B or key == KEY_MENU:
                self.confirm = None
                self.screen = "settings" if self._confirm_from == "settings" else "main"
            self.dirty = True
            return
        if scr == "padtest":
            if val == 1 and 314 in self.input.held and 315 in self.input.held:
                self.stop_test()
            self.dirty = True
            return
            self.dirty = True
            return
        if scr == "main":
            items = self.main_items()
            if key == KEY_UP:
                self.sel = (self.sel - 1) % len(items)
            elif key == KEY_DOWN:
                self.sel = (self.sel + 1) % len(items)
            elif key == KEY_A:
                if self.sel == 0:
                    self.start_pad()
                elif self.sel == 1:
                    self.start_test()
                elif self.sel == 2:
                    self.screen, self.sel = "settings", 0
                else:
                    self.ask_confirm("Exit Gamepad Mode?", "main", self.quit)
            elif key == KEY_MENU:
                self.ask_confirm("Exit Gamepad Mode?", "main", self.quit)
            self.dirty = True
            return
        if scr == "settings":
            items = self.settings_items()
            if key == KEY_UP:
                self.sel = (self.sel - 1) % len(items)
            elif key == KEY_DOWN:
                self.sel = (self.sel + 1) % len(items)
            elif key == KEY_B or key == KEY_MENU:
                self.screen, self.sel = "main", 0
            elif key == KEY_A:
                choice = items[self.sel]
                if choice == "Remap Buttons":
                    self.screen, self.sel = "remap", 0
                elif choice == "Disable Buttons":
                    self.screen, self.sel = "disable", 0
                elif choice == "Analog Sensitivity":
                    self.screen, self.sel = "sens", 0
                elif choice == "Reset to Defaults":
                    self.ask_confirm("Reset all settings to defaults?", "settings", self.do_reset)
                elif choice == "Save Configuration":
                    self.do_save()
                elif choice == "OK":
                    self.screen, self.sel = "main", 0
            self.dirty = True
            return
        if scr == "remap":
            names = [n for n, _ in B.LOGICAL_BUTTONS]
            if key == KEY_UP:
                self.sel = (self.sel - 1) % len(names)
            elif key == KEY_DOWN:
                self.sel = (self.sel + 1) % len(names)
            elif key == KEY_B or key == KEY_MENU:
                self.screen, self.sel = "settings", 0
            elif key == KEY_A:
                self.wait_for = names[self.sel]
                self.screen = "remap_wait"
            self.dirty = True
            return
        if scr == "disable":
            names = [n for n, _ in B.LOGICAL_BUTTONS]
            if key == KEY_UP:
                self.sel = (self.sel - 1) % len(names)
            elif key == KEY_DOWN:
                self.sel = (self.sel + 1) % len(names)
            elif key == KEY_B or key == KEY_MENU:
                self.screen, self.sel = "settings", 0
            elif key == KEY_A:
                n = names[self.sel]
                if self.is_disabled(n):
                    self.settings["disabled"].remove(n)
                else:
                    self.settings["disabled"].append(n)
            self.dirty = True
            return
        if scr == "sens":
            axes = list(B.AXIS_NAMES)
            if key == KEY_UP:
                self.sel = (self.sel - 1) % len(axes)
            elif key == KEY_DOWN:
                self.sel = (self.sel + 1) % len(axes)
            elif key in (KEY_LEFT, KEY_RIGHT):
                a = axes[self.sel]
                step = 5 if key == KEY_RIGHT else -5
                v = max(50, min(150, self.settings["sensitivity"].get(a, 100) + step))
                self.settings["sensitivity"][a] = v
            elif key == KEY_B or key == KEY_MENU:
                self.screen, self.sel = "settings", 0
            self.dirty = True
            return

    def ask_confirm(self, question, back_to, on_yes):
        self._confirm_from = back_to
        self.confirm = (question, on_yes)
        self.screen = "confirm"
        self.dirty = True

    def do_reset(self):
        self.settings = default_settings()
        self.say("Reset to defaults (not saved yet)")
        self.screen = "settings"

    def do_save(self):
        save_settings(self.settings)
        self.say("Saved")
        log("settings saved to %s" % SETTINGS_FILE)

    def quit(self):
        self.running = False

    # ------------------------------------------------------------- test mode
    def start_test(self):
        """Show the layout and live input without touching USB/Bluetooth."""
        try:
            self.test = (TestReader(), self._test_state())
        except OSError as e:
            self.say("no pad found: %s" % e)
            return
        self.screen = "padtest"
        self.dirty = True

    def _test_state(self):
        keys = {int(c): int(hid) for name, hid in B.LOGICAL_BUTTONS
                for c in [self.code_of_logical(name)] if c}
        disabled = set(self.settings["disabled"])
        keys = {c: h for c, h in keys.items() if h not in disabled}
        dpad = dict(B.DEFAULT_DPAD_KEYS)
        sens = dict(self.settings["sensitivity"])
        return B.State(keys, dpad, dict(B.ZERO28_AXES), {}, sensitivity=sens)

    def stop_test(self):
        if self.test:
            self.test[0].close()
            self.test = None
        self.screen = "main"
        self.dirty = True

    # ------------------------------------------------------------- drawing
    def frame(self):
        if time.time() > self.bat_at:
            self.bat = read_battery()
            self.bat_at = time.time() + 30
        if self.screen == "confirm":
            self.draw_confirm()
        elif self.screen == "remap":
            self.draw_remap()
        elif self.screen == "remap_wait":
            self.draw_remap_wait()
        elif self.screen == "disable":
            self.draw_disable()
        elif self.screen == "sens":
            self.draw_sens()
        elif self.screen == "settings":
            self.draw_list("Settings", self.settings_items())
        else:
            self.draw_main()
        if self.toast and time.time() < self.toast_until:
            self.draw_toast()
        elif self.toast:
            self.toast = None
            self.dirty = True

    def draw_main(self):
        u = self.ui
        u.rect(0, 0, u.w, u.h, BG)
        u.draw_text("Gamepad Mode", u.w // 2, 70, 36, FG, bold=True, center="x")
        u.draw_text(self.bat, u.w - 16, 14, 20, DIM)
        items = self.main_items()
        y = 160
        for i, it in enumerate(items):
            sel = i == self.sel
            u.draw_text(it, u.w // 2, y, 30, HI if sel else FG, bold=sel, center="x")
            if sel:
                u.line(u.w // 2 - 130, y + 38, u.w // 2 + 130, y + 38, HI, 2)
            y += 66
        u.draw_text("A: select   Menu: exit", u.w // 2, u.h - 44, 20, DIM, center="x")
        u.present()

    def draw_list(self, title, items):
        u = self.ui
        u.rect(0, 0, u.w, u.h, BG)
        u.draw_text(title, u.w // 2, 26, 30, FG, bold=True, center="x")
        y = 90
        for i, it in enumerate(items):
            sel = i == self.sel
            u.draw_text(it, 60, y, 26, HI if sel else FG, bold=sel)
            if sel:
                u.line(44, y + 34, u.w - 44, y + 34, HI, 2)
            y += 52
        u.draw_text("A: select   B: back", u.w // 2, u.h - 40, 20, DIM, center="x")
        u.present()

    def draw_remap(self):
        u = self.ui
        u.rect(0, 0, u.w, u.h, BG)
        u.draw_text("Remap Buttons", u.w // 2, 20, 30, FG, bold=True, center="x")
        y = 82
        for i, (name, _hid) in enumerate(B.LOGICAL_BUTTONS):
            sel = i == self.sel
            code = self.code_of_logical(name)
            dis = self.is_disabled(name)
            col = OFF if dis else (HI if sel else FG)
            u.draw_text(name, 50, y, 22, col, bold=sel)
            u.draw_text("evdev %d%s" % (code, "  [disabled]" if dis else ""),
                        u.w - 50, y, 22, OFF if dis else DIM, center="x")
            if sel:
                u.line(36, y + 28, u.w - 36, y + 28, HI, 2)
            y += 29
        u.draw_text("A: press new button   B: back", u.w // 2, u.h - 30, 20, DIM, center="x")
        u.draw_text("B here means the physical B, not cancel", u.w // 2, u.h - 58, 18, DIM, center="x")
        u.present()

    def draw_remap_wait(self):
        u = self.ui
        u.rect(0, 0, u.w, u.h, BG)
        cur = self.code_of_logical(self.wait_for)
        u.draw_text("Remap: %s" % self.wait_for, u.w // 2, 130, 34, FG, bold=True, center="x")
        u.draw_text("currently evdev %d" % cur, u.w // 2, 180, 24, DIM, center="x")
        u.draw_text("Press the physical button", u.w // 2, 260, 28, HI, center="x")
        u.draw_text("you want for %s" % self.wait_for, u.w // 2, 300, 28, HI, center="x")
        u.draw_text("press Start+Select together to cancel", u.w // 2, u.h - 60, 20, DIM, center="x")
        u.present()

    def draw_disable(self):
        u = self.ui
        u.rect(0, 0, u.w, u.h, BG)
        u.draw_text("Disable Buttons", u.w // 2, 20, 30, FG, bold=True, center="x")
        y = 82
        for i, (name, _hid) in enumerate(B.LOGICAL_BUTTONS):
            sel = i == self.sel
            dis = self.is_disabled(name)
            u.draw_text(name, 50, y, 22, HI if sel else FG, bold=sel)
            u.draw_text("OFF (ignored)" if dis else "ON", u.w - 50, y, 22,
                        WARN if dis else ON, center="x")
            if sel:
                u.line(36, y + 28, u.w - 36, y + 28, HI, 2)
            y += 29
        u.draw_text("A: toggle   B: back", u.w // 2, u.h - 30, 20, DIM, center="x")
        u.present()

    def draw_sens(self):
        u = self.ui
        u.rect(0, 0, u.w, u.h, BG)
        u.draw_text("Analog Sensitivity", u.w // 2, 26, 30, FG, bold=True, center="x")
        names = {"lx": "Left stick  X", "ly": "Left stick  Y",
                 "rx": "Right stick X", "ry": "Right stick Y"}
        y = 110
        for i, a in enumerate(B.AXIS_NAMES):
            sel = i == self.sel
            v = self.settings["sensitivity"].get(a, 100)
            u.draw_text(names[a], 50, y, 26, HI if sel else FG, bold=sel)
            bar_w = 280
            frac = (v - 50) / 100.0
            u.rect(230, y + 6, bar_w, 18, OFF)
            u.rect(230, y + 6, max(1, int(bar_w * frac)), 18, HI if sel else ON)
            u.draw_text("%d%%" % v, 540, y, 26, FG)
            if sel:
                u.line(36, y + 38, u.w - 36, y + 38, HI, 2)
            y += 72
        u.draw_text("left/right: change   B: back", u.w // 2, u.h - 60, 20, DIM, center="x")
        u.draw_text("100% = the stick's full reported travel", u.w // 2, u.h - 32, 18, DIM, center="x")
        u.present()

    def draw_confirm(self):
        u = self.ui
        u.rect(0, 0, u.w, u.h, BG)
        q = self.confirm[0] if self.confirm else "?"
        u.draw_text(q, u.w // 2, 190, 28, FG, bold=True, center="x")
        u.draw_text("A: yes      B: no", u.w // 2, 260, 26, HI, center="x")
        u.present()

    def draw_toast(self):
        u = self.ui
        w = 360
        u.rect((u.w - w) // 2, u.h - 110, w, 48, BODY)
        u.rect((u.w - w) // 2, u.h - 110, w, 48, HI, outline=True)
        u.draw_text(self.toast, u.w // 2, u.h - 100, 22, FG, bold=True, center="x")

    def _usb_state(self):
        """The UDC's link state, refreshed once a second: 'not attached'
        means no host is electrically present (cable out, charge-only
        cable, or the DC port instead of the data port)."""
        if time.time() > self._usb_at:
            self._usb_at = time.time() + 1.0
            import glob as _g
            self._usb_st_cache = None
            for p in _g.glob("/sys/class/udc/*/state"):
                try:
                    with open(p) as f:
                        self._usb_st_cache = f.read().strip()
                except OSError:
                    pass
        return self._usb_st_cache

    # ------------------------------------------------------------- pad run
    def start_pad(self):
        u = self.ui
        overrides = {
            "keys": {int(code): int(hid) for name, hid in B.LOGICAL_BUTTONS
                     for code in [self.code_of_logical(name)] if code},
            "disabled": list(self.settings["disabled"]),
            "sensitivity": dict(self.settings["sensitivity"]),
        }
        # Disabled buttons must not reach the report either.
        overrides["keys"] = {c: h for c, h in overrides["keys"].items()
                             if h not in set(overrides["disabled"])}
        u.rect(0, 0, u.w, u.h, BG)
        u.draw_text("Starting Gamepad Mode...", u.w // 2, u.h // 2 - 16, 30, FG, bold=True, center="x")
        u.present()
        log("pad starting with overrides:", overrides)
        try:
            rc = B.run(frame_cb=lambda state: self.draw_running(state),
                       overrides=overrides)
        except Exception as e:
            import traceback
            log("pad crashed:", repr(e))
            log(traceback.format_exc())
            self.say("Pad error: %s" % e, 3.0)
            return
        if rc == 0:
            # Clean stop (the exit combo was held, or SIGTERM): leave the
            # app entirely, back to Spruce.
            log("pad stopped cleanly; leaving Gamepad Mode")
            self.running = False
        else:
            self.say("Pad did not start - check gamepadmode.log", 3.0)

    def draw_running(self, state):
        """The live layout screen: RetroArch-style pad with press feedback."""
        now = time.time()
        if now - self._last_draw < 0.045:      # ~20 fps is plenty and stops flicker
            return
        self._last_draw = now
        u = self.ui
        W, H = u.w, u.h
        u.rect(0, 0, W, H, BG)
        title = "TEST" if self.screen == "padtest" else "GAMEPAD MODE"
        u.draw_text(title, 24, 10, 24, FG, bold=True)
        u.draw_text(read_battery(), W - 16, 14, 20, DIM)
        down = {}
        for name, hid in B.LOGICAL_BUTTONS:
            down[name] = bool(state.buttons & (1 << (hid - 1))) and not self.is_disabled(name)

        # pad body
        u.rect(30, 132, W - 60, 320, BODY)
        u.rect(30, 132, W - 60, 320, (66, 70, 82), outline=True)

        # shoulders: L2/L1 on the left, R2/R1 on the right
        pairs = [("L2", 10, 48), ("L1", 10, 90),
                 ("R2", W - 110, 48), ("R1", W - 110, 90)]
        for label, x, y in pairs:
            col = ON if down.get(label) else (OFF if self.is_disabled(label) else BODY)
            u.rect(x, y, 100, 36, col, outline=not down.get(label))
            u.draw_text(label, x + 50, y + 6, 20, BG if down.get(label) else FG, bold=True, center="x")

        # D-pad: four direction buttons in a diamond, like the ABXY cluster
        dcx, dcy, off, r = 150, 240, 46, 24
        arrows = (("up", "^", dcx, dcy - off), ("down", "v", dcx, dcy + off),
                  ("left", "<", dcx - off, dcy), ("right", ">", dcx + off, dcy))
        u.circle(dcx, dcy, 12, (58, 62, 74))
        for dname, arrow, x, y in arrows:
            active = state.dir[dname]
            u.circle(x, y, r, ON if active else (58, 62, 74))
            u.draw_text(arrow, x, y - 12, 22, BG if active else FG, bold=True, center="x")

        # face diamond at right (labels = the logical names on these spots now)
        fx, fy, r = W - 150, 220, 24
        spots = (("A", fx, fy + off), ("B", fx + off, fy), ("X", fx - off, fy), ("Y", fx, fy - off))
        for name, x, y in spots:
            dis = self.is_disabled(name)
            on = down.get(name)
            if not dis:
                u.circle(x, y, r, ON if on else BODY)
                u.circle(x, y, r, FG if on else DIM, fill=False)
            u.draw_text(name, x, y - 12, 22, (OFF if dis else (BG if on else FG)), bold=True, center="x")

        # select / start / menu, centred between the two clusters
        for name, y in (("Select", 160), ("Start", 220), ("Menu", 280)):
            col = ON if down.get(name) else BODY
            u.rect(W // 2 - 50, y, 100, 32, col, outline=not down.get(name))
            u.draw_text(name, W // 2, y + 5, 20, BG if down.get(name) else FG, bold=True, center="x")

        # sticks (knob follows the axis; ring lights on stick click)
        for label, sx, sy in (("L3", 150, 395), ("R3", W - 150, 395)):
            on = down.get(label)
            u.circle(sx, sy, 42, ON if on else DIM, fill=False)
            ax_name = "lx" if label == "L3" else "rx"
            ay_name = "ly" if label == "L3" else "ry"
            kx = (state.ax[ax_name] - 128) / 127.0 * 26
            ky = (state.ax[ay_name] - 128) / 127.0 * 26
            u.circle(int(sx + kx), int(sy + ky), 22, ON if on else (72, 78, 92))

        st = self._usb_state() if self.screen != "padtest" else None
        if st == "not attached":
            u.draw_text("no USB host: check the cable is in the data port",
                        W // 2, 60, 20, WARN, center="x")
        elif st == "configured":
            u.draw_text("USB host connected", W // 2, 60, 20, ON, center="x")
        elif st:
            u.draw_text("USB: %s" % st, W // 2, 60, 20, DIM, center="x")
        if self.screen == "padtest":
            u.draw_text("press Start+Select together to exit the test", W // 2, H - 30, 20, DIM, center="x")
        else:
            u.draw_text("hold L1+R1+Select+Start to quit", W // 2, H - 30, 20, DIM, center="x")
        u.present()

    # ------------------------------------------------------------- loop
    def loop(self):
        self.running = True
        last_test_frame = 0.0
        while self.running:
            for key in self.input.poll():
                code, val = key
                self.handle(code, val)
                if not self.running:
                    break
            if self.screen == "padtest" and self.test:
                reader, state = self.test
                reader.poll(state)
                now = time.time()
                if now - last_test_frame > 0.05:
                    self.draw_running(state)
                    last_test_frame = now
            elif self.dirty:
                self.frame()
                self.dirty = False
            else:
                time.sleep(0.02)

    def cleanup(self):
        self.stop_test()
        self.input.close()
        self.ui.quit()


LOCK_FILE = "/tmp/gamepadmode.pid"


def single_instance():
    """Exit if another Gamepad Mode instance is already running (two
    processes fight over the framebuffer and the gadget)."""
    try:
        with open(LOCK_FILE) as f:
            old = int(f.read().strip())
        os.kill(old, 0)
        print("Gamepad Mode is already running (pid %d); not starting twice" % old,
              flush=True)
        return False
    except (OSError, ValueError):
        pass
    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))
    return True


def main():
    if not single_instance():
        return 1
    try:
        app = App()
        try:
            app.loop()
        finally:
            app.cleanup()
    finally:
        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
