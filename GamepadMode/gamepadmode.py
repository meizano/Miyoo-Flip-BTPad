#!/usr/bin/env python3
"""SpruceOS Gamepad Mode - use a SpruceOS handheld as a wired USB gamepad.

A SpruceOS app: the handheld shows up on the tablet/PC as a standard USB HID
gamepad named "Retro Gamepad" over the USB-C cable. The menu on the device
starts the pad, tests the controls without connecting, remaps or disables
buttons, and sets analog stick sensitivity. While the pad runs, the screen
shows a controller layout with live press feedback and the battery
percentage.

The USB HID function is driven straight from this file: configfs f_hid when
the kernel has it, otherwise FunctionFS (the Miyoo/oakMOSS vendor kernels
ship without f_hid but always have FunctionFS, which is also what their own
ADB gadget uses - the pad function is added alongside it and the original
gadget is put back on exit).

This app began life as the USB mode of BTPad (a fork of
gladiator1014/Miyoo-Flip-BTPad, MIT); Bluetooth stayed in BTPad. See
README.md for credits.
"""
import copy
import ctypes
import errno
import fcntl
import glob
import json
import os
import select
import struct
import subprocess
import sys
import time

try:                       # only needed on the device
    import sdl2
    import sdl2.sdlttf as ttf
except ImportError:
    sdl2 = ttf = None      # Ui() reports this clearly

HERE = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "Retro Gamepad"

LOGICAL_BUTTONS = [("A", 1), ("B", 2), ("X", 4), ("Y", 5),
                   ("L1", 7), ("R1", 8), ("L2", 9), ("R2", 10),
                   ("Select", 11), ("Start", 12), ("Menu", 13),
                   ("L3", 14), ("R3", 15)]
AXIS_NAMES = ("lx", "ly", "rx", "ry")

# ---------------------------------------------------------------- HID descriptor
# Report ID 1: 16 buttons | hat (4 bits) + 4 pad | X Y Z Rz (8 bit each)
REPORT_DESC = bytes([
    0x05, 0x01, 0x09, 0x05, 0xA1, 0x01, 0x85, 0x01,
    0x05, 0x09, 0x19, 0x01, 0x29, 0x10, 0x15, 0x00, 0x25, 0x01,
    0x75, 0x01, 0x95, 0x10, 0x81, 0x02,
    0x05, 0x01, 0x09, 0x39, 0x15, 0x01, 0x25, 0x08, 0x35, 0x00,
    0x46, 0x3B, 0x01, 0x65, 0x14, 0x75, 0x04, 0x95, 0x01, 0x81, 0x42,
    0x65, 0x00, 0x35, 0x00, 0x45, 0x00,
    0x75, 0x04, 0x95, 0x01, 0x81, 0x03,
    0x05, 0x01, 0x09, 0x30, 0x09, 0x31, 0x09, 0x32, 0x09, 0x35,
    0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08, 0x95, 0x04, 0x81, 0x02,
    0xC0,
])
REPORT_LENGTH = 8  # report ID + 2 button bytes + hat + 4 axes

# ---------------------------------------------------------------- default mappings
# evdev key code -> HID button number (1-based), matching what Android expects:
# 1=A(south) 2=B(east) 4=X(north) 5=Y(west) 7=L1 8=R1 9=L2 10=R2 11=Select 12=Start 13=Mode
DEFAULT_KEYMAP = {304: 1, 305: 2, 307: 4, 308: 5, 310: 7, 311: 8,
                  314: 11, 315: 12, 316: 13, 317: 14, 318: 15}
DEFAULT_DPAD_KEYS = {103: "up", 108: "down", 105: "left", 106: "right",
                     544: "up", 545: "down", 546: "left", 547: "right"}
DEFAULT_AXES = {0: "lx", 1: "ly", 3: "rx", 4: "ry"}
DEFAULT_TRIGGER_AXES = {2: 9, 5: 10}
EXIT_COMBO = (310, 311, 314, 315)     # L1 R1 Select Start

# MagicX Mini Zero 28 ("magicx-input", simplepad UART pad). Labels are
# Nintendo-style (B bottom, A right, Y top, X left) but the mapping is by
# position, so the codes match the generic layout. L2/R2 are digital buttons
# (312/313). Right stick is ABS_Z (2) + ABS_RZ (5). MENU is KEY_BACK (158).
ZERO28_KEYMAP = {304: 1, 305: 2, 307: 4, 308: 5, 310: 7, 311: 8,
                 312: 9, 313: 10, 314: 11, 315: 12, 158: 13, 317: 14, 318: 15}
ZERO28_AXES = {0: "lx", 1: "ly", 2: "rx", 5: "ry"}

BUILTIN_PROFILES = [
    {
        "name": "Miyoo Flip",
        "devices": ["MIYOO Player1"],
        "keys": dict(DEFAULT_KEYMAP),
        "dpad_keys": dict(DEFAULT_DPAD_KEYS),
        "axes": dict(DEFAULT_AXES),
        "trigger_axes": dict(DEFAULT_TRIGGER_AXES),
    },
    {
        "name": "MagicX Mini Zero 28",
        "devices": ["magicx-input"],
        "keys": dict(ZERO28_KEYMAP),
        "dpad_keys": dict(DEFAULT_DPAD_KEYS),
        "axes": dict(ZERO28_AXES),
        "trigger_axes": {},
    },
]

EVENT_FMT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FMT)
EV_KEY, EV_ABS = 1, 3
ABS_HAT0X, ABS_HAT0Y = 16, 17


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- evdev helpers
def _ioc(direction, typ, nr, size):
    return (direction << 30) | (size << 16) | (ord(typ) << 8) | nr


def EVIOCGNAME(n): return _ioc(2, "E", 0x06, n)
def EVIOCGBIT(ev, n): return _ioc(2, "E", 0x20 + ev, n)
def EVIOCGABS(a): return _ioc(2, "E", 0x40 + a, 24)


EVIOCGRAB = _ioc(1, "E", 0x90, 4)


