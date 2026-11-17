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
    def __init__(self, keymap, dpad_keys, axes, trig):
        self.keymap, self.dpad_keys, self.axes, self.trig = keymap, dpad_keys, axes, trig
        self.buttons = 0
        self.held = set()
        self.dir = {"up": 0, "down": 0, "left": 0, "right": 0}
        self.ax = {"lx": 128, "ly": 128, "rx": 128, "ry": 128}

    def hat(self):
        x = self.dir["right"] - self.dir["left"]
        y = self.dir["down"] - self.dir["up"]
        return {(0, -1): 1, (1, -1): 2, (1, 0): 3, (1, 1): 4,
                (0, 1): 5, (-1, 1): 6, (-1, 0): 7, (-1, -1): 8}.get((x, y), 0)

    def report(self):
        b, a = self.buttons, self.ax
        return bytes([0x01, b & 0xFF, (b >> 8) & 0xFF, self.hat(),
                      a["lx"], a["ly"], a["rx"], a["ry"]])

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
# configfs + f_hid. The oakMOSS/MagicX devices (Zero 28 etc.) use
# /sys/kernel/config/usb_gadget/g1 for USB storage, so the same kernel has
# configfs gadget support; we create our own gadget next to it.
CONFIGFS_ROOT = "/sys/kernel/config"
USB_GADGET_ROOT = CONFIGFS_ROOT + "/usb_gadget"
USB_GADGET_NAME = "btpad"
USB_GADGET_PATH = USB_GADGET_ROOT + "/" + USB_GADGET_NAME
USB_VID = "0x1209"   # pid.codes (open-source vendor id)
USB_PID = "0xb7ad"   # BTPad
USB_HIDG = "/dev/hidg0"


def _write(path, value):
    with open(path, "w") as f:
        f.write(str(value))


def _read(path):
    with open(path) as f:
        return f.read().strip()


def first_udc():
    try:
        names = sorted(os.listdir("/sys/class/udc"))
    except OSError:
        return None
    return names[0] if names else None


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
    for mod in ("libcomposite", "usb_f_hid"):
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


def usb_hid_probe():
    """--check: is configfs + the HID function available? Creates no bound gadget."""
    if not mount_configfs():
        return False, "configfs is not available"
    load_gadget_modules()
    path = USB_GADGET_ROOT + "/btpad-probe"
    if os.path.isdir(path):
        usb_gadget_remove_tree(path)
    try:
        os.mkdir(path)
        os.makedirs(path + "/functions/hid.probe")
    except OSError as e:
        return False, "HID function not available (%s)" % e
    finally:
        usb_gadget_remove_tree(path)
    if not first_udc():
        return False, "HID function available, but no UDC in /sys/class/udc"
    return True, "HID function available (UDC %s)" % first_udc()


def usb_gadget_remove_tree(path):
    _remove_tree(path)


