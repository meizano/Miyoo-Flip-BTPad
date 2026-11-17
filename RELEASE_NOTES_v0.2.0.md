**SpruceOS BTPad v0.2.0 - Bluetooth and USB-cable gamepad**

Install: copy the `BTPad` folder from `SpruceOS-BTPad.zip` to `/App/` on the SD card,
then open **BT/USB Gamepad**. Quit with L1+R1+Select+Start (hold 2s).

New in this fork:
- **USB cable mode.** Plug the handheld into an Android tablet/phone or PC with a
  data-capable USB-C cable; it appears as a standard HID gamepad, no pairing. This is
  what makes the **MagicX Mini Zero 28** (no Bluetooth) work.
- Auto mode picks Bluetooth when the device has an adapter and USB when it does not.
  Force it with `settings.json` (`{"mode": "usb"}` or `"bt"`) or `--usb` / `--bt`.
- Built-in Mini Zero 28 mapping: positional face buttons, digital L2/R2, right stick on
  ABS_Z/ABS_RZ, MENU as Mode.
- Per-device profiles in `mapping.json`; the old flat file still works.
- `--check` now reports whether the kernel supports the USB HID gadget, which input pad
  was found, which profile was selected, and which mode auto would use.

Known limits:
- Runs as root and grabs the handheld's buttons while running.
- USB mode needs a kernel with configfs gadget support and `usb_f_hid`; only one gadget
  can own the port, so exit USB Storage Mode / adbd first.
- Bluetooth mode restarts `bluetoothd` without its input plugin (restored on exit; a
  reboot fixes anything odd), so Spruce's Bluetooth menu does not work while it runs.
- No on-screen status (the screen goes black). L2/R2 are digital on both supported pads.
- USB mode is new; reports from other SpruceOS devices are welcome.
