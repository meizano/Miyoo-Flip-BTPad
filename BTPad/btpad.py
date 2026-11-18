#!/usr/bin/env python3
"""
BTPad - use a SpruceOS handheld as a Bluetooth or USB-cable HID gamepad.

Modes (auto by default):
    auto   Bluetooth when the device has an adapter, otherwise the USB cable
           (the MagicX Mini Zero 28 has no Bluetooth, so it uses USB)
    bt     Bluetooth only          (run with --bt)
    usb    USB cable only          (run with --usb)

Usage:
    python3 btpad.py            run the pad (what launch.sh does)
    python3 btpad.py --check    report what this device has (Bluetooth, USB gadget, pads)
    python3 btpad.py --probe    print raw button/stick events (to fix mapping.json)
                                (add --all to watch every input device, e.g. to find L3/R3)
    python3 btpad.py --sticks   show stick ranges: declared vs observed vs what is sent

Needs only: python3 (with ctypes), and either BlueZ (bluetoothd + D-Bus system bus)
or a kernel with configfs USB gadget support and the HID function (f_hid).
No dbus-python: a tiny D-Bus client is built in. Must run as root.
Flags: --debug (bluetoothd log), --nograb, --usb, --bt.
"""
import copy
import errno
import fcntl
import glob
import json
import math
import os
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
import time

try:
    import ctypes
except ImportError:  # reported by --check
    ctypes = None

HERE = os.path.dirname(os.path.abspath(__file__))
NAME = "SpruceOS Pad"
HID_UUID = "00001124-0000-1000-8000-00805f9b34fb"
PROFILE_PATH = "/miyoo/btpad"
PSM_CTRL, PSM_INTR = 0x11, 0x13
AF_BLUETOOTH, BTPROTO_L2CAP = 31, 0
EXIT_HOLD_SECONDS = 2.0  # hold L1+R1+Select+Start to quit

# What the pad calls itself on the USB cable. The existing vendor gadget keeps
# its own ADB identity, so this is written over its product string while the
# pad runs and put back on exit.
USB_PRODUCT = "Retro Gamepad"

# Logical button name -> the HID button number the outside world sees (1-based).
# Remapping and disabling in the Gamepad Mode settings work on these names.
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

SDP_XML = """<?xml version="1.0" encoding="UTF-8" ?>
<record>
 <attribute id="0x0001"><sequence><uuid value="0x1124"/></sequence></attribute>
 <attribute id="0x0004"><sequence>
  <sequence><uuid value="0x0100"/><uint16 value="0x0011"/></sequence>
  <sequence><uuid value="0x0011"/></sequence></sequence></attribute>
 <attribute id="0x0005"><sequence><uuid value="0x1002"/></sequence></attribute>
 <attribute id="0x0006"><sequence><uint16 value="0x656e"/><uint16 value="0x006a"/><uint16 value="0x0100"/></sequence></attribute>
 <attribute id="0x0009"><sequence><sequence><uuid value="0x1124"/><uint16 value="0x0100"/></sequence></sequence></attribute>
 <attribute id="0x000d"><sequence><sequence>
  <sequence><uuid value="0x0100"/><uint16 value="0x0013"/></sequence>
  <sequence><uuid value="0x0011"/></sequence></sequence></sequence></attribute>
 <attribute id="0x0100"><text value="%(name)s"/></attribute>
 <attribute id="0x0101"><text value="Gamepad"/></attribute>
 <attribute id="0x0102"><text value="Miyoo"/></attribute>
 <attribute id="0x0200"><uint16 value="0x0100"/></attribute>
 <attribute id="0x0201"><uint16 value="0x0111"/></attribute>
 <attribute id="0x0202"><uint8 value="0x08"/></attribute>
 <attribute id="0x0203"><uint8 value="0x00"/></attribute>
 <attribute id="0x0204"><boolean value="true"/></attribute>
 <attribute id="0x0205"><boolean value="true"/></attribute>
 <attribute id="0x0206"><sequence><sequence><uint8 value="0x22"/><text encoding="hex" value="%(desc)s"/></sequence></sequence></attribute>
 <attribute id="0x0207"><sequence><sequence><uint16 value="0x0409"/><uint16 value="0x0100"/></sequence></sequence></attribute>
 <attribute id="0x020b"><uint16 value="0x0100"/></attribute>
 <attribute id="0x020c"><uint16 value="0x0c80"/></attribute>
 <attribute id="0x020d"><boolean value="true"/></attribute>
 <attribute id="0x020e"><boolean value="false"/></attribute>
 <attribute id="0x020f"><uint16 value="0x0640"/></attribute>
 <attribute id="0x0210"><uint16 value="0x0320"/></attribute>
</record>
""" % {"name": NAME, "desc": REPORT_DESC.hex()}

# ---------------------------------------------------------------- default mappings
# evdev key code -> HID button number (1-based), matching what Android expects:
# 1=A(south) 2=B(east) 4=X(north) 5=Y(west) 7=L1 8=R1 9=L2 10=R2 11=Select 12=Start 13=Mode
DEFAULT_KEYMAP = {304: 1, 305: 2, 307: 4, 308: 5, 310: 7, 311: 8,
                  314: 11, 315: 12, 316: 13, 317: 14, 318: 15}
DEFAULT_DPAD_KEYS = {103: "up", 108: "down", 105: "left", 106: "right",
                     544: "up", 545: "down", 546: "left", 547: "right"}
