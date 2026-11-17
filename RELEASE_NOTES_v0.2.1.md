**SpruceOS BTPad v0.2.1 - documentation release**

No code changes. Same app as v0.2.0.

Install: download `SpruceOS-BTPad.zip`, copy the `BTPad` folder to `/App/` on the SD
card, open **BT/USB Gamepad**. Quit with L1+R1+Select+Start (hold 2s).

What changed:
- The README now says plainly that this fork was **written with AI assistance**, and
  lists what that means for anyone trusting it.
- New **Requirements** section: SpruceOS, root, configfs + `usb_f_hid` and a UDC for
  USB mode, BlueZ for Bluetooth mode.
- New **repository layout** section, and install instructions that point at the release
  zip.
- The README now separates what has actually been verified (mapping/report logic,
  gadget lifecycle against a simulated configfs tree, full code review) from what has
  not (real USB HID output on a Mini Zero 28, other hosts, other SpruceOS versions).
- Credits rewritten: upstream BTPad, the SpruceOS files the device facts come from,
  and the AI assistant's role in this fork.

Still true from v0.2.0: USB cable mode with automatic Bluetooth/USB selection, built-in
Miyoo Flip and MagicX Mini Zero 28 profiles, per-device `mapping.json`, and USB
diagnostics in `--check`.
