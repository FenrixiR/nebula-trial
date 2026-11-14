# Nebula

Run an Elegoo Centauri Carbon with Kalico, Moonraker and a touch screen from a small computer on the USB cable, and hand the printer back to its stock firmware (OpenCentauri Cosmos) whenever you like.

Nebula installs on a Raspberry Pi Zero 2 W, any other 64-bit ARM or x86-64 board or mini PC running Debian, Ubuntu or Armbian, and needs a USB port that can act as a device (a "gadget" port).

## What you get

- Kalico and Moonraker running on the host, with the printer's own controller board used as the machine controller over the cable.
- An admin panel on port 8080: take over the printer, pass it back, update, install add-ons.
- HelixScreen on the printer's own screen.
- Optional add-ons: Mainsail or Fluidd, Crowsnest, and an internet block for the printer.
- A safety net: if the host stops talking to the printer, the printer returns to Cosmos by itself.

## Install

On the host, as a user that can use `sudo`:

    curl -fsSL https://github.com/FenrixiR/nebula-trial/releases/latest/download/install.sh | bash

The installer checks the machine, tells you what it will change, and asks before it does. It is safe to run again.

When the printer is first connected by the cable, restart the printer once, or use its Wi-Fi address for the first take-over.

## Update and remove

See `GUIDE.md` for the full walk-through. The panel has a Check for updates button. From a shell: `sudo nebula-update --check`, `sudo nebula-update --update`.

To remove everything Nebula put on the host and on the printer: `sudo nebula-uninstall`.

## Build from source

`packaging/build-release.sh VERSION` builds the packages (see the script for what it needs). Tests: `python3 -W ignore -m unittest discover -s nebula -t .`

## Licence

Copyright (c) 2026 Fenleigh 'FenrixiЯ' Åkerholm. GPL-3.0, see `LICENSE` and `NOTICE.md`. No warranty: you are changing how your printer is controlled, so read what the installer says before you confirm.