DEFAULT_AXES = {0: "lx", 1: "ly", 3: "rx", 4: "ry"}
DEFAULT_TRIGGER_AXES = {2: 9, 5: 10}  # Flip's L2/R2 are analog-style axes -> buttons 9/10
EXIT_COMBO = (310, 311, 314, 315)     # L1 R1 Select Start

# MagicX Mini Zero 28 ("magicx-input", simplepad UART pad).
# Labels are Nintendo-style (B bottom, A right, Y top, X left) but BTPad maps by
# position, so the button codes are the same as the Flip's. L2/R2 are digital
# buttons (312/313). Right stick is ABS_Z (2) + ABS_RZ (5). MENU is KEY_BACK (158).
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

EV_KEY, EV_ABS = 1, 3
ABS_HAT0X, ABS_HAT0Y = 16, 17
EVENT_FMT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FMT)


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


def _int_map(d):
    return {int(k): int(v) for k, v in d.items()}


def _axis_map(d):
    return {int(k): str(v) for k, v in d.items()}


def load_mapping():
    """-> (profiles, want). mapping.json overrides the built-in profiles.

    New format: {"profiles": {"magicx-input": {"axes": {"2": "rx"}}}}.
    The old flat format (keys/dpad_keys/axes/trigger_axes/devices) still works
    and replaces the built-in profiles, as in BTPad 0.1.
    """
    profiles = copy.deepcopy(BUILTIN_PROFILES)
    p = os.path.join(HERE, "mapping.json")
    if not os.path.exists(p):
        return profiles, None
    with open(p) as f:
        cfg = json.load(f)

    if "profiles" in cfg:
        for key, over in (cfg["profiles"] or {}).items():
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

    # Old flat mapping.json: one custom profile, exactly like BTPad 0.1.
    prof = copy.deepcopy(BUILTIN_PROFILES[0])
    prof["name"] = "mapping.json"
    if "keys" in cfg:
        prof["keys"] = _int_map(cfg["keys"])
    if "dpad_keys" in cfg:
        prof["dpad_keys"] = {int(k): v for k, v in cfg["dpad_keys"].items()}
    if "axes" in cfg:
        prof["axes"] = _axis_map(cfg["axes"])
    if "trigger_axes" in cfg:
        prof["trigger_axes"] = _int_map(cfg["trigger_axes"])
    devices = cfg.get("devices")
    prof["devices"] = list(devices) if devices is not None else []
    log("loaded mapping.json")
    return [prof], devices


# ---------------------------------------------------------------- tiny D-Bus client
def _align(b, n):
    b.extend(b"\0" * (-len(b) % n))


def _str(b, s):
    _align(b, 4)
    raw = s.encode()
    b.extend(struct.pack("<I", len(raw)) + raw + b"\0")


def _sig(b, s):
    raw = s.encode()
    b.append(len(raw))
    b.extend(raw + b"\0")


AGENT_PATH = "/miyoo/btpad_agent"


def _parse_fields(data):
    out, i = {}, 0
    while i < len(data):
        i += -i % 8
        if i >= len(data):
            break
        code = data[i]
        sl = data[i + 1]
        sig = data[i + 2:i + 2 + sl].decode()
        i += sl + 3
        if sig in ("o", "s"):
            i += -i % 4
            n = struct.unpack_from("<I", data, i)[0]
            i += 4
            out[code] = data[i:i + n].decode(errors="replace")
            i += n + 1
        elif sig == "g":
            n = data[i]
            out[code] = data[i + 1:i + 1 + n].decode()
            i += n + 2
        elif sig == "u":
            i += -i % 4
            out[code] = struct.unpack_from("<I", data, i)[0]
            i += 4
        else:
            break
    return out