class UsbGadget:
    """USB HID gamepad backend: configfs gadget + /dev/hidg0."""

    def __init__(self, name):
        self.name = name
        self.udc = None
        self.fd = None
        self._connected = False
        self._checked = 0.0
        self._open_warned = False

    def start(self):
        if os.getuid() != 0:
            raise RuntimeError("must run as root to create a USB gadget")
        if not mount_configfs():
            raise RuntimeError("configfs is not available (mount -t configfs none /sys/kernel/config failed)")
        load_gadget_modules()
        self.udc = first_udc()
        if not self.udc:
            raise RuntimeError("no USB device controller (/sys/class/udc is empty)")
        usb_gadget_remove()
        if os.path.isdir(USB_GADGET_PATH):
            try:
                os.rmdir(USB_GADGET_PATH)
            except OSError:
                pass
        if os.path.isdir(USB_GADGET_PATH):
            raise RuntimeError("a stale USB gadget at %s could not be removed; reboot and retry"
                               % USB_GADGET_PATH)
        try:
            os.mkdir(USB_GADGET_PATH)
            os.makedirs(USB_GADGET_PATH + "/functions", exist_ok=True)
            _write(USB_GADGET_PATH + "/idVendor", USB_VID)
            _write(USB_GADGET_PATH + "/idProduct", USB_PID)
            _write(USB_GADGET_PATH + "/bcdUSB", "0x0200")
            _write(USB_GADGET_PATH + "/bcdDevice", "0x0100")
            os.makedirs(USB_GADGET_PATH + "/strings/0x409")
            _write(USB_GADGET_PATH + "/strings/0x409/manufacturer", "SpruceOS")
            _write(USB_GADGET_PATH + "/strings/0x409/product", self.name)
            _write(USB_GADGET_PATH + "/strings/0x409/serialnumber", "SPRUCEBTPAD01")

            try:
                os.mkdir(USB_GADGET_PATH + "/functions/hid.usb0")
            except OSError as e:
                raise RuntimeError("the kernel has no USB HID gadget function "
                                   "(configfs f_hid/usb_f_hid): %s" % e)
            fn = USB_GADGET_PATH + "/functions/hid.usb0"
            _write(fn + "/protocol", "0")
            _write(fn + "/subclass", "0")
            _write(fn + "/report_length", str(REPORT_LENGTH))
            with open(fn + "/report_desc", "wb") as f:
                f.write(REPORT_DESC)

            os.makedirs(USB_GADGET_PATH + "/configs/c.1/strings/0x409")
            _write(USB_GADGET_PATH + "/configs/c.1/strings/0x409/configuration", "HID Gamepad")
            _write(USB_GADGET_PATH + "/configs/c.1/MaxPower", "250")
            os.symlink(fn, USB_GADGET_PATH + "/configs/c.1/hid.usb0")
            _write(USB_GADGET_PATH + "/UDC", self.udc)
        except Exception:
            usb_gadget_remove()
            raise

        if _read(USB_GADGET_PATH + "/UDC") != self.udc:
            usb_gadget_remove()
            raise RuntimeError("could not bind the USB gadget to %s" % self.udc)
        log("USB gadget ready on %s; plug the cable into the tablet/PC" % self.udc)

    def connected(self):
        now = time.time()
        if now - self._checked < 0.25:
            return self._connected
        self._checked = now
        try:
            self._connected = _read("/sys/class/udc/%s/state" % self.udc) == "configured"
        except OSError:
            self._connected = False
        return self._connected

    def _open(self):
        if not os.path.exists(USB_HIDG):
            # Minimal systems without devtmpfs/mdev rules need the node made by hand.
            try:
                dev = _read("/sys/class/hidg/%s/dev" % os.path.basename(USB_HIDG))
                major, minor = (int(x) for x in dev.split(":"))
                os.mknod(USB_HIDG, stat.S_IFCHR | 0o600, os.makedev(major, minor))
            except (OSError, ValueError):
                pass
        try:
            self.fd = os.open(USB_HIDG, os.O_WRONLY | os.O_NONBLOCK)
            self._open_warned = False
            log("USB host connected - pad is live")
        except OSError as e:
            if not self._open_warned:
                log("could not open %s: %s" % (USB_HIDG, e))
                self._open_warned = True

    def _close_fd(self, quiet=False):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
            if not quiet:
                log("USB host disconnected; waiting for reconnect")

    def push(self, report):
        if not self.connected():
            self._close_fd()
            return
        if self.fd is None:
            self._open()
        if self.fd is None:
            return
        try:
            os.write(self.fd, report)
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR):
                self._close_fd()

    def close(self):
        self._close_fd(quiet=True)
        usb_gadget_remove()
        log("USB gadget removed")


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
    print("USB controllers:", ", ".join(sorted(os.listdir("/sys/class/udc")))
          if os.path.isdir("/sys/class/udc") and os.listdir("/sys/class/udc") else "none")
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


def run():
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

    state = State(prof["keys"], prof["dpad_keys"], prof["axes"], prof["trigger_axes"])
    byfd = {p.fd: p for p in pads}
    exit_since = None
    last_push = 0.0

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
                    if changed:
                        push()
                        last_push = time.time()
            if ((bt and bt.intr) or (usb and usb.connected())) and time.time() - last_push > 0.1:
                push()  # keep-alive: re-send the current state so a lost report cannot stick
                last_push = time.time()
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
    elif "--sticks" in sys.argv:
        do_sticks()
    elif "--probe" in sys.argv:
        do_probe("--all" in sys.argv)
    else:
        sys.exit(run())
