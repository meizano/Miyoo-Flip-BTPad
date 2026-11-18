**SpruceOS BTPad v0.3.0 - the Gamepad Mode release**

Install: download `SpruceOS-BTPad.zip`, copy the `BTPad` folder to `App/` on the SD
card (`App/GamepadMode` is a good name), open **Gamepad Mode**.

What is new:
- The app no longer starts the pad blindly. You get a menu: **Start Gamepad Mode**,
  **Test Gamepad**, **Settings**, **Exit**.
- **Test Gamepad**: the live controller layout without connecting anything — check
  your mapping first. Exit with Start+Select together.
- While the pad runs you see a controller layout with **live press feedback** and
  the **battery percentage**. Quit with L1+R1+Select+Start (2 s); the app now exits
  all the way back to Spruce.
- **Settings**: remap buttons (press the physical button you want, release to set;
  every button including B can be remapped; Start+Select together cancels), disable
  buttons, analog stick sensitivity 50–150 % per axis, reset to defaults, save.
- USB on the MagicX Mini Zero 28 **works on real hardware now**: the kernel there
  has no configfs HID function, so the pad is served through FunctionFS and added
  to the firmware's own gadget next to ADB. Windows enumerates a gamepad named
  **Retro Gamepad** (`joy.cpl`), buttons, D-pad, sticks and stick clicks all working.

Under the hood: the gadget bind race against the firmware's ADB workqueue is fixed
(prepare before unbind + bind retry), the product string is swapped while the pad
runs and restored on exit, and `btpad.run()` takes remap/disable/sensitivity
overrides so the UI and the pad share one state.

Note: the Zero 28 can hard-reset under load when the battery is low and the USB port
is not powering the device — charge it (a dedicated charger is best) and, if the SD
card ever flips read-only after such a reset, `mount -o remount,rw /mnt/SDCARD`.

Still true from v0.2.x: automatic Bluetooth/USB selection, built-in Miyoo Flip and
MagicX Mini Zero 28 profiles, per-device `mapping.json`, and `--check` diagnostics.
