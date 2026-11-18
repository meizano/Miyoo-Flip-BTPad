# SpruceOS Gamepad Mode - use a SpruceOS handheld as a wired USB gamepad

**Gamepad Mode** is a SpruceOS app: plug the handheld into a tablet, phone or PC
with a USB-C cable and it shows up as a standard HID gamepad named
**Retro Gamepad**. No pairing, no driver, no app on the other end.

On the device itself, the app gives you:

- a menu — **Start Gamepad Mode**, **Test Gamepad**, **Settings**, **Exit**;
- a **test screen** that shows the controller layout with live press feedback
  and the battery percentage, without connecting anything;
- while the pad runs, the same RetroArch-style layout with **live press
  feedback**, the battery percentage and the USB link state;
- **Settings** to remap buttons (press the physical button you want), disable
  buttons, set analog stick sensitivity (50–150 % per axis), reset to defaults
  and save.

Built for and confirmed on the **MagicX Mini Zero 28**; the mapping system
supports other SpruceOS handhelds too (see
[Other handhelds](#other-handhelds)).

> **AI-assisted project.** This app was written by an AI coding assistant —
> **GLM 5.3 Flash (Z.ai)**, driven through OpenCode — working from the
> maintainer's direction and testing on real hardware. It began life as the
> USB mode of a fork of [BTPad](https://github.com/gladiator1014/Miyoo-Flip-BTPad)
> (MIT). See [Credits](#credits) before relying on it.

## Requirements

- **SpruceOS** on the handheld. Developed against the oakMOSS-based MagicX Mini
  Zero 28; other devices work if their pad matches a built-in profile or you
  add one in `mapping.json`.
- **Root** to grab the input device and create the USB gadget. Spruce apps
  already run as root, so launching from the menu is enough.
- A **data-capable USB-C cable** and a host that can act as USB host (Android
  device, PC, ...).
- The UI uses **pysdl2 + SDL_ttf and the fonts Spruce's PyUI ships** — both are
  part of every SpruceOS install; nothing extra is needed.
- Run the app once with **--check** (SSH) if you want it to verify all of this.

## Install

Copy the `GamepadMode` folder to `App/` on the SD card, so you have
`App/GamepadMode/launch.sh`, then open **Gamepad Mode** from the Apps menu.

### Using it

1. **Test Gamepad** first, if you like: press buttons, move and click the
   sticks, watch them light up. Leave with **Start+Select together**.
2. **Start Gamepad Mode** and plug the data cable into the tablet/PC.
   The layout screen shows **USB host connected** once the host is using the
   pad (and a red **no USB host** hint if the cable is missing, charge-only,
   or in a charging-only port).
3. In a game or a gamepad tester, the pad reports as **Retro Gamepad**.
4. Quit by holding **L1 + R1 + Select + Start** for 2 seconds — the app exits
   back to Spruce and the firmware's gadget is restored.

The layout screen redraws for what actually happens: a button lights while
held, a stick's knob follows its axis and its ring lights on stick click.

### Settings

- **Remap Buttons** — choose a logical button, then press the physical button
  you want for it. The capture commits on release, so *every* button including
  B can be remapped. **Start+Select together** cancels a capture.
- **Disable Buttons** — toggle any button off entirely.
- **Analog Sensitivity** — 50–150 % per stick axis, scaled around the centre of
  the report. 100 % is the stick's full reported travel.
- **Reset to Defaults** undoes everything (unsaved until you use **Save
  Configuration**); **OK** returns to the menu.

Settings live in `settings_ui.json` inside the app folder and are applied every
time the pad starts.

## Other handhelds

Built-in profiles cover the **MagicX Mini Zero 28** (`magicx-input`) and the
**Miyoo Flip** (`MIYOO Player1`). For anything else, find the raw codes over
SSH:

```
# list input devices (the gamepad one is marked)
GamepadMode/gamepadmode.py --devices

# watch live events: which code does each button/stick produce?
GamepadMode/gamepadmode.py --probe          # pads only
GamepadMode/gamepadmode.py --probe --all    # every input device
```

then add a profile in `mapping.json` (see the annotated example in that file).
`--check` reports the Python, root, configfs, USB controller and HID-function
state, which is the first thing to look at when something does not work.

## How it works

1. Reads the controller from `/dev/input/event*` (grabbed, so Spruce does not
   react to the buttons while the pad runs).
2. Builds an 8-byte HID report: 16 buttons, a hat, and 4 axes, with the user's
   remap/disable/sensitivity settings applied.
3. Creates a USB HID function:
   - configfs **f_hid** when the kernel has `CONFIG_USB_CONFIGFS_F_HID`;
   - otherwise **FunctionFS**: the HID descriptors and control requests are
     handled from userspace and reports go to the interrupt IN endpoint. The
     function is added to the firmware's own gadget (ADB) because vendor
     kernels allow only one gadget. While the pad runs, that gadget presents
     our own identity — manufacturer **SpruceOS**, product **Retro Gamepad**,
     VID/PID **1209:b7ad** — and everything is restored to the firmware's
     values ("Tina ADB", 18d1:d002) on exit. Changing the VID/PID matters:
     hosts such as Android cache device names by VID/PID and would otherwise
     keep calling the pad "Tina ADB".
4. The firmware's ADB setup re-claims the USB controller asynchronously
   (the kernel's `android_work`), so the app prepares everything *before*
   unbinding and retries the bind, un-binding whatever raced it, until it wins.
5. Sends a report whenever a button/stick changes, plus a 10 Hz keep-alive so
   a dropped report cannot leave an input stuck.

## Troubleshooting

- **Nothing appears on the tablet/PC.** Check the layout screen: it says
  **no USB host** unless the cable is delivering data — use the data port and a
  data-capable cable. `--check` verifies the rest (configfs, UDC, HID
  function); `gamepadmode.log` in the app folder has the details.
- **The host shows "Tina ADB" instead of "Retro Gamepad".** This is expected
  *before* you start the pad: with Gamepad Mode idle (or after exiting) the
  handheld is the firmware's ADB gadget. Only **Start Gamepad Mode** switches
  it to `1209:b7ad` "Retro Gamepad"; the log must show
  `USB: product now 'Retro Gamepad' (was 'Tina ADB')` and the VID/PID lines.
  Check the host's device list *after* the layout screen turns green. The ADB
  interface staying next to the gamepad is expected on the Zero 28 (one shared
  gadget) and does not affect gamepad input.
- **The screen goes black and returns to the menu.** Check `gamepadmode.log`.
  The UI needs pysdl2 + SDL_ttf from PyUI (present on every SpruceOS install)
  and a usable TTF font in PyUI's fonts folder.
- **Android/Samsung shows "Analog USB device not supported"** ("Perangkat USB
  analog tidak didukung") and the layout stays red **no USB host**: this is
  the phone's USB-C port mistaking the cable for a passive analog-headset
  adapter — it happens before USB enumeration, so it is not a gamepad/HID
  problem (the same cable works on a PC). Fixes, in order: flip the USB-C
  plug at the phone end (many handhelds terminate only one CC pin, so
  orientation matters); try a short, simple **USB 2.0** C-to-C data cable
  (not an e-marked/Thunderbolt or charge-only cable); or use a real **USB-C
  OTG adapter** on the phone (USB-C plug to USB-A socket) with a normal
  USB-A→USB-C cable to the handheld — the adapter's resistor forces the
  phone into host mode reliably. Some Samsung models also need **Settings →
  Connections → More connection settings → USB OTG** enabled, and lint or
  moisture in the phone's port can produce the same toast. **Confirmed
  working:** Samsung Galaxy S25 Edge and Lenovo Y700 (SD8 Elite) both reject
  a direct C-to-C cable but work with a **USB-C OTG adapter + USB-A→USB-C
  cable** into the Zero 28's data port. (A PC works with either a C-to-C or
  a USB-A→USB-C cable.) To isolate the phone: try a known OTG device (flash drive/mouse) with the same adapter — if that fails
  too, it is the phone/cable, not Gamepad Mode.
- **The pad was working and stopped.** If the log shows the app fighting
  another copy, the launcher already kills stale instances — just relaunch.
- **Handheld hard-resets under load / SD card goes read-only.** This is
  power-related on the Zero 28 (seen with a low battery and no charger).
  Charge the device (a dedicated charger is best) and, if the card flips
  read-only after such a reset, `mount -o remount,rw /mnt/SDCARD`.

## Repository layout

```
GamepadMode/            what goes on the SD card (as App/GamepadMode)
  gamepadmode.py        the whole app: menu, settings, live layout and the
                        USB HID gadget (standard library + pysdl2)
  launch.sh             finds Python, sets up SDL2, single-instance, logs to
                        gamepadmode.log
  config.json           Spruce app entry: "Gamepad Mode"
  mapping.json          optional per-device button and axis profiles
README.md               this file
CHANGELOG.md            version history
LICENSE                 MIT (upstream BTPad + this project)
```

## Credits

- **BTPad** by [gladiator1014](https://github.com/gladiator1014/Miyoo-Flip-BTPad)
  (MIT), written with the help of an AI assistant and tested on a Miyoo Flip.
  Gamepad Mode began as that project's USB fork; its HID report layout,
  configfs approach and evdev mapping design are the foundation of this app,
  and its Bluetooth mode (which this app does not have) lives on in BTPad
  itself.
- **AI assistance:** the standalone rewrite, the FunctionFS backend, the Zero
  28 support and all documentation were **written by GLM 5.3 Flash (Z.ai)**,
  an AI coding assistant used through OpenCode, working from the maintainer's
  direction and from device facts found in the SpruceOS repository
  (`spruce/scripts/platform/oakmoss/`, `App/USBStorageMode/usb_gadget.sh`).
  The code is not hand-written; review it before relying on it for anything
  sensitive.
- **What has been confirmed on hardware:** the FunctionFS USB path end-to-end
  on a MagicX Mini Zero 28 (Windows: composite device with ADB + HID gamepad,
  enumerated as "Retro Gamepad" in `joy.cpl`; all buttons, D-pad, sticks and
  stick clicks verified) and the full UI: menus, remap, disable, sensitivity,
  test mode, exit combo.
- **What has not been checked:** other handhelds, other SpruceOS versions, and
  Android hosts other than the one BTPad was tested with.

MIT licensed, see `LICENSE`. Use at your own risk.