class Bus:
    """Just enough D-Bus (system bus) for BlueZ: calls, property sets, and a pairing agent."""

    def __init__(self, path=None):
        if path is None:
            path = "/run/dbus/system_bus_socket"
            if not os.path.exists(path):
                path = "/var/run/dbus/system_bus_socket"
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect(path)
        self.serial = 0
        self.sock.sendall(b"\0AUTH EXTERNAL " + str(os.getuid()).encode().hex().encode() + b"\r\n")
        buf = b""
        while not buf.endswith(b"\r\n"):
            chunk = self.sock.recv(256)
            if not chunk:
                raise OSError("bus closed during auth")
            buf += chunk
        if not buf.startswith(b"OK"):
            raise OSError("bus auth failed: %r" % buf)
        self.sock.sendall(b"BEGIN\r\n")
        rb = self.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "Hello")
        n = struct.unpack_from("<I", rb, 0)[0]
        self.name = rb[4:4 + n].decode()

    # -- wire helpers
    def _recv(self, n):
        out = b""
        while len(out) < n:
            c = self.sock.recv(n - len(out))
            if not c:
                raise OSError("bus closed")
            out += c
        return out

    def _read(self):
        h = self._recv(16)
        blen, serial, flen = struct.unpack("<III", h[4:16])
        pad = -flen % 8
        rest = self._recv(flen + pad + blen)
        return h[1], serial, _parse_fields(rest[:flen]), rest[flen + pad:]

    def _build(self, typ, flags, flds, body):
        self.serial += 1
        h = bytearray()
        for code, vsig, val in flds:
            _align(h, 8)
            h.append(code)
            _sig(h, vsig)
            if vsig in ("o", "s"):
                _str(h, val)
            elif vsig == "g":
                _sig(h, val)
            elif vsig == "u":
                _align(h, 4)
                h.extend(struct.pack("<I", val))
        msg = bytearray(struct.pack("<cBBBII", b"l", typ, flags, 1, len(body), self.serial))
        msg += struct.pack("<I", len(h)) + h
        _align(msg, 8)
        msg += body
        return bytes(msg)

    def _reply(self, to, req_serial, sig="", body=b""):
        flds = [(5, "u", req_serial), (6, "s", to)]
        if sig:
            flds.append((8, "g", sig))
        self.sock.sendall(self._build(2, 1, flds, body))

    def _error(self, to, req_serial, name, text):
        body = bytearray()
        _str(body, text)
        flds = [(4, "s", name), (5, "u", req_serial), (6, "s", to), (8, "g", "s")]
        self.sock.sendall(self._build(3, 1, flds, bytes(body)))

    def _handle(self, typ, serial, f, body):
        """Answer calls BlueZ makes to our pairing agent."""
        if typ != 1:
            return
        member, sender = f.get(3, ""), f.get(7, "")
        if f.get(1) == AGENT_PATH and f.get(2) == "org.bluez.Agent1":
            log("agent call:", member)
            if member == "RequestPinCode":
                b = bytearray()
                _str(b, "0000")
                self._reply(sender, serial, "s", bytes(b))
            elif member == "RequestPasskey":
                self._reply(sender, serial, "u", struct.pack("<I", 0))
            else:  # RequestConfirmation, RequestAuthorization, AuthorizeService, Cancel, Release...
                self._reply(sender, serial)
        else:
            self._error(sender, serial, "org.freedesktop.DBus.Error.UnknownMethod", "unknown method")

    def pump(self):
        """Read and answer one incoming message (call when the socket is readable)."""
        self.sock.settimeout(3)
        typ, serial, f, body = self._read()
        self._handle(typ, serial, f, body)

    # -- calls
    def call(self, dest, path, iface, member, sig="", body=b""):
        flds = [(1, "o", path), (2, "s", iface), (3, "s", member), (6, "s", dest)]
        if sig:
            flds.append((8, "g", sig))
        self.sock.sendall(self._build(1, 0, flds, body))
        while True:
            typ, serial, f, rbody = self._read()
            if typ == 2:
                return rbody
            if typ == 3:
                text = ""
                if len(rbody) >= 4:
                    n = struct.unpack("<I", rbody[:4])[0]
                    text = rbody[4:4 + n].decode(errors="replace")
                raise RuntimeError("D-Bus error: " + text)
            self._handle(typ, serial, f, rbody)

    def set_prop(self, prop, vsig, val, iface="org.bluez.Adapter1", path="/org/bluez/hci0"):
        body = bytearray()
        _str(body, iface)
        _str(body, prop)
        _sig(body, vsig)
        if vsig == "s":
            _str(body, val)
        else:  # "b" or "u"
            _align(body, 4)
            body += struct.pack("<I", int(val))
        self.call("org.bluez", path, "org.freedesktop.DBus.Properties", "Set", "ssv", bytes(body))

    def get_prop(self, prop, iface="org.bluez.Adapter1", path="/org/bluez/hci0"):
        body = bytearray()
        _str(body, iface)
        _str(body, prop)
        rb = self.call("org.bluez", path, "org.freedesktop.DBus.Properties", "Get", "ss", bytes(body))
        sl = rb[0]
        sig = rb[1:1 + sl].decode()
        i = 1 + sl + 1
        if sig == "b":
            i += -i % 4
            return bool(struct.unpack_from("<I", rb, i)[0])
        return None

    def register_agent(self):
        body = bytearray()
        _str(body, AGENT_PATH)
        _str(body, "NoInputNoOutput")
        self.call("org.bluez", "/org/bluez", "org.bluez.AgentManager1", "RegisterAgent", "os", bytes(body))
        body = bytearray()
        _str(body, AGENT_PATH)
        self.call("org.bluez", "/org/bluez", "org.bluez.AgentManager1", "RequestDefaultAgent", "o", bytes(body))

    def unregister_agent(self):
        body = bytearray()
        _str(body, AGENT_PATH)
        self.call("org.bluez", "/org/bluez", "org.bluez.AgentManager1", "UnregisterAgent", "o", bytes(body))

    def register_profile(self):
        body = bytearray()
        _str(body, PROFILE_PATH)
        _str(body, HID_UUID)
        _align(body, 4)
        lenpos = len(body)
        body += b"\0\0\0\0"
        _align(body, 8)
        start = len(body)
        for key, vsig, val in (("ServiceRecord", "s", SDP_XML), ("Role", "s", "server"),
                               ("RequireAuthentication", "b", False),
                               ("RequireAuthorization", "b", False),
                               ("AutoConnect", "b", False)):
            _align(body, 8)
            _str(body, key)
            _sig(body, vsig)
            if vsig == "s":
                _str(body, val)
            else:
                _align(body, 4)
                body += struct.pack("<I", int(val))
        struct.pack_into("<I", body, lenpos, len(body) - start)
        self.call("org.bluez", "/org/bluez", "org.bluez.ProfileManager1",
                  "RegisterProfile", "osa{sv}", bytes(body))