def ev_bits(fd, ev, nbytes=96):
    buf = bytearray(nbytes)
    try:
        fcntl.ioctl(fd, EVIOCGBIT(ev, nbytes), buf)
    except OSError:
        return set()
    return {i for i in range(nbytes * 8) if buf[i // 8] >> (i % 8) & 1}


def ev_name(fd):
    buf = bytearray(256)
    try:
        n = fcntl.ioctl(fd, EVIOCGNAME(256), buf)
        return bytes(buf[:n]).split(b"\0")[0].decode(errors="replace")
    except OSError:
        return "?"


def abs_range(fd, code):
    buf = bytearray(24)
    try:
        fcntl.ioctl(fd, EVIOCGABS(code), buf)
        _val, lo, hi, _fz, _fl, _res = struct.unpack("iiiiii", bytes(buf))
        return (lo, hi) if hi > lo else (0, 255)
    except OSError:
        return (0, 255)


class Pad:
    def __init__(self, path, fd, name, keys, absc):
        self.path, self.fd, self.name = path, fd, name
        self.keys, self.abs = keys, absc
        self.ranges = {c: abs_range(fd, c) for c in absc}


def _looks_like_pad(keys, absc):
    """Gamepad buttons, or a D-pad plus at least one axis (keeps touchscreens/keyboards out)."""
    if keys & set(range(0x130, 0x140)):  # BTN_SOUTH..BTN_THUMBR
        return True
    return bool(absc) and bool(keys & {103, 105, 106, 108, 544, 545, 546, 547})


def open_pads(want=None):
    pads = []
    for path in sorted(glob.glob("/dev/input/event*")):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            continue
        name = ev_name(fd)
        keys, absc = ev_bits(fd, EV_KEY), ev_bits(fd, EV_ABS)
        if want:
            ok = any(w in name or w == path for w in want)
        else:
            ok = _looks_like_pad(keys, absc)
        if ok:
            pads.append(Pad(path, fd, name, keys, absc))
        else:
            os.close(fd)
    return pads


def _profile_matches(prof, pad):
    for d in prof.get("devices") or []:
        if d == pad.path or d.lower() in pad.name.lower():
            return True
    return False


def choose_profile(pads, profiles):
    """-> (profile, pad) for the first pad a profile knows, else (first profile, first pad)."""
    for pad in pads:
        for prof in profiles:
            if _profile_matches(prof, pad):
                return prof, pad
    if not pads:
        return profiles[0], None
    log("no mapping profile for %r; using %s" % (pads[0].name, profiles[0]["name"]))
    return profiles[0], pads[0]


def keep_pads(pads, prof, first):
    """Only the pads belonging to the chosen profile (or just the chosen pad)."""
    devs = prof.get("devices") or []
    if devs:
        keep = [p for p in pads if _profile_matches(prof, p)]
        if keep:
            return keep
    return [first] if first else []


# ---------------------------------------------------------------- pad state -> HID report
def scale_axis(value, lo, hi):
    """Raw evdev value -> 0..255 with a small centre dead zone."""
    v = int((value - lo) * 255 / (hi - lo))
    v = max(0, min(255, v))
    return 128 if abs(v - 128) < 8 else v


class State:
    def __init__(self, keymap, dpad_keys, axes, trig, sensitivity=None):
        self.keymap, self.dpad_keys, self.axes, self.trig = keymap, dpad_keys, axes, trig
        self.sensitivity = dict(sensitivity or {})
        self.buttons = 0
        self.held = set()
        self.dir = {"up": 0, "down": 0, "left": 0, "right": 0}
        self.ax = {"lx": 128, "ly": 128, "rx": 128, "ry": 128}

    def _sens(self, name, v):
        """User-set analog sensitivity: k% around the centre, clamped."""
        k = self.sensitivity.get(name, 100)
        if k == 100:
            return v
        return max(0, min(255, int(round(128 + (v - 128) * k / 100.0))))

    def hat(self):
        x = self.dir["right"] - self.dir["left"]
        y = self.dir["down"] - self.dir["up"]
        return {(0, -1): 1, (1, -1): 2, (1, 0): 3, (1, 1): 4,
                (0, 1): 5, (-1, 1): 6, (-1, 0): 7, (-1, -1): 8}.get((x, y), 0)

    def report(self):
        b, a = self.buttons, self.ax
        return bytes([0x01, b & 0xFF, (b >> 8) & 0xFF, self.hat(),
                      self._sens("lx", a["lx"]), self._sens("ly", a["ly"]),
                      self._sens("rx", a["rx"]), self._sens("ry", a["ry"])])

    def feed(self, pad, etype, code, value):
        """Returns True if the report changed."""
        before = self.report()
        if etype == EV_KEY:
            if value:
                self.held.add(code)
            else:
                self.held.discard(code)
            if code in self.keymap:
                bit = 1 << (self.keymap[code] - 1)
                self.buttons = (self.buttons | bit) if value else (self.buttons & ~bit)
            elif code in self.dpad_keys:
                self.dir[self.dpad_keys[code]] = 1 if value else 0
        elif etype == EV_ABS:
            if code == ABS_HAT0X:
                self.dir["left"], self.dir["right"] = int(value < 0), int(value > 0)
            elif code == ABS_HAT0Y:
                self.dir["up"], self.dir["down"] = int(value < 0), int(value > 0)
            elif code in self.trig:
                lo, hi = pad.ranges.get(code, (0, 255))
                bit = 1 << (self.trig[code] - 1)
                if value > lo + (hi - lo) * 0.5:
                    self.buttons |= bit
                else:
                    self.buttons &= ~bit
            elif code in self.axes:
                lo, hi = pad.ranges.get(code, (0, 255))
                self.ax[self.axes[code]] = scale_axis(value, lo, hi)
        return self.report() != before

    def exit_combo_held(self):
        return all(c in self.held for c in EXIT_COMBO)


# ---------------------------------------------------------------- mapping.json
def _int_map(d):
    return {int(k): int(v) for k, v in d.items()}


def _axis_map(d):
    return {int(k): v for k, v in d.items()}


def load_mapping():
    """-> (profiles, want). mapping.json overrides the built-in profiles:

    {"profiles": {"magicx-input": {"axes": {"2": "rx"}}}}
    """
    profiles = copy.deepcopy(BUILTIN_PROFILES)
    p = os.path.join(HERE, "mapping.json")
    if not os.path.exists(p):
        return profiles, None
    with open(p) as f:
        cfg = json.load(f)
    for key, over in (cfg.get("profiles") or {}).items():
        target = next((x for x in profiles
                       if key == x["name"] or key in (x.get("devices") or [])), None)
        base = copy.deepcopy(target) if target else {
            "name": key, "devices": [key], "keys": {}, "dpad_keys": {},
            "axes": {}, "trigger_axes": {}}
        if "devices" in over:
            base["devices"] = list(over["devices"])
        if "keys" in over:
            base["keys"] = _int_map(over["keys"])
        if "dpad_keys" in over:
            base["dpad_keys"] = {int(k): v for k, v in over["dpad_keys"].items()}
        if "axes" in over:
            base["axes"] = _axis_map(over["axes"])
        if "trigger_axes" in over:
            base["trigger_axes"] = _int_map(over["trigger_axes"])
        if target is None:
            profiles.append(base)
        else:
            target.update(base)
    log("loaded mapping.json profiles")
    return profiles, None


# ---------------------------------------------------------------- USB HID gadget
# Two ways to become a USB HID gamepad:
#
#  * configfs + f_hid  -> /dev/hidg0. Simple, but the kernel must have
#    CONFIG_USB_CONFIGFS_F_HID. The Miyoo Flip does; TinaLinux on the
#    MagicX/oakMOSS handhelds does not.
#  * configfs + FunctionFS (ffs) -> the HID class is implemented here:
#    descriptors go to ep0, HID control requests are answered from userspace,
#    reports go to an interrupt IN endpoint. Every gadget kernel has this.
#
# The MagicX also allows only *one* gadget, already taken by g1 (adb), so the
# ffs path adopts that gadget instead of creating a second one and puts it back
# the way it was on exit.
CONFIGFS_ROOT = "/sys/kernel/config"
USB_GADGET_ROOT = CONFIGFS_ROOT + "/usb_gadget"
USB_GADGET_NAME = "gamepadmode"
USB_GADGET_PATH = USB_GADGET_ROOT + "/" + USB_GADGET_NAME
USB_VID = "0x1209"   # pid.codes (open-source vendor id)
USB_PID = "0xb7ad"   # originally assigned for BTPad, kept for continuity
USB_HIDG = "/dev/hidg0"

FFS_NAME = "gamepadmode"
FFS_DIR = "/dev/usb-ffs/" + FFS_NAME
FFS_EP0 = FFS_DIR + "/ep0"

FFS_BIND, FFS_UNBIND, FFS_ENABLE, FFS_DISABLE, FFS_SETUP = 0, 1, 2, 3, 4
HID_DESC = struct.pack("<BBHBBBH", 9, 0x21, 0x0111, 0, 1, 0x22, len(REPORT_DESC))


def _write(path, value):
    # configfs/sysfs attribute writes of zero bytes are silently ignored, so an
    # empty value still needs the terminating newline (that is how "echo > UDC"
    # unbinds a gadget).
    text = str(value)
    if not text.endswith("\n"):
        text += "\n"
    with open(path, "w") as f:
        f.write(text)


def _read(path):
    with open(path) as f:
        return f.read().strip()


def udc_names():
    try:
        return sorted(os.listdir("/sys/class/udc"))
    except OSError:
        return []


def first_udc():
    names = udc_names()
    return names[0] if names else None


def udc_state(udc):
    try:
        return _read("/sys/class/udc/%s/state" % udc)
    except OSError:
        return "unknown"


def bound_udcs():
    """UDCs already claimed by a gadget: {udc name: gadget name}."""
    out = {}
    for path in glob.glob(USB_GADGET_ROOT + "/*/UDC"):
        try:
            name = _read(path)
        except OSError:
            continue
        if name and name != USB_GADGET_NAME:
            out[name] = os.path.basename(os.path.dirname(path))
    return out


def choose_udc():
    """Pick the USB controller to bind; prefer one no other gadget owns."""
    names = udc_names()
    if not names:
        return None
    busy = bound_udcs()
    free = [n for n in names if n not in busy]
    if free:
        if len(names) > 1:
            log("USB controllers:", ", ".join(names), "-> using", free[0])
        return free[0]
    log("every USB controller is already used by another gadget (%s); trying %s anyway"
        % (", ".join("%s by %s" % (k, v) for k, v in busy.items()), names[0]))
    return names[0]


def have_usb_gadget():
    return first_udc() is not None


def mount_configfs():
    if os.path.isdir(USB_GADGET_ROOT):
        return True
    try:
        subprocess.run(["mount", "-t", "configfs", "none", CONFIGFS_ROOT],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass
    return os.path.isdir(USB_GADGET_ROOT)


def load_gadget_modules():
    for mod in ("libcomposite", "usb_f_hid", "usb_f_fs"):
        try:
            subprocess.run(["modprobe", mod], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        except OSError:
            pass


def _remove_tree(path):
    """Best-effort recursive delete. configfs attribute files cannot be unlinked;
    that is fine, rmdir still drops them with the gadget."""
    if not os.path.exists(path):
        return
    for root, dirs, files in os.walk(path, topdown=False):
        for name in files:
            try:
                os.unlink(os.path.join(root, name))
            except OSError:
                pass
        for name in dirs:
            p = os.path.join(root, name)
            try:
                if os.path.islink(p):
                    os.unlink(p)
                else:
                    os.rmdir(p)
            except OSError:
                pass
    try:
        os.rmdir(path)
    except OSError:
        pass


def usb_gadget_remove():
    """Unbind and delete our gadget. Safe to call when nothing exists."""
    if not os.path.isdir(USB_GADGET_PATH):
        return
    try:
        _write(USB_GADGET_PATH + "/UDC", "")
    except OSError:
        pass
    time.sleep(0.2)  # let the UDC release the configuration
    _remove_tree(USB_GADGET_PATH)


def gadget_names():
    try:
        return sorted(os.listdir(USB_GADGET_ROOT))
    except OSError:
        return []


def gadget_bound_to(udc):
    """Name of the gadget currently bound to this UDC, if any."""
    for name in gadget_names():
        try:
            if _read(os.path.join(USB_GADGET_ROOT, name, "UDC")) == udc:
                return name
        except OSError:
            pass
    return None


def gadget_config_path(path):
    """The (first) config directory of a gadget, or None."""
    try:
        names = sorted(n for n in os.listdir(path + "/configs") if n.startswith("c."))
    except OSError:
        return None
    return path + "/configs/" + names[0] if names else None


def usb_probe_functions():
    """Which configfs HID paths work here: (f_hid, functionfs, gadget name).

    Creating a throwaway function inside an existing gadget is safe (an unbound
    function does nothing) and is the only way to probe on kernels that allow a
    single gadget, where mkdir of a second gadget fails with ENOMEM."""
    if not mount_configfs():
        return False, False, None
    load_gadget_modules()
    names = gadget_names()
    own = not names
    path = os.path.join(USB_GADGET_ROOT, names[0]) if names else USB_GADGET_ROOT + "/probe"
    if own:
        try:
            os.mkdir(path)
        except OSError:
            return False, False, None
        os.makedirs(path + "/functions", exist_ok=True)
    hid = ffs = False
    try:
        for kind in ("hid", "ffs"):
            p = path + "/functions/%s.probe" % kind
            try:
                os.mkdir(p)
            except OSError:
                continue
            if kind == "hid":
                hid = True
            else:
                ffs = True
            _remove_tree(p)
    finally:
        if own:
            _remove_tree(path)
    return hid, ffs, (names[0] if names else None)


def usb_hid_probe():
    """--check: can this kernel do USB HID gadget mode, and how?"""
    if not udc_names():
        return False, "no USB device controller (/sys/class/udc is empty)"
    hid, ffs, name = usb_probe_functions()
    if not hid and not ffs:
        return False, ("configfs is there, but this kernel has neither f_hid nor "
                       "FunctionFS (CONFIG_USB_CONFIGFS_F_HID / _F_FS)")
    sharing = " (shares the existing gadget %s)" % name if name else ""
    if hid:
        return True, "kernel HID function f_hid -> /dev/hidg0" + sharing
    return True, "no f_hid, using FunctionFS HID in userspace" + sharing


def otg_role_paths():
    return glob.glob("/sys/bus/platform/devices/usbc*/otg_role")


def set_device_role():
    """Some handhelds wire the data port to the OTG controller but leave it in
    host role until told otherwise; with the cable in that port the pad can then
    never enumerate. Returns (path, old role) when it changed something."""
    for path in otg_role_paths():
        try:
            role = _read(path)
        except OSError:
            continue
        if role == "usb_device":
            return None, None
        try:
            _write(path, "usb_device")
        except OSError as e:
            log("could not switch %s to usb_device: %s" % (path, e))
            return None, None
        log("USB: switched %s from %s to usb_device" % (path, role))
        return path, role
    return None, None


def ffs_report_path():
    """FunctionFS names its endpoint files after the endpoint address on most
    kernels ("ep81") and after the endpoint index on some ("ep1"). Take whatever
    appeared once the descriptors were accepted."""
    try:
        names = sorted(n for n in os.listdir(FFS_DIR) if n != "ep0")
    except OSError:
        return None
    return os.path.join(FFS_DIR, names[0]) if names else None


def ffs_descriptors():
    """FunctionFS descriptor block: one HID interface with an interrupt IN
    endpoint. The kernel rewrites bInterfaceNumber and picks the physical
    endpoint, so the numbers here are only our local names."""
    intf = struct.pack("<BBBBBBBBB", 9, 0x04, 0, 0, 1, 0x03, 0, 0, 0)
    ep = struct.pack("<BBBBHB", 7, 0x05, 0x81, 0x03, REPORT_LENGTH, 1)
    body = intf + HID_DESC + ep
    # magic 1 is the plain (legacy) header: magic, length, fs_count, hs_count.
    # The counts are *descriptor* counts, not bytes.
    return struct.pack("<IIII", 1, 16 + len(body), 3, 0) + body


def ffs_strings():
    # We reference no strings, and then both counts have to be zero.
    return struct.pack("<IIII", 2, 16, 0, 0)


class UsbGadget:
    """USB HID gamepad: configfs f_hid when the kernel has it, otherwise a HID
    function driven from userspace through FunctionFS."""

    def __init__(self, name):
        self.name = name
        self.udc = None
        self.mode = None          # "hid" (f_hid) or "ffs" (FunctionFS)
        self.fd = None            # report endpoint
        self.ep0 = None           # FunctionFS control endpoint
        self.gadget = None        # configfs gadget dir in use
        self.fn = None            # our function dir
        self.link = None          # our function symlink inside the config
        self.own_gadget = False
        self.restore_udc = None
        self.old_product = None
        self.old_manufacturer = None
        self.old_vid = None
        self.old_pid = None
        self.role_path = None
        self.role_restore = None
        self.host = False
        self.enabled = False     # FunctionFS sent FUNCTIONFS_ENABLE
        self.pending = None
        self.report_buf = bytes(REPORT_LENGTH)
        self.wrote_once = False
        self._open_warned = 0.0
        self._open_try = 0.0

    def start(self):
        if os.getuid() != 0:
            raise RuntimeError("must run as root to create a USB gadget")
        if not mount_configfs():
            raise RuntimeError("configfs is not available (mount -t configfs none %s failed)"
                               % CONFIGFS_ROOT)
        load_gadget_modules()
        # Some handhelds keep the OTG/data port in host role until told
        # otherwise; do it before looking for a controller to bind.
        self.role_path, self.role_restore = set_device_role()
        if self.role_path:
            time.sleep(0.5)   # the UDC is re-registered when the role changes
        self.udc = choose_udc()
        if not self.udc:
            raise RuntimeError("no USB device controller (/sys/class/udc is empty)")
        try:
            self._start_hid()
            return
        except Exception as e:
            hid_error = e
            self._teardown()
        try:
            self._start_ffs()
        except Exception as e:
            self._teardown()
            raise RuntimeError("no HID gadget (%s), and FunctionFS HID failed too (%s)"
                               % (hid_error, e))

    # -- path 1: kernel f_hid, /dev/hidg0 -----------------------------
    def _start_hid(self):
        usb_gadget_remove()
        if os.path.isdir(USB_GADGET_PATH):
            try:
                os.rmdir(USB_GADGET_PATH)
            except OSError:
                pass
        if os.path.isdir(USB_GADGET_PATH):
            raise RuntimeError("a stale USB gadget at %s could not be removed; reboot and retry"
                               % USB_GADGET_PATH)
        # Ask first whether f_hid exists. Creating a second gadget on vendor
        # kernels that allow only one fails halfway and can leave configfs in a
        # state where the next bind wedges the USB controller, so do not try it
        # unless the kernel really has the function.
        hid_ok, _ffs_ok, _name = usb_probe_functions()
        if not hid_ok:
            raise RuntimeError("the kernel has no USB HID gadget function (f_hid)")
        try:
            os.mkdir(USB_GADGET_PATH)
            self.own_gadget = True
            self.gadget = USB_GADGET_PATH
            os.makedirs(USB_GADGET_PATH + "/functions", exist_ok=True)
            _write(USB_GADGET_PATH + "/idVendor", USB_VID)
            _write(USB_GADGET_PATH + "/idProduct", USB_PID)
            _write(USB_GADGET_PATH + "/bcdUSB", "0x0200")
            _write(USB_GADGET_PATH + "/bcdDevice", "0x0100")
            os.makedirs(USB_GADGET_PATH + "/strings/0x409")
            _write(USB_GADGET_PATH + "/strings/0x409/manufacturer", "SpruceOS")
            _write(USB_GADGET_PATH + "/strings/0x409/product", self.name)
            _write(USB_GADGET_PATH + "/strings/0x409/serialnumber", "SPRUCEPAD01")

            self.fn = USB_GADGET_PATH + "/functions/hid.usb0"
            os.mkdir(self.fn)
            _write(self.fn + "/protocol", "0")
            _write(self.fn + "/subclass", "0")
            _write(self.fn + "/report_length", str(REPORT_LENGTH))
            with open(self.fn + "/report_desc", "wb") as f:
                f.write(REPORT_DESC)

            os.makedirs(USB_GADGET_PATH + "/configs/c.1/strings/0x409")
            _write(USB_GADGET_PATH + "/configs/c.1/strings/0x409/configuration", "HID Gamepad")
            _write(USB_GADGET_PATH + "/configs/c.1/MaxPower", "250")
            self.link = USB_GADGET_PATH + "/configs/c.1/hid.usb0"
            os.symlink(self.fn, self.link)
            _write(USB_GADGET_PATH + "/UDC", self.udc)
            if _read(USB_GADGET_PATH + "/UDC") != self.udc:
                raise RuntimeError("could not bind the USB gadget to %s" % self.udc)
        except Exception:
            usb_gadget_remove()
            self.own_gadget = False
            self.gadget = self.fn = self.link = None
            raise
        self.mode = "hid"
        log("USB HID gadget (f_hid) ready on %s; plug the cable into the tablet/PC" % self.udc)
        self._open()  # the node exists as soon as f_hid is bound

    # -- path 2: HID over FunctionFS, added to an existing gadget ------
    def _pick_gadget(self):
        name = gadget_bound_to(self.udc)
        if not name:
            names = [n for n in gadget_names() if n != USB_GADGET_NAME]
            name = names[0] if names else None
        return os.path.join(USB_GADGET_ROOT, name) if name else None

    def _start_ffs(self):
        self.mode = "ffs"
        self.gadget = self._pick_gadget()
        if not self.gadget:
            raise RuntimeError("there is no configfs gadget to attach a function to")
        try:
            self.restore_udc = _read(self.gadget + "/UDC") or None
        except OSError:
            self.restore_udc = None
        cfg = gadget_config_path(self.gadget)
        if not cfg:
            raise RuntimeError("%s has no configuration" % self.gadget)
        self.link = cfg + "/ffs." + FFS_NAME
        # Clean up whatever a crashed earlier run left behind: an old link or
        # mount would make the function directory impossible to remove.
        if os.path.lexists(self.link):
            try:
                os.unlink(self.link)
            except OSError:
                pass
        if os.path.isdir(FFS_DIR):
            try:
                subprocess.run(["umount", FFS_DIR], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
            except OSError:
                pass
        self.fn = self.gadget + "/functions/ffs." + FFS_NAME
        if os.path.isdir(self.fn):
            _remove_tree(self.fn)
        os.mkdir(self.fn)
        log("USB: function ffs.%s created" % FFS_NAME)
        os.makedirs(FFS_DIR, exist_ok=True)
        r = subprocess.run(["mount", "-t", "functionfs", FFS_NAME, FFS_DIR],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if r.returncode != 0:
            raise RuntimeError("mount -t functionfs failed: %s"
                               % r.stdout.decode(errors="replace").strip())
        # Descriptors first, then the (empty) string block: only after both does
        # FunctionFS accept control requests, and the gadget must not be bound
        # before that. All of this is safe while the gadget is still bound, so
        # it happens BEFORE the unbind: the window in which the vendor's ADB
        # workqueue (android_work) can steal the controller back is then only
        # the few milliseconds between the unbind and our bind below.
        self.ep0 = os.open(FFS_EP0, os.O_RDWR)
        os.write(self.ep0, ffs_descriptors())
        os.write(self.ep0, ffs_strings())
        os.set_blocking(self.ep0, False)
        log("USB: descriptors accepted by FunctionFS")
        if self.restore_udc:
            _write(self.gadget + "/UDC", "")
            log("USB: unbound %s" % os.path.basename(self.gadget))
        # Hosts cache device names by VID/PID (notably Android), so the identity
        # has to change too: otherwise a host that already knows this gadget as
        # "Tina ADB" (VID 18d1 PID d002) keeps showing that cached name even
        # though the product string now says Retro Gamepad. Do this while the
        # gadget is unbound and restore everything on exit.
        self._set_identity()
        os.symlink(self.fn, self.link)
        log("USB: function linked into %s" % os.path.basename(cfg))
        self._bind_udc()
        log("USB HID gadget (FunctionFS) ready on %s, added to %s"
            % (self.udc, os.path.basename(self.gadget)))
        log("USB: waiting for the host to enable the HID function")

    # The firmware's gadget (g1) is borrowed for the FunctionFS path: make it
    # present our own identity (product string, manufacturer, VID and PID)
    # while it is ours. A host that has seen the ADB gadget before caches its
    # name by VID/PID, so changing only the product string is not enough:
    # Android keeps showing "Tina ADB" unless the identity changes too.
    IDENTITY = (("old_product", "/strings/0x409/product", APP_NAME),
                ("old_manufacturer", "/strings/0x409/manufacturer", "SpruceOS"),
                ("old_vid", "/idVendor", USB_VID),
                ("old_pid", "/idProduct", USB_PID))

    def _set_identity(self):
        for attr, path, new in self.IDENTITY:
            full = self.gadget + path
            try:
                old = _read(full)
            except OSError:
                setattr(self, attr, None)
                continue
            setattr(self, attr, old)
            if old != new:
                try:
                    _write(full, new)
                except OSError:
                    setattr(self, attr, None)
                    continue
                log("USB: %s now %r (was %r)" % (os.path.basename(path), new, old))

    def _restore_identity(self):
        for attr, path, _new in self.IDENTITY:
            old = getattr(self, attr)
            if self.gadget and old:
                try:
                    _write(self.gadget + path, old)
                except OSError:
                    pass
            setattr(self, attr, None)

    def _bind_udc(self):
        """Write our controller into UDC. If something else (the vendor ADB
        workqueue) grabbed the controller in the meantime, unbind it and take
        the controller right back; there is no notification between the two
        writes, so a fast retry wins the race."""
        err = None
        for attempt in range(10):
            try:
                _write(self.gadget + "/UDC", self.udc)
            except OSError as e:
                err = e
                owner = _read(self.gadget + "/UDC")
                if owner:
                    log("USB: %s was reclaimed (%s); taking it back"
                        % (self.udc, owner))
                    try:
                        _write(self.gadget + "/UDC", "")
                    except OSError:
                        pass
                else:
                    log("USB: binding to %s failed (%s); retrying" % (self.udc, e))
                time.sleep(0.05)
                continue
            if _read(self.gadget + "/UDC") == self.udc:
                return
            err = RuntimeError("UDC read back %r" % _read(self.gadget + "/UDC"))
            time.sleep(0.05)
        raise RuntimeError("could not bind %s to %s (%s)"
                           % (self.gadget, self.udc, err))

    # Do not open the report endpoint yet: on this hardware a write into a
    # function the host has not enabled locks up the USB controller, so the
    # endpoint is opened only after FUNCTIONFS_ENABLE (see _pump_ep0).

    # -- runtime ------------------------------------------------------
    def tick(self):
        """Handle control requests and follow the controller state. Called once
        per main-loop pass; never decides whether reports may be sent."""
        if self.mode == "ffs":
            self._pump_ep0()
        st = udc_state(self.udc)
        if st == "configured" and not self.host:
            self.host = True
            log("USB host configured the pad - live (controller state %r)" % st)
            self._open()
        elif st != "configured" and self.host:
            self.host = False
            log("USB host disconnected (controller state %r); waiting for reconnect" % st)
            if self.fd is not None:
                self._close_fd(quiet=True)
        if st == "configured" and self.fd is None and time.time() - self._open_try > 1.0:
            # FunctionFS creates its endpoint file only once the host has
            # enabled the function, so a configured gadget without an open
            # report pipe should try again (rate limited).
            self._open()
        return st

    def _pump_ep0(self):
        if self.ep0 is None:
            return
        while True:
            try:
                ev = os.read(self.ep0, 12)
            except OSError as e:
                if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR):
                    return
                log("USB control endpoint error:", e)
                return
            if len(ev) < 12:
                return
            kind = ev[8]
            if kind == FFS_SETUP:
                self._control(ev[:8])
            elif kind == FFS_DISABLE:
                # The host deconfigured or reset us; the old report pipe is dead.
                self.host = False
                self.enabled = False
                self._close_fd(quiet=True)
            elif kind == FFS_ENABLE:
                self.enabled = True
                self.host = True
                self._open()

    def _control(self, raw):
        bm, req, val, _idx, length = struct.unpack("<BBHHH", raw)
        kind = val >> 8
        if bm & 0x80:                       # IN: the host reads from us
            if req == 0x06 and kind == 0x21:
                self._ep0_write(HID_DESC)
            elif req == 0x06 and kind == 0x22:
                self._ep0_write(REPORT_DESC)
            elif req == 0x01 and (bm & 0x60) == 0x20:      # GET_REPORT
                self._ep0_write(self.report_buf[:length])
            else:
                self._ep0_write(b"")        # ack class requests we do not use
            return
        if length:                          # OUT with data, e.g. SET_REPORT
            try:
                os.read(self.ep0, length)
            except OSError:
                pass

    def _ep0_write(self, data):
        if self.ep0 is None:
            return
        try:
            os.write(self.ep0, data)
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR):
                log("USB control reply failed:", e)

    def _report_path(self):
        if self.mode == "ffs":
            # The kernel creates this file only after the descriptors are
            # accepted, and its name varies ("ep81" or "ep1").
            return ffs_report_path()
        return USB_HIDG

    def _open(self):
        if self.fd is not None:
            return
        if self.mode == "ffs" and not self.enabled:
            # Writes to a FunctionFS endpoint before FUNCTIONFS_ENABLE can wedge
            # the controller on this hardware.
            return
        path = self._report_path()
        if not path:
            return
        try:
            self.fd = os.open(path, os.O_WRONLY)
        except OSError as e:
            self._open_try = time.time()
            if time.time() - self._open_warned > 10:
                self._open_warned = time.time()
                log("USB: %s not open yet (%s); will retry" % (path, e))
            return
        self._open_try = 0.0
        log("USB: %s open, sending reports" % path)

    def _close_fd(self, quiet=False):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
            if not quiet:
                log("USB: report endpoint closed")

    def push(self, report):
        if not self.host:
            # Nothing to talk to before the host has configured the gadget.
            # Writing early can hang this USB controller.
            self.pending = report
            return False
        if self.fd is None:
            self._open()
        if self.fd is None:
            self.pending = report
            return False
        try:
            os.write(self.fd, report)
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR):
                self.pending = report
                return False
            # EPIPE/ECONNRESET/ESHUTDOWN mean the host reset or deconfigured us;
            # that is a normal unplug, not an error worth a line per report.
            if e.errno in (errno.EPIPE, errno.ECONNRESET, errno.ESHUTDOWN):
                self._close_fd(quiet=True)
                self.host = False
            else:
                log("USB write failed (%s); reopening %s" % (e, self._report_path()))
                self._close_fd(quiet=True)
            self.pending = report
            return False
        self.pending = None
        if not self.wrote_once:
            self.wrote_once = True
            log("USB: first report accepted by the host")
        return True

    def flush(self):
        if self.pending is not None:
            return self.push(self.pending)
        return False

    def close(self):
        self.pending = None
        self._close_fd(quiet=True)
        self._teardown()
        log("USB gadget removed")

    def _teardown(self):
        if self.mode == "ffs":
            self._stop_ffs()
        elif self.mode == "hid":
            usb_gadget_remove()
            self.own_gadget = False
        self.mode = None
        if self.role_path:
            # Switching the OTG controller back and forth is what makes this
            # hardware hang (and then reboot through its 16 s watchdog), so the
            # device role is left in place. A reboot restores host mode.
            log("USB: leaving %s in device role; reboot to use the port as a host again"
                % self.role_path)
            self.role_path = self.role_restore = None

    def _stop_ffs(self):
        if self.ep0 is not None:
            try:
                os.close(self.ep0)
            except OSError:
                pass
            self.ep0 = None
        if self.gadget:
            try:
                _write(self.gadget + "/UDC", "")
            except OSError:
                pass
        time.sleep(0.2)
        if self.link and os.path.lexists(self.link):
            try:
                os.unlink(self.link)
            except OSError:
                pass
        try:
            subprocess.run(["umount", FFS_DIR], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        except OSError:
            pass
        # Restore the firmware's identity (product/manufacturer/VID/PID) while
        # the gadget is unbound, before the vendor's ADB gadget is bound again.
        self._restore_identity()
        if self.gadget and self.restore_udc:
            try:
                _write(self.gadget + "/UDC", self.restore_udc)
            except OSError:
                pass
        if self.fn:
            _remove_tree(self.fn)
        self.gadget = self.fn = self.link = None
        self.restore_udc = None


# ---------------------------------------------------------------- the pad loop
def run_pad(overrides=None, frame_cb=None):
    """Run the pad over USB. overrides carries the settings made in the UI:
    keys {evdev code: hid button}, disabled [hid button], sensitivity
    {lx/ly/rx/ry: percent}. frame_cb(state) is called every loop pass (the UI
    draws the live layout with it). Returns 0 on a clean stop, 1 on failure."""
    running = [True]
    import signal
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: running.__setitem__(0, False))

    profiles, want = load_mapping()
    pads = open_pads(want)
    if not pads:
        log("no gamepad input devices found; run with --check")
        return 1
    prof, first = choose_profile(pads, profiles)
    if overrides:
        prof = dict(prof)
        remap = overrides.get("keys") or {}
        disabled = set(overrides.get("disabled") or ())
        if remap:
            prof["keys"] = {int(c): int(h) for c, h in remap.items()}
        if disabled:
            prof["keys"] = {c: h for c, h in prof["keys"].items() if h not in disabled}
        log("settings applied: %d remap, %d disabled, sensitivity %s"
            % (len(overrides.get("keys") or {}), len(disabled),
               overrides.get("sensitivity") or {}))
    pads = keep_pads(pads, prof, first)
    for p in pads:
        log("using", p.path, p.name)
        try:
            fcntl.ioctl(p.fd, EVIOCGRAB, 1)  # keep Spruce from also reacting to the buttons
        except OSError:
            log("could not grab", p.path)
    log("buttons are grabbed: Spruce's screen will not respond while this runs")
    log("to quit: hold L1+R1+Select+Start for 2s, or press Ctrl-C here")
    log("profile:", prof["name"])

    if not have_usb_gadget():
        log("this device has no USB device controller; cable mode is not possible")
        for p in pads:
            try:
                fcntl.ioctl(p.fd, EVIOCGRAB, 0)
                os.close(p.fd)
            except OSError:
                pass
        return 1

    usb = None
    try:
        usb = UsbGadget(APP_NAME)
        usb.start()
        log("mode: USB cable")
    except Exception as e:
        log("could not start the USB gadget:", e)
        log("run with --check to see what is missing")
        if usb:
            usb.close()
        for p in pads:
            try:
                fcntl.ioctl(p.fd, EVIOCGRAB, 0)
                os.close(p.fd)
            except OSError:
                pass
        return 1

    state = State(prof["keys"], prof["dpad_keys"], prof["axes"],
                  prof["trigger_axes"],
                  sensitivity=(overrides or {}).get("sensitivity"))
    byfd = {p.fd: p for p in pads}
    exit_since = None
    last_push = 0.0
    seen_input = False
    warned_input = time.time()

    def push():
        if usb:
            usb.push(state.report())

    try:
        while running[0]:
            rl = list(byfd)
            try:
                ready, _, _ = select.select(rl, [], [], 0.05)
            except InterruptedError:
                continue
            for s in ready:
                if s in byfd:
                    pad = byfd[s]
                    try:
                        data = os.read(s, EVENT_SIZE * 64)
                    except BlockingIOError:
                        continue
                    except OSError:
                        continue
                    changed = False
                    for off in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
                        _sec, _usec, t, c, v = struct.unpack(EVENT_FMT,
                                                             data[off:off + EVENT_SIZE])
                        if t in (EV_KEY, EV_ABS):
                            changed |= state.feed(pad, t, c, v)
                    if changed and not seen_input:
                        seen_input = True
                        log("first input seen on %r - mapping is live" % pad.name)
                    if changed:
                        push()
                        last_push = time.time()
            if usb:
                usb.tick()
                if usb.pending is not None:
                    usb.flush()
                if not seen_input and time.time() - warned_input > 10:
                    warned_input = time.time()
                    log("no button or stick events yet; if the host shows the pad "
                        "but nothing happens, check the mapping with --probe")
            if usb and usb.host and time.time() - last_push > 0.1:
                push()  # keep-alive: re-send the current state so a lost report cannot stick
                last_push = time.time()
            if frame_cb:
                frame_cb(state)
            if state.exit_combo_held():
                exit_since = exit_since or time.time()
                if time.time() - exit_since > 2.0:
                    log("exit combo held, quitting")
                    break
            else:
                exit_since = None
    finally:
        if usb:
            usb.close()
        for p in pads:
            try:
                fcntl.ioctl(p.fd, EVIOCGRAB, 0)
                os.close(p.fd)
            except OSError:
                pass
    return 0


# ---------------------------------------------------------------- diagnostics
def hardware_issue():
    """-> list of reasons this device cannot run the pad, or [] if it can.

    Runs at launch so an unsupported device gets a clear on-screen answer
    instead of a menu whose "Start" button only fails silently."""
    missing = []
    pads = open_pads()
    for p in pads:
        try:
            os.close(p.fd)
        except OSError:
            pass
    if not pads:
        missing.append("no gamepad input device found on /dev/input")
    if not have_usb_gadget():
        missing.append("no USB device controller (no OTG/data port)")
    return missing


def check():
    print("python %s" % sys.version.split()[0])
    print("root:", "yes" if os.getuid() == 0 else "NO - the gadget needs root")
    pads = open_pads()
    if pads:
        print("gamepad input devices:")
        for p in pads:
            print("  %s  %s" % (p.path, p.name))
    else:
        print("gamepad input devices: NONE found")
    print("configfs:", "ok" if mount_configfs() else "not available")
    print("USB controllers:", ", ".join(udc_names()) or "none")
    ok, why = usb_hid_probe()
    print("USB HID function: %s" % ("OK - " + why if ok else "MISSING - " + why))
    try:
        with open("/sys/class/power_supply/axp2202-battery/capacity") as f:
            print("battery:", f.read().strip() + "%")
    except OSError:
        pass
    if sdl2 is None:
        print("pysdl2: MISSING (the Gamepad Mode UI needs Spruce's python + PyUI)")
    else:
        print("pysdl2: ok")


def devices():
    for path in sorted(glob.glob("/dev/input/event*")):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            continue
        keys, absc = ev_bits(fd, EV_KEY), ev_bits(fd, EV_ABS)
        tag = " <-- gamepad" if _looks_like_pad(keys, absc) else ""
        print("%s  %s%s" % (path, ev_name(fd), tag))
        os.close(fd)


def probe(all_nodes=False):
    """Watch live events: --probe (pads only) or --probe --all."""
    fds = []
    for path in sorted(glob.glob("/dev/input/event*")):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            continue
        if not all_nodes:
            keys, absc = ev_bits(fd, EV_KEY), ev_bits(fd, EV_ABS)
            if not _looks_like_pad(keys, absc):
                os.close(fd)
                continue
        fds.append((path, fd, ev_name(fd)))
    if not fds:
        print("no input devices to watch")
        return
    print("watching (Ctrl-C to stop):")
    for path, _fd, name in fds:
        print("  %s  %s" % (path, name))
    try:
        while True:
            for path, fd, name in fds:
                r, _, _ = select.select([fd], [], [], 0)
                if not r:
                    continue
                try:
                    data = os.read(fd, EVENT_SIZE * 32)
                except OSError:
                    continue
                for off in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
                    _s, _u, t, c, v = struct.unpack(EVENT_FMT, data[off:off + EVENT_SIZE])
                    if t == EV_KEY:
                        print("%-22s %-18s KEY %-5d %s" % (path, name, c, "down" if v else "up"))
                    elif t == EV_ABS:
                        print("%-22s %-18s ABS %-5d %d" % (path, name, c, v))
            time.sleep(0.01)
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------- settings (the UI's own)
def default_settings():
    """Logical name -> evdev code, from the Zero 28 profile (the codes we
    verified against the hardware)."""
    by_hid = {hid: name for name, hid in LOGICAL_BUTTONS}
    remap = {}
    for code, hid in sorted(ZERO28_KEYMAP.items()):
        if hid in by_hid:
            remap[by_hid[hid]] = code
    return {"remap": remap, "disabled": [],
            "sensitivity": {a: 100 for a in AXIS_NAMES}}

# ---------------------------------------------------------------- UI constants
# UI controls (evdev codes on the Zero 28's simplepad)
KEY_UP, KEY_DOWN, KEY_LEFT, KEY_RIGHT = 103, 108, 105, 106
KEY_A, KEY_B, KEY_MENU = 304, 305, 158

# PyUI ships SDL2 + SDL_ttf and its fonts; find any usable TTF rather than
# depending on one exact file name across devices.
FONT_DIRS = ["/mnt/SDCARD/App/PyUI/fonts", "/mnt/SDCARD/App/PyUI/font",
             "/mnt/SDCARD/spruce/config/PPSSPP/assets/fonts"]


def _find_font(what):
    """Prefer PyUI's own body font family; a display font like Audiowide
    (regular-only, stylized) reads poorly as UI body text."""
    for d in FONT_DIRS:
        try:
            names = sorted(f for f in os.listdir(d) if f.lower().endswith(".ttf"))
        except OSError:
            continue
        if not names:
            continue
        exact = {"regular": "BeVietnamPro-Regular.ttf",
                 "bold": "BeVietnamPro-SemiBold.ttf"}[what]
        if exact in names:
            return os.path.join(d, exact)
        # Otherwise any family that has both a regular and a bold variant.
        fams = {}
        for n in names:
            stem = n.rsplit(".", 1)[0]
            fam = stem.replace("-SemiBold", "").replace("-Bold", "").replace("-Regular", "")
            fams.setdefault(fam, set()).add(stem)
        for fam in sorted(fams):
            variants = fams[fam]
            reg = next((v for v in sorted(variants) if v.endswith("-Regular")), None)
            bold = next((v for v in sorted(variants)
                         if v.endswith("-Bold") or v.endswith("-SemiBold")), None)
            if reg and bold:
                return os.path.join(d, (bold if what == "bold" else reg) + ".ttf")
        return os.path.join(d, names[0])
    return None


FONT_REGULAR = _find_font("regular")
FONT_BOLD = _find_font("bold") or FONT_REGULAR
SETTINGS_FILE = os.path.join(HERE, "settings_ui.json")

BG = (24, 26, 32)
FG = (235, 235, 235)
DIM = (110, 115, 125)
HI = (255, 170, 40)      # selection highlight
ON = (90, 220, 120)      # pressed / enabled
OFF = (70, 74, 84)       # disabled
BODY = (52, 56, 66)
WARN = (255, 90, 90)

# Stick axes report 0..255 around a 128 centre.
STICK_RANGES = {0: (-32768, 32767), 1: (-32768, 32767),
                2: (-32768, 32767), 5: (-32768, 32767)}


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
        # Draw in a 640x480 logical space; SDL scales it to the panel,
        # so the layout holds on any device.
        sdl2.SDL_RenderSetScale(self.ren,
                                ctypes.c_float(self.w / 640.0),
                                ctypes.c_float(self.h / 480.0))
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
        for name, hid in LOGICAL_BUTTONS:
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
            names = [n for n, _ in LOGICAL_BUTTONS]
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
            names = [n for n, _ in LOGICAL_BUTTONS]
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
            axes = list(AXIS_NAMES)
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
        keys = {int(c): int(hid) for name, hid in LOGICAL_BUTTONS
                for c in [self.code_of_logical(name)] if c}
        disabled = set(self.settings["disabled"])
        keys = {c: h for c, h in keys.items() if h not in disabled}
        dpad = dict(DEFAULT_DPAD_KEYS)
        sens = dict(self.settings["sensitivity"])
        return State(keys, dpad, dict(ZERO28_AXES), {}, sensitivity=sens)

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
        for i, (name, _hid) in enumerate(LOGICAL_BUTTONS):
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
        for i, (name, _hid) in enumerate(LOGICAL_BUTTONS):
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
        for i, a in enumerate(AXIS_NAMES):
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
            "keys": {int(code): int(hid) for name, hid in LOGICAL_BUTTONS
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
            rc = run_pad(frame_cb=lambda state: self.draw_running(state),
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
        for name, hid in LOGICAL_BUTTONS:
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

    def show_unsupported(self, missing):
        """Explain on-screen why this device cannot be a wired gamepad."""
        log("unsupported device:", "; ".join(missing))
        u = self.ui
        u.rect(0, 0, u.w, u.h, BG)
        y = 48
        u.draw_text("Gamepad Mode", u.w // 2, y, 36, WARN, bold=True, center="x")
        y += 62
        u.draw_text("This device cannot be used as a gamepad.", u.w // 2, y,
                    22, FG, bold=True, center="x")
        y += 42
        u.draw_text("Missing hardware:", 40, y, 22, HI, bold=True)
        y += 34
        for line in missing:
            u.draw_text("- " + line, 48, y, 20, FG)
            y += 30
        y += 18
        try:
            has_bt = bool(os.listdir("/sys/class/bluetooth"))
        except OSError:
            has_bt = False
        notes = [
            "This app needs a gamepad on /dev/input and a USB device",
            "controller (OTG/data port) to create a USB HID gamepad.",
            ("Bluetooth adapter found, but this app is USB-only;"
             if has_bt else "No Bluetooth adapter detected;"),
            "BTPad is the separate project that supports Bluetooth.",
        ]
        for line in notes:
            u.draw_text(line, 40, y, 19, DIM)
            y += 26
        u.draw_text("Press A or B to return to Spruce", u.w // 2, u.h - 44,
                    20, FG, center="x")
        u.present()
        end = time.time() + 30
        while time.time() < end:
            for code, val in self.input.poll():
                if val and code in (KEY_A, KEY_B, KEY_MENU):
                    return
            time.sleep(0.03)

    def cleanup(self):
        self.stop_test()
        self.input.close()
        self.ui.quit()


LOCK_FILE = os.path.join(HERE, "gamepadmode.pid")


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


def show_unsupported(missing):
    """Show a clear on-screen reason why this device cannot run the pad.

    Try the normal SDL screen first (supported devices that simply lack a
    UDC); if SDL is broken on this platform, draw straight to /dev/fb0 with
    a tiny built-in font so the message is still visible. No SpruceOS file
    is modified and nothing is written outside the app folder."""
    log("unsupported device:", "; ".join(missing))
    try:
        app = App()
        try:
            app.show_unsupported(missing)
        finally:
            app.cleanup()
        return
    except Exception as e:
        log("SDL message unavailable (%s); using framebuffer fallback" % e)

    lines = ["GAMEPAD MODE - UNSUPPORTED DEVICE", "",
             "This device cannot be used as a gamepad.",
             "Missing hardware:", ""]
    lines += ["- " + m for m in missing]
    lines += ["", "This app needs a gamepad on /dev/input and a USB",
              "device controller (OTG/data port).", "Bluetooth mode is in BTPad,",
              "the project this app grew out of.", "", "Press A or B to return to Spruce."]
    try:
        _fb_text(lines)
    except Exception as e:
        log("framebuffer message unavailable:", e)


# 5x7 bitmap font (5-bit rows) for the fallback message; covers A-Z, 0-9 and
# the punctuation used above. Kept here so the message needs no external font.
_FONT = {
    " ": (0, 0, 0, 0, 0, 0, 0),
    "A": (14, 17, 17, 31, 17, 17, 17), "B": (30, 17, 17, 30, 17, 17, 30),
    "C": (14, 17, 16, 16, 16, 17, 14), "D": (30, 17, 17, 17, 17, 17, 30),
    "E": (31, 16, 16, 30, 16, 16, 31), "F": (31, 16, 16, 30, 16, 16, 16),
    "G": (14, 17, 16, 23, 17, 17, 15), "H": (17, 17, 17, 31, 17, 17, 17),
    "I": (14, 4, 4, 4, 4, 4, 14), "J": (7, 2, 2, 2, 2, 18, 12),
    "K": (17, 18, 20, 24, 20, 18, 17), "L": (16, 16, 16, 16, 16, 16, 31),
    "M": (17, 27, 21, 21, 17, 17, 17), "N": (17, 25, 21, 19, 17, 17, 17),
    "O": (14, 17, 17, 17, 17, 17, 14), "P": (30, 17, 17, 30, 16, 16, 16),
    "Q": (14, 17, 17, 17, 21, 18, 13), "R": (30, 17, 17, 30, 20, 18, 17),
    "S": (15, 16, 16, 14, 1, 1, 30), "T": (31, 4, 4, 4, 4, 4, 4),
    "U": (17, 17, 17, 17, 17, 17, 14), "V": (17, 17, 17, 17, 17, 10, 4),
    "W": (17, 17, 17, 21, 21, 27, 17), "X": (17, 17, 10, 4, 10, 17, 17),
    "Y": (17, 17, 10, 4, 4, 4, 4), "Z": (31, 1, 2, 4, 8, 16, 31),
    "0": (14, 17, 19, 21, 25, 17, 14), "1": (4, 12, 4, 4, 4, 4, 14),
    "2": (14, 17, 1, 2, 4, 8, 31), "3": (31, 2, 4, 2, 1, 17, 14),
    "4": (2, 6, 10, 18, 31, 2, 2), "5": (31, 16, 30, 1, 1, 17, 14),
    "6": (6, 8, 16, 30, 17, 17, 14), "7": (31, 17, 1, 2, 4, 4, 4),
    "8": (14, 17, 17, 14, 17, 17, 14), "9": (14, 17, 17, 15, 1, 2, 12),
    "-": (0, 0, 0, 31, 0, 0, 0), ":": (0, 4, 4, 0, 4, 4, 0),
    "/": (1, 1, 2, 4, 8, 16, 16), ".": (0, 0, 0, 0, 0, 4, 4),
    "(": (2, 4, 8, 8, 8, 4, 2), ")": (8, 4, 2, 2, 2, 4, 8),
    "=": (0, 0, 31, 0, 31, 0, 0), "+": (0, 4, 4, 31, 4, 4, 0),
    "?": (14, 17, 1, 2, 4, 0, 4), ",": (0, 0, 0, 0, 4, 4, 8),
}


def _fb_text(lines):
    """Draw the lines onto /dev/fb0 (32 bpp BGRA) with no SDL and no fonts."""
    def readnum(p):
        with open(p) as f:
            return f.read().strip()
    w, h = (int(x) for x in readnum("/sys/class/graphics/fb0/virtual_size").split(","))
    bpp = int(readnum("/sys/class/graphics/fb0/bits_per_pixel"))
    if bpp != 32 or w < 320 or h < 240:
        raise RuntimeError("unsupported framebuffer %dx%d %dbpp" % (w, h, bpp))
    bg = bytes((32, 26, 24, 255))   # BGRA for (24,26,32)
    fg = bytes((235, 235, 235, 255))
    hi = bytes((40, 170, 255, 255))  # orange headings
    buf = bytearray(bg * (w * h))

    def pixel(x, y, color):
        if 0 <= x < w and 0 <= y < h:
            o = (y * w + x) * 4
            buf[o:o + 4] = color

    def block(x, y, bw, bh, color):
        for yy in range(y, y + bh):
            for xx in range(x, x + bw):
                pixel(xx, yy, color)

    scale = max(2, min(4, w // 200))
    step = 8 * scale
    y = max(20, h // 12)
    left = max(16, w // 14)
    for i, line in enumerate(lines[:h // step]):
        color = hi if i == 0 or line.startswith("-") else fg
        line = line.upper()
        for ci, ch in enumerate(line):
            rows = _FONT.get(ch, _FONT["?"])
            for ry, bits in enumerate(rows):
                for rx in range(5):
                    if bits & (1 << (4 - rx)):
                        block(left + (ci * 6 + rx) * scale, y + ry * scale,
                              scale, scale, color)
        y += step
    fd = os.open("/dev/fb0", os.O_RDWR)
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        view = memoryview(buf)
        while view:
            n = os.write(fd, view)
            if not n:
                break
            view = view[n:]
    finally:
        os.close(fd)
    time.sleep(2)


def main():
    if "--check" in sys.argv:
        check()
        return 0
    if "--devices" in sys.argv:
        devices()
        return 0
    if "--probe" in sys.argv:
        probe(all_nodes="--all" in sys.argv)
        return 0
    if not single_instance():
        return 1
    app = None
    try:
        missing = hardware_issue()
        if missing:
            show_unsupported(missing)
            return 0
        app = App()
        app.loop()
    finally:
        if app is not None:
            app.cleanup()
        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
