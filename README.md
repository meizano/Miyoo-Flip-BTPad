# SpruceOS BTPad - use a SpruceOS handheld as a Bluetooth **or USB-cable** gamepad

BTPad is a SpruceOS app that makes a handheld show up as a standard HID gamepad.
It can talk to the host over Bluetooth or over a USB-C cable, so it also works on
devices without Bluetooth, such as the **MagicX Mini Zero 28**.

This is a fork of [gladiator1014/Miyoo-Flip-BTPad](https://github.com/gladiator1014/Miyoo-Flip-BTPad)
(MIT). The upstream app only did Bluetooth and only knew the Miyoo Flip's controller.
This fork adds:

- a **USB HID gadget backend** (`configfs` + `/dev/hidg0`), so the handheld can be
  plugged straight into an Android tablet, phone or PC;
- a built-in **MagicX Mini Zero 28** mapping (`magicx-input`), including its
  digital L2/R2, `ABS_Z`/`ABS_RZ` right stick and MENU button;
- **automatic mode selection**: Bluetooth when the device has a Bluetooth adapter,
  USB cable when it does not;
- per-device profiles in `mapping.json`, with the original single mapping still
  accepted.

> **AI-assisted project.** The code and documentation added by this fork were written
> by an AI coding assistant working from the maintainer's direction, upstream sources
> and SpruceOS's own device configuration. Read
> [Credits and how this was built](#credits-and-how-this-was-built) before relying on it.

**Status: v0.2.1.** The Bluetooth path is upstream's hardware-tested code, unchanged in
behaviour. The USB path is new: it is built the same way SpruceOS's own USB gadget apps
build theirs (`App/USBStorageMode`), and it passes this project's logic and
simulated-configfs tests, but it has **not yet been confirmed on real hardware**. If it
does not work, `--check` and `btpad.log` say why; the usual cause is a kernel without
`usb_f_hid`. Reports for the Zero 28, Flip and other SpruceOS handhelds are welcome.

## Requirements

- **SpruceOS** on the handheld. Developed against the Miyoo Flip and the oakMOSS-based
  MagicX Mini Zero 28. Other devices work if their pad matches a built-in profile or
  you add one (see [Customising the buttons](#customising-the-buttons)).
- **Root**, to grab the input device and to create the gadget. Spruce apps already run
  as root, so launching from the menu is enough.
- **USB mode:** a kernel with configfs gadget support and the HID function
  (`CONFIG_USB_CONFIGFS_F_HID` / `usb_f_hid`), plus a USB device controller
  (`/sys/class/udc`). `--check` tests both. You also need a data-capable cable and a
  host that can act as USB host (Android device, PC, ...).
- **Bluetooth mode:** a Bluetooth adapter and BlueZ (`bluetoothd`), as on the Flip.

## Install

Download `SpruceOS-BTPad.zip` from the [Releases](../../releases) page, or use the
`BTPad` folder from this repo directly. Copy that folder to `App/` on the SD card, so
you have `App/BTPad/launch.sh`, then open **BT/USB Gamepad** from the Apps menu. The
screen goes black; that is normal.

The app has no icon. If your theme shows a blank one, copy an icon from another app
and fill in `icon` / `iconsel` in `config.json`.

### USB cable (recommended for the Zero 28)

1. Plug a **data-capable** USB-C cable into the handheld and into the tablet/PC.
   The tablet/PC is the USB host; the handheld is the gamepad.
2. Android may show a "USB device connected" notice. Allow it if asked.
3. The host sees a gamepad named **MagicX Zero 28 Pad** (or **Miyoo Flip Pad** on a
   Flip). There is no pairing and no driver to install.
4. Open a gamepad tester app / the game and check the buttons.

On the Mini Zero 28 (no Bluetooth), auto mode selects USB. You can also force it:

```json
{"mode": "usb"}
```

in `BTPad/settings.json`, or run `btpad.py --usb` over SSH.

### Bluetooth (Miyoo Flip and other Bluetooth-capable devices)

1. Turn Bluetooth **on** in Spruce's settings.
2. Open **BT/USB Gamepad**. Auto mode uses Bluetooth when an adapter is present.
3. On the tablet, forget any old "Miyoo Flip Pad" entry, scan, and pair with
   **Miyoo Flip Pad**. Pairing should need no code; if asked for a PIN, use `0000`.
4. Open a gamepad tester app to check the buttons.

`settings.json` can force Bluetooth with `{"mode": "bt"}`, or run `btpad.py --bt`.

## Repository layout

```
BTPad/                  what goes on the SD card
  btpad.py              the whole app (standard library only)
  launch.sh             finds Python and starts btpad.py, logging to btpad.log
  config.json           Spruce app entry: "BT/USB Gamepad"
  settings.json         transport: auto / usb / bt
  mapping.json          per-device button and axis profiles
README.md               this file
CHANGELOG.md            version history
RELEASE_NOTES_*.md      notes for each tagged release
```

## Quit

Hold **L1 + R1 + Select + Start** for 2 seconds. The app disconnects the host
(USB gadget removed, or Bluetooth handed back to Spruce) and returns to the menu.

## Controls

Face buttons are mapped **by position**, so both devices behave like a standard
Xbox-layout pad even though the MagicX labels are Nintendo-style:

| Position | Miyoo Flip label | Mini Zero 28 label | Host reports |
|----------|------------------|--------------------|--------------|
| bottom   | A | B | A |
| right    | B | A | B |
| top      | X | Y | X |
| left     | Y | X | Y |

| Control | Host reports |
|---------|--------------|
| L1 / R1 | L1 / R1 |
| L2 / R2 | L2 / R2 (both devices report these as digital buttons) |
| Select / Start / Menu | Select / Start / Mode |
| D-pad | Hat switch |
| Left / right stick | Left / right stick |
| Stick clicks (L3 / R3) | ThumbL / ThumbR |

The Zero 28's MENU key (`KEY_BACK`, 158) is sent as **Mode**. On some Zero 28
firmware images the A key emits an extra code (353) alongside 305; BTPad ignores
the rider and uses the real A code.

Pressing a stick down flexes it slightly, so a click can also nudge that stick's
axes by a small amount. That comes from the hardware, not from BTPad.

## What it does (and what to know before you run it)

BTPad runs **as root**. Read this if you are cautious:

- Takes exclusive control of the gamepad input device (`MIYOO Player1` on the Flip,
  `magicx-input` on the Zero 28) so Spruce's menu does not react to button presses
  while you play.
- **USB mode** mounts `configfs` if needed, creates a gadget named `btpad`,
  binds it to the device's USB controller (`/sys/class/udc`), and writes reports to
  `/dev/hidg0`. It does **not** touch `bluetoothd`. On quit it unbinds and removes
  the gadget. Only one USB gadget can own the port at a time: exit Spruce's
  **USB Storage Mode** (and, on devices that use it, `adbd`) first.
- **Bluetooth mode restarts `bluetoothd` without its `input` plugin.** BlueZ will
  not let a device present itself as a Bluetooth HID gamepad while that plugin is
  loaded. Spruce's normal Bluetooth menu (pairing speakers, controllers) will not
  work while BTPad is running. BTPad restores the original `bluetoothd` when you
  quit. If anything is left odd (for example after a crash), reboot.
- Registers a Bluetooth HID gamepad service with BlueZ over D-Bus, makes the device
  discoverable, and registers a pairing agent that accepts "just works" pairing.
  While it runs, **any nearby device can pair with it**. Quit it when you are not
  using it.
- Makes no network connections. The only file it writes is its own log
  (`btpad.log`, plus optional `bluetoothd.log` with `--debug`).

Everything is in `btpad.py` (standard library only). Spruce's Python on the Flip has
no `dbus` module and no Bluetooth socket support, so BTPad contains a small D-Bus
client and uses `ctypes` for the Bluetooth sockets. The USB backend uses the kernel's
configfs HID function (`usb_f_hid` / `f_hid`).

## Customising the buttons

`mapping.json` is optional and now supports per-device profiles. The built-in
profiles already cover the Flip and the Zero 28; edit the file only for another
handheld or a remap. To find raw codes, SSH in and run:

```
# Miyoo Flip
/mnt/sdcard/spruce/flip/bin/python3 /mnt/sdcard/App/BTPad/btpad.py --probe

# MagicX / oakMOSS devices
/mnt/sdcard/spruce/bin/python/bin/python3.10 /mnt/sdcard/App/BTPad/btpad.py --probe
```

Then, for example, to change the Zero 28's axes:

```json
{
  "profiles": {
    "magicx-input": {
      "axes": {"0": "lx", "1": "ly", "2": "rx", "5": "ry"}
    }
  }
}
```

Profiles are matched by input device name. A profile can set `devices`, `keys`
(evdev key code -> HID button), `dpad_keys`, `axes` (evdev ABS code -> `lx`/`ly`/
`rx`/`ry`) and `trigger_axes` (ABS code -> HID button). Fields you leave out keep
their built-in values; a field you include replaces the built-in value (use `{}` to
clear it). The old flat `mapping.json` format from BTPad 0.1 is still accepted and
replaces the built-ins, exactly as before.

Other options (run over SSH):

- `--check` shows Python, D-Bus, Bluetooth adapter, **USB HID gadget support**, UDCs,
  input pads, the selected profile and which mode auto would choose.
- `--sticks` shows each stick's declared range, observed range and what BTPad sends.
- `--usb` / `--bt` force a backend; `--nograb` does not grab the buttons.
- `--debug` also writes `bluetoothd.log` in Bluetooth mode.
- `--probe --all` watches every input device (useful for finding a missing button).

Do not run BTPad over SSH and from the menu at the same time. Two copies fight over
the controller and the menu copy exits back to Spruce.

## Troubleshooting

- **USB: nothing appears on the tablet/PC.** Run `--check`. `USB HID gadget` must say
  `OK - HID function available (UDC ...)`. Check `btpad.log` for `USB gadget ready`
  and, after plugging in, `USB host connected - pad is live`. Use a data cable, and
  make sure the tablet is acting as USB host.
- **`the kernel has no USB HID gadget function (configfs f_hid/usb_f_hid)`.** The
  kernel was built without `CONFIG_USB_CONFIGFS_F_HID`, so USB HID cannot work on
  that firmware. Bluetooth mode (on devices with Bluetooth) is unaffected. Report
  it; a kernel/module with `usb_f_hid` is needed.
- **`no USB device controller`.** `/sys/class/udc` is empty: the port or kernel has
  no gadget mode. Spruce's USB Storage Mode uses the same support, so if that works,
  this should too.
- **`could not bind the USB gadget`.** Another gadget owns the port (Spruce USB
  Storage Mode, `adbd`, or a stale gadget). Exit those and retry; a reboot clears
  anything left over.
- **USB works but the pad stops after unplug/replug.** It should reconnect by itself
  within a second. If it does not, check `btpad.log`; `--check` also helps.
- **Tablet cannot see the Bluetooth pad.** Check Bluetooth is on in Spruce first,
  then relaunch. Check `btpad.log`.
- **Pairs but no input over Bluetooth.** Forget the device on the tablet and pair
  again. Reconnecting later usually means tapping the device in the tablet's
  Bluetooth list while BTPad is running.
- **Sticks differ in shape or feel jittery.** Recalibrate the sticks in Spruce's
  settings first. `--sticks` shows the raw ranges BTPad sees.
- **L3 / R3 do not register.** `--probe --all`, click each stick, and add the code
  to the profile's `keys` (buttons 14 and 15).
- **Spruce says Bluetooth is on but nothing can see the device.** Spruce's toggle can
  disagree with the real adapter state. BTPad powers the adapter on itself and logs
  `adapter powered on`; if it logs a WARNING instead, run `bluetoothctl power on`
  over SSH and relaunch.
- **Spruce Bluetooth menu broken after a crash.** Reboot.
- **"no python found" in the log.** `launch.sh` looks for
  `/mnt/sdcard/spruce/flip/bin/python3` (Flip) and
  `/mnt/sdcard/spruce/bin/python/bin/python3.10` (oakMOSS/MagicX). Check which
  exists on your firmware.

## How it works

1. Reads the controller from `/dev/input/event*`.
2. Builds an 8-byte HID report: 16 buttons, a hat, and 4 axes.
3. Bluetooth: registers an SDP HID record with BlueZ, listens on L2CAP PSM 17/19
   for the host's HID control/interrupt channels, and sends reports with the `0xA1`
   DATA prefix.
4. USB: creates a configfs HID gadget with the same report descriptor and writes
   the report to `/dev/hidg0` whenever a host has configured the gadget.
5. Sends a report whenever a button/stick changes, plus a 10 Hz keep-alive so a
   dropped report cannot leave an input stuck.

## Credits and how this was built

- **Upstream:** BTPad by [gladiator1014](https://github.com/gladiator1014/Miyoo-Flip-BTPad)
  (MIT), written with the help of an AI assistant and tested on a Miyoo Flip. Its
  Bluetooth implementation, HID report layout and D-Bus/L2CAP plumbing are kept here.
- **This fork:** the USB HID backend, per-device profiles, MagicX Mini Zero 28 support,
  the launcher changes, this README, the changelog and the release notes were **written
  with AI assistance** — an AI coding assistant produced them from the maintainer's
  direction, the upstream code and SpruceOS's own configuration. The code is not
  hand-written; review it before relying on it for anything sensitive, and expect
  rough edges rather than a polished commercial app.
- **Sources for device facts:** input device names, key/axis codes, the configfs gadget
  recipe and the Python paths come from the SpruceOS repository, mainly
  `spruce/scripts/platform/oakmoss/Zero28.cfg` and `MagicXCommon.sh`, and
  `App/USBStorageMode/usb_gadget.sh`.
- **What has been checked:** Python syntax, unit tests for the button/axis/hat mapping,
  the exit combo, the HID report descriptor and USB report length, `mapping.json`
  format handling (new and legacy), and gadget creation/binding/report/unplug/removal
  against a simulated configfs tree. Every code path was also reviewed by hand.
- **What has not been checked:** real USB HID output on a Mini Zero 28 or any other
  device, real pairing on hosts other than the upstream-tested Android tablet, and
  behaviour on SpruceOS versions other than the ones those sources describe.

MIT licensed, see `LICENSE`. Use at your own risk.