# ---------------------------------------------------------------- bluetooth plumbing
def bluetoothd_info():
    """-> (pid, argv with real exe path, cwd) or (None, None, None)"""
    for p in os.listdir("/proc"):
        if not p.isdigit():
            continue
        try:
            with open("/proc/%s/comm" % p) as f:
                if f.read().strip() != "bluetoothd":
                    continue
            with open("/proc/%s/cmdline" % p, "rb") as f:
                args = [a.decode() for a in f.read().split(b"\0") if a]
            if not args:
                continue
            try:
                exe = os.readlink("/proc/%s/exe" % p).replace(" (deleted)", "")
            except OSError:
                exe = args[0]
            try:
                cwd = os.readlink("/proc/%s/cwd" % p)
            except OSError:
                cwd = "/"
            return int(p), [exe] + args[1:], cwd
        except OSError:
            pass
    return None, None, None


def restart_bluetoothd(args, cwd, logfile=None):
    pid = bluetoothd_info()[0]
    if pid:
        os.kill(pid, signal.SIGTERM)
        for _ in range(30):
            if bluetoothd_info()[0] is None:
                break
            time.sleep(0.2)
    time.sleep(0.5)
    out = open(logfile, "ab") if logfile else subprocess.DEVNULL
    subprocess.Popen(args, cwd=cwd, stdout=out, stderr=out, start_new_session=True)
    time.sleep(1.5)


def l2cap_accept(lsock):
    """accept() via libc: Python's socket.accept() raises 'bad family' for Bluetooth here."""
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.accept(lsock.fileno(), None, None)
    if fd < 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e))
    return socket.socket(AF_BLUETOOTH, socket.SOCK_SEQPACKET, BTPROTO_L2CAP, fileno=fd)


def make_listener(psm):
    """L2CAP listening socket. This Python lacks socket.AF_BLUETOOTH, so bind via libc."""
    s = socket.socket(AF_BLUETOOTH, socket.SOCK_SEQPACKET, BTPROTO_L2CAP)
    addr = struct.pack("<HH6sHBx", AF_BLUETOOTH, psm, b"\0" * 6, 0, 0)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.bind(s.fileno(), addr, len(addr)) != 0:
        e = ctypes.get_errno()
        s.close()
        raise OSError(e, os.strerror(e))
    s.listen(1)
    return s


def unblock_bluetooth():
    for d in glob.glob("/sys/class/rfkill/rfkill*"):
        try:
            with open(d + "/type") as f:
                if f.read().strip() != "bluetooth":
                    continue
            with open(d + "/soft", "w") as f:
                f.write("0")
        except OSError:
            pass


