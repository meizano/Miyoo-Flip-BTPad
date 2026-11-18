# SpruceOS BTPad - use a SpruceOS handheld as a Bluetooth **or USB-cable** gamepad

BTPad is a SpruceOS app that makes a handheld show up as a standard HID gamepad.
It can talk to the host over Bluetooth or over a USB-C cable, so it also works on
devices without Bluetooth, such as the **MagicX Mini Zero 28**.

This is a fork of [gladiator1014/Miyoo-Flip-BTPad](https://github.com/gladiator1014/Miyoo-Flip-BTPad)
(MIT). The upstream app only did Bluetooth and only knew the Miyoo Flip's controller.
This fork adds:

- the **Gamepad Mode app**: an on-device menu to start the pad, test the controls
  without connecting, remap or disable buttons, set analog stick sensitivity, reset
  and save; while the pad runs it shows a controller layout with live press feedback
  and the battery percentage;
- a **USB HID gadget backend** that works even on kernels without the configfs HID
  function (such as the Zero 28's), by driving a FunctionFS HID function in
  userspace, so the handheld can be plugged straight into an Android tablet, phone
  or PC;
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

**Status: v0.3.0.** The Bluetooth path is upstream's hardware-tested code, unchanged in
behaviour. The USB path has been **confirmed on real hardware**: a MagicX Mini Zero 28
running this fork enumerates on Windows as a gamepad named **Retro Gamepad** (in
`joy.cpl`), with all buttons, the D-pad, both sticks and stick clicks working. Because
the Zero 28's kernel ships without `CONFIG_USB_CONFIGFS_F_HID`, the USB backend
normally runs through FunctionFS instead (see [How it works](#how-it-works)); kernels
that do have `f_hid` still use the simpler configfs path. `--check` and `btpad.log`
say which one was chosen.

## Requirements

- **SpruceOS** on the handheld. Developed against the Miyoo Flip and the oakMOSS-based
  MagicX Mini Zero 28. Other devices work if their pad matches a built-in profile or
  you add one (see [Customising the buttons](#customising-the-buttons)).
- **Root**, to grab the input device and to create the gadget. Spruce apps already run
  as root, so launching from the menu is enough.
- **USB mode:** a kernel with configfs gadget support and a USB device controller
  (`/sys/class/udc`). The HID function can come from the kernel's `usb_f_hid`, or —
  when that is missing — from FunctionFS, which this fork drives from userspace.
  `--check` tests what is available. You also need a data-capable cable and a host
  that can act as USB host (Android device, PC, ...).
- **Bluetooth mode:** a Bluetooth adapter and BlueZ (`bluetoothd`), as on the Flip.

## Install

Download `SpruceOS-BTPad.zip` from the [Releases](../../releases) page, or use the
`BTPad` folder from this repo directly. Copy that folder to `App/` on the SD card —
`App/GamepadMode` is a good name — so you have `App/GamepadMode/launch.sh`, then open
**Gamepad Mode** from the Apps menu.

### The Gamepad Mode app

- **Start Gamepad Mode** runs the pad. The screen shows a controller layout with
  live press feedback and the battery percentage; quit with L1+R1+Select+Start
  (held 2 s), which returns you to Spruce.
- **Test Gamepad** shows the same live layout without starting USB or Bluetooth —
  useful for checking a remap before connecting. Leave with Start+Select together.
- **Settings** opens:
  - **Remap Buttons** — pick a button, then press the physical button you want for
    it (commit on release). Press **Start+Select together** to cancel. Every button,
    including B, can be remapped.
  - **Disable Buttons** — toggle any button off entirely.
  - **Analog Sensitivity** — 50–150 % per stick axis, applied around the centre.
  - **Reset to Defaults**, **Save Configuration**, **OK**.
- Settings are stored in `settings_ui.json` inside the app folder and are applied
  every time the pad starts.

The app has no icon. If your theme shows a blank one, copy an icon from another app
and fill in `icon` / `iconsel` in `config.json`.

### USB cable (recommended for the Zero 28)

1. Plug a **data-capable** USB-C cable into the handheld and into the tablet/PC.
   The tablet/PC is the USB host; the handheld is the gamepad.
2. Android may show a "USB device connected" notice. Allow it if asked.
3. The host sees a gamepad named **Retro Gamepad**. There is no pairing and no
   driver to install. (The firmware's ADB interface may still be listed next to
   it; they share the one USB gadget.)
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
BTPad/                  what goes on the SD card (as App/GamepadMode)
  btpad.py              the pad itself (standard library only)
  gamepadmode.py        the Gamepad Mode UI (menu, settings, live layout)
  launch.sh             finds Python and starts gamepadmode.py, logging to
                        gamepadmode.log
  config.json           Spruce app entry: "Gamepad Mode"
  settings.json         transport: auto / usb / bt
  mapping.json          per-device button and axis profiles
README.md               this file
CHANGELOG.md            version history
RELEASE_NOTES_*.md      notes for each tagged release
```

## Quit

Hold **L1 + R1 + Select + Start** for 2 seconds. The pad disconnects the host
(USB gadget released, or Bluetooth handed back to Spruce) and the app exits back
to Spruce's menu.

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
- **USB mode** mounts `configfs` if needed, prepares a HID function — the kernel's
  `f_hid` when present, otherwise a FunctionFS HID function served from userspace —
  and adds it to the firmware's own gadget so both ADB and the gamepad share the
  port. While it runs, the gadget's product string is **Retro Gamepad**; the
  original name is restored on quit. It does **not** touch `bluetoothd`. Only one
  USB gadget can own the port at a time: exit Spruce's **USB Storage Mode** (and,
  on devices that use it, `adbd`) first.
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

Everything is in `btpad.py` and `gamepadmode.py` (standard library only, plus
pysdl2 for the UI, which Spruce's PyUI already ships). Spruce's Python on the Flip
has no `dbus` module and no Bluetooth socket support, so BTPad contains a small
D-Bus client and uses `ctypes` for the Bluetooth sockets.

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

- **USB: nothing appears on the tablet/PC.** Run `--check`. It reports whether the
  HID function is available and which backend (configfs or FunctionFS) will be used.
  Check `gamepadmode.log` (or `btpad.log` over SSH) for `USB HID gadget ... ready`
  and, after plugging in, `first report accepted by the host`. Use a data cable,
  and make sure the tablet is acting as USB host.
- **USB: the pad shows up as "Tina ADB" or an ADB interface only.** That is the
  firmware's own gadget name before BTPad rewrites the product string; make sure
  the log says `product string now 'Retro Gamepad'` and that the pad is actually
  running. The ADB interface staying present next to the gamepad is expected on
  the Zero 28 (they share one gadget).
- **`could not bind the USB gadget`.** Another gadget owns the port (Spruce USB
  Storage Mode, `adbd`, or a stale gadget). Exit those and retry; a reboot clears
  anything left over. BTPad retries the bind automatically because the firmware's
  ADB daemon sometimes grabs the controller back mid-bind.
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
- **Gamepad Mode crashes back to the menu.** Check `gamepadmode.log` in the app
  folder. The UI needs pysdl2 + SDL_ttf from Spruce's PyUI (`/mnt/SDCARD/App/PyUI`);
  on the Zero 28 the panel is 640x480 and the UI is drawn for that.
- **Handheld hard-resets under load / SD card goes read-only.** This fork saw
  power-related resets with a low battery while the USB port was not powering the
  device. Charge from a dedicated charger, keep the battery above ~30 % for long
  sessions, and `mount -o remount,rw /mnt/SDCARD` if the card flips read-only.

## How it works

1. Reads the controller from `/dev/input/event*`.
2. Builds an 8-byte HID report: 16 buttons, a hat, and 4 axes.
3. Bluetooth: registers an SDP HID record with BlueZ, listens on L2CAP PSM 17/19
   for the host's HID control/interrupt channels, and sends reports with the `0xA1`
   DATA prefix.
4. USB, configfs path (kernels with `f_hid`): creates a configfs HID gadget with the
   same report descriptor and writes the report to its `hidg` node whenever a host
   has configured the gadget.
5. USB, FunctionFS path (kernels without `f_hid`, e.g. the Zero 28): the firmware's
   own gadget `g1` is briefly unbound, a FunctionFS HID function is prepared and
   linked into its configuration, and the gadget is bound again. BTPad then serves
   the HID descriptors and reports from userspace over `/dev/usb-ffs/btpad/ep1`,
   next to the firmware's ADB function, which keeps working. The vendor ADB setup
   re-claims the controller asynchronously (the kernel's `android_work`), so BTPad
   prepares everything *before* unbinding and retries the bind, un-binding whatever
   raced it, until it wins.
6. Sends a report whenever a button/stick changes, plus a 10 Hz keep-alive so a
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
- **What has been confirmed on hardware:** the FunctionFS USB path end-to-end on a
  MagicX Mini Zero 28 (Windows 10/11: composite device with ADB + HID gamepad,
  enumerated as "Retro Gamepad" in `joy.cpl`; all buttons, D-pad, sticks and stick
  clicks verified), and the Gamepad Mode UI on the device (menus, remap, disable,
  sensitivity, test mode, exit combo).
- **What has not been checked:** real pairing over Bluetooth on hosts other than the
  upstream-tested Android tablet, other SpruceOS versions, and other handhelds.

MIT licensed, see `LICENSE`. Use at your own risk.
