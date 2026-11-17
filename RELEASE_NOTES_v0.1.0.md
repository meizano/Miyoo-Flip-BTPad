**BTPad v0.1.0 - use a Miyoo Flip (SpruceOS) as a Bluetooth gamepad**

Install: copy the `BTPad` folder from `BTPad.zip` to `/App/` on the SD card, turn Bluetooth on in Spruce, open **BT Gamepad**, then pair **Miyoo Flip Pad** from the other device. Quit with L1+R1+Select+Start (hold 2s).

Tested on: Miyoo Flip, SpruceOS `<version>`, Android `<tablet model / Android version>`.

Known limits:
- Runs as root, takes over the Flip's buttons while running, and restarts `bluetoothd` without its input plugin (restored on exit; a reboot fixes anything odd).
- Spruce's own Bluetooth menu does not work while BTPad runs.
- No on-screen status (the screen goes black), L2/R2 are digital.
- Not tested on other hosts or Spruce versions - reports welcome.
