# Changelog

## 0.2.0
- Fork of [Miyoo-Flip-BTPad](https://github.com/gladiator1014/Miyoo-Flip-BTPad) with cable support.
- New **USB HID gadget** backend (configfs + `f_hid` + `/dev/hidg0`), so the handheld can be
  plugged into an Android tablet, phone or PC with a USB-C cable. No pairing needed.
- Automatic mode selection: Bluetooth when the device has an adapter, USB when it does not
  (the MagicX Mini Zero 28 has no Bluetooth). `settings.json` (`mode`), `--usb` and `--bt`
  force a backend.
- Built-in **MagicX Mini Zero 28** profile (`magicx-input`): digital L2/R2 (312/313), right
  stick on `ABS_Z`/`ABS_RZ` (2/5), MENU (KEY_BACK 158) -> Mode, and the same positional
  face-button mapping as the Flip.
- `mapping.json` now supports per-device profiles; the old flat format still works.
- `--check` reports USB gadget support (configfs + `usb_f_hid` + UDC), the selected profile
  and the mode auto would choose.
- USB hot-plug handling: opens `/dev/hidg0` only while the host has configured the gadget,
  closes it on unplug and reopens on reconnect. Creates the `/dev/hidg0` node itself if the
  firmware's mdev does not.
- `launch.sh` finds the oakMOSS/MagicX Python (`spruce/bin/python/bin/python3.10`) as well as
  the Flip Python; it forwards extra arguments (`launch.sh --usb`).

## 0.1.0
- First public release.
- Presents the Miyoo Flip as a Bluetooth HID gamepad (16 buttons, hat, 4 axes).
- Built-in D-Bus client and ctypes L2CAP sockets (Spruce's Python has neither dbus nor Bluetooth sockets).
- Maps both sticks, stick clicks (L3/R3), D-pad and L2/R2.
- Built-in pairing agent ("just works", PIN `0000` fallback).
- Powers the Bluetooth adapter on itself (Spruce's toggle can disagree with the real state).
- Periodic report refresh so a dropped report cannot leave an input stuck.
- Diagnostics: `--check`, `--probe`, `--sticks`, `--nograb`, `--debug`.