def ensure_powered(bus):
    """Spruce can show Bluetooth as 'on' while the adapter is actually off; make sure it's on."""
    for attempt in range(12):
        try:
            if bus.get_prop("Powered"):
                return True
        except Exception as e:
            log("adapter not ready yet:", e)
        unblock_bluetooth()
        try:
            bus.set_prop("Powered", "b", True)
        except Exception as e:
            log("power on failed:", e)
        if attempt == 3:
            try:
                subprocess.run(["hciconfig", "hci0", "up"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
            except OSError:
                pass
        time.sleep(1)
    log("WARNING: the Bluetooth adapter is still powered off; the host will not see the pad")
    return False


def setup_adapter(bus, name):
    if ensure_powered(bus):
        log("adapter powered on")
    time.sleep(0.5)
    for prop, vsig, val in (("Alias", "s", name), ("Pairable", "b", True),
                            ("DiscoverableTimeout", "u", 0), ("Discoverable", "b", True)):
        try:
            bus.set_prop(prop, vsig, val)
        except Exception as e:
            log("could not set %s: %s" % (prop, e))
    try:
        bus.register_agent()
    except Exception as e:
        log("could not register pairing agent:", e)


def log_adapter_info():
    for cmd in (["hciconfig", "hci0", "-a"], ["hciconfig", "hci0", "sspmode"]):
        try:
            out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 universal_newlines=True).stdout
        except OSError:
            continue
        for line in out.splitlines():
            if any(k in line for k in ("Name", "Class", "UP", "SSP", "BD Address")):
                log("hci:", line.strip())


def set_device_class():
    try:
        r = subprocess.run(["hciconfig", "hci0", "class", "0x002508"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if r.returncode == 0:
            return
    except OSError:
        pass
    log("could not set gamepad device class (not fatal)")


class BtLink:
    """Bluetooth HID device backend (L2CAP PSM 17/19 + BlueZ profile)."""

    def __init__(self, name, debug=False):
        self.name = name
        self.debug = debug
        self.original_btd = None
        self.bus = None
        self.ctrl_l = self.intr_l = None
        self.ctrl = self.intr = None
        self._closed = False

    def start(self):
        _pid, args, cwd = bluetoothd_info()
        if not args:
            log("bluetoothd is not running - turn Bluetooth on in Spruce first")
            return False
        btd_log = os.path.join(HERE, "bluetoothd.log") if self.debug else None
        if "--noplugin=input" not in args:
            # BlueZ's input plugin owns the HID UUID and ports, which blocks HID *device* mode
            log("restarting bluetoothd without its input plugin (needed to act as a HID device)")
            self.original_btd = (args, cwd)
            restart_bluetoothd(args + ["--noplugin=input"] + (["-d"] if self.debug else []),
                               cwd, btd_log)
        self.ctrl_l, self.intr_l = make_listener(PSM_CTRL), make_listener(PSM_INTR)
        self.bus = Bus()
        deadline = time.time() + 15
        while True:
            try:
                self.bus.register_profile()
                break
            except RuntimeError as e:
                if "org.bluez" not in str(e) or time.time() > deadline:
                    self.close()
                    raise
                log("waiting for bluez:", e)
                time.sleep(1)
        setup_adapter(self.bus, self.name)
        set_device_class()
        log_adapter_info()
        log("ready: on the host, scan for '%s' and pair" % self.name)
        return True

    def sockets(self):
        out = []
        if self.bus:
            out.append(self.bus.sock)
        for s in (self.ctrl_l, self.intr_l, self.ctrl, self.intr):
            if s:
                out.append(s)
        return out

    def drop(self):
        had = bool(self.ctrl or self.intr)
        for k in ("ctrl", "intr"):
            c = getattr(self, k)
            if c:
                try:
                    c.close()
                except OSError:
                    pass
                setattr(self, k, None)
        if had:
            log("host disconnected; waiting for reconnect")

    def push(self, report):
        if self.intr:
            try:
                self.intr.send(b"\xA1" + report)
            except OSError:
                self.drop()

    def handle_ctrl(self, state):
        try:
            data = self.ctrl.recv(64)
        except OSError:
            data = b""
        if not data:
            self.drop()
            return
        t = data[0] >> 4
        try:
            if t == 0x1 and (data[0] & 0xF) == 5:      # virtual cable unplug
                self.drop()
            elif t == 0x4:                              # GET_REPORT
                self.ctrl.send(b"\xA1" + state.report())
            elif t == 0x6:                              # GET_PROTOCOL -> report mode
                self.ctrl.send(b"\xA0\x01")
            elif t == 0x8:                              # GET_IDLE
                self.ctrl.send(b"\xA0\x00")
            elif t in (0x5, 0x7, 0x9):                  # SET_REPORT/PROTOCOL/IDLE -> success
                self.ctrl.send(b"\x00")
        except OSError:
            self.drop()

    def handle(self, sock, state):
        if sock is self.bus.sock:
            self.bus.pump()
        elif sock is self.ctrl_l:
            if self.ctrl:
                self.ctrl.close()
            self.ctrl = l2cap_accept(self.ctrl_l)
            log("control channel connected")
        elif sock is self.intr_l:
            if self.intr:
                self.intr.close()
            self.intr = l2cap_accept(self.intr_l)
            log("interrupt channel connected - pad is live")
        elif sock is self.ctrl:
            self.handle_ctrl(state)
        elif sock is self.intr:
            try:
                if not self.intr.recv(64):
                    self.drop()
            except OSError:
                self.drop()

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.drop()
        if self.bus:
            for fn in (lambda: self.bus.set_prop("Discoverable", "b", False), self.bus.unregister_agent):
                try:
                    fn()
                except Exception:
                    pass
        for l in (self.ctrl_l, self.intr_l):
            if l:
                try:
                    l.close()
                except OSError:
                    pass
        if self.bus:
            try:
                self.bus.sock.close()
            except OSError:
                pass
        self.bus = self.ctrl_l = self.intr_l = None
        if self.original_btd:
            log("restoring bluetoothd")
            try:
                restart_bluetoothd(self.original_btd[0], self.original_btd[1])
            except Exception as e:
                log("could not restore bluetoothd (%s); restore by hand: %s"
                    % (e, " ".join(self.original_btd[0])))
            self.original_btd = None


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
USB_GADGET_NAME = "btpad"
USB_GADGET_PATH = USB_GADGET_ROOT + "/" + USB_GADGET_NAME
USB_VID = "0x1209"   # pid.codes (open-source vendor id)
USB_PID = "0xb7ad"   # BTPad
USB_HIDG = "/dev/hidg0"

FFS_NAME = "btpad"
FFS_DIR = "/dev/usb-ffs/" + FFS_NAME
FFS_EP0 = FFS_DIR + "/ep0"
FFS_EP_IN = FFS_DIR + "/ep81"   # named after the endpoint descriptor address

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
    """Pick the USB controller to bind. Spruce's own gadget holds one on some devices,
    and some kernels expose more than one, so honour settings.json "udc" first and
    otherwise take a controller no other gadget owns."""
    names = udc_names()
    if not names:
        return None
    want = str(load_settings().get("udc") or "").strip()
    if want:
        if want in names:
            return want
        log("settings.json asks for USB controller %r but this device has: %s"
            % (want, ", ".join(names)))
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
    path = os.path.join(USB_GADGET_ROOT, names[0]) if names else USB_GADGET_ROOT + "/btpad-probe"
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
    if name:
        sharing = " (shares the existing gadget %s)" % name
    else:
        sharing = ""
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


def usb_gadget_remove_tree(path):
    _remove_tree(path)


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
        self.role_path = None
        self.role_restore = None
        self.host = False
        self.enabled = False     # FunctionFS sent FUNCTIONFS_ENABLE
        self.pending = None
        self.report = bytes(REPORT_LENGTH)
        self.wrote_once = False
        self._open_warned = 0.0
        self._open_try = 0.0

    def start(self):
        if os.getuid() != 0:
            raise RuntimeError("must run as root to create a USB gadget")
        if not mount_configfs():
            raise RuntimeError("configfs is not available (mount -t configfs none /sys/kernel/config failed)")
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
            _write(USB_GADGET_PATH + "/strings/0x409/serialnumber", "SPRUCEBTPAD01")

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
        self._rename_product()
        self.ep0 = os.open(FFS_EP0, os.O_RDWR)
        os.write(self.ep0, ffs_descriptors())
        os.write(self.ep0, ffs_strings())
        os.set_blocking(self.ep0, False)
        log("USB: descriptors accepted by FunctionFS")
        if self.restore_udc:
            _write(self.gadget + "/UDC", "")
            log("USB: unbound %s" % os.path.basename(self.gadget))
        os.symlink(self.fn, self.link)
        log("USB: function linked into %s" % os.path.basename(cfg))
        self._bind_udc()
        log("USB HID gadget (FunctionFS) ready on %s, added to %s"
            % (self.udc, os.path.basename(self.gadget)))
        log("USB: waiting for the host to enable the HID function")

    def _rename_product(self):
        """The host should see the pad, not the vendor ADB gadget name."""
        prod = self.gadget + "/strings/0x409/product"
        try:
            self.old_product = _read(prod)
        except OSError:
            self.old_product = None
        if self.old_product != USB_PRODUCT:
            try:
                _write(prod, USB_PRODUCT)
            except OSError:
                self.old_product = None
                return
            log("USB: product string now %r (was %r)" % (USB_PRODUCT, self.old_product))

    def _restore_product(self):
        if self.gadget and self.old_product:
            try:
                _write(self.gadget + "/strings/0x409/product", self.old_product)
            except OSError:
                pass
        self.old_product = None

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
                self._ep0_write(self.report[:length])
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
        self._open_try = time.time()
        path = self._report_path()
        if path is None:
            return
        if self.mode != "ffs" and not os.path.exists(path):
            # Minimal systems without devtmpfs/mdev rules need the node made by hand.
            try:
                dev = _read("/sys/class/hidg/%s/dev" % os.path.basename(path))
                major, minor = (int(x) for x in dev.split(":"))
                os.mknod(path, stat.S_IFCHR | 0o600, os.makedev(major, minor))
            except (OSError, ValueError):
                pass
        try:
            self.fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
            self._open_warned = 0.0
            log("USB: %s open, sending reports" % path)
        except OSError as e:
            # warn at most every 15 s so a missing node does not flood the log
            if time.time() - self._open_warned > 15:
                log("could not open %s: %s" % (path, e))
                self._open_warned = time.time()

    def _close_fd(self, quiet=False):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
            self._open_try = 0.0
            if not quiet:
                log("closing %s; waiting for the host" % self._report_path())

    def push(self, report):
        """Send one report. A full pipe is not an error: keep it and retry in flush()."""
        self.report = report
        if self.mode == "ffs" and not self.enabled:
            # Never touch the endpoint before the host enabled the function.
            self.pending = report
            return False
        if not self.host:
            # Same for the f_hid path: nothing to talk to before the host has
            # configured the gadget. Writing early can hang this USB controller.
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
        if self.fn and os.path.isdir(self.fn):
            _remove_tree(self.fn)
        try:
            os.rmdir(FFS_DIR)
        except OSError:
            pass
        if self.own_gadget:
            usb_gadget_remove()
        elif self.gadget and self.restore_udc:
            self._restore_product()
            try:
                _write(self.gadget + "/UDC", self.restore_udc)
            except OSError:
                pass
        self.own_gadget = False
        self.gadget = self.fn = self.link = None


# ---------------------------------------------------------------- mode selection
def load_settings():
    try:
        with open(os.path.join(HERE, "settings.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def has_bluetooth_adapter():
    return bool(glob.glob("/sys/class/bluetooth/hci*"))


def choose_mode():
    if "--usb" in sys.argv:
        return "usb"
    if "--bt" in sys.argv:
        return "bt"
    mode = str(load_settings().get("mode", "auto")).lower()
    if mode in ("usb", "bt"):
        return mode
    if has_bluetooth_adapter():
        log("auto mode: Bluetooth adapter found")
        return "bt"
    if have_usb_gadget():
        log("auto mode: no Bluetooth adapter; using the USB cable")
        return "usb"
    log("auto mode: no Bluetooth adapter and no USB device controller")
    return "bt"


def link_name(prof):
    devices = " ".join(prof.get("devices") or []).lower()
    if "miyoo" in devices:
        return "Miyoo Flip Pad"
    if "magicx" in devices:
        return "MagicX Zero 28 Pad"
    return NAME


# ---------------------------------------------------------------- modes
def do_check():
    print("python", sys.version.split()[0])
    print("ctypes:", "OK" if ctypes else "MISSING  <- required")
    try:
        s = socket.socket(AF_BLUETOOTH, socket.SOCK_SEQPACKET, BTPROTO_L2CAP)
        s.close()
        print("L2CAP socket: OK")
    except OSError as e:
        print("L2CAP socket: FAILED", e)
    try:
        Bus()
        print("D-Bus system bus: OK")
    except Exception as e:
        print("D-Bus system bus: FAILED", e)
    for tool in ("hciconfig",):
        found = any(os.access(os.path.join(d, tool), os.X_OK) for d in os.environ.get("PATH", "").split(":"))
        print("%-13s %s" % (tool, "found" if found else "not in PATH"))
    print("bluetooth adapter:", "yes" if has_bluetooth_adapter() else "no")
    print("bluetoothd:", bluetoothd_info())
    names = udc_names()
    busy = bound_udcs()
    if names:
        print("USB controllers:", ", ".join(
            "%s[%s%s]" % (n, udc_state(n), ", used by " + busy[n] if n in busy else "")
            for n in names))
    else:
        print("USB controllers: none (/sys/class/udc is empty)")
    print("hidg node:", USB_HIDG if os.path.exists(USB_HIDG)
          else USB_HIDG + " absent (not needed with FunctionFS)")
    roles = otg_role_paths()
    for p in roles:
        try:
            print("otg role:", p, "=", _read(p))
        except OSError:
            pass
    if not roles:
        print("otg role: none (no usbc*/otg_role on this device)")
    want_udc = str(load_settings().get("udc") or "").strip()
    if want_udc:
        print("settings.json udc:", want_udc)
    ok, msg = usb_hid_probe()
    print("USB HID gadget:", ("OK - " if ok else "FAILED - ") + msg)
    print("uid:", os.getuid(), "(needs 0)")

    profiles, want = load_mapping()
    print("profiles:", ", ".join(p["name"] for p in profiles))
    pads = open_pads(want)
    print("pads that will be used:", [p.name for p in pads])
    if pads:
        prof, first = choose_profile(pads, profiles)
        use = keep_pads(pads, prof, first)
        print("selected profile:", prof["name"], "for", first.name, "(pads: %s)"
              % ", ".join(p.name for p in use))
    for p in pads:
        os.close(p.fd)
    print("mode that auto would choose:", choose_mode())


def do_devices():
    """List every input device with the codes it can send (for a missing profile)."""
    profiles, _want = load_mapping()
    paths = sorted(glob.glob("/dev/input/event*"))
    if not paths:
        print("no /dev/input/event* devices")
        return
    for path in paths:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as e:
            print("%-16s <%s>" % (path, e))
            continue
        name = ev_name(fd)
        keys, absc = ev_bits(fd, EV_KEY), ev_bits(fd, EV_ABS)
        prof = None
        for pr in profiles:
            if pr["devices"] and (path in pr["devices"]
                                  or any(d.lower() in name.lower() for d in pr["devices"])):
                prof = pr
                break
        print("%s  %r" % (path, name))
        print("    %s%s" % ("gamepad-like" if _looks_like_pad(keys, absc) else "not a gamepad",
                            "; profile: " + prof["name"] if prof else
                            "; no profile (would fall back)"))
        if keys:
            shown = [c for c in sorted(keys) if c < 0x300]
            print("    keys:", " ".join(str(c) for c in shown) or "none")
        if absc:
            print("    abs :", " ".join("%d(%d..%d)" % (c, *abs_range(fd, c))
                                        for c in sorted(absc) if c < 0x40) or "none")
        os.close(fd)


def do_usb_test():
    """--usb-test: bind the gadget and pulse A, to check the host side without the pad."""
    usb = UsbGadget("BTPad test pad")
    try:
        usb.start()
    except Exception as e:
        log("could not start:", e)
        if have_usb_gadget():
            log("run --check to see whether this kernel supports USB HID gadget mode")
        return 1
    log("USB test: pulsing button A (bit 1) every 0.5 s - watch the host")
    on = False
    last_note = time.time()
    try:
        while True:
            st = usb.tick()
            if not usb.host and time.time() - last_note > 10:
                log("waiting for a host; controller state %r (cable in the data/OTG port?)" % st)
                last_note = time.time()
            usb.push(bytes([0x01, 0x01 if on else 0x00, 0x00, 0x00, 0x80, 0x80, 0x80, 0x80]))
            usb.flush()
            on = not on
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        usb.close()
    return 0


def do_probe(all_devices=False):
    pads = open_pads([""] if all_devices else None)
    if not pads:
        print("no gamepad-like input devices found")
        return
    for p in pads:
        print("watching", p.path, p.name)
    print("press buttons / move sticks (Ctrl-C to stop)")
    byfd = {p.fd: p for p in pads}
    try:
        while True:
            r, _, _ = select.select(list(byfd), [], [], 1)
            for fd in r:
                data = os.read(fd, EVENT_SIZE * 32)
                for off in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
                    _s, _u, t, c, v = struct.unpack(EVENT_FMT, data[off:off + EVENT_SIZE])
                    if t in (EV_KEY, EV_ABS):
                        print("%s  %s code=%d value=%d" % (byfd[fd].name, "KEY" if t == EV_KEY else "ABS", c, v))
    except KeyboardInterrupt:
        pass


def do_sticks():
    """Show declared range, observed range and what BTPad would send for each stick axis."""
    profiles, _want = load_mapping()
    pads = open_pads(None)
    if not pads:
        print("no gamepad-like input devices found")
        return
    prof, _first = choose_profile(pads, profiles)
    axes = prof["axes"]
    print("profile:", prof["name"])
    obs, ranges, radius = {}, {}, {}
    for p in pads:
        for c in sorted(set(axes) & p.abs):
            ranges[c] = p.ranges[c]
            print("axis %d (%s): declared range %s" % (c, axes[c], p.ranges[c]))
    print("Rotate each stick slowly around its edge a few times, then push it halfway left and hold.")
    print("Ctrl-C prints a summary.")
    byfd = {p.fd: p for p in pads}
    last, shown = {}, 0.0
    try:
        while True:
            r, _, _ = select.select(list(byfd), [], [], 0.2)
            for fd in r:
                data = os.read(fd, EVENT_SIZE * 64)
                for off in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
                    _s, _u, t, c, v = struct.unpack(EVENT_FMT, data[off:off + EVENT_SIZE])
                    if t == EV_ABS and c in ranges:
                        o = obs.setdefault(c, [v, v])
                        o[0], o[1] = min(o[0], v), max(o[1], v)
                        last[c] = v
                        for a, b in (("lx", "ly"), ("rx", "ry")):
                            ca = next((k for k, n in axes.items() if n == a), None)
                            cb = next((k for k, n in axes.items() if n == b), None)
                            if c in (ca, cb) and ca in last and cb in last:
                                r_ = math.hypot(last[ca] / float(ranges[ca][1]), last[cb] / float(ranges[cb][1]))
                                radius[a[0]] = max(radius.get(a[0], 0.0), r_)
            if time.time() - shown > 0.3 and last:
                shown = time.time()
                print("\r" + "  ".join("%s raw=%6d sent=%3d" % (
                    axes[c], v, scale_axis(v, *ranges[c])) for c, v in sorted(last.items())), end="   ", flush=True)
    except KeyboardInterrupt:
        pass
    print("\n--- summary ---")
    for c, (lo, hi) in sorted(ranges.items()):
        o = obs.get(c)
        if not o:
            print("%s: no movement seen" % axes[c])
            continue
        note = "  <-- goes OUTSIDE the declared range (BTPad clips it)" if o[0] < lo or o[1] > hi else ""
        print("%s: declared %s, observed %d..%d%s" % (axes[c], (lo, hi), o[0], o[1], note))
    for side, name in (("l", "left"), ("r", "right")):
        if side in radius:
            print("%s stick: furthest reach = %.2f x full range (1.00 = round gate, 1.41 = square corners)"
                  % (name, radius[side]))


def run(frame_cb=None, overrides=None):
    """Run the pad. frame_cb(state) is called every loop pass (used by the
    Gamepad Mode UI to draw the live layout); overrides carries the settings
    made in that UI: keys {evdev code: hid button}, disabled [hid button],
    sensitivity {lx/ly/rx/ry: percent}. Both default to the plain CLI run."""
    debug = "--debug" in sys.argv
    grab = "--nograb" not in sys.argv
    running = [True]
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: running.__setitem__(0, False))

    profiles, want = load_mapping()
    pads = open_pads(want)
    if not pads:
        log("no gamepad input devices found; run --check / --probe")
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
        if grab:
            try:
                fcntl.ioctl(p.fd, EVIOCGRAB, 1)  # keep Spruce from also reacting to the buttons
            except OSError:
                log("could not grab", p.path)
    if grab:
        log("buttons are grabbed: Spruce's screen will not respond while this runs")
    log("to quit: hold L1+R1+Select+Start for 2s, or press Ctrl-C here")
    log("profile:", prof["name"])

    mode = choose_mode()
    name = link_name(prof)
    bt = usb = None
    try:
        if mode == "usb":
            if not have_usb_gadget():
                raise RuntimeError("this device has no USB device controller; cable mode is not possible")
            usb = UsbGadget(name)
            usb.start()
            log("mode: USB cable")
        else:
            log("mode: Bluetooth")
            bt = BtLink(name, debug)
            if not bt.start():
                raise RuntimeError("Bluetooth is not available; try --usb or set \"mode\": \"usb\"")
    except Exception as e:
        log("could not start:", e)
        if mode == "usb":
            log("run 'btpad.py --check' to see whether this kernel supports USB HID gadget mode")
        if bt:
            bt.close()
        if usb:
            usb.close()
        for p in pads:
            try:
                fcntl.ioctl(p.fd, EVIOCGRAB, 0)
                os.close(p.fd)
            except OSError:
                pass
        return 1

    state = State(prof["keys"], prof["dpad_keys"], prof["axes"], prof["trigger_axes"],
                  sensitivity=(overrides or {}).get("sensitivity"))
    byfd = {p.fd: p for p in pads}
    exit_since = None
    last_push = 0.0
    seen_input = False
    warned_input = time.time()

    def push():
        report = state.report()
        if bt:
            bt.push(report)
        if usb:
            usb.push(report)

    try:
        while running[0]:
            rl = list(byfd)
            if bt:
                rl += bt.sockets()
            try:
                ready, _, _ = select.select(rl, [], [], 0.05)
            except InterruptedError:
                continue
            for s in ready:
                if bt and s in bt.sockets():
                    try:
                        bt.handle(s, state)
                    except (OSError, socket.timeout) as e:
                        log("Bluetooth connection problem:", e)
                        running[0] = False
                elif s in byfd:
                    pad = byfd[s]
                    try:
                        data = os.read(s, EVENT_SIZE * 64)
                    except BlockingIOError:
                        continue
                    changed = False
                    for off in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
                        _sec, _usec, t, c, v = struct.unpack(EVENT_FMT, data[off:off + EVENT_SIZE])
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
                    log("no button or stick events yet; if the host shows the pad but nothing "
                        "happens, quit and run: btpad.py --devices")
            if ((bt and bt.intr) or (usb and usb.host)) and time.time() - last_push > 0.1:
                push()  # keep-alive: re-send the current state so a lost report cannot stick
                last_push = time.time()
            if frame_cb:
                frame_cb(state)
            if state.exit_combo_held():
                exit_since = exit_since or time.time()
                if time.time() - exit_since > EXIT_HOLD_SECONDS:
                    log("exit combo held, quitting")
                    break
            else:
                exit_since = None
    finally:
        if bt:
            bt.close()
        if usb:
            usb.close()
        for p in pads:
            try:
                fcntl.ioctl(p.fd, EVIOCGRAB, 0)
                os.close(p.fd)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    if "--check" in sys.argv:
        do_check()
    elif "--devices" in sys.argv:
        do_devices()
    elif "--usb-test" in sys.argv:
        sys.exit(do_usb_test())
    elif "--sticks" in sys.argv:
        do_sticks()
    elif "--probe" in sys.argv:
        do_probe("--all" in sys.argv)
    else:
        sys.exit(run())
