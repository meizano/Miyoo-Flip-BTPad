# Miyoo-Flip-BTPad
Turn your Miyoo Flip into a bluetooth controller for Android 
Made entirely with Claude AI and tested on my device


# BTPad - use your Miyoo Flip as a Bluetooth gamepad

BTPad is a SpruceOS app that makes the Miyoo Flip show up as a standard Bluetooth
gamepad. Pair it with an Android tablet or phone (or anything that accepts a Bluetooth
controller) and play with the Flip's buttons, D-pad, sticks and triggers.

**Status: early (v0.1.0).** It works on the setup it was written on (below), but it is
lightly tested. Please report what happens on yours.

- Tested on: Miyoo Flip, SpruceOS `4.2 & 4.6`, Android tablet `Samsung Tab 7, Android 13`
- Not tested: other Spruce versions, other host devices (PC, iOS, Switch, other Android versions)

## Install

Download `BTPad.zip` from the [Releases](../../releases) page (or copy the `BTPad` folder from this repo).

1. Turn Bluetooth **on** in Spruce's settings.
2. Copy the `BTPad` folder to `App/` on the SD card (so you have `App/BTPad/launch.sh`).
3. Open **BT Gamepad** from the Apps menu. The screen goes black; that is normal.
4. On the tablet, forget any old "Miyoo Flip Pad" entries, scan, and pair with **Miyoo Flip Pad**.
   Pairing should need no code. If the tablet asks for a PIN, use `0000`.
5. Open a gamepad tester app on the tablet to check your buttons.

The app has no icon. If your theme shows a blank one, copy an icon from another app
and fill in `icon` / `iconsel` in `config.json`.

## Quit

Hold **L1 + R1 + Select + Start** for 2 seconds. The app disconnects, hands Bluetooth back
to Spruce, and returns to the menu.

## Controls

| Flip | Android reports |
|------|-----------------|
| Face buttons | A / B / X / Y **by position** (bottom = A, right = B, top = X, left = Y) |
| L1 / R1 | L1 / R1 |
| L2 / R2 | L2 / R2 (the Flip reports them as 0/255 axes, so they are digital) |
| Select / Start / Menu | Select / Start / Mode |
| D-pad | Hat switch |
| Left / right stick | Left / right stick |
| Stick clicks (L3 / R3) | ThumbL / ThumbR |

Pressing a stick down flexes it slightly, so a click can also nudge that stick's axes by a small amount. That comes from the hardware, not from BTPad.

## What it does (and what to know before you run it)

BTPad runs **as root** and does these things. Read them if you are cautious:

- Takes exclusive control of the `MIYOO Player1` input device so Spruce's menu does not react
  to your button presses while you play.
- **Restarts `bluetoothd` without its `input` plugin.** BlueZ will not let a device present
  itself as a Bluetooth HID gamepad while that plugin is loaded. Spruce's normal Bluetooth
  menu (pairing speakers, controllers) will not work while BTPad is running. BTPad restores
  the original `bluetoothd` when you quit. If anything is left odd (for example after a crash),
  rebooting the Flip puts everything back.
- Registers a Bluetooth HID gamepad service with BlueZ over D-Bus, makes the Flip discoverable,
  and registers a pairing agent that accepts "just works" pairing. While it runs, **any nearby
  device can pair with it**. Quit it when you are not using it.
- Makes no network connections and writes nothing outside its own folder (log file `btpad.log`).

Everything is in `btpad.py` (about 900 lines, standard library only). Spruce's Python has no
`dbus` module and no Bluetooth socket support, so BTPad contains a small D-Bus client and uses
`ctypes` for the Bluetooth sockets.

## Customising the buttons

Edit `mapping.json` (button codes are Linux evdev codes). To find the codes for your device,
SSH in and run:

```
/mnt/sdcard/spruce/flip/bin/python3 /mnt/sdcard/App/BTPad/btpad.py --probe
```

Other options (run from SSH):

- `--check` shows whether Python, D-Bus and Bluetooth sockets work and which controller it found.
- `--sticks` shows each stick's declared range, observed range and what BTPad sends (for stick shape or drift problems).
- `--nograb` does not take exclusive control of the buttons (useful for testing).
- `--debug` also writes `bluetoothd.log` (BlueZ usually logs elsewhere, so it may stay empty).

Do not run BTPad over SSH and from the menu at the same time. Two copies fight over the
controller and the menu copy exits back to Spruce.

## Troubleshooting

- **Tablet cannot see "Miyoo Flip Pad":** check Bluetooth is on in Spruce first, then relaunch.
  Check `btpad.log` in the app folder.
- **Pairs but no input:** forget the device on the tablet and pair again. Reconnecting later
  usually means tapping the device in the tablet's Bluetooth list while BTPad is running.
- **Sticks differ in shape or feel jittery / stuck in a gamepad tester:** recalibrate the sticks in Spruce's settings first. On the author's Flip this gave both sticks the same range and a much steadier right stick. `--sticks` shows the raw ranges BTPad is seeing.
- **L3 / R3 (stick clicks) do not register:** make sure `mapping.json` contains `"317": 14` and `"318": 15` in `"keys"` (an older `mapping.json` replaces the built-in defaults). Otherwise run `btpad.py --probe --all`, click each stick, and add the code you see.
- **Wrong button positions or stick directions:** use `--probe` and edit `mapping.json`.
- **Spruce says Bluetooth is on but nothing can see the Flip:** Spruce's toggle can disagree with the real adapter state. BTPad now powers the adapter on itself and logs `adapter powered on`; if it logs a WARNING instead, run `bluetoothctl power on` over SSH and relaunch.
- **Spruce Bluetooth menu broken after a crash:** reboot the Flip.
- **"no python found" in the log:** BTPad looks for Spruce's Python at
  `/mnt/sdcard/spruce/flip/bin/python3`. Check that path exists on your firmware.

## How it works

1. Reads the controller from `/dev/input/event*`.
2. Registers an SDP HID record (a "Game Pad" with 16 buttons, a hat, and 4 axes) with BlueZ.
3. Listens on L2CAP PSM 17 and 19 for the host's HID control and interrupt connections.
4. Sends an input report to the host whenever a button or stick changes.

## Credits and license

Written with the help of an AI assistant (Claude) and tested on real hardware by the author.
MIT licensed, see `LICENSE`. Use at your own risk.
