# Changelog — SpruceOS Gamepad Mode

## 1.0.3
- The framebuffer fallback for unsupported devices now pre-rotates its text
  **180 degrees** for panels (like the Miyoo Mini Flip) whose `/dev/fb0` is
  mapped upside down. Set `GAMEPADMODE_FB_ROTATE=0` if a device's framebuffer
  is already upright.

## 1.0.2
- **Unsupported devices now explain themselves at launch.** Before opening
  the UI the app checks for a gamepad input device and a USB device
  controller; if either is missing it shows an on-screen "Gamepad Mode -
  unsupported device" message listing what is missing and why (and that
  Bluetooth mode lives in BTPad), then returns to Spruce. The message is
  drawn with SDL when available and falls back to a tiny built-in 5x7 font
  written straight to `/dev/fb0` on devices where SDL cannot initialize.
- **Launcher portability:** validates each Python candidate by actually
  running it (some armv7 images ship an aarch64 binary at the "flip" path),
  detects 32- vs 64-bit and sets the matching library paths, and picks the
  first SDL2 directory that really loads. This keeps one SD card usable in
  both an aarch64 Zero 28 and an armv7 Miyoo Mini Flip.
- The single-instance lock moved from `/tmp` into the app folder; with the
  exception of runtime USB gadget/evdev state, everything the app creates
  stays inside `GamepadMode/`.
- **Unsupported hardware:** the Miyoo Mini Flip (armv7) has no USB device
  controller, no Bluetooth adapter and no gamepad evdev node, so it can only
  show the message above — Gamepad Mode needs a wired USB data/OTG port.
  See the README for confirmed working hosts.

## 1.0.1
- The FunctionFS backend now changes the borrowed gadget's **manufacturer,
  product string, VID and PID** to `SpruceOS` / `Retro Gamepad` / `1209:b7ad`
  while the pad runs (restoring the firmware's `Tina ADB` / `18d1:d002`
  identity on exit). Hosts like Android cache device names by VID/PID and
  could keep showing "Tina ADB" when only the product string changed.
- README: document that the handheld is the firmware ADB gadget while Gamepad
  Mode is idle, and when the name changes; record confirmed host/cable
  combinations (PC C-to-C and A-to-C; Samsung S25 Edge and Lenovo Y700 with a
  USB-C OTG adapter + USB-A→USB-C cable; direct C-to-C rejected by both
  Android hosts).

## 1.0.0
- **Independent app.** Gamepad Mode no longer shares code with BTPad:
  `GamepadMode/gamepadmode.py` is self-contained (the USB HID gadget, the
  evdev mapping and the pad loop live in the app). It is a wired USB-cable
  gamepad; Bluetooth stays in
  [BTPad](https://github.com/gladiator1014/Miyoo-Flip-BTPad), which this
  project grew out of — see the Credits section of the README.
- The repository is renamed to **SpruceOS Gamepad Mode**; the app directory is
  `GamepadMode/` and the release zip is `SpruceOS-Gamepad-Mode.zip`.
- Everything from the BTPad-fork releases is here:
  - USB HID gamepad over the USB-C cable: configfs `f_hid` when the kernel has
    it, otherwise a **FunctionFS** HID function served from userspace and added
    to the firmware's own gadget next to ADB (vendor kernels allow only one
    gadget, `android0`).
  - The gadget bind race against the firmware's ADB workqueue (`android_work`)
    is handled: everything is prepared before the unbind and the bind retries,
    taking the controller back until it wins.
  - The host sees the pad as **Retro Gamepad**; the original product string is
    restored on exit.
  - The **Gamepad Mode menu**: Start Gamepad Mode, Test Gamepad (live layout
    without connecting; exit with Start+Select), Settings, Exit.
  - Settings (persisted in `settings_ui.json`, applied on every start): remap
    buttons (press + release captures; Start+Select cancels), disable buttons,
    analog stick sensitivity 50–150 % per axis, reset to defaults, save.
  - The live RetroArch-style controller layout with press feedback, stick
    movement, battery percentage and USB link state; quit with
    L1+R1+Select+Start (2 s) exits the app back to Spruce.
- Clean-install hardening: the renderer draws in a fixed 640x480 logical space
  scaled to whatever panel the device has, fonts are discovered from PyUI's
  fonts folder instead of one hard-coded file name, the launcher tries every
  known Spruce Python and SDL2 location, and a single-instance lock plus a
  stale-instance kill in `launch.sh` prevent two copies from fighting over the
  screen and the USB gadget.
- Diagnostics: `--check` (python/root/input devices/configfs/UDC/HID function),
  `--devices`, `--probe [--all]`.

## History (as a BTPad fork)
- **0.1.0** — presented the Miyoo Flip as a Bluetooth HID gamepad (upstream
  BTPad).
- **0.2.0–0.2.1** — fork added USB cable mode, the MagicX Mini Zero 28 profile,
  per-device `mapping.json`, and documentation.
- **0.3.0** — the Gamepad Mode UI and the FunctionFS backend, still running on
  top of the imported `btpad` module.
