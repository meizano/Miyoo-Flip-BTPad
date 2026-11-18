# Changelog

## 0.3.0
- New **Gamepad Mode app** (`gamepadmode.py`, pysdl2 + SDL_ttf from PyUI): instead of
  starting the pad blindly, the app opens a menu — **Start Gamepad Mode**, **Test
  Gamepad**, **Settings**, **Exit**.
- **Test Gamepad** shows the live controller layout (buttons, D-pad, sticks, battery)
  without starting USB or Bluetooth. Exit with Start+Select together.
- While the pad runs, the screen shows a **RetroArch-style controller layout with
  live press feedback** and the battery percentage (capacity + charging state).
- **Settings** (stored in `settings_ui.json`, applied on every start):
  - **Remap Buttons** — choose a logical button, press the physical one you want;
    the capture commits on release so *every* button, including B, can be remapped.
    Press Start+Select together to cancel a capture.
  - **Disable Buttons** — toggle any logical button off entirely.
  - **Analog Sensitivity** — 50–150 % per stick axis, scaled around the centre in
    the HID report.
  - **Reset to Defaults**, **Save Configuration** and **OK**.
- `btpad.run()` gained `frame_cb` (UI redraw hook) and `overrides` (remap/disable/
  sensitivity applied to the chosen profile before the pad starts).
- **USB: FunctionFS HID backend** for kernels without `CONFIG_USB_CONFIGFS_F_HID`
  (the Zero 28's kernel). A HID function is served from userspace over
  `/dev/usb-ffs/btpad` and added to the firmware's own gadget next to ADB, so no
  second gadget is needed (the vendor kernel only supports the one gadget,
  `android0`).
- **USB: bind race fixed.** The firmware's ADB workqueue (`android_work`) re-binds
  the gadget asynchronously and could steal the controller between BTPad's unbind
  and bind (`EBUSY`). Everything is now prepared *before* the unbind, and the bind
  retries, un-binding whatever raced it, until it wins.
- **USB: the host now sees the pad as "Retro Gamepad"** — the gadget's product
  string is replaced while the pad runs and restored on exit.
- The exit combo (L1+R1+Select+Start, held 2 s) now quits the app back to Spruce
  instead of returning to its menu.
- `launch.sh` sets up PyUI's SDL2 environment on oakMOSS devices; `config.json` is
  now labelled **Gamepad Mode**.

## 0.2.1
- Documentation release, no code changes.
- README now states plainly that this fork was written with AI assistance, what was
  verified (logic and simulated-configfs tests) and what was not (real USB HID output,
  hosts other than the upstream-tested tablet).
- README gained a Requirements section, a repository layout, install-from-release
  instructions and a clearer statement of the USB mode's status.
- Credits expanded: upstream authors, the SpruceOS files the device facts come from,
  and the AI assistant's role.

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
